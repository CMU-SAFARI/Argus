"""plot_correctness_mode.py - per-victim correctness bar chart for a fixed
perturbation M, replacing the misleading "% of reps flagged in subsystem"
view with "% of reps correctly diagnosed".

For each rep in the latest sweep JSON:
  - score == 1.0  -> correct (either the agent submitted the expected path,
                     OR the agent correctly abstained when expected=['none'])
  - score <  1.0  -> wrong

The chart shows one horizontal bar per victim with that fraction, and
annotates each bar with a breakdown:
  "2/3 ok | 1 mis-flag" or "3/3 ok (all abstain)" etc.

Reads the same sweep files as plot_aggressor_mode.py but interprets them
through `score_path()` (which honours the per-victim manifest including
Option A structural-null markings) and the post-Option-A submit_diagnosis
overrides that have already been applied to the JSONs by
scripts/resimulate_overrides.py.

Usage:
  python paper/plot_correctness_mode.py --perturbation p10_thp_aggressor
  python paper/plot_correctness_mode.py --perturbation p_cache_thrash \\
      --victims gups,stream,pf_anon_write,llama_cpp,fork_storm

Output:
  paper/figures/fig5_correctness_<perturbation>_<baseline>.{pdf,png}
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from glob import glob
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
import yaml  # noqa: E402
from orchestrator.agentctl.agent_baselines import score_path  # noqa: E402
from orchestrator.agentctl.perturb import expected_paths, expected_subsystem  # noqa: E402

SWEEPS = REPO / "results" / "sweeps"
OUT = REPO / "paper" / "figures"
OUT.mkdir(parents=True, exist_ok=True)

DEFAULT_VICTIMS = ["gups", "stream", "pf_anon_write", "llama_cpp", "fork_storm"]

# Cached subsystem tree -- used to decide whether a commit terminates at a
# tree leaf (deepest possible diagnosis under that L1) or at a non-leaf
# (the agent stopped earlier than the tree allows, typically because the
# probe set can't decompose the actual bottleneck even though the tree
# has deeper leaves).
_TREE_CACHE: dict | None = None


def _tree() -> dict:
    global _TREE_CACHE
    if _TREE_CACHE is None:
        path = REPO / "orchestrator" / "configs" / "subsystems.yaml"
        _TREE_CACHE = yaml.safe_load(path.read_text())
    return _TREE_CACHE


def _node_at(path: list[str]) -> dict | None:
    """Return the subsystem tree node at the given path, or None if absent."""
    if not path:
        return None
    nodes = (_tree().get("subsystems") or [])
    cur = None
    for name in path:
        cur = next((n for n in nodes if n.get("name") == name), None)
        if cur is None:
            return None
        nodes = cur.get("children") or []
    return cur


def is_leaf_commit(path: list[str]) -> bool:
    """True iff the deepest node in `path` has no children in the tree.
    A depth-1 commit at e.g. ['kernel_paging'] is NOT a leaf (kernel_paging
    has children); ['kernel_scheduler', 'load_balance'] IS a leaf
    (load_balance has no children). Abstain ['none'] returns False here --
    callers handle abstain separately."""
    if not path or path == ["none"]:
        return False
    node = _node_at(path)
    if node is None:
        return False
    return not (node.get("children") or [])

_TS_RE = re.compile(r"_(\d{8}_\d{6})\.json$")


def _filename_ts(fp: str) -> str:
    m = _TS_RE.search(fp)
    return m.group(1) if m else ""


def latest_sweep(baseline: str, victim: str, perturbation: str) -> Path | None:
    cands = []
    for fp in glob(str(SWEEPS / f"{baseline}_{victim}_{perturbation}_*.json")):
        d = json.load(open(fp))
        if (d.get("n") or 0) < 3:
            continue
        cands.append((_filename_ts(fp), Path(fp)))
    if not cands:
        return None
    return max(cands)[1]


def classify_rep(rep: dict, exp_sub: list[str], exp_paths: list[list[str]]
                 ) -> str:
    """Return one of:
       'correct-flag-leaf'    - score==1.0, commit reaches a tree leaf
       'correct-flag-nonleaf' - score==1.0, commit is at a non-leaf
                                (deeper tree paths exist but our probe
                                set can't decompose; manifest accepts L1)
       'correct-abstain'      - submitted ['none'] and expected=['none']
       'miss-flag'            - submitted a non-abstain path that misses
       'miss-abstain'         - submitted ['none'] but expected non-abstain
    """
    diag = rep.get("diagnosed_path") or []
    s = score_path(diag, exp_sub, exp_paths)
    submitted_abstain = (diag == ["none"]) or (not diag)
    if s == 1.0:
        if submitted_abstain:
            return "correct-abstain"
        return ("correct-flag-leaf" if is_leaf_commit(diag)
                else "correct-flag-nonleaf")
    return "miss-abstain" if submitted_abstain else "miss-flag"


def build_breakdown(baseline: str, perturbation: str, victims: list[str]
                    ) -> tuple[list[dict], list[str]]:
    rows: list[dict] = []
    missing: list[str] = []
    for v in victims:
        fp = latest_sweep(baseline, v, perturbation)
        if fp is None:
            missing.append(v)
            rows.append({"victim": v, "file": None, "n": 0,
                         "correct-flag-leaf": 0, "correct-flag-nonleaf": 0,
                         "correct-abstain": 0,
                         "miss-flag": 0, "miss-abstain": 0,
                         "expected_abstain": None})
            continue
        d = json.load(open(fp))
        exp_sub = expected_subsystem(perturbation, victim=v)
        exp_paths = expected_paths(perturbation, victim=v)
        expected_abstain = (exp_sub == ["none"]) or (not exp_paths and
                                                     exp_sub in (["none"], []))
        counts = {"correct-flag-leaf": 0, "correct-flag-nonleaf": 0,
                  "correct-abstain": 0,
                  "miss-flag": 0, "miss-abstain": 0,
                  "nonleaf-L1": 0, "nonleaf-L2": 0, "nonleaf-L3": 0}
        nonleaf_depths: list[int] = []
        for r in d.get("reps", []):
            cls = classify_rep(r, exp_sub, exp_paths)
            counts[cls] += 1
            if cls == "correct-flag-nonleaf":
                depth = _meaningful_depth(r.get("diagnosed_path") or [])
                nonleaf_depths.append(depth)
                key = f"nonleaf-L{min(depth, 3)}"
                counts[key] += 1
        rows.append({"victim": v, "file": fp.name, "n": d.get("n", 0),
                     **counts, "expected_abstain": expected_abstain,
                     "nonleaf_depths": nonleaf_depths})
    return rows, missing


def render(rows: list[dict], baseline: str, perturbation: str,
           out_stem: str, bold: bool = True) -> None:
    fw = "bold" if bold else "normal"
    n_victims = len(rows)
    # 1-column wide, short: vertical stacked bars (victims along x-axis).
    # Sized to match paper/plot_ttd_medians.py for visual consistency.
    fig, ax = plt.subplots(figsize=(4.0, 1.4), layout="constrained")

    x = np.arange(n_victims)

    # Stacked vertical segments per victim:
    #   green        = correctly flagged at leaf level (tree leaf)
    #   light purple = correctly flagged at non-leaf depth L1
    #   dark purple  = correctly flagged at non-leaf depth L2
    #   blue         = correctly abstained
    GREEN, NL_L1, NL_L2, BLUE = "#7fb069", "#c5a8d9", "#6d4d8f", "#5a8bbf"

    leaf_pct = np.array([
        100.0 * r["correct-flag-leaf"] / max(r["n"], 1) for r in rows
    ])
    nl_l1_pct = np.array([
        100.0 * r["nonleaf-L1"] / max(r["n"], 1) for r in rows
    ])
    nl_l2_pct = np.array([
        100.0 * r["nonleaf-L2"] / max(r["n"], 1) for r in rows
    ])
    abstain_pct = np.array([
        100.0 * r["correct-abstain"] / max(r["n"], 1) for r in rows
    ])
    correct_pct = leaf_pct + nl_l1_pct + nl_l2_pct + abstain_pct

    bar_w = 0.7
    ax.bar(x, leaf_pct, bar_w, color=GREEN, edgecolor="black",
           linewidth=0.6, label="_nolegend_")
    ax.bar(x, nl_l1_pct, bar_w, bottom=leaf_pct, color=NL_L1,
           edgecolor="black", linewidth=0.6, label="_nolegend_")
    ax.bar(x, nl_l2_pct, bar_w, bottom=leaf_pct + nl_l1_pct, color=NL_L2,
           edgecolor="black", linewidth=0.6, label="_nolegend_")
    ax.bar(x, abstain_pct, bar_w,
           bottom=leaf_pct + nl_l1_pct + nl_l2_pct,
           color=BLUE, edgecolor="black", linewidth=0.6, label="_nolegend_")

    # Integer count inside each segment that takes >= 25%.
    for i, r in enumerate(rows):
        if r["n"] == 0:
            continue
        cum = 0.0
        for cnt, seg, txt_color in [
                (r["correct-flag-leaf"], leaf_pct[i], "white"),
                (r["nonleaf-L1"],         nl_l1_pct[i], "black"),
                (r["nonleaf-L2"],         nl_l2_pct[i], "white"),
                (r["correct-abstain"],    abstain_pct[i], "white")]:
            if seg >= 25 and cnt:
                ax.text(x[i], cum + seg / 2.0, str(cnt),
                        ha="center", va="center",
                        fontsize=8, fontweight=fw, color=txt_color)
            cum += seg
        miss = r["miss-flag"] + r["miss-abstain"]
        if miss:
            ax.text(x[i], correct_pct[i] + 3, f"{miss}",
                    ha="center", va="bottom",
                    fontsize=8, fontweight=fw, color="#c44")

    ax.set_xticks(x)
    ax.set_xticklabels([r["victim"] for r in rows],
                       fontsize=7, fontweight=fw, rotation=20, ha="right")
    ax.set_ylim(0, 110)
    ax.set_yticks([0, 50, 100])
    ax.set_yticklabels(["0", "50", "100"],
                       fontsize=7, fontweight=fw)
    ax.set_ylabel("Correct reps (%)",
                  fontsize=8, fontweight=fw)
    ax.set_axisbelow(True)
    ax.yaxis.grid(True, linestyle=":", color="0.65", linewidth=0.6)
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.tick_params(axis="x", length=2, pad=1)
    ax.tick_params(axis="y", length=2, pad=1)

    # Compact legend at top, single row.
    from matplotlib.patches import Patch
    legend = [
        Patch(facecolor=GREEN, edgecolor="black", label="leaf"),
        Patch(facecolor=NL_L1, edgecolor="black", label="non-leaf L1"),
        Patch(facecolor=NL_L2, edgecolor="black", label="non-leaf L2"),
        Patch(facecolor=BLUE, edgecolor="black", label="abstain"),
    ]
    ax.legend(handles=legend, loc="upper center",
              bbox_to_anchor=(0.5, 1.4), ncol=4,
              framealpha=0.95, frameon=False,
              handlelength=0.9, handleheight=0.9, columnspacing=1.2,
              prop={"weight": "bold", "size": 7.5})

    fig.savefig(OUT / f"{out_stem}.pdf", bbox_inches="tight")
    fig.savefig(OUT / f"{out_stem}.png", bbox_inches="tight", dpi=160)
    plt.close(fig)


def _meaningful_depth(path: list[str]) -> int:
    """Return the length of the longest prefix of `path` that traces a real
    parent->child chain in the subsystem tree. Paths like
    ['kernel_paging', 'kernel_memory_mgmt'] (where the second element is
    another L1, not a tree-child of the first) get truncated to length 1.
    Abstain or empty paths return 1 to mirror prior behaviour."""
    if not path or path == ["none"]:
        return 1
    nodes = (_tree().get("subsystems") or [])
    cur_name = path[0]
    cur = next((n for n in nodes if n.get("name") == cur_name), None)
    if cur is None:
        return 1
    depth = 1
    for name in path[1:]:
        kids = cur.get("children") or []
        nxt = next((c for c in kids if c.get("name") == name), None)
        if nxt is None:
            break
        cur = nxt
        depth += 1
    return depth


def _nonleaf_depth(r: dict) -> int:
    """Median depth of non-leaf correct-flag submissions in this victim's
    reps. Depth is len(diagnosed_path); L1=root subsystem (len 1), L2=child
    (len 2), L3=grandchild (len 3). Falls back to 1 if no non-leaf reps.
    """
    depths = sorted(r.get("nonleaf_depths") or [])
    return depths[len(depths) // 2] if depths else 1


def _path_short(r: dict) -> str:
    """Short tag describing which subsystem(s) were correctly flagged.
    Pulls the L1 head from the diagnosed_path of the correct-flag reps."""
    fp = SWEEPS / r["file"] if r["file"] else None
    if not fp or not fp.exists():
        return "flag"
    d = json.load(open(fp))
    heads: list[str] = []
    for rep in d.get("reps", []):
        diag = rep.get("diagnosed_path") or []
        if rep.get("correct") == 1 and diag and diag != ["none"]:
            heads.append(diag[0])
    if not heads:
        return "flag"
    uniq = sorted(set(heads))
    if len(uniq) == 1:
        return uniq[0].replace("kernel_", "k_")
    return "/".join(u.replace("kernel_", "k_") for u in uniq)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--baseline", default="B2")
    ap.add_argument("--perturbation", required=True)
    ap.add_argument("--victims", default=",".join(DEFAULT_VICTIMS),
                    help="comma-separated victim list")
    ap.add_argument("--out-suffix", default="")
    args = ap.parse_args()

    victims = [v.strip() for v in args.victims.split(",") if v.strip()]
    rows, missing = build_breakdown(args.baseline, args.perturbation, victims)
    if missing:
        print(f"warning: no sweep file for: {missing}", file=sys.stderr)

    out_stem = (f"fig5_correctness_{args.perturbation}_{args.baseline}"
                f"{args.out_suffix}")
    render(rows, args.baseline, args.perturbation, out_stem)

    print(f"\nbaseline={args.baseline}  M={args.perturbation}")
    print(f"{'victim':<16}{'n':>4} {'leaf':>5} {'nonleaf':>8} {'abs':>4} "
          f"{'misflag':>8} {'misabs':>7}  source")
    print("-" * 95)
    for r in rows:
        print(f"{r['victim']:<16}{r['n']:>4} "
              f"{r['correct-flag-leaf']:>5} {r['correct-flag-nonleaf']:>8} "
              f"{r['correct-abstain']:>4} "
              f"{r['miss-flag']:>8} {r['miss-abstain']:>7}  "
              f"{r['file'] or '-'}")
    print()
    print(f"wrote {OUT / out_stem}.pdf + .png")
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
