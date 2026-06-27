"""Variance summary across the latest sweep JSONs.

For each (baseline, benchmark, perturbation) cell, reads the most-recent
sweep JSON and reports:
- Wilson 95% binomial CI for deep% / shallow% / wrong%
- mean +/- std of wall_s per rep
- mean depth_reached
- descended%

Intended to give the paper its error bars before any claim like
"B2 hits 40% deep" is published.

Usage:
  python paper/variance_summary.py \\
      --benchmarks pf_anon_write \\
      --perturbations p1_thp_off p14_madvise_storm p7_taskset_oversubscribe \\
      --baselines B0 B0.5 B1 B2
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
from collections import defaultdict
from glob import glob
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SWEEPS = REPO / "results" / "sweeps"


def wilson_ci(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """Wilson 95% binomial confidence interval. Returns (lo, hi) in [0,1]."""
    if n == 0:
        return (0.0, 0.0)
    p = k / n
    denom = 1.0 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = (z / denom) * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return (max(0.0, centre - half), min(1.0, centre + half))


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


def fmt_pct_ci(k: int, n: int) -> str:
    if n == 0:
        return "n/a"
    pct = 100.0 * k / n
    lo, hi = wilson_ci(k, n)
    return f"{pct:5.1f}% [{100*lo:4.1f}, {100*hi:4.1f}]"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--benchmarks", nargs="+", required=True)
    ap.add_argument("--perturbations", nargs="+", required=True)
    ap.add_argument("--baselines", nargs="+", default=["B0", "B0.5", "B1", "B2"])
    args = ap.parse_args()

    print(f"{'baseline':<6} {'cell':<42} {'n':>3}  "
          f"{'deep% (95% CI)':<22} {'shallow% (95% CI)':<22} "
          f"{'wrong% (95% CI)':<22} {'descended%':<12} "
          f"{'wall_s (mean±std)':<18}")
    pooled = defaultdict(lambda: {"deep": 0, "shal": 0, "wrong": 0,
                                    "desc": 0, "n": 0, "depth_sum": 0,
                                    "wall": []})
    rows_printed = 0
    for b in args.baselines:
        for bench in args.benchmarks:
            for pert in args.perturbations:
                d = latest_sweep(b, bench, pert)
                if d is None:
                    continue
                reps = d.get("reps", [])
                n = len(reps)
                if n == 0:
                    continue
                deep = sum(1 for r in reps if r.get("score") == 1.0)
                shal = sum(1 for r in reps if r.get("score") == 0.5)
                wrong = sum(1 for r in reps if r.get("score") == 0.0)
                desc = sum(1 for r in reps if r.get("descents"))
                walls = [r.get("wall_s", 0.0) or 0.0 for r in reps]
                wmean = sum(walls) / max(len(walls), 1)
                wstd = math.sqrt(
                    sum((w - wmean) ** 2 for w in walls) / max(len(walls), 1))
                depths = [r.get("depth_reached", 0) or 0 for r in reps]
                cell = f"{bench} x {pert}"
                print(f"{b:<6} {cell:<42} {n:>3}  "
                      f"{fmt_pct_ci(deep, n):<22} "
                      f"{fmt_pct_ci(shal, n):<22} "
                      f"{fmt_pct_ci(wrong, n):<22} "
                      f"{fmt_pct_ci(desc, n):<12} "
                      f"{wmean:5.1f}±{wstd:5.1f}")
                rows_printed += 1
                p = pooled[b]
                p["deep"] += deep
                p["shal"] += shal
                p["wrong"] += wrong
                p["desc"] += desc
                p["n"] += n
                p["depth_sum"] += sum(depths)
                p["wall"].extend(walls)

    if rows_printed == 0:
        print("error: no matching sweeps found", file=sys.stderr)
        return 1

    print()
    print(f"{'POOLED':<49} {'n':>3}  {'deep% (95% CI)':<22} "
          f"{'shallow% (95% CI)':<22} {'wrong% (95% CI)':<22} "
          f"{'descended%':<12} {'wall_s':<10}")
    for b in args.baselines:
        if b not in pooled:
            continue
        p = pooled[b]
        n = p["n"]
        if n == 0:
            continue
        wmean = sum(p["wall"]) / len(p["wall"])
        wstd = math.sqrt(
            sum((w - wmean) ** 2 for w in p["wall"]) / len(p["wall"]))
        print(f"{b:<49} {n:>3}  "
              f"{fmt_pct_ci(p['deep'], n):<22} "
              f"{fmt_pct_ci(p['shal'], n):<22} "
              f"{fmt_pct_ci(p['wrong'], n):<22} "
              f"{fmt_pct_ci(p['desc'], n):<12} "
              f"{wmean:5.1f}±{wstd:5.1f}")

    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
