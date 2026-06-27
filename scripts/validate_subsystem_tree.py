"""Sanity-check every probe target in subsystems.yaml against the running
kernel's /proc/kallsyms and /sys/kernel/tracing/events trees.

Reports per-probe whether it can be attached as the multiplex template
expects:
  - bare symbol target: must appear in /sys/kernel/tracing/available_filter_functions
                         (this is the kprobe-attachable set, narrower than
                         /proc/kallsyms which includes notrace functions
                         like __schedule).
  - 'tp:cat/name' target: /sys/kernel/tracing/events/<cat>/<name>/ must exist.

Caught issues result in a non-zero exit code so this can be wired into
CI or a pre-flight check before sweeps. Without sudo, kallsyms reads as
zero-addressed and afff is readable; that's enough.

Usage:
  python scripts/validate_subsystem_tree.py
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from orchestrator.agentctl.subsystem_tree import iter_descendants, load_tree

AVAILABLE_FILTERS = Path("/sys/kernel/tracing/available_filter_functions")
TRACING_EVENTS = Path("/sys/kernel/tracing/events")


def _load_filterable_symbols() -> set[str]:
    """The kprobe-attachable set (== ftrace function-tracer set,
    excluding notrace/blocked symbols)."""
    if not AVAILABLE_FILTERS.exists():
        return set()
    out: set[str] = set()
    with open(AVAILABLE_FILTERS) as f:
        for line in f:
            # lines may include " [module]" suffix; strip whitespace.
            sym = line.split(maxsplit=1)[0]
            out.add(sym)
    return out


def _tracepoint_exists(spec: str) -> bool:
    """spec like 'tp:sched/sched_switch' -> check that
    /sys/kernel/tracing/events/sched/sched_switch/ exists."""
    if not spec.startswith("tp:"):
        return False
    rest = spec[3:]
    if "/" not in rest:
        return False
    cat, name = rest.split("/", 1)
    return (TRACING_EVENTS / cat / name).is_dir()


def main() -> int:
    tree = load_tree()
    filters = _load_filterable_symbols()
    if not filters:
        print("warn: /sys/kernel/tracing/available_filter_functions not "
              "readable; kprobe-attachability cannot be verified",
              file=sys.stderr)

    bad: list[tuple[str, str, str]] = []   # (path, probe, reason)
    total = 0
    for path, node in iter_descendants(tree):
        for probe in (node.probes or []):
            total += 1
            if probe.startswith("tp:"):
                if not _tracepoint_exists(probe):
                    bad.append((".".join(path), probe,
                                "tracepoint dir does not exist"))
            else:
                if filters and probe not in filters:
                    bad.append((".".join(path), probe,
                                "not in available_filter_functions "
                                "(notrace / inlined / wrong name)"))

    print(f"checked {total} probe target(s) across {len(tree)} L1 subsystem(s)")
    if not bad:
        print("OK: all probes are attachable on this kernel.")
        return 0
    print(f"\n{len(bad)} probe(s) FAIL:\n")
    for p, probe, reason in bad:
        print(f"  {p:40s}  {probe:35s}  {reason}")
    print(f"\n  Fix subsystems.yaml: replace these probes with attachable "
          f"alternatives, or rebuild the kernel with the symbols un-inlined.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
