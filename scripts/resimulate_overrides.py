"""resimulate_overrides.py - replay submit_diagnosis post-submission overrides
against existing sweep JSONs and rescore.

Saves a full LLM rerun: the descent JSONs already record `parent_path` and
`flagged` per descent, so we can deterministically replay what
submit_diagnosis would do *today* (with the depth-mismatch-abstain rule
added 2026-05-16) against the path the LLM originally submitted.

For each sweep file matched, rewrites in place:
  - reps[i].diagnosed_path -> overridden path (if override fired)
  - reps[i].diagnosed       -> overridden path (kept in lockstep)
  - reps[i].score           -> rescored against current manifest
  - reps[i].correct         -> int(score == 1.0)
  - reps[i].override_applied -> short tag (one of: 'abstain', 'depth-mismatch',
                                'flagged-leaf', or absent if no change)
Top-level `correct`, `first_try_ok`, `retry_budget_ok` are recomputed.

NOTE: this duplicates the override logic in
orchestrator/agentctl/agent_tools.py:submit_diagnosis (lines 420-480).
Keep in sync. The reason it's not imported is that submit_diagnosis is
defined inside a closure with SessionState; lifting it cleanly is more
churn than the duplication.

Usage:
  python scripts/resimulate_overrides.py B2 pf_anon_write p10_thp_aggressor
  python scripts/resimulate_overrides.py B2 '*' p10_thp_aggressor   # all victims
  python scripts/resimulate_overrides.py B2 '*' '*'                  # all B2 cells
  python scripts/resimulate_overrides.py --dry-run B2 '*' '*'        # print only
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from orchestrator.agentctl.agent_baselines import score_path  # noqa: E402
from orchestrator.agentctl.perturb import expected_paths, expected_subsystem  # noqa: E402

SWEEPS = REPO / "results" / "sweeps"


def recover_pre_override_path(current_path: list[str],
                              override_tag: str | None,
                              descents: list[dict]) -> list[str]:
    """When a JSON has already been overridden by a previous resimulate
    pass, the original LLM submission was lost from `diagnosed_path`.
    For depth-mismatch overrides we can reconstruct it from descents:

    The agent submitted at depth-1. That L1 was the parent of a
    sub-descent AND was flagged by the root descent. There's exactly
    one such L1 for every depth-mismatch case in our data
    (verified 2026-05-16 across all 5 affected reps).
    """
    if override_tag != "depth-mismatch":
        return list(current_path)
    root = next((d for d in descents
                 if not (d.get("parent_path") or [])), None)
    root_flagged = list((root or {}).get("flagged") or [])
    sub_parents = [tuple(d.get("parent_path") or [])
                   for d in descents
                   if d.get("parent_path")]
    candidates = [l1 for l1 in root_flagged
                  if (l1,) in [tuple(p) for p in sub_parents]]
    if len(candidates) == 1:
        return [candidates[0]]
    # Fallback: pick the only L1 that has a sub-descent (covers cases
    # like rep 3 of pf_anon_write where root_flagged was set but the
    # heuristic above might miss).
    only_subs = sorted({(p[0],) for p in sub_parents if p})
    if len(only_subs) == 1:
        return [only_subs[0][0]]
    return list(current_path)


def apply_overrides(path: list[str], descents: list[dict]) -> tuple[list[str], str | None]:
    """Mirror of agent_tools.submit_diagnosis post-submission overrides.

    Returns (new_path, override_tag). override_tag is one of:
      'abstain'         - all descents flagged=[] AND depth <= 1
      'depth-mismatch'  - L1 commit but sub-descents into that L1 all
                          flagged=[] AND L1 not in root_flagged
      'flagged-leaf'    - L2+ commit but leaf not in parent descent's flagged
      None              - no override fired
    """
    p = list(path)

    # Rule 1: abstain enforcement (all descents flagged=[])
    all_empty = (
        len(descents) > 0
        and all(not d.get("flagged") for d in descents)
    )
    if all_empty and len(p) <= 1 and p != ["none"]:
        return ["none"], "abstain"

    # Rule 2: depth-mismatch abstain (added 2026-05-16, refined to keep L1
    # commits when L1 itself was flagged by the root descent).
    if p != ["none"] and len(p) == 1:
        sub = [d for d in descents
               if list(d.get("parent_path") or []) == [p[0]]]
        if sub and all(not d.get("flagged") for d in sub):
            root = next((d for d in descents
                         if not (d.get("parent_path") or [])), None)
            root_flagged = list((root or {}).get("flagged") or [])
            if p[0] not in root_flagged:
                return ["none"], "depth-mismatch"
            # else: keep L1 commit (no override).

    # Rule 3: flagged-leaf enforcement
    if p != ["none"] and len(p) >= 2 and descents:
        parent_tup = list(p[:-1])
        leaf = p[-1]
        for d in reversed(descents):
            if list(d.get("parent_path") or []) == parent_tup:
                flagged = list(d.get("flagged", []) or [])
                if flagged and leaf not in flagged:
                    z_log = d.get("z_log_scores") or {}
                    best = max(flagged,
                               key=lambda s: float(z_log.get(s, 0.0)))
                    return parent_tup + [best], "flagged-leaf"
                break

    return p, None


def process_file(fp: Path, dry_run: bool) -> dict:
    """Returns summary stats for the file."""
    d = json.loads(fp.read_text())
    victim = d.get("benchmark") or fp.stem.split("_")[1]
    pert = d.get("perturbation") or "_".join(fp.stem.split("_")[2:-2])
    exp_sub = expected_subsystem(pert, victim=victim)
    exp_paths = expected_paths(pert, victim=victim)

    stats = {
        "file": fp.name,
        "n": 0,
        "overrides": {"abstain": 0, "depth-mismatch": 0, "flagged-leaf": 0},
        "flips_to_pass": 0,
        "flips_to_fail": 0,
        "old_correct": 0,
        "new_correct": 0,
    }
    for r in d.get("reps", []):
        stats["n"] += 1
        # If a previous resimulator pass overrode the path (e.g. forced
        # ['none']), recover the original LLM submission from the descent
        # data before re-applying the (possibly updated) override logic.
        prior_tag = r.get("override_applied")
        recovered_path = recover_pre_override_path(
            list(r.get("diagnosed_path") or []),
            prior_tag,
            r.get("descents") or [],
        )
        old_path = recovered_path
        descents = r.get("descents") or []
        new_path, tag = apply_overrides(old_path, descents)
        old_score = score_path(old_path, exp_sub, exp_paths)
        new_score = score_path(new_path, exp_sub, exp_paths)
        stats["old_correct"] += int(old_score == 1.0)
        stats["new_correct"] += int(new_score == 1.0)
        if tag:
            stats["overrides"][tag] += 1
        if old_score != 1.0 and new_score == 1.0:
            stats["flips_to_pass"] += 1
        if old_score == 1.0 and new_score != 1.0:
            stats["flips_to_fail"] += 1
        if not dry_run:
            r["diagnosed_path"] = new_path
            r["diagnosed"] = new_path
            r["score"] = new_score
            r["correct"] = int(new_score == 1.0)
            if tag:
                r["override_applied"] = tag
            else:
                r.pop("override_applied", None)

    if not dry_run:
        d["correct"] = stats["new_correct"]
        # first_try_ok / retry_budget_ok metrics are downstream of correct;
        # recompute simply (all reps are single submissions in current sweeps).
        d["first_try_ok"] = stats["new_correct"]
        d["retry_budget_ok"] = stats["new_correct"]
        fp.write_text(json.dumps(d, indent=2))

    return stats


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("baseline", help="e.g., B2  (or '*' for all)")
    ap.add_argument("victim", help="e.g., pf_anon_write  (or '*')")
    ap.add_argument("perturbation", help="e.g., p10_thp_aggressor  (or '*')")
    ap.add_argument("--dry-run", action="store_true",
                    help="don't rewrite JSONs, just print per-file summary")
    ap.add_argument("--all-versions", action="store_true",
                    help="process every matching timestamp (default: latest only)")
    args = ap.parse_args()

    pat = f"{args.baseline}_{args.victim}_{args.perturbation}_*.json"
    files = sorted(SWEEPS.glob(pat))
    if not files:
        print(f"no files matched: {pat}", file=sys.stderr)
        return 1

    if not args.all_versions:
        # Keep only the latest timestamp per (baseline, victim, perturbation)
        # triplet, where "latest" comes from the YYYYMMDD_HHMMSS in the
        # filename (NOT mtime -- rescore touches mtime indiscriminately).
        keep: dict[tuple, Path] = {}
        for fp in files:
            parts = fp.stem.split("_")
            ts = "_".join(parts[-2:])
            key = tuple(parts[:-2])
            if key not in keep or ts > "_".join(keep[key].stem.split("_")[-2:]):
                keep[key] = fp
        files = sorted(keep.values())

    print(f"processing {len(files)} file(s){' (dry-run)' if args.dry_run else ''}\n")
    print(f"{'file':<78} {'old':>5} {'new':>5} {'abs':>4} {'dpt':>4} {'flg':>4} "
          f"{'>>':>3} {'<<':>3}")
    print("-" * 113)
    total = {"n": 0, "old_correct": 0, "new_correct": 0,
             "abstain": 0, "depth-mismatch": 0, "flagged-leaf": 0,
             "flips_to_pass": 0, "flips_to_fail": 0}
    for fp in files:
        s = process_file(fp, dry_run=args.dry_run)
        print(f"{s['file']:<78} "
              f"{s['old_correct']}/{s['n']:<3} {s['new_correct']}/{s['n']:<3} "
              f"{s['overrides']['abstain']:>4} "
              f"{s['overrides']['depth-mismatch']:>4} "
              f"{s['overrides']['flagged-leaf']:>4} "
              f"{s['flips_to_pass']:>3} {s['flips_to_fail']:>3}")
        for k in total:
            if k in s:
                total[k] += s[k]
            elif k in s.get("overrides", {}):
                total[k] += s["overrides"][k]
    print("-" * 113)
    print(f"{'TOTAL':<78} "
          f"{total['old_correct']}/{total['n']:<3} {total['new_correct']}/{total['n']:<3} "
          f"{total['abstain']:>4} {total['depth-mismatch']:>4} "
          f"{total['flagged-leaf']:>4} "
          f"{total['flips_to_pass']:>3} {total['flips_to_fail']:>3}")
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
