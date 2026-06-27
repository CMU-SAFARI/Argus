# Argus — Demo Guide

A hands-on walkthrough of driving Argus, structured so it doubles as a script
for a live demo session. It assumes the machine is already set up
([setup.md](setup.md)) and you understand the mechanism at a high level
([docs/MECHANISM.md](docs/MECHANISM.md)).

Each `agent` cell takes roughly **20–40 s** and costs about **$0.005** of LLM
budget on the default `gemini-2.5-flash`. We show a demo around 3–4 cells.

The launcher `./agentctl.py` is a thin wrapper over
`python -m orchestrator.agentctl.cli`; use whichever you prefer.


## 0. Before the demo (do this once, off-stage)

```bash
# Daemon: build, grant capabilities, start it (stays up for the whole session)
make
sudo setcap "cap_bpf,cap_perfmon,cap_sys_admin,cap_sys_resource+ep" build/agentd
./build/agentd > /tmp/agentd.log 2>&1 &
sleep 1 && tail -2 /tmp/agentd.log    # expect a line like: [ipc] listening on /tmp/agentd.sock

# Backend: export your Vertex project / ADC (or another backend; see setup.md §5)
export GOOGLE_CLOUD_PROJECT=<your-project-id>
export GOOGLE_CLOUD_LOCATION=us-central1
source .venv/bin/activate

# References: build the idle baseline for every victim you'll demo (per-machine,
# ~3 min each). Argus cannot run a cell without the victim's reference.
for v in gups pf_anon_write stream; do ./agentctl.py reference $v; done
```


## The command you'll repeat

```bash
sudo -E .venv/bin/python3 ./agentctl.py agent \
    --baseline B2 --benchmark <victim> --perturbation <perturbation>
```

`sudo -E .venv/bin/python3` (not bare `./agentctl.py`) keeps the venv on `PATH`
under sudo. `B2` is the full Argus system. Each run prints a result block and
writes a full trace to `results/runs/<cell_id>/log.json`:

```
=== B2: gups x p1_thp_off ===
  expected_subsystem : kernel_paging              # ground truth, hidden from the agent
  diagnosed          : ['kernel_paging']          # what Argus committed to
  correct            : True
  workload_ok        : True  wall=24.3s
  hint               : [B2:Submitted pivots=0 ...]
```

(Numbers vary per machine and run — watch the *shape*: did it commit to the
right subsystem, descend to the right leaf, or correctly abstain?)


## Demo 1 — Localize a real bottleneck

**Cell:** `gups × p1_thp_off`. Turning off Transparent Huge Pages forces 4 KB
pages, so GUPS's random walk hammers the page-fault / TLB path.

```bash
sudo -E .venv/bin/python3 ./agentctl.py agent \
    --baseline B2 --benchmark gups --perturbation p1_thp_off
```

**What to narrate while it runs:** Argus profiled GUPS on an idle system first.
Now, under the perturbation, the agent authors one eBPF probe covering the top
level of the kernel tree, measures, z-scores against the idle baseline, sees
`kernel_paging` light up, and **descends** into it, authoring a deeper probe
for the paging code paths until it reaches a leaf.

**Then show the work** — open the trace to reveal the eBPF the agent actually
wrote and the descent it took:

```bash
cell=$(ls -t results/runs | head -1)
jq '{termination, diagnosed_path, descents: [.descents[].flagged]}' results/runs/$cell/log.json
ls agent_handlers/runs/$cell/pivot*/retry*/     # the candidate .bpf.c the agent authored
```

The point to make: this is not a fixed probe set — the agent *wrote* the
instrumentation, and the verifier (inside `agentd`) gates every program before
it touches the kernel.


## Demo 2 — Why the reference matters (the paper's thesis, live)

**Cell:** `gups × p4_drop_caches`. Dropping caches evicts the *page* cache, but
GUPS's working set is anonymous memory that survives — so there is **no
kernel-traceable bottleneck** on GUPS's hot path. The right answer is "nothing" (i.e., abstain).

Run Argus, then run an LLM-prior baseline on the *same* cell:

```bash
# Argus: reference calibration sees no significant deviation -> abstains
sudo -E .venv/bin/python3 ./agentctl.py agent \
    --baseline B2 --benchmark gups --perturbation p4_drop_caches
# expect: diagnosed ['none']  (correct abstain)

# B-LLM-Prior: same agent, no reference -> guesses a deep path from the
# perturbation name alone
sudo -E .venv/bin/python3 ./agentctl.py agent \
    --baseline B-LLM-Prior --benchmark gups --perturbation p4_drop_caches
# expect: a confident, wrong deep diagnosis (a hallucination)
```

**The talking point:** same model, same workload, same perturbation. The only
difference is that Argus has a measured idle baseline to compare against. Without
it, the agent hallucinates a bottleneck that isn't there; with it, Argus
abstains. That gap is the entire result of the paper (Fig 1 / Fig 2: 2%
hallucination vs 45–52%).


## Demo 3 — The latency metric (T_x) catches what counts miss

**Cell:** `pf_anon_write × p_cache_thrash`. A cache-polluting co-runner doesn't
change *how many* page faults happen — it changes *how long* each one takes
(cold page-table walks). A count-only profiler sees nothing move.

```bash
sudo -E .venv/bin/python3 ./agentctl.py agent \
    --baseline B2 --benchmark pf_anon_write --perturbation p_cache_thrash
```

Argus still localizes `kernel_paging → anon_fault → minor`, because its probes
capture per-call latency (`T_x` = kprobe+kretprobe). To show the signal split,
open the descent's per-slot z-scores in the log:

```bash
cell=$(ls -t results/runs | head -1)
jq '.descents[-1] | {flagged, z_count: .z_count_scores, z_latency: .z_log_scores}' results/runs/$cell/log.json
# count z ~ 0 (faults unchanged), latency z high (each fault is slower)
```


## Demo 4 — The figures (no agent runs, no root)

If you have the result data present, regenerate the paper's figures live in
seconds:

```bash
./paper/run_all_analysis.sh
```

Point at `paper/figures/fig_hallucination.pdf` (the 2% vs 45 / 52 / 32% bar) and
`paper/figures/cs1_heatmap*.pdf` (Case Study 2: five page-fault perturbers
resolving into four distinct kernel signatures). See [ARTIFACT.md](ARTIFACT.md)
for the full figure ↔ generator map.


## A 6-minute live script

| min | do | say |
|---|---|---|
| 0:00 | Demo 1 starts (`gups × p1_thp_off`) | "Argus already has an idle profile of this workload. Watch it author probes and walk the kernel tree." |
| 0:40 | `jq` the log, `ls` the authored `.bpf.c` | "The agent *wrote* this eBPF; the verifier gated it. It descended paging → anon_fault." |
| 2:00 | Demo 2, B2 then B-LLM-Prior | "Same model, same cell. With a reference: abstains. Without: hallucinates." |
| 4:00 | Demo 3 (`p_cache_thrash`) | "Counts don't move; latency does. Argus measures per-call time, so it still finds it." |
| 5:15 | Demo 4 figures | "And here's that effect across the whole matrix." |


## Operating tips & failure modes

- **Pre-build references and pre-run every cell.** A missing reference aborts the
  cell; a cold ADC token adds latency. Cache everything beforehand.
- **Keep a recorded backup.** `... agent --json | tee /tmp/cell.json` captures a
  run you can replay or read aloud if the network/GPU misbehaves on stage.
- **Speed/cost:** ~20–40 s and ~$0.005 per cell on `gemini-2.5-flash`. For a
  snappier demo pass `--model gemini-2.5-flash-lite` (cheaper, slightly weaker).
- **Perturbations need privilege.** `p1`/`p4`/`p10`/`p_cache_thrash` flip kernel
  knobs or spawn co-runners via `sudo`; confirm the sudoers grants from setup.md
  are in place, or the cell errors at "apply perturbation".
- **Reset between runs.** If a run is killed mid-perturbation, clear stale state:
  `sudo rm -f /tmp/agenticbpf-p*.snapshot` and re-check THP:
  `cat /sys/kernel/mm/transparent_hugepage/enabled`.
- **agentd must be the current build.** Re-run the `setcap` line after any
  `make` (capabilities don't survive replacing the binary).


## Menu (what you can mix and match)

- **Victims** (`--benchmark`): `gups`, `stream`, `pf_anon_write`, `pf_cow`,
  `pf_major`, `pf_page_cache`, `pf_zero_page`, `llama_cpp`, `fork_storm`.
- **Perturbations** (`--perturbation`): `p1_thp_off`, `p4_drop_caches`,
  `p10_thp_aggressor`, `p_cache_thrash` (`p0_thp_always` is an auto-applied
  pre-step for THP-sensitive victims).
- **Baselines** (`--baseline`): `B2` (Argus) and the ablations `B-NR`,
  `B-LLM-Prior`, `B-LLM-Prior-Tree`, `B-LLM-Prior-Tree-Probes-Every-Level`.
- **Aggressor mode** (one perturbation across every victim with a reference):
  `scripts/run_aggressor_mode.sh p10_thp_aggressor 3 B2`.

Cells with a co-running aggressor (`p10`, `p_cache_thrash`) are the most visually
compelling; structural-null cells (`gups × p4_drop_caches`, `stream × p10`) are
the best for showing Argus correctly abstain.
