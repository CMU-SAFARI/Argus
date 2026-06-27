"""agentctl - command-line driver for AgenticBPF orchestrator.

Subcommands:
  reference <benchmark>                          M2: build IFV from N idle runs
  show-reference <benchmark>                     pretty-print results/reference/<bench>.json
  agent --baseline {B2,B-NR,B-LLM-Prior,B-LLM-Prior-Tree,B-LLM-Prior-Tree-Probes-Every-Level}
        --benchmark X --perturbation Y            Run one diagnostic cell.

  B2 is the full Argus system (reference calibration + tree-guided traversal +
  agent-authored probes). The B-LLM-Prior* arms are the paper's ablation
  ladder; B-NR drops the idle reference entirely.
"""

from __future__ import annotations

import argparse
import json
import sys

from .feature_vector import LABELS
from .ipc_client import AgentdClient
from .reference import build_ifv, ReferenceProfile


def cmd_reference(args: argparse.Namespace) -> int:
    c = AgentdClient(args.socket)
    if not c.ping():
        print("agentd not responding", file=sys.stderr)
        return 1
    rp = build_ifv(c, args.benchmark, n_runs=args.n_runs, warmup=args.warmup,
                   gate_thr=args.cov_thr, verbose=True)
    path = rp.save()
    print(f"\n[reference] saved {path}")
    print(f"[reference] gate_pass={rp.gate_pass}")
    if rp.gate_failures:
        for f in rp.gate_failures:
            print(f"  ! {f}")
    print("\n[reference] mu / sigma / cov per metric (per work-unit rates):")
    for i, label in enumerate(LABELS):
        print(f"  {label:22s} mu={rp.mu[i]:.6e}  sigma={rp.sigma[i]:.6e}  cov={rp.cov[i]:.4f}")
    return 0


def cmd_show_reference(args: argparse.Namespace) -> int:
    rp = ReferenceProfile.load(args.benchmark)
    print(json.dumps(rp.as_dict(), indent=2))
    return 0


def cmd_reference_multiplex(args: argparse.Namespace) -> int:
    from .reference import reference_for_descent
    c = AgentdClient(args.socket)
    if not c.ping():
        print("agentd not responding", file=sys.stderr)
        return 1
    parent_path = tuple(args.parent_path.split("."))
    entry = reference_for_descent(c, args.benchmark, parent_path,
                                  n_runs=args.n_runs, warmup=args.warmup,
                                  force_rebuild=args.force_rebuild,
                                  verbose=True)
    print()
    print(f"[mxref] {args.benchmark} x {'.'.join(parent_path)}: per-slot stats")
    print(f"  n_runs={entry['n_runs']}  n_programs={entry.get('n_programs')}")
    print(f"  attached/run: {entry.get('n_attached', [])}")
    for i, label in enumerate(entry["labels"]):
        print(f"  [{i}] {label:<14s} mu={entry['mu'][i]:.6e}  "
              f"sigma={entry['sigma'][i]:.6e}  cov={entry['cov'][i]:.4f}")
    return 0


def cmd_prebuild_references(args: argparse.Namespace) -> int:
    """Walk the active subsystem tree and build a depth-N reference for
    every non-leaf node of the requested benchmark(s). Idempotent: cached
    entries are skipped unless --force-rebuild is set.

    Cost model: each (benchmark, parent_path) takes n_runs (default 30)
    workload re-runs with a multiplexed probe attached. For pf_anon_write
    (~75ms/run) and 13 non-leaf nodes that's ~30 seconds. For heavier
    workloads it scales linearly.

    Use this BEFORE running B2 sweeps so the agent loop never pays the
    lazy-rebuild cost mid-cell (which would inflate B2's wall-time and
    bias the accuracy/wall comparisons).
    """
    from .reference import ReferenceProfile, reference_for_descent
    from .subsystem_tree import iter_descendants, load_tree

    c = AgentdClient(args.socket)
    if not c.ping():
        print("agentd not responding", file=sys.stderr)
        return 1

    # Resolve target benchmarks.
    if args.all:
        REPO = __import__("pathlib").Path(__file__).resolve().parents[2]
        import yaml
        bench_manifest = yaml.safe_load(
            (REPO / "benchmarks" / "manifest.yaml").read_text())
        benchmarks = sorted(bench_manifest.keys())
    elif args.benchmarks:
        benchmarks = list(args.benchmarks)
    else:
        print("error: must pass either --all or one or more benchmarks",
              file=sys.stderr)
        return 2

    # Resolve target parents (non-leaf nodes in the active tree).
    tree = load_tree()
    if args.parents:
        parents = [tuple(p.split(".")) for p in args.parents]
    else:
        parents = []
        for path, node in iter_descendants(tree):
            if not node.is_leaf:
                parents.append(path)

    # Sanity: for each benchmark, the L1 perf-stat reference must exist.
    # We refuse to fabricate one as a side effect of depth-N prebuild
    # because the L1 mu/sigma matters for accuracy and shouldn't
    # be sourced opportunistically.
    print(f"[prebuild] target benchmarks: {benchmarks}")
    print(f"[prebuild] target parents ({len(parents)}): "
          f"{[' . '.join(p) for p in parents]}")
    print()

    n_built = 0
    n_cached = 0
    n_failed = 0
    for bench in benchmarks:
        try:
            ReferenceProfile.load(bench)
        except Exception as e:
            print(f"[prebuild] SKIP {bench}: L1 reference missing or "
                  f"incompatible ({type(e).__name__}: {e}). "
                  f"Run `agentctl reference {bench}` first.")
            continue

        for path in parents:
            label = ".".join(path)
            print(f"[prebuild] {bench} x {label} ...", flush=True)
            try:
                rp = ReferenceProfile.load(bench)
                cache = rp.extra.get("multiplex", {})
                already = label in cache and not args.force_rebuild
                if already:
                    n_cached += 1
                    print(f"  cached (n_runs={cache[label].get('n_runs')})",
                          flush=True)
                    continue
                reference_for_descent(c, bench, path, n_runs=args.n_runs,
                                      warmup=args.warmup,
                                      force_rebuild=args.force_rebuild,
                                      verbose=False)
                n_built += 1
                print(f"  built", flush=True)
            except Exception as e:
                n_failed += 1
                print(f"  FAILED {type(e).__name__}: {e}", flush=True)

    print()
    print(f"[prebuild] summary: built={n_built}  cached={n_cached}  "
          f"failed={n_failed}  total_pairs={n_built+n_cached+n_failed}")
    return 0 if n_failed == 0 else 1


def cmd_agent(args: argparse.Namespace) -> int:
    c = AgentdClient(args.socket)
    if not c.ping():
        print("agentd not responding", file=sys.stderr)
        return 1
    if args.baseline == "B2":
        # Lazy import so google-genai is only pulled in when running the agent.
        from .agent import run_b2
        result = run_b2(c, args.benchmark, args.perturbation,
                        max_pivots=args.max_pivots,
                        max_verifier_retries=args.max_verifier_retries,
                        model=args.model)
    elif args.baseline == "B-NR":
        # Same ReAct loop as B2 but with compare_to_reference and descend
        # tools removed + a prompt prefix that disables reference-aware
        # instructions. Motivation-section ablation: shows what the
        # LLM-only agent does when stripped of the idle-baseline reference.
        from .agent import run_b2
        result = run_b2(c, args.benchmark, args.perturbation,
                        max_pivots=args.max_pivots,
                        max_verifier_retries=args.max_verifier_retries,
                        model=args.model, no_reference=True)
    elif args.baseline == "B-LLM-Prior":
        # Like B-NR but the prompt prefix OVERRIDES the strict
        # cross-validation rule and demands the agent submit the deepest
        # plausible path from prior knowledge. Measures the LLM's a priori
        # deep-path accuracy with no probe evidence at L2/L3.
        from .agent import run_b2
        result = run_b2(c, args.benchmark, args.perturbation,
                        max_pivots=args.max_pivots,
                        max_verifier_retries=args.max_verifier_retries,
                        model=args.model, llm_prior_deep=True)
    elif args.baseline == "B-LLM-Prior-Tree":
        # Like B-LLM-Prior but the prompt also embeds the static
        # subsystem tree and constrains submissions to its vocabulary.
        # Measures how much of the LLM's hallucination is reduced when
        # the output space is enumerated up-front.
        from .agent import run_b2
        result = run_b2(c, args.benchmark, args.perturbation,
                        max_pivots=args.max_pivots,
                        max_verifier_retries=args.max_verifier_retries,
                        model=args.model, llm_prior_with_tree=True)
    elif args.baseline == "B-LLM-Prior-Tree-Probes-Every-Level":
        # B2 minus the L1 reference tool: descend is kept (uses its
        # own depth-N reference internally), tree constraint applies,
        # no deep-guess override. Tests how much the L1 idle baseline
        # buys vs measured z-scores via descend.
        from .agent import run_b2
        result = run_b2(c, args.benchmark, args.perturbation,
                        max_pivots=args.max_pivots,
                        max_verifier_retries=args.max_verifier_retries,
                        model=args.model, llm_prior_tree_probes=True)
    else:
        print(f"unknown baseline {args.baseline}", file=sys.stderr)
        return 2

    print(f"\n=== {result.baseline}: {result.benchmark} x {result.perturbation} ===")
    print(f"  expected_subsystem : {result.expected_subsystem}")
    print(f"  diagnosed          : {result.diagnosed_subsystems}")
    print(f"  correct            : {result.correct}")
    print(f"  workload_ok        : {result.workload_ok}  wall={result.wall_s:.2f}s")
    if result.canned_probe:
        print(f"  canned_probe       : {result.canned_probe}")
        if result.counters:
            nz = [(i, v) for i, v in enumerate(result.counters) if v > 0]
            print(f"  agent_output (non-zero): {nz}")
    print(f"  hint               : {result.hint[:120]}")
    if args.json:
        print(json.dumps({
            "baseline": result.baseline,
            "benchmark": result.benchmark,
            "perturbation": result.perturbation,
            "expected_subsystem": result.expected_subsystem,
            "diagnosed_subsystems": result.diagnosed_subsystems,
            "correct": result.correct,
            "z_scores": result.z_scores,
            "canned_probe": result.canned_probe,
            "counters": result.counters,
        }, indent=2))
    return 0 if result.correct else 0  # don't fail script on miss; matrix.py decides


def cmd_sweep(args: argparse.Namespace) -> int:
    """Run N reps of one baseline x cell, then print the aggregate metrics
    the paper reports: first-try-acceptance, retry-budget acceptance,
    diagnostic accuracy, and the termination breakdown.

    Each rep gets its own cell_id (run_b2 assigns one fresh per call), so
    rep logs don't overwrite each other. We sleep INTER_REP_SETTLE_S between
    reps so khugepaged finishes coalescing 4K -> 2M from the previous run
    before the next one measures rates.
    """
    import time
    INTER_REP_SETTLE_S = 3.0

    c = AgentdClient(args.socket)
    if not c.ping():
        print("agentd not responding", file=sys.stderr)
        return 1

    if args.baseline == "B2":
        from .agent import run_b2
        def _run_one():
            return run_b2(c, args.benchmark, args.perturbation,
                          max_pivots=args.max_pivots,
                          max_verifier_retries=args.max_verifier_retries,
                          model=args.model)
    elif args.baseline == "B-NR":
        from .agent import run_b2
        def _run_one():
            return run_b2(c, args.benchmark, args.perturbation,
                          max_pivots=args.max_pivots,
                          max_verifier_retries=args.max_verifier_retries,
                          model=args.model, no_reference=True)
    elif args.baseline == "B-LLM-Prior":
        from .agent import run_b2
        def _run_one():
            return run_b2(c, args.benchmark, args.perturbation,
                          max_pivots=args.max_pivots,
                          max_verifier_retries=args.max_verifier_retries,
                          model=args.model, llm_prior_deep=True)
    elif args.baseline == "B-LLM-Prior-Tree":
        from .agent import run_b2
        def _run_one():
            return run_b2(c, args.benchmark, args.perturbation,
                          max_pivots=args.max_pivots,
                          max_verifier_retries=args.max_verifier_retries,
                          model=args.model, llm_prior_with_tree=True)
    elif args.baseline == "B-LLM-Prior-Tree-Probes-Every-Level":
        from .agent import run_b2
        def _run_one():
            return run_b2(c, args.benchmark, args.perturbation,
                          max_pivots=args.max_pivots,
                          max_verifier_retries=args.max_verifier_retries,
                          model=args.model, llm_prior_tree_probes=True)
    else:
        print(f"unknown baseline {args.baseline}", file=sys.stderr)
        return 2

    reps: list[dict] = []
    print(f"\n=== sweep: {args.baseline} x {args.benchmark} x "
          f"{args.perturbation} x {args.reps} reps ===")

    for i in range(args.reps):
        print(f"\n--- rep {i+1}/{args.reps} ---")
        t0 = time.time()
        try:
            result = _run_one()
        except Exception as e:
            print(f"  rep {i+1}: EXCEPTION {type(e).__name__}: {e}")
            reps.append({"rep": i+1, "exception": f"{type(e).__name__}: {e}",
                         "correct": False, "termination": "Exception",
                         "first_try_accepted": False, "pivots_used": None,
                         "wall_s": time.time() - t0})
            time.sleep(INTER_REP_SETTLE_S)
            continue

        # Every baseline here is an agent run that writes a per-rep log.json
        # with termination/first_try/pivots/descents. `log` is initialised to
        # {} so downstream `log.get(...)` is safe even if the file is missing.
        log: dict = {}
        if args.baseline in ("B2", "B-NR", "B-LLM-Prior", "B-LLM-Prior-Tree",
                             "B-LLM-Prior-Tree-Probes-Every-Level"):
            from pathlib import Path
            REPO = Path(__file__).resolve().parents[2]
            run_dirs = sorted((REPO / "results" / "runs").iterdir(),
                              key=lambda p: p.stat().st_mtime, reverse=True)
            if run_dirs:
                try:
                    log = json.loads((run_dirs[0] / "log.json").read_text())
                except Exception:
                    log = {}
            termination = log.get("termination", "?")
            first_try = log.get("first_try_accepted", False)
            pivots = log.get("pivots_used", None)
            cell_id = run_dirs[0].name if run_dirs else None
        else:
            termination = "Submitted" if result.correct else "NoConvergence"
            first_try = None
            pivots = None
            cell_id = None

        elapsed = time.time() - t0

        print(f"  rep {i+1}: correct={result.correct} "
              f"term={termination} first_try={first_try} "
              f"pivots={pivots} wall={elapsed:.1f}s")
        # Pull depth-aware fields from the per-rep log if present (descents
        # are persisted into log.json).
        descents = []
        if args.baseline in ("B2", "B-NR") and isinstance(log, dict):
            descents = log.get("descents", [])

        reps.append({"rep": i+1, "correct": result.correct,
                     "termination": termination,
                     "first_try_accepted": first_try, "pivots_used": pivots,
                     "wall_s": elapsed,
                     "diagnosed": result.diagnosed_subsystems,
                     "expected": result.expected_subsystem,
                     "cell_id": cell_id,
                     # Depth-aware scoring fields.
                     "diagnosed_path": result.diagnosed_path,
                     "expected_path": result.expected_path,
                     "score": result.score,
                     "depth_reached": result.depth_reached,
                     "descents": descents,
                     # Persist per-rep z-scores so cross-metric correlation
                     # analysis (paper Section 5) can read them later. Cheap
                     # (~17 floats per rep) and required for under-perturbation
                     # correlation matrices distinct from idle reference.
                     "z_scores": result.z_scores})

        if i < args.reps - 1:
            time.sleep(INTER_REP_SETTLE_S)

    # Aggregate
    n = len(reps)
    correct = sum(1 for r in reps if r.get("correct"))
    first_try_ok = sum(1 for r in reps
                       if r.get("first_try_accepted") and r.get("correct"))
    retry_budget_ok = sum(1 for r in reps
                          if r.get("termination") == "Submitted"
                          and r.get("correct"))
    term_counts: dict[str, int] = {}
    for r in reps:
        t = r.get("termination", "?")
        term_counts[t] = term_counts.get(t, 0) + 1

    # Depth-aware aggregates (Phase 6/7).
    score_counts = {1.0: 0, 0.5: 0, 0.0: 0}
    depths = []
    n_descended = 0
    for r in reps:
        sc = r.get("score", 0.0)
        score_counts[sc] = score_counts.get(sc, 0) + 1
        depths.append(r.get("depth_reached", 0) or 0)
        if r.get("descents"):
            n_descended += 1

    print(f"\n=== sweep aggregate ({n} reps) ===")
    print(f"  diagnostic accuracy        : {correct}/{n} = "
          f"{100*correct/n:.0f}%   (legacy L1-only)")
    print(f"  ternary score breakdown    : "
          f"deep={score_counts.get(1.0,0)}  "
          f"shallow={score_counts.get(0.5,0)}  "
          f"wrong={score_counts.get(0.0,0)}")
    if depths:
        mean_d = sum(depths) / len(depths)
        print(f"  depth_reached              : "
              f"min={min(depths)} mean={mean_d:.1f} max={max(depths)}")
    print(f"  cells with descent         : {n_descended}/{n}")
    print(f"  first-try acceptance       : {first_try_ok}/{n} = "
          f"{100*first_try_ok/n:.0f}%   (correct AND first-try probe accepted)")
    print(f"  retry-budget acceptance    : {retry_budget_ok}/{n} = "
          f"{100*retry_budget_ok/n:.0f}%   (correct AND ended in Submitted)")
    print(f"  termination breakdown      : {term_counts}")
    walls = [r.get("wall_s", 0) for r in reps]
    if walls:
        print(f"  wall per rep               : "
              f"min={min(walls):.1f}s mean={sum(walls)/len(walls):.1f}s "
              f"max={max(walls):.1f}s")

    # Persist to results/sweeps/<timestamp>.json so the paper plotter can
    # pick it up later.
    from pathlib import Path
    REPO = Path(__file__).resolve().parents[2]
    sweep_dir = REPO / "results" / "sweeps"
    sweep_dir.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y%m%d_%H%M%S")
    out = sweep_dir / f"{args.baseline}_{args.benchmark}_{args.perturbation}_{ts}.json"
    out.write_text(json.dumps({
        "baseline": args.baseline, "benchmark": args.benchmark,
        "perturbation": args.perturbation, "reps": reps,
        "model": args.model, "n": n, "correct": correct,
        "first_try_ok": first_try_ok, "retry_budget_ok": retry_budget_ok,
        "term_counts": term_counts,
    }, indent=2))
    print(f"  saved                      : {out}")

    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="agentctl")
    p.add_argument("--socket", default="/tmp/agentd.sock")
    sub = p.add_subparsers(dest="cmd", required=True)

    pr = sub.add_parser("reference", help="Build IFV from idle runs")
    pr.add_argument("benchmark")
    pr.add_argument("--n-runs", type=int, default=30)
    pr.add_argument("--warmup", type=int, default=2)
    pr.add_argument("--cov-thr", type=float, default=0.15)
    pr.set_defaults(func=cmd_reference)

    ps = sub.add_parser("show-reference", help="Pretty-print saved reference")
    ps.add_argument("benchmark")
    ps.set_defaults(func=cmd_show_reference)

    pm = sub.add_parser("reference-multiplex",
                        help="Build/cache a depth-N multiplex reference for "
                             "(benchmark, parent_path). E.g. parent_path = "
                             "kernel_paging or kernel_paging.anon_fault.")
    pm.add_argument("benchmark")
    pm.add_argument("parent_path",
                    help="dot-separated tree path, e.g. 'kernel_paging' "
                         "(depth-2) or 'kernel_paging.anon_fault' (depth-3)")
    pm.add_argument("--n-runs", type=int, default=30)
    pm.add_argument("--warmup", type=int, default=2)
    pm.add_argument("--force-rebuild", action="store_true",
                    help="rebuild and overwrite the cached entry")
    pm.set_defaults(func=cmd_reference_multiplex)

    pp = sub.add_parser("prebuild-references",
                        help="Walk the subsystem tree and build depth-N "
                             "multiplex references for one or more "
                             "benchmarks. Idempotent: cached entries are "
                             "skipped unless --force-rebuild. Run this "
                             "before B2 sweeps so the agent loop never "
                             "pays lazy-rebuild cost mid-cell.")
    g = pp.add_mutually_exclusive_group(required=False)
    g.add_argument("--all", action="store_true",
                   help="prebuild for every benchmark in benchmarks/manifest.yaml")
    pp.add_argument("benchmarks", nargs="*",
                    help="benchmark names; mutually exclusive with --all")
    pp.add_argument("--parents", nargs="*", default=None,
                    help="restrict to specific parent paths (dot-separated). "
                         "Default: every non-leaf in the active tree.")
    pp.add_argument("--n-runs", type=int, default=30)
    pp.add_argument("--warmup", type=int, default=2)
    pp.add_argument("--force-rebuild", action="store_true")
    pp.set_defaults(func=cmd_prebuild_references)

    pa = sub.add_parser("agent", help="Run one diagnostic cell")
    pa.add_argument("--baseline", choices=["B2", "B-NR", "B-LLM-Prior", "B-LLM-Prior-Tree", "B-LLM-Prior-Tree-Probes-Every-Level"],
                    required=True)
    pa.add_argument("--benchmark", required=True)
    pa.add_argument("--perturbation", required=True)
    pa.add_argument("--max-pivots", type=int, default=5)
    pa.add_argument("--max-verifier-retries", type=int, default=5)
    pa.add_argument("--model", default=None,
                    help="Model id for B2. Defaults to gemini-2.5-flash in "
                         "prod mode, qwen2.5-coder:32b in debug mode. Override "
                         "explicitly for anthropic (claude-opus-4-5) etc.")
    pa.add_argument("--json", action="store_true",
                    help="also emit a JSON dict for machine consumption")
    pa.set_defaults(func=cmd_agent)

    psw = sub.add_parser("sweep",
                         help="Run N reps of one baseline x cell, print + "
                              "persist aggregate metrics")
    psw.add_argument("--baseline", choices=["B2", "B-NR", "B-LLM-Prior", "B-LLM-Prior-Tree", "B-LLM-Prior-Tree-Probes-Every-Level"],
                     required=True)
    psw.add_argument("--benchmark", required=True)
    psw.add_argument("--perturbation", required=True)
    psw.add_argument("--reps", type=int, default=10)
    psw.add_argument("--max-pivots", type=int, default=5)
    psw.add_argument("--max-verifier-retries", type=int, default=5)
    psw.add_argument("--model", default=None)
    psw.set_defaults(func=cmd_sweep)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
