"""Per-cell reliability: does the L1 detector consistently flag the
expected subsystem across reps, and does B2 deep% correlate with that?

For each (benchmark, perturbation) cell:
  B0_reliability = fraction of B0 reps where diagnosed_path[0] is in the
                   manifest's expected_subsystem set.
  B2_deep_rate   = fraction of B2 reps with score == 1.0.
  B2_descended_rate = fraction of B2 reps that called descend().

A cell with B0_reliability < 0.7 is "marginal" -- the FV signal is
borderline and the headline numbers will be noisy regardless of B2
sophistication. The paper should call those out explicitly rather than
quote single-rep results.

Usage:
  python paper/cell_reliability.py --benchmarks pf_anon_write \\
      --perturbations p1_thp_off p14_madvise_storm p7_taskset_oversubscribe
"""
from __future__ import annotations

import argparse
import json
import os
import sys
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


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--benchmarks", nargs="+", required=True)
    ap.add_argument("--perturbations", nargs="+", required=True)
    ap.add_argument("--marginal-thresh", type=float, default=0.7,
                    help="B0_reliability below this = marginal cell "
                         "(default 0.7)")
    args = ap.parse_args()

    print(f"{'cell':<48} {'B0_rel':>7} {'B0.5_rel':>9} {'B1_rel':>7}  "
          f"{'B2_deep':>8} {'B2_desc':>8}  {'flag':<10}")
    for bench in args.benchmarks:
        for pert in args.perturbations:
            row: dict[str, float | None] = {}
            for b in ["B0", "B0.5", "B1", "B2"]:
                d = latest_sweep(b, bench, pert)
                if d is None:
                    row[b] = None
                    continue
                reps = d.get("reps", [])
                exp = (d.get("reps", [{}])[0].get("expected_subsystem")
                       or d.get("reps", [{}])[0].get("expected") or [])
                if not reps:
                    row[b] = None
                    continue
                if b == "B2":
                    row[f"{b}_deep"] = sum(1 for r in reps
                                            if r.get("score") == 1.0) / len(reps)
                    row[f"{b}_desc"] = sum(1 for r in reps
                                            if r.get("descents")) / len(reps)
                # Reliability: fraction whose diagnosed_path[0] is in expected.
                hits = 0
                for r in reps:
                    diag = r.get("diagnosed_path", []) or []
                    if diag and diag[0] in exp:
                        hits += 1
                row[f"{b}_rel"] = hits / len(reps)
            cell = f"{bench} x {pert}"
            b0r = row.get("B0_rel")
            b05r = row.get("B0.5_rel")
            b1r = row.get("B1_rel")
            b2deep = row.get("B2_deep")
            b2desc = row.get("B2_desc")
            flag = "MARGINAL" if (b0r is not None
                                    and b0r < args.marginal_thresh) else "ok"
            print(f"{cell:<48} "
                  f"{(b0r if b0r is not None else float('nan')):>7.2f} "
                  f"{(b05r if b05r is not None else float('nan')):>9.2f} "
                  f"{(b1r if b1r is not None else float('nan')):>7.2f}  "
                  f"{(b2deep if b2deep is not None else float('nan')):>8.2f} "
                  f"{(b2desc if b2desc is not None else float('nan')):>8.2f}  "
                  f"{flag:<10}")

    print()
    print("Reliability = fraction of reps whose diagnosed L1 is in the "
          "manifest's expected_subsystem set.")
    print("Marginal cells (B0_rel < {:.0%}) have FV signals near the "
          "3-sigma threshold; per-rep numbers are noisy and headline "
          "statistics need n>=20 to be tight.".format(args.marginal_thresh))
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
