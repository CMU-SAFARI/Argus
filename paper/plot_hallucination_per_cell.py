"""plot_hallucination_per_cell.py - per-cell hallucination breakdown.

Companion to plot_hallucination.py. Renders two side-by-side heatmaps
(one per baseline) showing the number of hallucinated submissions per
(victim, perturbation) cell -- where "hallucination" = a depth >= 2
path submission that did not match the cell's expected path.

Cells are coloured by hallucination count (0, 1, 2, 3 reps out of 3),
with hatched overlay if any of those hallucinations were fabricated
names (L2/L3 leaf not in subsystems.yaml).

Output:
  paper/figures/fig_hallucination_per_cell.{pdf,png}
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
PERT_LABEL = {
    # Short tick code (for x-axis); description (for figure legend).
    # Renumbered 2026-05-15 so the x-axis reads p1..p4 left-to-right
    # regardless of the underlying perturbation-id numbering.
    "p1_thp_off":        "p1",
    "p4_drop_caches":    "p2",
    "p10_thp_aggressor": "p3",
    "p_cache_thrash":    "p4",
}
PERT_DESC = {
    "p1_thp_off":        "p1 = THP off",
    "p4_drop_caches":    "p2 = drop_caches",
    "p10_thp_aggressor": "p3 = THP aggressor",
    "p_cache_thrash":    "p4 = LLC pollute",
}
BASELINES = ["B2", "B-LLM-Prior", "B-LLM-Prior-Tree",
             "B-LLM-Prior-Tree-Probes-Every-Level"]


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


def per_cell_matrix(baseline: str, valid: set
                    ) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Returns (total_halluc, fabricated, n_reps) matrices,
    each shape (len(VICTIMS), len(PERTS))."""
    total = np.zeros((len(VICTIMS), len(PERTS)))
    fab = np.zeros((len(VICTIMS), len(PERTS)))
    n = np.zeros((len(VICTIMS), len(PERTS)))
    for i, v in enumerate(VICTIMS):
        for j, p in enumerate(PERTS):
            exp_l1 = expected_subsystem(p, victim=v)
            exp_paths = expected_paths(p, victim=v)
            fp = _latest(baseline, v, p)
            if fp is None:
                total[i, j] = np.nan
                fab[i, j] = np.nan
                continue
            d = json.loads(fp.read_text())
            reps = d.get("reps", [])
            n[i, j] = len(reps)
            for r in reps:
                diag = r.get("diagnosed_path") or []
                if len(diag) < 2:
                    continue
                s = score_path(diag, exp_l1, exp_paths)
                if s == 1.0:
                    continue  # not a hallucination -- it's correct
                total[i, j] += 1
                if tuple(diag) not in valid:
                    fab[i, j] += 1
    return total, fab, n


def draw(ax, mat_total: np.ndarray, mat_fab: np.ndarray,
         title: str, show_y: bool, vmax: float) -> object:
    # Use a red-tinted ramp so the visual maps "more hallucinations = redder"
    im = ax.imshow(mat_total, cmap="Reds", vmin=0, vmax=vmax, aspect="auto")
    for i in range(mat_total.shape[0]):
        for j in range(mat_total.shape[1]):
            t = mat_total[i, j]
            f = mat_fab[i, j]
            if np.isnan(t):
                txt = "n/a"
            elif f > 0:
                # Put the fabrication count on its own line so it fits
                # the cell width at fontsize=13 even in narrow columns.
                txt = f"{int(t)}\n({int(f)} fab)"
            else:
                txt = f"{int(t)}"
            color = "white" if (not np.isnan(t) and t >= vmax * 0.55) else "black"
            ax.text(j, i, txt, ha="center", va="center",
                    color=color, fontsize=14, fontweight="bold",
                    linespacing=0.95)
    # Light dotted separators between perturbation columns -- the cells
    # within one column are commensurable, but neighbouring columns are
    # different perturbations entirely, so the visual seam helps the eye.
    # Use a blended transform so x is in data-space (between columns) but
    # y in axes-space (extends below the plot so the tick labels are also
    # separated, not just the heatmap cells).
    from matplotlib.transforms import blended_transform_factory
    trans = blended_transform_factory(ax.transData, ax.transAxes)
    for k in range(1, len(PERTS)):
        ax.plot([k - 0.5, k - 0.5], [-0.08, 1.0], transform=trans,
                color="#888888", linestyle=":", linewidth=1.2, alpha=0.9,
                clip_on=False, zorder=5)
    ax.set_xticks(range(len(PERTS)))
    ax.set_xticklabels([PERT_LABEL[p] for p in PERTS],
                       fontsize=11, fontweight="bold")
    if show_y:
        ax.set_yticks(range(len(VICTIMS)))
        ax.set_yticklabels(VICTIMS, fontsize=13, fontweight="bold")
    else:
        ax.set_yticks(range(len(VICTIMS)))
        ax.set_yticklabels([])
    ax.set_title(title, fontsize=12, fontweight="bold")
    return im


def main() -> None:
    valid = _valid_paths()
    b2_total,    b2_fab,    n_reps = per_cell_matrix("B2",               valid)
    bl_total,    bl_fab,    _      = per_cell_matrix("B-LLM-Prior",      valid)
    blt_total,   blt_fab,   _      = per_cell_matrix("B-LLM-Prior-Tree", valid)
    blptp_total, blptp_fab, _      = per_cell_matrix(
        "B-LLM-Prior-Tree-Probes-Every-Level", valid)

    # Common color scale = total reps per cell (typically 3) so the
    # heatmap directly reads as "0 of 3 / 1 of 3 / 2 of 3 / 3 of 3"
    vmax = float(np.nanmax([n_reps.max(), 3]))

    fig, axes = plt.subplots(2, 2, figsize=(10.5, 10.4))
    # Manual margins: reserve top band for the p1..p4 legend, bottom
    # band for the "Aggressor" supxlabel. Subplot titles like
    # "(i) B-LLM-Prior" sit just under the top band.
    fig.subplots_adjust(left=0.08, right=0.92, top=0.91,
                        bottom=0.10, wspace=0.18, hspace=0.22)
    # Ordering reads left-to-right, top-to-bottom (i, ii, iii, iv);
    # Argus lands in the bottom-right.
    draw(axes[0][0], bl_total, bl_fab,
         "(i) B-LLM-Prior",
         show_y=True, vmax=vmax)
    draw(axes[0][1], blt_total, blt_fab,
         "(ii) B-LLM-Prior-Tree",
         show_y=False, vmax=vmax)
    draw(axes[1][0], blptp_total, blptp_fab,
         "(iii) B-LLM-Prior-Tree-Probes",
         show_y=True, vmax=vmax)
    im = draw(axes[1][1], b2_total, b2_fab,
              "(iv) Argus",
              show_y=False, vmax=vmax)

    # "Victim" label on the left column only (i and iii).
    axes[0][0].set_ylabel("Victim", fontsize=16, fontweight="bold")
    axes[1][0].set_ylabel("Victim", fontsize=16, fontweight="bold")
    # p1..p4 legend pinned to the TOP of the figure (above the subplot
    # titles); "Aggressor" supxlabel stays at the bottom as the x-axis
    # title for both rows.
    legend_text = "    •    ".join(PERT_DESC[p] for p in PERTS)
    fig.text(0.5, 0.965, legend_text,
             ha="center", va="bottom",
             fontsize=14, color="#1f3a93", fontweight="bold")
    fig.text(0.5, 0.025, "Aggressor",
             ha="center", va="bottom",
             fontsize=18, fontweight="bold")

    cbar = fig.colorbar(im, ax=axes.ravel().tolist(), shrink=0.85,
                        pad=0.02, fraction=0.025,
                        label="Hallucinations per cell (count of reps)")
    cbar.ax.tick_params(labelsize=12)
    cbar.set_label("Hallucinations per cell (count of reps)",
                   fontsize=13, fontweight="bold")
    cbar.set_ticks([0, 1, 2, 3])

    # Grey "card" behind the Argus subplot to anchor the eye. Must be
    # added AFTER fig.colorbar() because the colorbar resizes the listed
    # axes to make room for itself; computing the rectangle from the
    # axes bbox before that would leave the card offset to the right.
    # We force a draw first so get_position() reflects the post-colorbar
    # geometry.
    fig.canvas.draw()
    from matplotlib.patches import Rectangle
    bbox = axes[1][1].get_position()
    pad_left, pad_right = 0.025, 0.025
    pad_top, pad_bot = 0.05, 0.05
    card = Rectangle(
        (bbox.x0 - pad_left, bbox.y0 - pad_bot),
        bbox.width + pad_left + pad_right,
        bbox.height + pad_top + pad_bot,
        facecolor="#ececec", edgecolor="none",
        transform=fig.transFigure, zorder=0,
    )
    fig.add_artist(card)

    for ext in ("pdf", "png"):
        fp = OUT / f"fig_hallucination_per_cell.{ext}"
        # Use pad_inches with bbox_inches="tight" so the reserved bottom
        # band (Aggressor title + p1..p4 legend) isn't cropped.
        fig.savefig(fp, bbox_inches="tight", pad_inches=0.2,
                    dpi=200 if ext == "png" else None)
        print(f"saved: {fp}")
    plt.close(fig)

    # Text summary
    print()
    print(f"{'Victim':<14s} {'Pert':<22s}  "
          f"{'B2':>15s}  {'B-LLM-Prior':>15s}  "
          f"{'B-LLM-Prior-Tree':>17s}  {'B-LLM-PT-Probes':>17s}")
    print("-" * 115)
    for i, v in enumerate(VICTIMS):
        for j, p in enumerate(PERTS):
            b2t  = b2_total[i, j];    b2f  = b2_fab[i, j]
            bpt  = bl_total[i, j];    bpf  = bl_fab[i, j]
            btt  = blt_total[i, j];   btf  = blt_fab[i, j]
            bppt = blptp_total[i, j]; bppf = blptp_fab[i, j]
            b2s = "n/a" if np.isnan(b2t) else (
                f"{int(b2t)} ({int(b2f)} fab)" if b2f else f"{int(b2t)}")
            bps = "n/a" if np.isnan(bpt) else (
                f"{int(bpt)} ({int(bpf)} fab)" if bpf else f"{int(bpt)}")
            bts = "n/a" if np.isnan(btt) else (
                f"{int(btt)} ({int(btf)} fab)" if btf else f"{int(btt)}")
            bpps = "n/a" if np.isnan(bppt) else (
                f"{int(bppt)} ({int(bppf)} fab)" if bppf else f"{int(bppt)}")
            print(f"{v:<14s} {p:<22s}  {b2s:>15s}  {bps:>15s}  "
                  f"{bts:>17s}  {bpps:>17s}")
    print("-" * 115)
    print(f"{'TOTAL':<14s} {'':<22s}  "
          f"{int(np.nansum(b2_total)):>4d} ({int(np.nansum(b2_fab))} fab) / 45   "
          f"{int(np.nansum(bl_total)):>4d} ({int(np.nansum(bl_fab))} fab) / 45   "
          f"{int(np.nansum(blt_total)):>4d} ({int(np.nansum(blt_fab))} fab) / 45     "
          f"{int(np.nansum(blptp_total)):>4d} ({int(np.nansum(blptp_fab))} fab) / 45")


if __name__ == "__main__":
    main()
