"""Render the CS#1 cross-workload heatmap from the CSV produced by
scripts/cs1_pf_types_matrix.py.

For each L2 slot under `kernel_paging` (or whatever parent the matrix
ran), produce a heatmap of z-scores indexed by (workload, reference).

The diagonal (workload == reference) should be near zero; off-diagonal
cells reveal which slot distinguishes the workload from the reference
baseline. The headline observation is that each PF-type victim "lights
up" exactly the L2 child it's authored against.

Usage:
  python paper/plot_cs1_heatmap.py \\
      --csv paper/figures/cs1_matrix_kernel_paging.csv
  # or, for an L3 matrix:
  python paper/plot_cs1_heatmap.py \\
      --csv paper/figures/cs1_matrix_kernel_paging_anon_fault.csv
"""
from __future__ import annotations

import argparse
import csv
import sys
from collections import defaultdict
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
from matplotlib.colors import SymLogNorm

REPO = Path(__file__).resolve().parents[1]

# Display-only relabels (CSV/YAML unchanged). The L2 leaf is named
# `tlb_shootdown` in subsystems.yaml, but its probe set
# (`flush_tlb_mm_range` + `native_flush_tlb_multi`) measures total mm-range
# TLB-flush volume biased toward IPI shootdowns -- not pure shootdowns.
# We label the panel `tlb_flush` to match what the probes actually count.
DISPLAY_NAMES = {"tlb_shootdown": "tlb_flush"}


def parse_z(s: str) -> float:
    if s == "inf":
        return float("inf")
    if s == "-inf":
        return float("-inf")
    try:
        return float(s)
    except ValueError:
        return 0.0


def load_csv(csv_path: Path) -> tuple[list[str], list[str], list[str],
                                         dict[tuple[str, str], dict[str, float]]]:
    workloads: list[str] = []
    references: list[str] = []
    slots: list[str] = []
    cell: dict[tuple[str, str], dict[str, float]] = {}
    with open(csv_path) as f:
        rdr = csv.reader(f)
        header = next(rdr)
        slots = header[2:]
        for row in rdr:
            w, r, *zs = row
            if w not in workloads:
                workloads.append(w)
            if r not in references:
                references.append(r)
            cell[(w, r)] = dict(zip(slots, [parse_z(s) for s in zs]))
    return workloads, references, slots, cell


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--csv", required=True, type=Path)
    ap.add_argument("--out-name", default=None,
                    help="output filename stem; default derives from --csv")
    ap.add_argument("--linthresh", type=float, default=10.0,
                    help="size of the linear region of the diverging colour "
                         "scale (default 10). |z_log| <= linthresh maps "
                         "linearly to colour; outside that, the colour map "
                         "compresses logarithmically -- so a cell at +90 and "
                         "one at +1625 still look visually distinct. The CSV "
                         "values themselves are T_x log-space z-scores (no "
                         "transform applied at plot time). Inf shown as 'inf'.")
    ap.add_argument("--vmax", type=float, default=None,
                    help="cap the colour scale at +/- this value. If unset, "
                         "uses max(|z|) across all panels so the colourbar "
                         "spans the actual data.")
    ap.add_argument("--drop-slots", default="",
                    help="comma-separated list of slot names to omit from "
                         "the figure (e.g. 'swap_in' when the column is "
                         "all-zero and adds no information).")
    ap.add_argument("--cmap", default="RdBu_r",
                    help="matplotlib colormap (default RdBu_r). Use "
                         "RdYlBu_r / coolwarm / vlag for softer palettes.")
    ap.add_argument("--no-title", action="store_true",
                    help="suppress the suptitle.")
    ap.add_argument("--bold-labels", action="store_true",
                    help="render axis labels (workload/reference), tick "
                         "labels, and panel titles in bold.")
    args = ap.parse_args()
    fw = "bold" if args.bold_labels else "normal"

    workloads, references, slots, cell = load_csv(args.csv)
    if not slots:
        print("error: empty CSV", file=sys.stderr)
        return 1
    drop = {s.strip() for s in args.drop_slots.split(",") if s.strip()}
    if drop:
        slots = [s for s in slots if s not in drop]

    out_stem = args.out_name or args.csv.stem.replace("cs1_matrix_",
                                                       "cs1_heatmap_")
    out_pdf = REPO / "paper" / "figures" / f"{out_stem}.pdf"
    out_png = REPO / "paper" / "figures" / f"{out_stem}.png"

    n_slots = len(slots)
    nrows = 1
    ncols = n_slots
    panel_w = 3.6 if args.bold_labels else 3.0
    fig, axes = plt.subplots(nrows, ncols,
                              figsize=(panel_w * ncols + 1.4, 3.8),
                              squeeze=False, layout="constrained")

    # Build Z matrices first so we can compute a consistent colourbar
    # range across all panels. Inf/-inf are replaced with a large finite
    # sentinel so SymLogNorm doesn't choke; cell text still shows 'inf'.
    Zs: list[np.ndarray] = []
    for slot in slots:
        Z = np.zeros((len(workloads), len(references)))
        for r_i, w in enumerate(workloads):
            for c_i, ref in enumerate(references):
                z = cell.get((w, ref), {}).get(slot, 0.0)
                if z == float("inf"):
                    Z[r_i][c_i] = 1e6
                elif z == float("-inf"):
                    Z[r_i][c_i] = -1e6
                else:
                    Z[r_i][c_i] = z
        Zs.append(Z)

    finite_max = max(
        (np.abs(Z[np.isfinite(Z) & (np.abs(Z) < 1e6)]).max()
         for Z in Zs if Z.size > 0),
        default=args.linthresh,
    )
    vmax = args.vmax if args.vmax is not None else max(finite_max,
                                                        args.linthresh * 1.1)
    # SymLogNorm: linear in (-linthresh, +linthresh), log outside. Lets a
    # +90 cell and a +1625 cell stay visually distinct without the small
    # diagonal cells (|z| < linthresh) collapsing into one colour.
    norm = SymLogNorm(linthresh=args.linthresh,
                      linscale=1.0,
                      vmin=-vmax, vmax=vmax, base=10)

    for j, slot in enumerate(slots):
        ax = axes[0][j]
        Z = Zs[j]
        im = ax.imshow(Z, cmap=args.cmap, aspect="auto", norm=norm)
        ax.set_xticks(np.arange(len(references)))
        ax.set_xticklabels(references, rotation=30, ha="right",
                           fontsize=7, fontweight=fw)
        ax.set_yticks(np.arange(len(workloads)))
        ax.set_yticklabels(workloads, fontsize=7, fontweight=fw)
        ax.set_title(DISPLAY_NAMES.get(slot, slot),
                     fontsize=10, fontweight=fw)
        if j == 0:
            ax.set_ylabel("workload", fontsize=9, fontweight=fw)
        ax.set_xlabel("reference", fontsize=9, fontweight=fw)
        # Annotate each cell with the z-score (raw, not log).
        for r_i, w in enumerate(workloads):
            for c_i, ref in enumerate(references):
                z = cell.get((w, ref), {}).get(slot, 0.0)
                # z_log is in sigma units (log-T_x space). Typical range
                # is roughly [-10, +20]; show one decimal everywhere so
                # both faint cells (e.g. +0.4) and strong ones (+11.2)
                # read cleanly.
                if z == float("inf"):
                    txt = "inf"
                elif z == float("-inf"):
                    txt = "-inf"
                else:
                    txt = f"{z:+.1f}"
                ax.text(c_i, r_i, txt, ha="center", va="center",
                        fontsize=8 if args.bold_labels else 6,
                        fontweight=fw,
                        color=("white"
                               if abs(Z[r_i][c_i]) > args.linthresh
                               else "black"))
    cbar = fig.colorbar(im, ax=axes[0].tolist(), shrink=0.85,
                         pad=0.02, fraction=0.04,
                         label=r"$T_x$ z-score")
    if not args.no_title:
        fig.suptitle(
            f"CS#1: cross-workload L2 z-scores by slot ({args.csv.stem})",
            fontsize=10,
        )
    fig.savefig(out_pdf, bbox_inches="tight")
    fig.savefig(out_png, bbox_inches="tight", dpi=140)
    print(f"saved: {out_pdf}")
    print(f"saved: {out_png}")
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
