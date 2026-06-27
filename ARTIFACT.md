# Argus — Artifact Guide

This is the artifact for *"Argus: Agentic, Reference-Calibrated, Tree-Guided,
System-Level Bottleneck Localization"* (Architecture 2.0 Workshop @ ISCA 2026).

Argus is an agentic eBPF profiler. It hands an LLM agent two forms of grounding: 1) a per-workload **reference profile** measured on an idle system, and 2) a static
**tree of kernel code paths**. This lets the agent author eBPF probes and reason
over their output in a feedback loop until it localizes a bottleneck to a
specific kernel code path (e.g. *paging → anonymous fault → minor*), or abstains.

- New to the system? Read [docs/MECHANISM.md](docs/MECHANISM.md) first — it
  explains the pipeline, the kernel tree, and the metric, with diagrams.
- Setting up a machine? Follow [setup.md](setup.md).


## Requirements

Argus instruments a live kernel, so it needs real hardware (not a typical CI
container) and root-equivalent privileges.

- **OS:** Ubuntu/Debian Linux (22.04+ tested).
- **Kernel:** ≥ 5.15 with BTF (`/sys/kernel/btf/vmlinux` present) and eBPF +
  tracepoint support. Probe symbol names in `subsystems.yaml` were resolved
  against kernel **6.17**; on a very different kernel some symbols may need
  updating (the tree records the renames it already absorbed).
- **Toolchain:** `clang ≥ 14`, `bpftool ≥ 7`, `llvm-strip`, `make`, `libelf-dev`.
- **Privileges:** `agentd` needs `CAP_BPF`/`CAP_PERFMON`/`CAP_SYS_ADMIN` (run
  via `setcap`, see setup.md) and the perturbation scripts need a few sudoers
  grants.
- **Python ≥ 3.10** for the orchestrator.
- **LLM backend:** a credentialed backend for the agent (default: Google Vertex
  AI / `gemini-2.5-flash`; Anthropic, OpenAI-compatible, and AI Studio are also
  supported via `chat_backend.py`). Needed only to *regenerate* result data, not
  to re-render figures from shipped data.


## Build

```bash
git clone --recurse-submodules <repo-url> agenticbpf   # third_party/libbpf is a submodule
cd agenticbpf
make                                   # libbpf (static) + dispatcher + vmlinux.h + agentd
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt        # google-genai (default); openai/anthropic optional
```

`make` prints `Build complete: .../build/agentd`. `vmlinux.h` is generated from
the running kernel's BTF (`make vmlinux`) — it is host-specific and not checked
in. See [setup.md](setup.md) for the `setcap` grant, the tracefs permissions,
CPU pinning on hybrid Intel parts, and backend credentials.


## Reproducing the figures

**Tier 1 — re-render figures from shipped result data (cheap, no LLM, no root).**
Each plot script reads result files under `results/` and writes to
`paper/figures/`. If the result data is present (see the packaging note below),
this regenerates every figure with no agent runs:

| Paper item | Command | Output (`paper/figures/`) | Expected headline |
|---|---|---|---|
| **Fig 1** per-cell hallucination | `python paper/plot_hallucination_per_cell.py` | `fig_hallucination_per_cell.{pdf,png}` | Argus mostly 0 per cell |
| **Fig 2** aggregate hallucination | `python paper/plot_hallucination.py` | `fig_hallucination.{pdf,png}` | Argus **2%** vs 45 / 52 / 32% |
| **TtD** medians | `python paper/plot_ttd_medians.py` | `fig_ttd_medians.{pdf,png}` | Argus **30.9 s** (33.4 / 26.3 / 28.7) |
| **Fig 5** overhead | `python paper/plot_ftrace_overhead_cpu_v2.py` | `fig_overhead.{pdf,png}` | **+49 / +201 / +11963 ns** per call |
| **Fig 6 / CS1** aggressor mode | `python paper/plot_correctness_mode.py` | `fig5_correctness_*.{pdf,png}` | per-victim diagnoses under p10 |
| **Fig 7 / CS2** page-fault disambiguation | `python paper/plot_cs1_heatmap.py --csv paper/figures/cs1_matrix_kernel_paging_motivation.csv` | `cs1_heatmap*.{pdf,png}` | 5 perturbers → 4 signatures |

`paper/run_all_analysis.sh` runs all six in order.

**Tier 2 — regenerate the result data from scratch (slow, needs agentd + an LLM
backend; the agent runs cost real LLM budget).** This is what produced the data
Tier 1 reads. References are per-machine, so build them first:

```bash
sudo setcap "cap_bpf,cap_perfmon,cap_sys_admin,cap_sys_resource+ep" build/agentd
./build/agentd > /tmp/agentd.log 2>&1 &          # leave running
source .venv/bin/activate

# 1. idle reference per victim (per-machine; ~3 min each)
scripts/p15a_rebuild_all_references.sh

# 2. Fig 1 / 2 / TtD — full (victim × perturber × baseline) matrix + plots
scripts/run_T_x_full_sweep.sh

# 3. Fig 5 — eBPF measurement overhead
REPS=5 bash scripts/run_overhead_experiment.sh && python paper/plot_ftrace_overhead_cpu_v2.py

# 4. Fig 6 / CS1 — THP-aggressor across the victim pool
scripts/run_aggressor_mode.sh p10_thp_aggressor 3 B2 && python paper/plot_correctness_mode.py

# 5. Fig 7 / CS2 — page-fault disambiguation matrix (also run by run_T_x_full_sweep.sh, phase F)
python scripts/cs1_pf_types_matrix.py --parent kernel_paging \
    --victims gups,stream,pf_anon_write,llama_cpp,fork_storm \
    --references gups,stream,pf_anon_write,llama_cpp,fork_storm \
    --out paper/figures/cs1_matrix_kernel_paging_motivation.csv
python paper/plot_cs1_heatmap.py --csv paper/figures/cs1_matrix_kernel_paging_motivation.csv
```

The evaluation matrix is the five victims `{gups, stream, pf_anon_write,
llama_cpp, fork_storm}` crossed with the four perturbers `{p1_thp_off,
p4_drop_caches, p10_thp_aggressor, p_cache_thrash}`, three repetitions per cell.

> **Packaging note.** `results/` is in `.gitignore`, so a fresh `git clone` does
> **not** include the result data Tier 1 reads, nor are agent run-logs shipped.
> To distribute a Tier-1-reproducible artifact you must ship `results/` (and the
> already-rendered `paper/figures/`) out of band, or un-ignore the specific
> result files. Decide this before packaging.


## Paper ⇄ code notes

**Baselines.** The paper's four arms map to these code labels (the label string
appears in `results/sweeps/*.json` filenames and `AgentRunResult.baseline`):

| Paper | Code label (`--baseline`) | How it is configured |
|---|---|---|
| **Argus** (full) | `B2` | `run_b2` with no ablation flags |
| B-LLM-Prior-Tree-Probes | `B-LLM-Prior-Tree-Probes-Every-Level` | `llm_prior_tree_probes=True` |
| B-LLM-Prior-Tree | `B-LLM-Prior-Tree` | `llm_prior_with_tree=True` |
| B-LLM-Prior | `B-LLM-Prior` | `llm_prior_deep=True` |
| *(no paper arm)* | `B-NR` | `no_reference=True` — extra ablation, not in the paper |

**Other mappings.** `T_x` = `mx_sum_ns` (accumulated kernel-ns). The scheduler
tree node `preemption` = paper Fig 4 `context_switch`; `wakeup_latency` =
`wakeup`. \
The page-fault disambiguation case study is **paper CS2** but is named
`cs1_*` in the code/scripts (`cs1_pf_types_matrix.py`, `cs1_heatmap`). \
The aggressor case study is **paper CS1** and its figure file is `fig5_correctness_*`
(not paper Fig 5). The shipped `paper/figures/` predate the paper's figure
numbers.

## Repository layout

```
agenticbpf/
├── ARTIFACT.md            ← you are here
├── README.md              short overview
├── setup.md               full machine setup
├── docs/MECHANISM.md      how Argus works (diagrams)
├── Makefile  agentctl.py  build + repo-root launcher
├── src/                   agentd — the C++ daemon
│   ├── bpf/dispatcher.bpf.c          always-attached hooks (PROG_ARRAY)
│   ├── include/profiler_types.h      shared C/eBPF contract
│   └── agentd/{main,ipc_server,handler_loader,multiplex_runner,workload_runner,json}.cpp
├── orchestrator/agentctl/ the Python brain
│   ├── agent.py           ReAct loop (run_b2) + baseline flags
│   ├── agent_tools.py     the agent's tools (author/run/compare/descend/submit)
│   ├── reference.py       idle baseline (RMV)
│   ├── compare.py detector.py   z-score test + L1 flagging
│   ├── multiplex.py       one-probe-per-level renderer + T_x
│   ├── subsystem_tree.py  loads the static tree
│   ├── chat_backend.py    multi-provider LLM abstraction
│   └── cli.py             `agentctl` subcommands
├── orchestrator/configs/
│   ├── subsystems.yaml    the static kernel-code-path tree (paper Fig 4)
│   └── prompts/system.md  the agent's system prompt
├── benchmarks/            victims: gups, stream, pf_{anon_write,cow,major,page_cache,zero_page},
│                          llama_cpp, fork_storm, pf_types (shared binary) + manifest.yaml
├── perturbations/         p1_thp_off, p4_drop_caches, p10_thp_aggressor, p_cache_thrash,
│                          p0_thp_always (pre-step), overhead_* probes + manifest.yaml
├── agent_handlers/        seed/ (example probe) + canned/ (few-shot probe sources)
├── scripts/               reproduction drivers (run_T_x_full_sweep, run_aggressor_mode,
│                          run_overhead_experiment, cs1_*, validate_subsystem_tree, ...)
├── paper/                 the 6 figure generators + analysis helpers + figures/
└── results/               reference profiles + sweep data (gitignored — see packaging note)
```
