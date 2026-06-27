"""B2 per-rep cost profile from results/runs/<cell_id>/log.json files.

For each B2 sweep cell, walks the run dirs (one per rep), aggregates:
  - tool-call totals: time spent in compile_ebpf, attach_ebpf, run_workload,
    collect_ofv, compare_to_reference, descend, detach, submit_diagnosis.
  - the gap (LLM RTT) is implicit: total_wall - sum(tool elapsed_s).

Usage:
  python paper/cost_profile.py \\
      --benchmarks pf_anon_write \\
      --perturbations p1_thp_off p14_madvise_storm p7_taskset_oversubscribe
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
RUNS = REPO / "results" / "runs"
SWEEPS = REPO / "results" / "sweeps"


def latest_b2_sweep_mtime(bench: str, pert: str) -> float:
    """Return the mtime of the most recent B2 sweep for this cell. Run dirs
    older than this likely belong to an older sweep iteration -- we keep
    only those whose mtime is >= sweep_mtime - some_slack."""
    best = 0.0
    for fp in glob(str(SWEEPS / f"B2_{bench}_{pert}_*.json")):
        try:
            d = json.load(open(fp))
        except Exception:
            continue
        if d.get("baseline") == "B2":
            best = max(best, os.path.getmtime(fp))
    return best


def percentile(xs: list[float], p: float) -> float:
    if not xs:
        return 0.0
    s = sorted(xs)
    idx = max(0, min(len(s) - 1, int(math.ceil(p / 100 * len(s))) - 1))
    return s[idx]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--benchmarks", nargs="+", required=True)
    ap.add_argument("--perturbations", nargs="+", required=True)
    ap.add_argument("--max-age-min", type=int, default=180,
                    help="ignore run dirs older than the latest B2 sweep "
                         "by more than this many minutes")
    args = ap.parse_args()

    print(f"{'cell':<48} {'tool':<24} "
          f"{'mean_s':>7} {'p95_s':>7} {'count':>6} {'reps':>5}")
    pooled: dict[str, list[float]] = defaultdict(list)
    pooled_reps = 0
    for bench in args.benchmarks:
        for pert in args.perturbations:
            sweep_mt = latest_b2_sweep_mtime(bench, pert)
            if sweep_mt == 0.0:
                continue
            cutoff = sweep_mt - args.max_age_min * 60.0
            run_glob = RUNS / f"{bench}_{pert}_*"
            buckets: dict[str, list[float]] = defaultdict(list)
            wall_per_rep: list[float] = []
            n_reps = 0
            for d in glob(str(run_glob)):
                if os.path.getmtime(d) < cutoff:
                    continue
                lf = Path(d) / "log.json"
                if not lf.exists():
                    continue
                try:
                    log = json.load(open(lf))
                except Exception:
                    continue
                attempts = log.get("attempts", []) or []
                rep_total = 0.0
                for a in attempts:
                    t = a.get("tool_name", "?")
                    e = float(a.get("elapsed_s", 0.0) or 0.0)
                    buckets[t].append(e)
                    rep_total += e
                # descend isn't in attempts; it's in the descents list.
                for desc in log.get("descents", []) or []:
                    e = float(desc.get("elapsed_s", 0.0) or 0.0)
                    if e > 0:
                        buckets["descend"].append(e)
                        rep_total += e
                wall_per_rep.append(rep_total)
                n_reps += 1
            if n_reps == 0:
                continue
            cell = f"{bench} x {pert}"
            for tool in sorted(buckets):
                vs = buckets[tool]
                print(f"{cell:<48} {tool:<24} "
                      f"{sum(vs)/len(vs):>7.2f} {percentile(vs, 95):>7.2f} "
                      f"{len(vs):>6d} {n_reps:>5d}")
                pooled[tool].extend(vs)
            print(f"{cell:<48} {'TOTAL_WALL_PER_REP':<24} "
                  f"{sum(wall_per_rep)/n_reps:>7.2f} "
                  f"{percentile(wall_per_rep, 95):>7.2f} {n_reps:>6d} {n_reps:>5d}")
            print()
            pooled_reps += n_reps

    print(f"=== POOLED across all cells (n_reps={pooled_reps}) ===")
    for tool in sorted(pooled):
        vs = pooled[tool]
        print(f"  {tool:<24} mean={sum(vs)/len(vs):6.2f}s "
              f"p95={percentile(vs, 95):6.2f}s  count={len(vs)}")
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
