"""CS#1: 5x5 cross-workload PF-type heatmap.

For each (workload, reference) pair where both come from the PF-types
benchmark family (pf_anon_write, pf_cow, pf_major, pf_page_cache,
pf_zero_page), run the kernel_paging multiplex on the *workload*, then
z-score the per-slot rates against the *reference*'s idle profile. Emit a
table of per-slot z-scores per (workload, reference) pair.

Diagonal cells (workload == reference) should give z near 0 across all
slots. Off-diagonal cells should reveal which L2 child distinguishes the
workload from the reference baseline (e.g. cow vs minor for pf_cow vs
pf_anon_write).

This is the generalisation of scripts/cross_workload_pf_cow_demo.py to a
matrix.

Prereqs: each victim's L1 reference exists with 17-slot FV, and each
victim has a kernel_paging L2 multiplex reference under
extra.multiplex.kernel_paging.

  python -m orchestrator.agentctl.cli reference pf_cow --n-runs 30
  python -m orchestrator.agentctl.cli prebuild-references pf_cow \
      --parents kernel_paging --n-runs 30
  ... (repeat for pf_major, pf_page_cache, pf_zero_page)

Then:
  python scripts/cs1_pf_types_matrix.py
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from orchestrator.agentctl.ipc_client import AgentdClient
from orchestrator.agentctl.multiplex import (run_multiplex_against,
                                              spec_for_descent)
from orchestrator.agentctl.reference import ReferenceProfile
from orchestrator.agentctl.subsystem_tree import find, load_tree

PF_VICTIMS = ["pf_anon_write", "pf_cow", "pf_major", "pf_page_cache",
              "pf_zero_page"]
DEFAULT_PARENT = "kernel_paging"


def workload_argv(victim: str) -> list[str]:
    """Build the workload argv from benchmarks/manifest.yaml. Hardcoding
    run.sh broke for victims whose binary is named differently (gups,
    stream, llama_cpp); fall back to that name only when the manifest
    has no entry."""
    try:
        from orchestrator.agentctl.workload import Workload
        return Workload(victim)._argv()
    except Exception:
        return [str(REPO / "benchmarks" / victim / "run.sh")]


def slot_z(obs: float, mu: float, sigma: float) -> float:
    """Legacy linear z (count-rate space). Kept for back-compat callers
    of this module; the matrix CSV switched to z_log (below) on 2026-05-15."""
    if sigma > 0:
        return (obs - mu) / sigma
    if mu == 0.0 and obs > 0.0:
        return float("inf")
    return 0.0


def slot_z_log(obs_sum_ns_per_wu: float, mu_log: float, sigma_log: float,
               epsilon: float = 1.0, sigma_floor: float = 0.01) -> float:
    """T_x log-space z-score. Mirrors compare.py:_flag_slots_log so the
    heatmap reflects the same metric Argus actually flags on.

        z_log = (log2(obs + eps) - mu_log) / max(sigma_log, sigma_floor)

    The sigma floor clamps pathologically tight idle variance (e.g. a
    slot whose 30 idle runs all measured ~0 with sigma 1e-5) -- without
    it, microscopic jitter inflates into 100+-sigma signals."""
    import math
    obs = max(obs_sum_ns_per_wu, 0.0)
    obs_log = math.log2(obs + epsilon)
    sigma_eff = max(sigma_log, sigma_floor)
    if sigma_eff <= 0:
        if mu_log == 0.0 and obs_log > 0.0:
            return float("inf")
        return 0.0
    return (obs_log - mu_log) / sigma_eff


def fmt_z(z: float) -> str:
    if z == float("inf"):
        return "  +inf"
    if z == float("-inf"):
        return "  -inf"
    if abs(z) < 1e-3:
        return f"{0.0:+8.2f}"
    return f"{z:+8.2f}"


def main() -> int:
    ap = argparse.ArgumentParser(
        description="CS#1 cross-workload matrix: per-slot z-scores across "
                    "(victim x reference) pairs for a chosen multiplex parent."
    )
    ap.add_argument("--parent", default=DEFAULT_PARENT,
                    help="dotted parent path in the subsystem tree, "
                         "e.g. 'kernel_paging' or 'kernel_paging.anon_fault'.")
    ap.add_argument("--victims", default=",".join(PF_VICTIMS),
                    help="comma-separated victims to use as rows. Default: "
                         "the 5 PF-types. For the motivation matrix use "
                         "'gups,stream,pf_anon_write,llama_cpp,fork_storm'.")
    ap.add_argument("--references", default=None,
                    help="comma-separated victims to use as columns. "
                         "Defaults to --victims (square matrix).")
    ap.add_argument("--out", default=None,
                    help="output CSV path; defaults to "
                         "paper/figures/cs1_matrix_<parent>.csv (slashes "
                         "replaced by underscores).")
    args = ap.parse_args()

    parent_path: tuple[str, ...] = tuple(args.parent.split("."))
    victims = [v.strip() for v in args.victims.split(",") if v.strip()]
    references = [v.strip() for v in (args.references or args.victims).split(",")
                  if v.strip()]
    tree = load_tree()
    parent_node = find(tree, list(parent_path))
    if parent_node is None:
        print(f"error: {parent_path} not in tree", file=sys.stderr)
        return 1
    spec = spec_for_descent(parent_path, parent_node)
    slot_labels = [s[0] for s in spec.slots]
    parent_key = ".".join(parent_path)
    print(f"matrix parent: {parent_key}")
    print(f"slots:         {slot_labels}")
    print(f"victims:       {victims}")
    print(f"references:    {references}\n")

    # 1) Run each victim once with the multiplex; collect per-slot rates.
    client = AgentdClient()
    rates_per_victim: dict[str, dict[str, float]] = {}
    for victim in victims:
        bench_dir = REPO / "benchmarks" / victim
        if not bench_dir.exists():
            print(f"  SKIP  {victim}: no benchmarks/{victim}/", file=sys.stderr)
            continue
        print(f"  RUN   {victim} (multiplex over {parent_key}) ...", flush=True)
        run = run_multiplex_against(spec, workload_argv(victim), client)
        if not run.ok:
            print(f"        failed: {run.error}", file=sys.stderr)
            continue
        if run.attach_errors:
            for ae in run.attach_errors:
                print(f"        warn (attach): {ae}", file=sys.stderr)
        # Capture BOTH count-rate (legacy) and sum_ns/wu (T_x).
        # The CSV now stores z_log computed from sum_ns/wu, but we
        # keep the rate around in case downstream callers want it.
        rates_per_victim[victim] = (run.per_slot_per_wu(),
                                     run.per_slot_sum_ns_per_wu())

    if not rates_per_victim:
        print("error: no victim runs succeeded", file=sys.stderr)
        return 1

    # 2) Cross-z each (workload, reference) pair using T_x log-space z.
    #    Cell value = (log2(perturbed_sum_ns/wu + 1) - mu_log) / sigma_log
    #    (with a sigma_log floor of 0.01 -- see slot_z_log()).
    #    This is the same metric Argus uses to flag slots, so the heatmap
    #    visualises what Argus actually decides on.
    print(f"\n{'workload':<16} {'reference':<16}  " +
          "  ".join(f"{lbl:>10}" for lbl in slot_labels) + "  (z_log)")
    for workload in victims:
        if workload not in rates_per_victim:
            continue
        _, sum_ns_per_wu = rates_per_victim[workload]
        for ref_v in references:
            try:
                rp = ReferenceProfile.load(ref_v)
            except FileNotFoundError:
                continue
            mx = rp.extra.get("multiplex", {}).get(parent_key)
            if not mx:
                continue
            mu_log = mx.get("mu_log") or [0.0] * len(mx["labels"])
            sigma_log = mx.get("sigma_log") or [0.0] * len(mx["labels"])
            zs: list[float] = []
            for label, ml, sl in zip(mx["labels"], mu_log, sigma_log):
                zs.append(slot_z_log(sum_ns_per_wu.get(label, 0.0), ml, sl))
            print(f"{workload:<16} {ref_v:<16}  " +
                  "  ".join(fmt_z(z) for z in zs))
        print()

    # 3) Write a CSV alongside the figures dir for downstream plotting.
    if args.out:
        out = Path(args.out)
    else:
        out = (REPO / "paper" / "figures"
               / f"cs1_matrix_{parent_key.replace('.', '_')}.csv")
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w") as f:
        f.write("workload,reference," + ",".join(slot_labels) + "\n")
        for workload in victims:
            if workload not in rates_per_victim:
                continue
            _, sum_ns_per_wu = rates_per_victim[workload]
            for ref_v in references:
                try:
                    rp = ReferenceProfile.load(ref_v)
                except FileNotFoundError:
                    continue
                mx = rp.extra.get("multiplex", {}).get(parent_key)
                if not mx:
                    continue
                mu_log = mx.get("mu_log") or [0.0] * len(mx["labels"])
                sigma_log = mx.get("sigma_log") or [0.0] * len(mx["labels"])
                zs = []
                for label, ml, sl in zip(mx["labels"], mu_log, sigma_log):
                    zs.append(slot_z_log(sum_ns_per_wu.get(label, 0.0), ml, sl))
                f.write(f"{workload},{ref_v}," +
                        ",".join(("inf" if z == float("inf") else
                                  "-inf" if z == float("-inf") else f"{z:.4f}")
                                 for z in zs) + "\n")
    print(f"saved CSV: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
