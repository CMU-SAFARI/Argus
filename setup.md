# Argus — Setup

This document covers everything you need to run Argus end-to-end on a fresh machine. For the conceptual overview see [README.md](README.md) and [docs/MECHANISM.md](docs/MECHANISM.md); for how to reproduce the paper's figures see [ARTIFACT.md](ARTIFACT.md).

## Supported platforms

**Ubuntu / Debian Linux only.**

The artifact assumes a Debian-derived distro (Ubuntu 22.04+ tested). It relies on:
- `apt-get` for system packages.
- The Linux kernel with BTF (`/sys/kernel/btf/vmlinux` present) and full eBPF + tracepoint support.
- `perf` from `linux-tools-$(uname -r)`.
- `clang ≥ 14`, `bpftool ≥ 7`, `llvm-strip`.

We do **not** support macOS, Windows, or non-Debian Linux distros (Fedora/RHEL/Arch). If you need to run on those platforms, build a Debian VM / container and follow this guide inside it.

## 1. Clone the repo

```bash
git clone --recurse-submodules <repo-url> agenticbpf
cd agenticbpf
git status                # confirm you're on the branch you expect
git log --oneline -3      # confirm HEAD is what you expect
```

The `--recurse-submodules` flag is required: `third_party/libbpf` is a git submodule pinned to a specific commit. If you forgot it (or cloned via the GitLab/GitHub UI which doesn't recurse), fix retroactively:

```bash
git submodule update --init --recursive
ls third_party/libbpf/src/Makefile        # MUST exist before `make`; otherwise the build fails with a missing libbpf header
```

## 2. System dependencies

```bash
sudo apt-get update
sudo apt-get install -y \
  build-essential clang llvm \
  linux-tools-common linux-tools-generic linux-tools-$(uname -r) \
  linux-headers-$(uname -r) \
  libelf-dev zlib1g-dev libzstd-dev pkg-config \
  jq curl git \
  python3 python3-venv python3-pip
```

> **Don't `apt install bpftool` directly on Ubuntu 24.04.** It is a virtual package there and the install fails with `Package 'bpftool' has no installation candidate`. The actual binary is shipped inside `linux-tools-$(uname -r)`, which the line above already pulls in. Verify with `bpftool version`.

Verify the toolchain:

```bash
clang --version    # expect ≥ 14
bpftool version    # expect ≥ 7
perf --version     # any recent version is fine
```

## 3. Build the C++ daemon, BPF objects, and benchmarks

From the repo root:

```bash
make                                           # libbpf + dispatcher + agentd
make -C benchmarks/stream                      # STREAM benchmark
make -C benchmarks/gups                        # GUPS benchmark

# Canned eBPF probes. Two of them (tlb_shootdown, reclaim) are also inlined as
# few-shot examples in the agent's system prompt, so build them at least once.
for p in compaction reclaim tlb_shootdown; do
  make handler BPF_SRC=agent_handlers/canned/$p.bpf.c
done
```

A successful build prints `Build complete: .../build/agentd` and produces the binary at `build/agentd` (note: under `build/`, not the repo root). Smoke-check with:

```bash
ls -la build/agentd                               # should be ~2.8 MB ELF
file build/agentd                                 # ELF 64-bit LSB pie executable
ls -la benchmarks/stream/stream_bench benchmarks/gups/gups
```

`agentd --help` prints its usage; otherwise it is a daemon that opens the IPC socket and listens. The real smoke comes in §7 below, where `agentctl.py` drives it through a real cell.

### 3a. Lab-machine fixes (do these once)

The defaults assume a vanilla Ubuntu desktop. On real lab boxes (especially hybrid Intel CPUs and locked-down kernels) you need three one-time fixes before §6 will work:

**1. Run `agentd` unprivileged via Linux capabilities** instead of `sudo`. On hybrid CPUs (Raptor Lake-S i9-14900K and similar), `sudo perf` loses access to the P-core PMU and silently returns `<not counted>` for hardware events — it works under unprivileged perf because of how the kernel's PMU multiplexing handles UID transitions. Granting the caps `agentd` actually needs avoids both that footgun and the `RLIMIT_MEMLOCK -EPERM` issue with `bpf_prog_load`:

```bash
sudo setcap "cap_bpf,cap_perfmon,cap_sys_admin,cap_sys_resource+ep" build/agentd
# then run unprivileged:
./build/agentd > /tmp/agentd.log 2>&1 &
```

If `setcap` rejects, install it: `sudo apt-get install -y libcap2-bin`. Re-run the `setcap` after every `make` rebuild — the binary's capability xattrs don't survive replacement.

**2. Make tracefs world-traversable** so the unprivileged `agentd` can attach tracepoints (otherwise the dispatcher fails with `-EACCES` on `vmscan/mm_vmscan_direct_reclaim_begin`):

```bash
sudo chmod -R o+r /sys/kernel/tracing
sudo chmod o+x /sys/kernel/tracing /sys/kernel/tracing/events
```

**3. Pin the workload to P-cores** (Intel hybrid only). [src/agentd/workload_runner.cpp](src/agentd/workload_runner.cpp) prepends `taskset -c $AGENTICBPF_PIN_CPUS` to every `perf stat` invocation. Default is `0-7` (the eight P-cores on i9-14900K). To override:

```bash
export AGENTICBPF_PIN_CPUS="0-15"   # e.g. for an EPYC, AMD desktop, or homogeneous Xeon
export AGENTICBPF_PIN_CPUS=""       # disable pinning entirely (useful on VMs or containers)
```

Without pinning, GUPS runs hop between P- and E-cores mid-run, inflating `dtlb_load_misses` CoV from ~0.09 → ~0.18 and risking the reference-stability gate. Lab-measured on Raptor Lake-S; effect is smaller on homogeneous chips but pinning is harmless there.

For the perf-event hybrid issue specifically (separate from pinning): hardware-PMU events are explicitly qualified `cpu_core/event/` in workload_runner.cpp — that's the actual fix for the unqualified events emitting one zero `cpu_atom/...` line and one nonzero `cpu_core/...` line that our parser ignores. You don't need to do anything; this just explains why the binary works on your hybrid CPU.

## 4. Python virtual environment

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
```

`requirements.txt` pins `google-genai` (mandatory; default backend) plus `openai` and `anthropic` (optional, lazy-imported only when you select those backends).

## 5. Pick a B2 backend

The B2 ReAct agent supports four LLM backends via two env vars:

```
AGENTICBPF_AGENT_MODE     = debug | prod                 (default: prod)
AGENTICBPF_AGENT_BACKEND  = ollama | google | anthropic | openai | studio
```

Default mapping when only mode is set:

| mode | backend | model | rough cost per B2 cell |
|---|---|---|---|
| `prod` | `google` (Vertex AI) | `gemini-2.5-flash` | ~$0.005 |
| `debug` | `google` (Vertex AI) | `gemini-2.5-flash-lite` | ~$0.001 |

Both default modes hit Vertex AI. `debug` exists to give you a cheaper / quota-friendlier model for prompt iteration, not a fundamentally different backend. Lite is ~1/5 the cost of Flash and the GCP $300 free credit covers tens of thousands of debug cells.

A truly-local debug path (Ollama or a self-hosted llama-server) is wired up in code via the `openai-compat` backend, but no model in the 8-32B sweet spot drives the B2 ReAct loop end-to-end on consumer GPUs - see §5b for the empirical table.

Pick the section below that matches your intended backend. You only need to follow **one** of these.

### 5a. Production: Google Vertex AI (default)

Recommended if you have a Google Cloud project (the $300 GCP free credit covers our entire sweep budget many times over).

**Step 1 — Install the Google Cloud SDK on Ubuntu/Debian.**

```bash
sudo apt-get install -y apt-transport-https ca-certificates gnupg curl
curl https://packages.cloud.google.com/apt/doc/apt-key.gpg | sudo gpg --dearmor -o /usr/share/keyrings/cloud.google.gpg
echo "deb [signed-by=/usr/share/keyrings/cloud.google.gpg] https://packages.cloud.google.com/apt cloud-sdk main" | sudo tee /etc/apt/sources.list.d/google-cloud-sdk.list
sudo apt-get update && sudo apt-get install -y google-cloud-cli
```

**Step 2 — Authenticate.**

If the machine has a desktop browser:

```bash
gcloud auth application-default login        # opens a browser; populates ADC token cache
```

If the machine is a remote / headless box (lab server, container, no display), use the delegated-browser flow:

```bash
gcloud auth application-default login --no-browser
# Prints a `gcloud auth application-default login --remote-bootstrap=https://...` line.
# Copy that ENTIRE line, run it on your LAPTOP (which has a browser).
# A page opens, you authenticate, the laptop prints a one-line response token.
# Paste that token back into the headless prompt.
```

Either flow populates `~/.config/gcloud/application_default_credentials.json`. The Google SDK in our codebase reads this file automatically — there is no API key to manage.

**Step 3 — Find the correct project ID.**

> ⚠️ **Use the `PROJECT_ID` column, not the display name.** GCP shows two columns: `PROJECT_ID` is what every API and SDK expects; `NAME` is the human-friendly label shown in the console. They are not interchangeable. Using the display name produces a `403 PERMISSION_DENIED … CONSUMER_INVALID … Permission denied on resource project <name>` error that looks like a permissions problem but is actually a project-id mismatch.

```bash
gcloud projects list
# Example output:
#   PROJECT_ID                       NAME                    PROJECT_NUMBER
#   project-d04858fa-1c87-432a-b76   AgenticBPF              732115199156
#   gen-lang-client-0950421952       Default Gemini Project  439236587599
#                ↑
#         use THIS column
```

Pick the project that has your billing / $300 credit attached and copy its `PROJECT_ID`.

**Step 4 — Bind ADC, enable Vertex AI, verify.**

```bash
PROJECT_ID=<paste-your-project-id-here>     # e.g. project-d04858fa-1c87-432a-b76

# Tell ADC which project to bill API quota against. Without this you get a
# noisy "authenticated using end user credentials … without a quota project"
# warning on every call, and some endpoints will refuse to serve.
gcloud auth application-default set-quota-project $PROJECT_ID

# Enable the Vertex AI API on the project (one-time; ~10 s).
gcloud services enable aiplatform.googleapis.com --project=$PROJECT_ID

# Confirm Vertex AI is live.
gcloud services list --enabled --project=$PROJECT_ID | grep aiplatform
# expect: aiplatform.googleapis.com   Vertex AI API
```

**Step 5 — Export the per-shell env vars.**

```bash
export GOOGLE_CLOUD_PROJECT=$PROJECT_ID
export GOOGLE_CLOUD_LOCATION=us-central1     # or europe-west4, etc.
# AGENTICBPF_AGENT_MODE=prod is the default; no need to set it
```

To make these persist across shell sessions, append them to your `~/.zshrc` (or `~/.bashrc`):

```bash
echo "export GOOGLE_CLOUD_PROJECT=$PROJECT_ID" >> ~/.zshrc
echo "export GOOGLE_CLOUD_LOCATION=us-central1" >> ~/.zshrc
```

**Step 6 — Verify the SDK accepts the project + ADC pair.**

```bash
gcloud auth application-default print-access-token | head -c 20; echo "..."
.venv/bin/python3 -c "
from google import genai
import os
c = genai.Client(vertexai=True,
                 project=os.environ['GOOGLE_CLOUD_PROJECT'],
                 location=os.environ['GOOGLE_CLOUD_LOCATION'])
print('vertex client ok; project=' + c._api_client.project)
"
```

Both should print successfully. ADC tokens last ~1 hour and silently refresh from the cached credentials. If a long sweep ever fails on auth, run the same `print-access-token` command to force a refresh.

### 5b. Debug: local LLM (plumbing only; not validated for sweeps)

`chat_backend.OpenAICompatBackend` can point the agent at any OpenAI-compatible `/v1/chat/completions` endpoint (Ollama, `llama-server`, vLLM) by setting `AGENTICBPF_AGENT_BACKEND=ollama` and `OLLAMA_BASE_URL`, passing the model on the CLI:

```bash
export AGENTICBPF_AGENT_MODE=debug
export AGENTICBPF_AGENT_BACKEND=ollama
export OLLAMA_BASE_URL=http://localhost:8080/v1
sudo -E .venv/bin/python3 ./agentctl.py agent --baseline B2 \
  --benchmark gups --perturbation p1_thp_off --model <your-local-model>
```

Tool calls round-trip correctly when the model emits OpenAI-shaped responses, but in our testing no local model in the 8–32B range reliably drives the full ReAct loop end-to-end — small models either mis-format the tool arguments (passing a Python repr instead of JSON, or copying placeholder text) or never emit a tool call at all. Use Vertex (§5a) for real experiments; this backend is kept as evidence that the orchestrator is provider-agnostic.

### 5c. Production: Anthropic Claude (override)

```bash
export AGENTICBPF_AGENT_BACKEND=anthropic
export ANTHROPIC_API_KEY=<your-key-from-console.anthropic.com>
# Pass the model on the CLI:
sudo -E .venv/bin/python3 ./agentctl.py agent --baseline B2 \
  --benchmark gups --perturbation p1_thp_off --model claude-opus-4-5
```

### 5d. Production: AI Studio (Gemini, free tier capped at 20 RPD)

The free-tier 20-requests-per-day cap on `gemini-2.5-flash` is too tight for the full sweep, but useful for one-shot experiments without GCP setup.

```bash
export AGENTICBPF_AGENT_BACKEND=studio
export GEMINI_API_KEY=<your-key-from-aistudio.google.com>
```

## 6. Build the reference distribution

The reference profiler measures 30 idle runs of each benchmark on **this machine** to build per-metric μ/σ that the detector compares OFV z-scores against. The reference is **CPU- and kernel-specific** — values from another machine cannot be reused.

```bash
sudo pkill -f build/agentd 2>/dev/null
sudo ./build/agentd &
sleep 1

./agentctl.py reference gups       # ~3 min on a typical lab box
./agentctl.py reference stream     # optional; only if you'll use STREAM
```

If you're moving an in-flight project from another machine, **do not copy `results/reference/*.json` across machines** — rebuild on the new machine. (Copying `results/runs/<cell_id>/` and `agent_handlers/runs/<cell_id>/` between machines is fine, since those are historical run logs, not measurement priors.)

## 7. End-to-end smoke

```bash
sudo -E .venv/bin/python3 ./agentctl.py agent --baseline B2 \
  --benchmark gups --perturbation p1_thp_off
```

Expected: `correct=True / Submitted / pivots=0` (or close to that). The per-attempt log lands at `results/runs/<cell_id>/log.json`.

## Common issues

- **`sudo` strips the venv's `python3` from `PATH`.** Always invoke `sudo -E .venv/bin/python3 ./agentctl.py …` rather than `./agentctl.py` (whose shebang resolves to `/usr/bin/python3`). Note: with §3a's capability fix, you no longer need `sudo` for `agentd` itself, only for the orchestrator if you need to write to root-owned dirs.
- **`perf_event_paranoid` rejects perf-stat events.** If you set up `agentd` via `setcap` (§3a) and the orchestrator runs unprivileged, `cap_perfmon` bypasses the paranoid check directly. If you're running under `sudo` and seeing `Access to performance monitoring … is limited`, your kernel is in lockdown mode — switch to the capability path.
- **Hardware perf events return `<not counted>` under `sudo` on Intel hybrid CPUs (Raptor Lake-S, Meteor Lake, etc.) but work unprivileged.** Symptom: reference table shows `mu=0` for `dtlb_load_misses`, `llc_load_misses`, `cache_misses`, `cpu_cycles`. Cause: `sudo perf` loses access to the `cpu_core/` PMU on hybrid topologies; the unprivileged path keeps it. Fix: §3a (capabilities + unprivileged `agentd`). The `cpu_core/event/` qualifier in [src/agentd/workload_runner.cpp](src/agentd/workload_runner.cpp) is the complementary fix that makes `perf` emit the right event lines for our parser regardless.
- **Reference CoV gate fails on `dtlb_load_misses` (~0.17 > 0.15) on Intel hybrid CPUs.** This was measured on i9-14900K and is structural (small per-core dTLB + tiny per-work-unit means). Two fixes: (a) make sure `AGENTICBPF_PIN_CPUS` is set (default `0-7` for P-cores) — pinning drops CoV from 0.17 → 0.09; (b) the per-metric override `GATE_THR_BY_METRIC = {"dtlb_load_misses": 0.20}` in [reference.py](orchestrator/agentctl/reference.py) accepts up to 0.20 since the perturbed signal is +30σ either way.
- **Capabilities lost after `make`.** `setcap` xattrs don't survive when the binary is replaced. Re-run `sudo setcap "cap_bpf,cap_perfmon,cap_sys_admin,cap_sys_resource+ep" build/agentd` after every rebuild (or wrap it in a `make install` target if you want).
- **Tracepoint attach fails with `-EACCES` even after `setcap`.** Run `sudo chmod -R o+r /sys/kernel/tracing && sudo chmod o+x /sys/kernel/tracing /sys/kernel/tracing/events`. Some distros lock tracefs to `root:root 0700` by default.
- **`403 PERMISSION_DENIED … CONSUMER_INVALID … Permission denied on resource project <name>` from Vertex.** Your `GOOGLE_CLOUD_PROJECT` is set to the project's display name instead of its `PROJECT_ID`. Run `gcloud projects list` and use the value in the **`PROJECT_ID`** column (typically `<name>-<random-suffix>` or `project-<uuid-prefix>`, e.g. `project-d04858fa-1c87-432a-b76`).
- **`UserWarning: authenticated using end user credentials from Google Cloud SDK without a quota project`.** ADC isn't bound to a quota target. Run `gcloud auth application-default set-quota-project <PROJECT_ID>` once. The warning is harmless on its own but some Vertex endpoints refuse to serve when the binding is missing.
- **`Cannot send a request, as the client has been closed`** from `google-genai`. The `genai.Client` was garbage-collected before its chat object made the first request. Make sure the client is held on a long-lived attribute, not a local variable. (Already fixed inside `chat_backend.GoogleBackend`; this note is for anyone forking the abstraction.)
- **GCP ADC expired mid-run.** Run `gcloud auth application-default print-access-token` to force a refresh. Tokens last ~1 hour but the SDK refreshes silently from the cached credentials in most cases.
- **Stale perturbation snapshot files.** If a previous run was killed mid-perturbation, `/tmp/agenticbpf-p*.snapshot` may be stale. Delete them: `sudo rm -f /tmp/agenticbpf-p*.snapshot`.

## What now?

- [ARTIFACT.md](ARTIFACT.md) — reproduce the paper's figures.
- [docs/MECHANISM.md](docs/MECHANISM.md) — how Argus works, with diagrams.
- [README.md](README.md) — one-page overview.
