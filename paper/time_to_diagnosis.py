"""time_to_diagnosis.py - per-baseline Time-to-Diagnosis (TtD) report.

Reads the same sweep JSONs as plot_hallucination_per_cell.py
(BASELINES x VICTIMS x PERTS, the 5x4 motivation grid) and reports:

  - per-baseline mean / median / total wall_s across all reps
  - per-cell mean wall_s (5x4 grid per baseline)
  - relative TtD: how many x slower Argus is than each LLM-prior baseline

`wall_s` is the field each sweep rep records as the end-to-end time
between the agent loop start and submit_diagnosis(). It is the user-
visible diagnosis latency. Sweep JSONs that lack `wall_s` (older runs)
are skipped silently.

Usage:
  python paper/time_to_diagnosis.py
"""
from __future__ import annotations

import glob
import json
import re
import statistics
import sys
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parents[1]
SWEEPS = REPO / "results" / "sweeps"

VICTIMS = ["gups", "stream", "pf_anon_write", "llama_cpp", "fork_storm"]
PERTS = ["p1_thp_off", "p4_drop_caches", "p10_thp_aggressor", "p_cache_thrash"]
BASELINES = ["B-LLM-Prior", "B-LLM-Prior-Tree",
             "B-LLM-Prior-Tree-Probes-Every-Level", "B2"]

TS_RE = re.compile(r"_(\d{8}_\d{6})\.json$")


def _filename_ts(p: Path) -> str:
    m = TS_RE.search(p.name)
    return m.group(1) if m else ""


def latest(baseline: str, victim: str, pert: str) -> Path | None:
    """Pick the most recent sweep file by the YYYYMMDD_HHMMSS embedded
    in the filename (not mtime: rescore touched all mtimes)."""
    cands = sorted(SWEEPS.glob(f"{baseline}_{victim}_{pert}_*.json"),
                   key=_filename_ts, reverse=True)
    return cands[0] if cands else None


def per_cell_walls(baseline: str) -> tuple[np.ndarray, list[list[list[float]]]]:
    """Returns (mean_wall_s_matrix, raw_walls_per_cell) for the 5x4 grid.
    raw_walls_per_cell[i][j] is the list of wall_s values for that cell."""
    mat = np.zeros((len(VICTIMS), len(PERTS)))
    raw: list[list[list[float]]] = [
        [[] for _ in PERTS] for _ in VICTIMS
    ]
    for i, v in enumerate(VICTIMS):
        for j, p in enumerate(PERTS):
            fp = latest(baseline, v, p)
            if fp is None:
                mat[i, j] = np.nan
                continue
            d = json.loads(fp.read_text())
            walls = [r.get("wall_s") for r in d.get("reps", [])
                     if isinstance(r.get("wall_s"), (int, float))]
            raw[i][j] = walls
            mat[i, j] = float(statistics.fmean(walls)) if walls else float("nan")
    return mat, raw


def main() -> int:
    per_baseline_walls: dict[str, list[float]] = {}
    per_baseline_mat: dict[str, np.ndarray] = {}
    per_baseline_raw: dict[str, list[list[list[float]]]] = {}
    for b in BASELINES:
        mat, raw = per_cell_walls(b)
        per_baseline_mat[b] = mat
        per_baseline_raw[b] = raw
        flat = [w for row in raw for cell in row for w in cell]
        per_baseline_walls[b] = flat

    # ---- per-baseline aggregate report ----------------------------------
    print()
    print(f"{'Baseline':<40s}  {'reps':>5s}  "
          f"{'mean':>8s}  {'median':>8s}  {'p90':>8s}  "
          f"{'total':>10s}")
    print("-" * 92)
    for b in BASELINES:
        ws = per_baseline_walls[b]
        if not ws:
            print(f"{b:<40s}  (no data)")
            continue
        mean = statistics.fmean(ws)
        median = statistics.median(ws)
        p90 = float(np.percentile(ws, 90))
        total = sum(ws)
        print(f"{b:<40s}  {len(ws):>5d}  "
              f"{mean:>7.1f}s  {median:>7.1f}s  {p90:>7.1f}s  "
              f"{total:>9.0f}s")
    print()

    # ---- per-cell mean wall, for each baseline ---------------------------
    for b in BASELINES:
        print(f"--- {b}: per-cell mean wall_s (5 victims x 4 perturbations) ---")
        print(f"{'victim':<16s}  " + "  ".join(f"{p:>22s}" for p in PERTS))
        for i, v in enumerate(VICTIMS):
            cells = [f"{per_baseline_mat[b][i, j]:>22.1f}"
                     if not np.isnan(per_baseline_mat[b][i, j]) else
                     f"{'n/a':>22s}"
                     for j, _ in enumerate(PERTS)]
            print(f"{v:<16s}  " + "  ".join(cells))
        print()

    # ---- relative TtD: Argus vs each baseline (MEDIANS) ------------------
    print("--- Relative TtD (MEDIANS): Argus (B2) vs each baseline ---")
    print(f"{'Comparison':<48s}  "
          f"{'Argus med':>12s}  {'Other med':>12s}  "
          f"{'ratio':>7s}  {'delta':>10s}")
    print("-" * 96)
    b2_med = statistics.median(per_baseline_walls["B2"]) \
        if per_baseline_walls.get("B2") else float("nan")
    for b in BASELINES:
        if b == "B2":
            continue
        ws = per_baseline_walls[b]
        if not ws or not per_baseline_walls.get("B2"):
            continue
        m = statistics.median(ws)
        ratio = b2_med / m if m > 0 else float("nan")
        delta = b2_med - m
        print(f"B2 vs {b:<42s}  "
              f"{b2_med:>11.1f}s  {m:>11.1f}s  "
              f"{ratio:>6.2f}x  {delta:>+9.1f}s")
    print()

    # Aggregate "Argus vs average of LLM-prior baseline medians"
    llm_meds = [statistics.median(per_baseline_walls[b])
                for b in BASELINES if b != "B2" and per_baseline_walls[b]]
    if llm_meds and per_baseline_walls.get("B2"):
        avg_llm = statistics.fmean(llm_meds)
        print(f"Average of LLM-prior medians:   {avg_llm:.1f}s")
        print(f"Argus (B2) median:              {b2_med:.1f}s")
        delta_pct = 100.0 * (b2_med - avg_llm) / avg_llm
        print(f"Argus vs avg LLM-prior median:  "
              f"{b2_med / avg_llm:.2f}x "
              f"({b2_med - avg_llm:+.1f}s, "
              f"{delta_pct:+.0f}%)")
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
