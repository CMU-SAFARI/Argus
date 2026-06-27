"""inspect_sweep_cell.py - per-rep diagnostic dump for a (baseline, victim,
perturbation) cell.

For each rep in the latest sweep JSON it prints:
  - what the agent submitted (path) and whether it scored correct
  - the expected path/subsystem from the CURRENT manifest (post Option A)
  - the L1 descend's per-subsystem z_log values + which it flagged
  - any L2/L3 descend's per-slot z_log values + flagged set

This lets you eyeball, for a "miss" rep:
  (a) did the agent pick a subsystem that descend flagged but wasn't expected?
      -> LLM-side issue (should have abstained / picked sibling).
  (b) did descend flag the expected subsystem but the agent picked a sibling?
      -> Prompt / scoring issue.
  (c) did descend NOT flag the expected subsystem (z_log < threshold)?
      -> Threshold / measurement issue (try lower tau, or it's genuine noise).

Usage:
  python scripts/inspect_sweep_cell.py B2 pf_anon_write p10_thp_aggressor
  python scripts/inspect_sweep_cell.py B2 fork_storm p10_thp_aggressor
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from orchestrator.agentctl.agent_baselines import score_path  # noqa: E402
from orchestrator.agentctl.perturb import expected_paths, expected_subsystem  # noqa: E402

SWEEPS = REPO / "results" / "sweeps"

TS_RE = re.compile(r"_(\d{8}_\d{6})\.json$")


def _filename_ts(p: Path) -> str:
    m = TS_RE.search(p.name)
    return m.group(1) if m else ""


def latest_sweep(baseline: str, victim: str, pert: str) -> Path | None:
    cands = sorted(
        SWEEPS.glob(f"{baseline}_{victim}_{pert}_*.json"),
        key=_filename_ts,
        reverse=True,
    )
    return cands[0] if cands else None


def fmt_z(d: dict[str, float], threshold: float = 2.0) -> str:
    """Format {slot: z} dict, sort by |z| desc, ★ mark slots above threshold."""
    if not d:
        return "(empty)"
    items = sorted(d.items(), key=lambda kv: abs(kv[1]), reverse=True)
    parts = []
    for k, v in items:
        mark = " *" if abs(v) >= threshold else ""
        parts.append(f"{k}={v:+.2f}{mark}")
    return ", ".join(parts)


def main() -> int:
    if len(sys.argv) != 4:
        print(__doc__, file=sys.stderr)
        print("\nusage: inspect_sweep_cell.py <baseline> <victim> <perturbation>",
              file=sys.stderr)
        return 2
    baseline, victim, pert = sys.argv[1:]
    fp = latest_sweep(baseline, victim, pert)
    if fp is None:
        print(f"no sweep file found for {baseline}/{victim}/{pert}",
              file=sys.stderr)
        return 1

    d = json.loads(fp.read_text())
    exp_sub = expected_subsystem(pert, victim=victim)
    exp_paths = expected_paths(pert, victim=victim)

    print(f"file      : {fp.name}  (ts={_filename_ts(fp)})")
    print(f"cell      : {baseline} / {victim} / {pert}")
    print(f"expected  : subsystem={exp_sub}  paths={exp_paths}")
    print(f"top-level : correct={d.get('correct')}/{d.get('n')}  "
          f"term_counts={d.get('term_counts')}")
    print("-" * 92)

    for r in d.get("reps", []):
        diag_path = r.get("diagnosed_path") or []
        # Re-score with current manifest (the JSON's stored `score` may
        # be stale if rescore_sweeps.py hasn't been run on a recent edit).
        s = score_path(diag_path, exp_sub, exp_paths)
        passed = (s == 1.0)
        tag = "[PASS]" if passed else "[FAIL]"
        print(f"\n{tag} rep {r['rep']}  term={r['termination']:9s}  "
              f"depth={r.get('depth_reached')}  wall={r.get('wall_s', 0):.1f}s  "
              f"submitted={diag_path}  score={s:.2f}")

        descents = r.get("descents") or []
        if not descents:
            print("       (no descents)")
            continue
        for i, dd in enumerate(descents):
            parent = dd.get("parent_path") or []
            parent_s = ".".join(parent) if parent else "<root L1>"
            zs = dd.get("z_scores") or {}
            flagged = dd.get("flagged") or dd.get("flagged_slots") or []
            print(f"       descend[{i}] parent={parent_s}  flagged={flagged}")
            print(f"          z: {fmt_z(zs)}")

    print()
    print("legend: '*' = z_log >= 2.0 (would trip flag threshold)")
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
