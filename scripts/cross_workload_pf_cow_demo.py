"""Cross-workload positive control: pf_cow against pf_anon_write reference.

Runs pf_cow with the kernel_paging multiplex probe attached, then z-scores
the per-slot rates against pf_anon_write's idle reference (NOT pf_cow's
own). Establishes that the multiplex probe distinguishes COW (wp_page_copy)
from minor anon faults (do_anonymous_page) when the workload mix differs
from the baseline.

Phase 4+5 originally validated this at z=20,625; this script makes the
result reproducible from the repo state.

Usage (assumes agentd running, pf_anon_write reference current):
    python scripts/cross_workload_pf_cow_demo.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from orchestrator.agentctl.compare import compare_multiplex
from orchestrator.agentctl.ipc_client import AgentdClient
from orchestrator.agentctl.multiplex import (run_multiplex_against,
                                              spec_for_descent)
from orchestrator.agentctl.reference import ReferenceProfile
from orchestrator.agentctl.subsystem_tree import find, load_tree


def main() -> int:
    parent_path: tuple[str, ...] = tuple(sys.argv[1:]) or ("kernel_paging",)
    workload_argv = [str(REPO / "benchmarks" / "pf_cow" / "run.sh")]

    tree = load_tree()
    parent_node = find(tree, list(parent_path))
    if parent_node is None:
        print(f"error: {parent_path} not in subsystem tree", file=sys.stderr)
        return 1
    spec = spec_for_descent(parent_path, parent_node)

    client = AgentdClient()
    print(f"running pf_cow with multiplex probe over {parent_path}'s "
          f"{spec.n_slots} children: "
          f"{[s[0] for s in spec.slots]}")
    run = run_multiplex_against(spec, workload_argv, client)
    if not run.ok:
        print(f"error: multiplex.run failed: {run.error}", file=sys.stderr)
        return 1

    rates = run.per_slot_per_wu()
    print(f"\npf_cow observed rates per work-unit:")
    for label, r in rates.items():
        print(f"  {label:20s}  {r:12.4e}")

    # Load pf_anon_write reference (NOT pf_cow) to demonstrate cross-workload
    # discrimination.
    ref_bench = "pf_anon_write"
    rp = ReferenceProfile.load(ref_bench)
    key = ".".join(parent_path)
    mx_ref = rp.extra.get("multiplex", {}).get(key)
    if mx_ref is None:
        print(f"error: no multiplex reference for {key} in {ref_bench}",
              file=sys.stderr)
        return 1

    print(f"\ncomparing against {ref_bench} idle reference (n_runs="
          f"{mx_ref.get('n_runs', '?')}):")
    print(f"  {'slot':20s}  {'mu':>12s}  {'sigma':>12s}  {'observed':>12s}  {'z':>10s}")
    for label, mu, sigma in zip(mx_ref["labels"], mx_ref["mu"], mx_ref["sigma"]):
        obs = rates.get(label, 0.0)
        if sigma > 0:
            z = (obs - mu) / sigma
        elif mu == 0.0 and obs > 0.0:
            z = float("inf")
        else:
            z = 0.0
        z_str = ("    +inf" if z == float("inf") else
                 "    -inf" if z == float("-inf") else f"{z:+10.2f}")
        print(f"  {label:20s}  {mu:12.4e}  {sigma:12.4e}  {obs:12.4e}  {z_str}")

    cmp = compare_multiplex(mx_ref, list(rates.values()),
                             parent_path=parent_path,
                             agent_hypothesis=None)
    print(f"\nflagged children: {cmp.flagged}")
    print(f"hint: {cmp.hint}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
