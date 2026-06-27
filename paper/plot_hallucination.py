"""plot_hallucination.py - bar chart of hallucination rates.

Hallucination defined as: agent submitted a depth >= 2 path that did
not match the ground-truth path under the per-victim manifest. Such
submissions are confident deep claims that turn out wrong -- exactly
the LLM "made up an answer" failure mode the paper claims B2 mitigates.

Stratified into two flavors:
  - Fabricated: the L2/L3 child name is not even in the subsystems.yaml
    tree. Pure LLM invention.
  - Wrong-attribution: name exists in the tree, but doesn't match the
    expected path for this (victim, perturbation) cell.

Output:
  paper/figures/fig_hallucination.{pdf,png}
"""
from __future__ import annotations

import glob
import json
import sys
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
import yaml

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
from orchestrator.agentctl.agent_baselines import score_path  # noqa: E402
from orchestrator.agentctl.perturb import (  # noqa: E402
    expected_paths,
    expected_subsystem,
)

OUT = REPO / "paper" / "figures"
OUT.mkdir(parents=True, exist_ok=True)
SWEEPS = REPO / "results" / "sweeps"

VICTIMS = ["gups", "stream", "pf_anon_write", "llama_cpp", "fork_storm"]
PERTS = ["p1_thp_off", "p4_drop_caches", "p10_thp_aggressor", "p_cache_thrash"]
BASELINES = ["B-LLM-Prior", "B-LLM-Prior-Tree",
             "B-LLM-Prior-Tree-Probes-Every-Level", "B2"]
# ^ Order = how bars appear left-to-right. Argus (B2) is rightmost so
#   the narrative reads "weakest baseline -> strongest agent."


def _valid_paths() -> set[tuple[str, ...]]:
    tree = yaml.safe_load((REPO / "orchestrator" / "configs" /
                           "subsystems.yaml").read_text())
    paths: set[tuple[str, ...]] = set()
    for l1 in tree.get("subsystems", []):
        n1 = l1["name"]
        paths.add((n1,))
        for l2 in (l1.get("children") or []):
            n2 = l2["name"]
            paths.add((n1, n2))
            for l3 in (l2.get("children") or []):
                paths.add((n1, n2, l3["name"]))
    return paths


def _latest(b: str, v: str, p: str) -> Path | None:
    cands = sorted(SWEEPS.glob(f"{b}_{v}_{p}_*.json"),
                   key=lambda x: x.stat().st_mtime, reverse=True)
    return cands[0] if cands else None


def collect_stats() -> dict[str, dict[str, int]]:
    """Per-baseline: total reps, total deep submissions, fabricated, wrong-attr."""
    valid = _valid_paths()
    out = {b: {"total": 0, "deep": 0, "fabricated": 0,
               "wrong_attr": 0, "deep_correct": 0} for b in BASELINES}
    for v in VICTIMS:
        for p in PERTS:
            exp_l1 = expected_subsystem(p, victim=v)
            exp_paths = expected_paths(p, victim=v)
            for b in BASELINES:
                fp = _latest(b, v, p)
                if fp is None:
                    continue
                d = json.loads(fp.read_text())
                for r in d.get("reps", []):
                    diag = r.get("diagnosed_path") or []
                    out[b]["total"] += 1
                    if len(diag) < 2:
                        continue
                    out[b]["deep"] += 1
                    s = score_path(diag, exp_l1, exp_paths)
                    if s == 1.0:
                        out[b]["deep_correct"] += 1
                    else:
                        if tuple(diag) not in valid:
                            out[b]["fabricated"] += 1
                        else:
                            out[b]["wrong_attr"] += 1
    return out


# Matches paper/figures/overhead_cpu.png styling: bold-axis labels, hatched
# bars, value annotations on top.
COLORS = {"B2": "#9ab36a", "B-LLM-Prior": "#cc6b6b",
          "B-LLM-Prior-Tree": "#d6a85a",
          "B-LLM-Prior-Tree-Probes-Every-Level": "#6a8cb3"}  # green/red/amber/blue
SHORT = {"B-LLM-Prior":      "(i)\nB-LLM-Prior",
         "B-LLM-Prior-Tree": "(ii)\nB-LLM-Prior\n-Tree",
         "B-LLM-Prior-Tree-Probes-Every-Level":
                             "(iii)\nB-LLM-Prior\n-Tree-Probes",
         "B2":               "(iv)\nArgus"}
HATCHES = {"wrong_attr": "...", "fabricated": "xxx"}


def main() -> None:
    stats = collect_stats()
    print("Per-baseline submission breakdown:")
    for b in BASELINES:
        s = stats[b]
        print(f"  {b}: total={s['total']}  deep={s['deep']}  "
              f"deep_correct={s['deep_correct']}  "
              f"wrong_attr={s['wrong_attr']}  fabricated={s['fabricated']}")
    print()

    # Sized to match paper/plot_correctness_mode.py for visual consistency.
    fig, ax = plt.subplots(figsize=(4.0, 1.4), layout="constrained")
    bar_w = 0.55
    x = np.arange(len(BASELINES))

    # Stacked: wrong_attr (bottom) + fabricated (top)
    wrong_attr = np.array([stats[b]["wrong_attr"] for b in BASELINES])
    fab = np.array([stats[b]["fabricated"] for b in BASELINES])
    totals = wrong_attr + fab
    n_reps = stats["B2"]["total"]  # same denominator for both

    # As percent of total reps
    wa_pct = 100.0 * wrong_attr / n_reps
    fab_pct = 100.0 * fab / n_reps
    tot_pct = wa_pct + fab_pct

    # NOTE: bars are colored per-baseline (semantic = "which baseline").
    # The legend, in contrast, communicates the hatch convention (semantic
    # = "wrong-attr vs fabricated") and should NOT inherit any one
    # baseline's color, otherwise readers conflate green with "good".
    # We pass legend handles explicitly with a neutral grey fill below.
    bars_wa = ax.bar(x, wa_pct, bar_w,
                     color=[COLORS[b] for b in BASELINES],
                     edgecolor="black", linewidth=0.8,
                     hatch=HATCHES["wrong_attr"])
    bars_fab = ax.bar(x, fab_pct, bar_w, bottom=wa_pct,
                      color=[COLORS[b] for b in BASELINES],
                      edgecolor="black", linewidth=0.8,
                      alpha=0.65,
                      hatch=HATCHES["fabricated"])

    from matplotlib.patches import Patch
    # Neutral faded-pink fill in the legend patches: a colour that
    # appears nowhere else in the figure (the per-bar palette is
    # green/red/amber/blue), so the reader can't confuse a legend
    # swatch with a baseline.
    LEGEND_FILL = "#9777c6"
    legend_handles = [
        Patch(facecolor=LEGEND_FILL, edgecolor="black", linewidth=0.8,
              hatch=HATCHES["wrong_attr"],
              label="Wrong attribution"),
        Patch(facecolor=LEGEND_FILL, edgecolor="black", linewidth=0.8,
              alpha=0.65, hatch=HATCHES["fabricated"],
              label="Fabricated"),
    ]

    # Top annotations: total percent + count
    for i, b in enumerate(BASELINES):
        wa_n, fab_n = wrong_attr[i], fab[i]
        total_n = wa_n + fab_n
        pct = tot_pct[i]
        ax.text(i, pct + 1.5, f"+{total_n} ({pct:.0f}%)",
                ha="center", va="bottom", fontsize=7, fontweight="bold")

    ax.set_xticks(x)
    ax.set_xticklabels([SHORT[b] for b in BASELINES],
                       fontsize=7, fontweight="bold")
    ax.set_ylabel("Hallucinations\n(deep & miss)",
                  fontsize=8, fontweight="bold")
    ax.set_ylim(0, max(tot_pct) * 1.45 + 5)
    ax.set_yticks(range(0, int(max(tot_pct) * 1.30) + 5, 20))
    ax.set_yticklabels([f"{v}%" for v in range(0, int(max(tot_pct) * 1.30) + 5, 20)],
                       fontsize=7)
    ax.grid(axis="y", linestyle=":", alpha=0.4)
    ax.set_axisbelow(True)
    ax.legend(handles=legend_handles,
              loc="upper left", fontsize=6, framealpha=0.95, ncol=2,
              handlelength=1.0, handleheight=0.9, borderpad=0.3,
              columnspacing=1.0)
    # White background overall; a grey highlight band sits only behind
    # the Argus bar (rightmost, index 3) to anchor the eye on the
    # proposed mechanism, matching the visual treatment of fig_overhead.
    ax.set_facecolor("white")
    # Pin xlim FIRST so the band can be drawn flush to the right spine
    # without dragging xlim further out (axvspan otherwise extends the
    # auto-xlim and squishes the bars to the left half).
    ax.set_xlim(-0.5, len(BASELINES) - 0.5)
    argus_idx = BASELINES.index("B2") if "B2" in BASELINES else None
    if argus_idx is not None:
        ax.axvspan(argus_idx - 0.5, ax.get_xlim()[1],
                   color="#ececec", zorder=0)
    # Dotted grey vertical separators between bars (matches fig_overhead).
    for sep in np.arange(len(BASELINES) - 1) + 0.5:
        ax.axvline(sep, linestyle=":", color="0.55", linewidth=1.0,
                   alpha=0.85, zorder=1)

    for ext in ("pdf", "png"):
        fp = OUT / f"fig_hallucination.{ext}"
        fig.savefig(fp, bbox_inches="tight",
                    dpi=200 if ext == "png" else None)
        print(f"saved: {fp}")
    plt.close(fig)


if __name__ == "__main__":
    main()
