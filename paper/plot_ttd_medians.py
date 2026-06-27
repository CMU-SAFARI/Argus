"""plot_ttd_medians.py - compact 1-column bar chart of median TtD per
evaluation point.

Mirrors the visual design of paper/plot_ftrace_overhead_cpu_v2.py:
  - narrow figure (1-column width)        - +Ns annotations above bars
  - per-bar hatch + colour palette        - dotted-grey separators
  - grey background highlight under Argus - no top/right spines
  - Roboto font, bold ticks/labels        - linear y axis

Data source: same sweep JSONs that fig_hallucination / fig_hallucination_per_cell
read; medians are computed inline by reusing paper/time_to_diagnosis.py's
data path.

Output:
  paper/figures/fig_ttd_medians.{pdf,png}
"""
from __future__ import annotations

import glob
import json
import re
import statistics
import sys
from pathlib import Path

import matplotlib.font_manager as fm
import matplotlib.pyplot as plt
import numpy as np

REPO = Path(__file__).resolve().parents[1]
SWEEPS = REPO / "results" / "sweeps"
OUT_DIR = REPO / "paper" / "figures"

for ttf in glob.glob("/usr/share/fonts/truetype/roboto/**/*.ttf", recursive=True):
    fm.fontManager.addfont(ttf)

VICTIMS = ["gups", "stream", "pf_anon_write", "llama_cpp", "fork_storm"]
PERTS = ["p1_thp_off", "p4_drop_caches", "p10_thp_aggressor", "p_cache_thrash"]

# (baseline key, x-label, fill colour, hatch). Colours mirror
# paper/plot_hallucination.py so the figure reads consistently with the
# other baseline comparisons (red/amber/blue/green for i..iv).
CONDITIONS = [
    ("B-LLM-Prior",                          "(i)\nB-LLM\nPrior",            "#cc6b6b", "xx"),
    ("B-LLM-Prior-Tree",                     "(ii)\nB-LLM\nPrior-Tree",      "#d6a85a", "//"),
    ("B-LLM-Prior-Tree-Probes-Every-Level",  "(iii)\nB-LLM\nPrior-Tree-Probes", "#6a8cb3", ".."),
    ("B2",                                   "(iv)\nArgus",                   "#9ab36a", "oo"),
]
HIGHLIGHT_KEY = "B2"

TS_RE = re.compile(r"_(\d{8}_\d{6})\.json$")


def _filename_ts(p: Path) -> str:
    m = TS_RE.search(p.name)
    return m.group(1) if m else ""


def latest(baseline: str, victim: str, pert: str) -> Path | None:
    cands = sorted(SWEEPS.glob(f"{baseline}_{victim}_{pert}_*.json"),
                   key=_filename_ts, reverse=True)
    return cands[0] if cands else None


def median_for(baseline: str) -> float:
    walls: list[float] = []
    for v in VICTIMS:
        for p in PERTS:
            fp = latest(baseline, v, p)
            if fp is None:
                continue
            d = json.loads(fp.read_text())
            for r in d.get("reps", []):
                w = r.get("wall_s")
                if isinstance(w, (int, float)):
                    walls.append(float(w))
    return statistics.median(walls) if walls else float("nan")


def main() -> int:
    medians = {k: median_for(k) for k, *_ in CONDITIONS}
    print("Median TtD per baseline (across 5 victims x 4 perts x 3 reps):")
    for k, label, _, _ in CONDITIONS:
        print(f"  {k:<42s}  median={medians[k]:>6.1f}s")
    print()

    plt.rcParams.update({
        "font.family":      "Roboto",
        "font.size":        11,
        "axes.linewidth":   1.2,
        "axes.labelweight": "bold",
    })

    fig, ax = plt.subplots(figsize=(4.0, 1.3))
    fig.subplots_adjust(left=0.20, right=0.97, bottom=0.48, top=0.92)

    x = np.arange(len(CONDITIONS))
    vals = [medians[k] for k, _, _, _ in CONDITIONS]
    colors = [c for _, _, c, _ in CONDITIONS]
    hatches = [h for _, _, _, h in CONDITIONS]

    for xi, val, col, hatch in zip(x, vals, colors, hatches):
        ax.bar(xi, val, 0.55, color=col, hatch=hatch,
               edgecolor="black", linewidth=1.0, zorder=3)

    # Dotted-grey vertical separators between adjacent bars.
    for sep in np.arange(len(CONDITIONS) - 1) + 0.5:
        ax.axvline(sep, linestyle=":", color="0.55", linewidth=1.0,
                   alpha=0.85, zorder=2)

    # Per-bar median annotation.
    ymax = max(vals)
    for xi, val in zip(x, vals):
        ax.text(xi, val + ymax * 0.04, f"{val:.1f}s",
                ha="center", va="bottom",
                fontsize=8, fontweight="bold", color="#333333")

    ax.set_xticks(x)
    ax.set_xticklabels([lab for _, lab, _, _ in CONDITIONS],
                       fontsize=6, fontweight="bold")
    ax.set_xlim(-0.5, len(CONDITIONS) - 0.5)
    ax.tick_params(axis="y", labelsize=7)
    for label in ax.get_yticklabels():
        label.set_fontweight("bold")
    ax.set_ylabel("Median\nTime-to-Diagnosis (s)",
                  fontsize=8, fontweight="bold")
    ax.set_ylim(0, ymax * 1.22)

    # White background everywhere except a grey "highlight" band behind
    # the Argus bar.
    ax.set_facecolor("white")
    highlight_idx = next((i for i, c in enumerate(CONDITIONS)
                          if c[0] == HIGHLIGHT_KEY), None)
    if highlight_idx is not None:
        ax.axvspan(highlight_idx - 0.5, highlight_idx + 0.5,
                   color="#EBEBEB", zorder=0.5)
    ax.yaxis.grid(True, zorder=2, which="major",
                  linestyle="--", linewidth=0.7, color="0.65")
    ax.set_axisbelow(True)
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.spines["left"].set_linewidth(1.2)
    ax.spines["bottom"].set_linewidth(1.2)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for ext in ("pdf", "png"):
        fp = OUT_DIR / f"fig_ttd_medians.{ext}"
        fig.savefig(fp, bbox_inches="tight",
                    dpi=300 if ext == "png" else None)
        print(f"saved: {fp}")
    plt.close(fig)
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
