"""plot_overhead.py — beautified single-panel CPU-time overhead chart.

Renders results/overhead_experiment.csv (produced by
scripts/run_overhead_experiment.sh) as the banner-style 3-bar figure
the paper's motivation section uses, mirroring the visual design of
paper/plot_overhead_cpu.py:

  - narrow figure (3.4 x 1.2)            - +N ns annotations above bars
  - per-bar hatch + colour palette       - dotted-grey separators
  - grey background, no top/right spines - Roboto font, bold ticks/labels
  - LOG-SCALE y axis                     (added 2026-05-15)

Bars are ordered ftrace -> count+latency (T_x) -> count-only,
biggest-first, and the y-axis values are the per-fault (per-work-unit)
*delta* over the no-probe baseline. The baseline itself is implicit at
y = 0 and not drawn as a bar.

Output:
  paper/figures/fig_overhead.{pdf,png}
"""
from __future__ import annotations

import csv
import glob
import statistics
import sys
from collections import defaultdict
from pathlib import Path

import matplotlib.font_manager as fm
import matplotlib.pyplot as plt
import numpy as np

REPO = Path(__file__).resolve().parents[1]
CSV = REPO / "results" / "overhead_experiment.csv"
OUT_DIR = REPO / "paper" / "figures"

# Register Roboto if installed; fall back to default if not.
for ttf in glob.glob("/usr/share/fonts/truetype/roboto/**/*.ttf", recursive=True):
    fm.fontManager.addfont(ttf)

# (mode_key_in_csv, x-label, fill colour, hatch).
# Visual palette and hatches echo paper/plot_overhead_cpu.py.
CONDITIONS = [
    ("ftrace_fg",     "ftrace func_graph",               "#C44E52", "xx"),
    ("count_only",    "BPF kprobe\n(count only)",        "#A4B465", "//"),
    ("count_latency", "BPF kprobe+kretprobe\n(T_x)",     "#DDA853", ".."),
]
# Index of the "headline" condition (the proposed mechanism). The plot
# highlights this bar with a grey panel background while the other bars
# sit on white, so the eye lands on T_x.
HIGHLIGHT_KEY = "count_latency"


def main() -> int:
    if not CSV.exists():
        print(f"missing {CSV} -- run scripts/run_overhead_experiment.sh first",
              file=sys.stderr)
        return 1

    rows: dict[str, list[float]] = defaultdict(list)
    with open(CSV) as f:
        for r in csv.DictReader(f):
            try:
                rows[r["mode"]].append(float(r["ns_per_wu"]))
            except (KeyError, ValueError):
                continue

    if not rows.get("no_probe"):
        print("no_probe baseline missing from CSV; cannot normalize",
              file=sys.stderr)
        return 1

    base_mean = statistics.fmean(rows["no_probe"])
    deltas: dict[str, float] = {}
    sds: dict[str, float] = {}
    for key, _, _, _ in CONDITIONS:
        samples = rows.get(key) or []
        m = statistics.fmean(samples) if samples else float("nan")
        s = statistics.pstdev(samples) if len(samples) > 1 else 0.0
        deltas[key] = m - base_mean
        sds[key] = s

    print("Per-mode overhead summary:")
    print(f"  no_probe       baseline mean={base_mean:.2f} ns/wu "
          f"(n={len(rows['no_probe'])})")
    for key, label, _, _ in CONDITIONS:
        n = len(rows.get(key, []))
        print(f"  {key:<14s} n={n}  delta=+{deltas[key]:.2f} ns/wu "
              f"sd={sds[key]:.2f}")
    print()

    # ---- render --------------------------------------------------------
    plt.rcParams.update({
        "font.family":      "Roboto",
        "font.size":        11,
        "axes.linewidth":   1.2,
        "axes.labelweight": "bold",
    })

    fig, ax = plt.subplots(figsize=(3.4, 1.2))
    fig.subplots_adjust(left=0.25, right=0.97, bottom=0.48, top=0.95)

    x = np.arange(len(CONDITIONS))
    vals = [max(deltas[k], 1.0) for k, _, _, _ in CONDITIONS]
    # ^ clip at 1 ns/wu so log scale doesn't blow up on near-zero overhead;
    #   the +N annotation still shows the true delta.
    colors = [c for _, _, c, _ in CONDITIONS]
    hatches = [h for _, _, _, h in CONDITIONS]

    for xi, val, col, hatch in zip(x, vals, colors, hatches):
        ax.bar(xi, val, 0.32, color=col, hatch=hatch,
               edgecolor="black", linewidth=1.0, zorder=3)

    # Dotted-grey vertical separators between adjacent bars.
    for sep in np.arange(len(CONDITIONS) - 1) + 0.5:
        ax.axvline(sep, linestyle=":", color="0.55", linewidth=1.0,
                   alpha=0.85, zorder=2)

    # Per-bar overhead annotation (uses the TRUE delta, not the clipped value).
    for xi, k in zip(x, [c[0] for c in CONDITIONS]):
        true_v = deltas[k]
        plot_v = max(true_v, 1.0)
        ax.text(xi, plot_v * 1.25, f"+{true_v:.0f} ns",
                ha="center", va="bottom",
                fontsize=6, fontweight="bold", color="#333333")

    ax.set_xticks(x)
    ax.set_xticklabels([lab for _, lab, _, _ in CONDITIONS],
                       fontsize=6, fontweight="bold")
    ax.set_xlim(-0.5, len(CONDITIONS) - 0.5)
    ax.tick_params(axis="y", labelsize=6)
    for label in ax.get_yticklabels():
        label.set_fontweight("bold")
    ax.set_ylabel("CPU Execution\nTime overhead\n(log scale)",
                  fontsize=7, fontweight="bold")
    ax.set_yscale("log")
    # Pad the top so the +N labels don't clip; in log space this is a
    # multiplicative factor.
    ymax = max(vals)
    ax.set_ylim(1.0, ymax * 6.0)

    # White background everywhere except a grey "highlight" band behind
    # the T_x bar (visually anchors the proposed mechanism).
    ax.set_facecolor("white")
    highlight_idx = next((i for i, c in enumerate(CONDITIONS)
                          if c[0] == HIGHLIGHT_KEY), None)
    if highlight_idx is not None:
        ax.axvspan(highlight_idx - 0.5, highlight_idx + 0.5,
                   color="#EBEBEB", zorder=0.5)
    # Grid sits ABOVE the highlight band (zorder 2) so the dashed
    # horizontal lines remain visible across the entire plot, not just
    # the white-background region.
    ax.yaxis.grid(True, zorder=2, which="both",
                  linestyle="--", linewidth=0.7, color="0.65")
    ax.set_axisbelow(True)
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.spines["left"].set_linewidth(1.2)
    ax.spines["bottom"].set_linewidth(1.2)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for ext in ("pdf", "png"):
        fp = OUT_DIR / f"fig_overhead.{ext}"
        fig.savefig(fp, bbox_inches="tight",
                    dpi=300 if ext == "png" else None)
        print(f"saved: {fp}")
    plt.close(fig)
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
