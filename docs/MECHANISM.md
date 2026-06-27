# How Argus works

This document explains Argus's mechanism in enough detail to read the code
alongside the paper. It assumes familiarity with eBPF basics (kprobe,
kretprobe, tracepoint, the eBPF verifier) but defines everything specific to
Argus on first use.

Argus localizes an operating-system performance bottleneck down to a specific
kernel **code path** (e.g. *paging → anonymous fault → minor*). It does this by
letting an LLM agent author eBPF instrumentation and reason over its
output, grounded by two mechanisms:

1. **Reference calibration** — the agent never judges a raw number in isolation.
   Every measurement is compared, as a z-score, against a baseline of the *same
   workload* profiled on an *idle* system. A "bottleneck" is a statistically
   significant deviation from that baseline, not a large absolute value.

2. **Tree-guided traversal** — the agent may only name code paths that exist in
   a static tree of kernel subsystems built offline from kernel source. This
   stops the agent from inventing function names or misplacing real ones.

Term note: throughout, **RMV** (Reference Measurement Vector) is the profile
measured on the idle system; **PMV** (Perturbed Measurement Vector) is the
profile measured on the system under load. Each is a feature vector at tree
level 1 plus per-code-path vectors at deeper levels. In the code these objects
are `ReferenceProfile` (`reference.py`) and the per-run `perf_fv` / multiplex
counters; the paper's acronyms (IFV/OFV/CPV) do not appear as identifiers.


## Two processes

Argus is split into a long-lived C++ daemon and a Python orchestrator so the
kernel instrumentation stays attached across the whole session while the agent
iterates LLM candidates in seconds.

```
  orchestrator/agentctl  (Python)                 src/agentd  (C++ daemon, privileged)
  ┌───────────────────────────────┐   JSON over   ┌────────────────────────────────────┐
  │ agent.py        ReAct loop     │   AF_UNIX     │ ipc_server.cpp     RPC endpoints     │
  │ reference.py    builds RMV     │ ◀───────────▶ │ handler_loader.cpp load + verify     │
  │ compare.py      z-score test   │               │ multiplex_runner.cpp PMV + T_x       │
  │ multiplex.py    renders probe  │               │ workload_runner.cpp  perf-stat (FV)  │
  │ subsystem_tree.py static tree  │               │ dispatcher.bpf.c     attached hooks  │
  └───────────────────────────────┘               └────────────────────────────────────┘
```

- **`agentd`** opens, loads, and attaches one BPF object (`dispatcher.bpf.c`)
  at startup, then serves line-delimited JSON over an `AF_UNIX` socket. It owns
  every privileged action: loading agent code (which runs it through the eBPF
  verifier), attaching probes, running the workload under `perf stat`, and
  reading counters back. Endpoints: `ping`, `handler.load`, `handler.attach`,
  `handler.output`, `handler.detach`, `workload.run`, `multiplex.run`.

- **`agentctl`** is the brain. `agent.py::run_b2` drives one *(workload,
  perturbation)* cell through the pipeline below. It talks to `agentd` over the
  socket via `ipc_client.py`.


## The pipeline (paper Fig 3)

```
  ┌─ 1 ─────────┐   ┌─ 2 ──────────────┐   ┌─ 3 ──────────────┐
  │ pick        │   │ reference        │   │ agent authors    │
  │ workload M  │──▶│ profiler on      │──▶│ ONE multiplexed  │◀────────────┐
  │             │   │ IDLE system → RMV│   │ eBPF probe for   │             │
  └─────────────┘   └──────────────────┘   │ tree level L     │             │
                                           └────────┬─────────┘             │
                                                    ▼                       │ descend into
                                           ┌─ 4 ──────────────┐             │ flagged child
                                           │ run M on the     │             │ subtree
                                           │ PERTURBED system │             │ (L ← L+1)
                                           │ with probe → PMV │             │
                                           └────────┬─────────┘             │
                                                    ▼                       │
                                           ┌─ 5 ──────────────┐    flagged & │
                                           │ z = (PMV − RMV)  │    not leaf  │
                                           │ flag dims z > k  │─────────────┘
                                           └────────┬─────────┘
                                          no flag,  │  flagged & at a leaf
                                          or leaf   ▼
                                           ┌─ 6 ──────────────┐
                                           │ report diagnosis │
                                           │ (path or "none") │
                                           └──────────────────┘
```

Step by step, with the code that implements each:

1. **Pick workload M.** `cli.py` resolves the benchmark id through
   `benchmarks/manifest.yaml` (`workload.py`).

2. **Reference profiling (RMV).** `reference.py::build_ifv` runs M on an idle
   system `n_runs` times (default 30), normalizes counters per work-unit, and
   stores μ/σ per dimension to `results/reference/<bench>.json`. Deeper levels
   get per-code-path references from `build_multiplex_reference`. This is the
   *reference calibration* pillar.

3. **Author one multiplexed probe for level L.** A **multiplexed probe** is a
   single eBPF program that hooks *every* branch of the tree at the current
   level at once, sharing one eBPF map so the whole level is measured by one
   load. `multiplex.py::render_multiplex_c` emits it; each hook is a
   kprobe+kretprobe pair (see *T_x* below). At level 1 the agent may also author
   free-form probes via the `compile_ebpf`/`attach_ebpf` tools. Generated code
   goes through the eBPF verifier in `handler_loader.cpp::bpf_object__load`; on
   rejection the verifier log is returned and the agent retries (a ReAct loop).

4. **Run M on the perturbed system (PMV).** `perturb.py` applies the
   perturbation from `perturbations/*.sh`; `multiplex_runner.cpp` runs M with
   the probe attached and reads back per-slot counts (`mx_counters`) and
   accumulated kernel-nanoseconds (`mx_sum_ns`). Level-1 coarse counters come
   from `workload_runner.cpp` (`perf stat`).

5. **Compare and decide.** For each dimension *i*, `compare.py` computes a
   z-score `z_i = (PMV_i − RMV_i)/σ_i` and flags dimensions with `z_i > k`.
   If nothing is flagged → report *no bottleneck*. If something is flagged and L
   is not a leaf → descend into the flagged child subtree and return to step 3
   (`agent_tools.py::descend`). If flagged and L is a leaf → report it.

6. **Report.** `agent_tools.py::submit_diagnosis` returns the flagged path
   (e.g. `kernel_paging → anon_fault → minor`) or `["none"]` to abstain.


## The kernel subsystem tree (paper Fig 4)

The tree is defined in [orchestrator/configs/subsystems.yaml](../orchestrator/configs/subsystems.yaml)
and loaded by `subsystem_tree.py`. Level 1 enumerates kernel subsystems; level 2
splits each into code paths; level 3 splits selected paths into fine-grained
variants. Each node lists the kernel `probes` (kprobe symbols or `tp:cat/name`
tracepoints) that the multiplexed probe attaches for that node.

The paper's Fig 4 shows two illustrative subsystems. The shipped tree has eight
L1 subsystems:

```
  Argus root
  ├── kernel_paging              (depth 3)   handle_mm_fault
  │   ├── anon_fault             do_anonymous_page, wp_page_copy
  │   │   ├── minor              demand-zero
  │   │   ├── cow                copy-on-write
  │   │   └── thp_collapse       khugepaged 4K→2M
  │   ├── file_fault             filemap_fault, do_read_fault
  │   │   ├── minor              page-cache hit
  │   │   └── major              forces disk read (submit_bio)
  │   ├── swap_in                do_swap_page
  │   └── tlb_shootdown          flush_tlb_mm_range          (paper: "TLB shootdown")
  ├── kernel_memory_mgmt         (depth 3)   __alloc_pages_noprof
  │   ├── slab_fast · slab_slow · vmalloc · reclaim (→ kswapd / direct)
  ├── kernel_scheduler           (depth 2)   try_to_wake_up
  │   ├── wakeup_latency         (paper: "wakeup")
  │   ├── preemption             tp:sched/sched_switch   (paper: "context_switch")
  │   ├── load_balance           tp:sched/sched_migrate_task
  ├── block_io                   submit_bio
  ├── network_stack              tcp_sendmsg
  ├── vfs_filesystems            vfs_read
  ├── sync_rcu                   __mutex_lock_slowpath  (mutex / rwsem / RCU contention)
```

Because one multiplexed probe covers a whole level and descent only follows
flagged children, Argus reaches a leaf in at most three loaded probes, versus
one probe per leaf for a flat search.

Naming reconciliation with paper Fig 4: the scheduler's `preemption` node is the
paper's `context_switch`; `wakeup_latency` is the paper's `wakeup`.


## The T_x metric (paper Fig 5)

`T_x` is the per-call entry/exit latency of a kernel function, captured by a
kprobe+kretprobe pair: the kprobe stamps entry time, the kretprobe adds the
elapsed nanoseconds into a per-slot accumulator. In the code, `T_x` is
`mx_sum_ns` (accumulated kernel-ns per slot), produced by the probe template in
`multiplex.py` (`_KPROBE_TMPL`, `mx_record_entry`/`mx_record_exit`) and read by
`multiplex_runner.cpp`. The deeper-level z-scores are taken in log space over
`T_x` per work-unit (`mu_log`/`sigma_log` in `reference.py`/`compare.py`).

Why latency and not just counts: the overhead experiment (`Fig 5`, driven by
`scripts/run_overhead_experiment.sh`) shows a count-only kprobe costs ~49 ns per
call and a kprobe+kretprobe pair ~201 ns, versus ~12 µs per call for ftrace's
`function_graph`. The kprobe+kretprobe pair buys per-call latency — which
surfaces bottlenecks that change *how long* a path takes without changing *how
often* it runs (e.g. cache-line pollution) — at a fraction of ftrace's cost.


## Two probe paths

Argus has two distinct ways to get an eBPF program into the kernel, and they
serve different parts of the pipeline.

```
  (A) Dispatcher path — agent-authored single probes (level 1, free-form)

      dispatcher.bpf.c stays attached at ~13 kernel hooks. Each hook is a thin
      stub that bpf_tail_calls into a PROG_ARRAY slot. The agent's compiled
      program is inserted into a slot (handler.load + handler.attach); an empty
      slot is a zero-overhead no-op. Used for the agent's own L1 probes.

         kprobe/handle_mm_fault ──tail_call──▶ PROG_ARRAY[slot] ──▶ agent program

  (B) Multiplex path — one program per tree level (levels 1–3 descent)

      multiplex_runner.cpp loads an orchestrator-rendered program that hooks
      every node at the current level directly (libbpf auto-attaches every SEC),
      bypassing the dispatcher, and reads mx_counters + mx_sum_ns. This is the
      path that implements "one multiplexed probe per level" and collects T_x.
```

Path (B) — the multiplex — is what drives the level-by-level descent the paper
describes and what produces every figure. Path (A) is the general-purpose
agent-authoring channel the daemon was built around. Both run the program
through the verifier before it touches the kernel.


## Where to look in the code

| Concept (paper)                         | Code |
|-----------------------------------------|------|
| 6-step loop / ReAct                     | `orchestrator/agentctl/agent.py::run_b2` |
| Agent tools (author/run/compare/descend)| `orchestrator/agentctl/agent_tools.py` |
| Reference calibration (RMV)             | `orchestrator/agentctl/reference.py` |
| z-score test, flagging                  | `orchestrator/agentctl/compare.py`, `detector.py` |
| Multiplexed probe + T_x                 | `orchestrator/agentctl/multiplex.py`, `src/agentd/multiplex_runner.cpp` |
| Static tree (Fig 4)                     | `orchestrator/configs/subsystems.yaml`, `subsystem_tree.py` |
| Level-1 feature vector                  | `src/agentd/workload_runner.cpp`, `feature_vector.py` |
| Verifier gate                           | `src/agentd/handler_loader.cpp` |
| IPC contract                            | `src/agentd/ipc_server.cpp`, `ipc_client.py` |
| LLM backends                            | `orchestrator/agentctl/chat_backend.py` |
