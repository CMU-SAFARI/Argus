# Argus - Agentic, Reference-Calibrated, Tree-Guided, System-Software-Level Bottleneck Localization

> An agentic eBPF profiler: a language model authors kernel probes online and
> reasons over their output to localize an **OS-level bottleneck** to a specific
> kernel code path — grounded by a measured reference and a static kernel tree,
> not by raw counters alone.

Artifact for *"Argus: Agentic, Reference-Calibrated, Tree-Guided, System-Level
Bottleneck Localization"* (Architecture 2.0 Workshop @ ISCA 2026).

- **[ARTIFACT.md](ARTIFACT.md)** - requirements, build, and how to reproduce
  each figure.
- **[docs/MECHANISM.md](docs/MECHANISM.md)** - how Argus works, with diagrams.
- **[setup.md](setup.md)** - full machine setup (toolchain, capabilities, LLM
  backend).
- **[DEMO.md](DEMO.md)** - hands-on walkthrough and a script for a live demo
  session.


## What it does

You pick a workload and a perturbation that stresses some OS subsystem (e.g.
`THP=never`, `drop_caches`, a THP-aggressor co-runner). Argus then:

1. **Builds a reference profile** by running the workload on an *idle* system
   and recording per-work-unit counters — the baseline a deviation is measured
   against.
2. **Runs the workload under the perturbation** while an LLM agent authors an
   eBPF probe for the current level of a static kernel-code-path tree.
3. **Compares** the perturbed measurement to the reference as a z-score. If a
   code path deviates significantly, the agent **descends** into it and repeats;
   otherwise it stops.
4. **Reports** the localized code path (e.g. `kernel_paging → anon_fault →
   minor`) or abstains when nothing deviates.

The point: hardware counters tell you *that* dTLB misses are high, not *whether*
the cause is application data layout (user space) or a kernel code path. An
agent-authored kprobe attributes the cost to a specific path; the reference and
the tree keep the agent honest about what counts as a bottleneck and what code
paths actually exist.

**In scope:** bottlenecks in kernel code paths — page faults, allocation,
reclaim/compaction, scheduling, TLB shootdown, and cross-workload contention on
shared kernel structures. \
**Out of scope:** bottlenecks entirely in user space
(hot-loop ALU, app-internal locks, app-data cache thrashing).


## Architecture

Two processes talk JSON over an `AF_UNIX` socket. The split matters: the kernel
instrumentation stays attached for the whole session while the agent iterates
LLM candidates in seconds — no reattach per candidate.

```
  orchestrator/agentctl  (Python brain)            src/agentd  (C++ daemon, privileged)
  ┌─────────────────────────────┐   JSON over   ┌───────────────────────────────────┐
  │ ReAct loop on an LLM         │   AF_UNIX     │ loads + verifies agent eBPF code   │
  │ reference calibration (RMV)  │ ◀──────────▶  │ attaches probes, runs the workload │
  │ z-score test + tree descent  │               │ reads back counters + T_x latency  │
  └─────────────────────────────┘               │ holds dispatcher.bpf.c attached    │
                                                 └───────────────────────────────────┘
```

- **`agentd`** (`src/agentd/`) owns all privileged actions: running agent code
  through the eBPF verifier, attaching probes, running the workload under `perf
  stat`, and reading counters. It holds one BPF object (`dispatcher.bpf.c`)
  attached for the session.
- **`agentctl`** (`orchestrator/agentctl/`) is the orchestrator: it builds the
  idle reference, drives the agent's ReAct loop, applies/reverts perturbations,
  runs the z-score comparison, and scores the diagnosis.

The baselines used in the ablation are the full system (`B2` = Argus) plus three
LLM-prior arms (`B-LLM-Prior`, `B-LLM-Prior-Tree`,
`B-LLM-Prior-Tree-Probes-Every-Level`); see [ARTIFACT.md](ARTIFACT.md) for the
mapping to the paper's names. The full mechanism is in
[docs/MECHANISM.md](docs/MECHANISM.md).


## Build & run

```bash
git clone --recurse-submodules <repo-url> agenticbpf
cd agenticbpf
make                                  # libbpf + dispatcher + agentd
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

Then follow [setup.md](setup.md) for the `setcap` grant and LLM backend, and
[ARTIFACT.md](ARTIFACT.md) to build a reference and run a cell:

```bash
sudo setcap "cap_bpf,cap_perfmon,cap_sys_admin,cap_sys_resource+ep" build/agentd
./build/agentd > /tmp/agentd.log 2>&1 &
./agentctl.py reference gups                                   # idle reference (per-machine)
./agentctl.py agent --baseline B2 --benchmark gups --perturbation p1_thp_off
```

## Citation

The paper is under submission; this README will carry the citation handle on
acceptance.
