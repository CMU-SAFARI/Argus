"""Failure taxonomy across the latest B2 sweep JSONs.

Classifies every B2 rep into one of:
  - DEEP                    : score = 1.0 (full credit)
  - SHALLOW_NoDescent       : score = 0.5; agent never called descend()
  - SHALLOW_WrongL2Leaf     : score = 0.5; descended but L2 child does not match expected_path
  - WRONG_AgentGaveUp       : score = 0.0; termination = ModelGaveUp
  - WRONG_VerifierExhausted : score = 0.0; termination = VerifierBlocked
  - WRONG_AbstainedExplicit : score = 0.0; submitted empty path
  - WRONG_WrongL1           : score = 0.0; submitted a path whose L1 is not in expected_subsystem
  - WRONG_NoConvergence     : score = 0.0; termination = NoConvergence
  - WRONG_Other             : score = 0.0; none of the above

Helps understand where prompt patches / system fixes pay off most.

Usage:
  python paper/failure_taxonomy.py \\
      --benchmarks pf_anon_write \\
      --perturbations p1_thp_off p14_madvise_storm p7_taskset_oversubscribe \\
      --baseline B2
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict
from glob import glob
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SWEEPS = REPO / "results" / "sweeps"


def latest_sweep(baseline: str, bench: str, pert: str) -> dict | None:
    best_mt = -1.0
    best = None
    for fp in glob(str(SWEEPS / "*.json")):
        try:
            d = json.load(open(fp))
        except Exception:
            continue
        if (d.get("baseline") == baseline and d.get("benchmark") == bench
                and d.get("perturbation") == pert):
            mt = os.path.getmtime(fp)
            if mt > best_mt:
                best_mt, best = mt, d
    return best


def classify(rep: dict, expected: list[str]) -> str:
    score = rep.get("score", 0.0)
    term = rep.get("termination", "")
    diag = rep.get("diagnosed_path", []) or []
    descents = rep.get("descents") or []
    if score == 1.0:
        return "DEEP"
    if score == 0.5:
        if not descents:
            return "SHALLOW_NoDescent"
        return "SHALLOW_WrongL2Leaf"
    # score == 0.0
    if term == "ModelGaveUp":
        return "WRONG_AgentGaveUp"
    if term == "VerifierBlocked":
        return "WRONG_VerifierExhausted"
    if not diag:
        return "WRONG_AbstainedExplicit"
    if diag and expected and diag[0] not in expected:
        return "WRONG_WrongL1"
    if term == "NoConvergence":
        return "WRONG_NoConvergence"
    return "WRONG_Other"


CATEGORY_ORDER = [
    "DEEP",
    "SHALLOW_NoDescent",
    "SHALLOW_WrongL2Leaf",
    "WRONG_AgentGaveUp",
    "WRONG_VerifierExhausted",
    "WRONG_AbstainedExplicit",
    "WRONG_WrongL1",
    "WRONG_NoConvergence",
    "WRONG_Other",
]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--benchmarks", nargs="+", required=True)
    ap.add_argument("--perturbations", nargs="+", required=True)
    ap.add_argument("--baseline", default="B2")
    args = ap.parse_args()

    pooled: dict[str, int] = defaultdict(int)
    pooled_n = 0
    print(f"{'cell':<48} {'category':<28} {'n':>3} {'pct':>6}")
    for bench in args.benchmarks:
        for pert in args.perturbations:
            d = latest_sweep(args.baseline, bench, pert)
            if d is None:
                continue
            reps = d.get("reps", [])
            n = len(reps)
            if n == 0:
                continue
            counts: dict[str, int] = defaultdict(int)
            for r in reps:
                cat = classify(r, r.get("expected_subsystem")
                                or r.get("expected") or [])
                counts[cat] += 1
                pooled[cat] += 1
            cell = f"{bench} x {pert}"
            for cat in CATEGORY_ORDER:
                k = counts.get(cat, 0)
                if k == 0:
                    continue
                print(f"{cell:<48} {cat:<28} {k:>3} {100*k/n:>5.1f}%")
            print()
            pooled_n += n

    print(f"=== POOLED across all cells ({args.baseline}, n={pooled_n}) ===")
    for cat in CATEGORY_ORDER:
        k = pooled.get(cat, 0)
        if k == 0:
            continue
        print(f"  {cat:<28} {k:>3} {100*k/max(pooled_n,1):>5.1f}%")
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
