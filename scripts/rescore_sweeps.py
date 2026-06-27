#!/usr/bin/env python3
"""Re-score saved sweep JSONs against the *current* manifest.

Each rep's `score` is computed once, at agent-submit time, against the
manifest's state then. After a manifest update (e.g. Option A
structural-null + accept-list edits applied post-sweep), the stored
scores go stale. Plotters read `score` directly, so they'd still show
the old numbers.

This script walks results/sweeps/*.json, re-applies
`score_path(diagnosed_path, expected_subsystem, expected_paths)` with
the current manifest, and writes updated `score` / `correct` /
`expected` / `expected_path` back into each rep + recomputes the
top-level aggregates. No agent inference. No eBPF. Just file I/O +
score arithmetic.

Run:
    python scripts/rescore_sweeps.py                    # all sweep JSONs
    python scripts/rescore_sweeps.py results/sweeps/B2_*.json
    python scripts/rescore_sweeps.py --dry-run          # show diffs only
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from orchestrator.agentctl.agent_baselines import score_path
from orchestrator.agentctl.perturb import expected_paths, expected_subsystem


def rescore_one(path: Path, dry_run: bool = False) -> tuple[bool, str]:
    """Re-score a single sweep JSON. Returns (changed, summary)."""
    d = json.loads(path.read_text())
    perturbation = d.get("perturbation")
    benchmark = d.get("benchmark")
    if not perturbation or not benchmark:
        return False, "missing perturbation/benchmark"

    exp = expected_subsystem(perturbation, victim=benchmark)
    exp_paths = expected_paths(perturbation, victim=benchmark)

    reps = d.get("reps") or []
    diffs: list[str] = []
    new_scores: list[float] = []
    new_correct = 0
    for r in reps:
        diagnosed = r.get("diagnosed_path") or []
        old_score = float(r.get("score") or 0.0)
        new_score = score_path(diagnosed, exp, exp_paths)
        l1_correct = bool(diagnosed) and diagnosed[0] in exp
        if abs(new_score - old_score) > 1e-9:
            diffs.append(
                f"  rep={r.get('rep')} diag={diagnosed} "
                f"score {old_score:.2f} -> {new_score:.2f}"
            )
        r["score"] = new_score
        r["correct"] = l1_correct
        r["expected"] = exp
        r["expected_path"] = exp_paths
        new_scores.append(new_score)
        new_correct += int(l1_correct)

    # Top-level aggregates that plotters / CLI summary read.
    d["correct"] = new_correct
    # first_try_ok / retry_budget_ok are agent-side mechanics, not
    # ground-truth, so they're untouched.

    if not diffs:
        return False, "unchanged"
    if not dry_run:
        path.write_text(json.dumps(d, indent=2))
    return True, "\n".join(diffs)


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(
        description="Re-score sweep JSONs against the current manifest.")
    ap.add_argument(
        "paths", nargs="*", type=Path,
        help="Sweep JSONs to re-score. Default: results/sweeps/*.json")
    ap.add_argument("--dry-run", action="store_true",
                    help="Print diffs without writing back.")
    ap.add_argument("--quiet", action="store_true",
                    help="Only print files whose scores changed.")
    args = ap.parse_args(argv)

    paths = args.paths or sorted((REPO / "results" / "sweeps").glob("*.json"))
    n_total = 0
    n_changed = 0
    for p in paths:
        n_total += 1
        try:
            changed, summary = rescore_one(p, dry_run=args.dry_run)
        except Exception as e:
            print(f"  ERR {p.name}: {type(e).__name__}: {e}")
            continue
        if changed:
            n_changed += 1
            print(f"\n{p.name}:")
            print(summary)
        elif not args.quiet:
            print(f"  ok  {p.name}")

    suffix = " (dry-run, nothing written)" if args.dry_run else ""
    print(f"\n=== {n_changed} / {n_total} files updated{suffix}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
