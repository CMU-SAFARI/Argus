"""Registry of canned probes (B1 baseline).

Maps a perturbation id to the canned probe most aligned with its expected
subsystem. Used by:
  - B1 baseline: stub agent, no LLM call.
  - Reference-side probe (Day 7+): loaded under both idle and perturbed runs.
  - Few-shot examples in B2's system prompt.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
CANNED_DIR = REPO / "agent_handlers" / "canned"
HANDLER_OBJ_DIR = REPO / "build" / "handler_obj"


@dataclass(frozen=True)
class ProbeSpec:
    """A canned probe's source path, compiled object path, and the dispatcher
    slots it expects to occupy. Slot indices match dispatcher.bpf.c constants.
    """
    name: str            # short id, e.g. "compaction"
    source: Path         # .bpf.c
    obj: Path            # .bpf.o (compiled by `make handler`)
    slots: list[dict]    # [{"prog":..., "kind":"tracepoint"|"kprobe", "idx":N}]
    target_subsystem: str  # the subsystem this probe is engineered to attribute


# TIDX_* / KIDX_* constants from src/include/profiler_types.h:
#   TIDX_RECLAIM_BEGIN=0, RECLAIM_END=1, COMPACTION_BEGIN=2, COMPACTION_END=3,
#   MEMCG_RECLAIM_BEGIN=4, MEMCG_RECLAIM_END=5, TLB_FLUSH=6, THP_COLLAPSE=7
#   KIDX_MM_FAULT_ENTRY=0, MM_FAULT_EXIT=1
PROBES: dict[str, ProbeSpec] = {
    "compaction": ProbeSpec(
        name="compaction",
        source=CANNED_DIR / "compaction.bpf.c",
        obj=HANDLER_OBJ_DIR / "compaction.bpf.o",
        slots=[
            {"prog": "canned_compaction_begin", "kind": "tracepoint", "idx": 2},
            {"prog": "canned_compaction_end",   "kind": "tracepoint", "idx": 3},
        ],
        target_subsystem="kernel_memory_mgmt",
    ),
    "reclaim": ProbeSpec(
        name="reclaim",
        source=CANNED_DIR / "reclaim.bpf.c",
        obj=HANDLER_OBJ_DIR / "reclaim.bpf.o",
        slots=[
            {"prog": "canned_direct_reclaim_begin", "kind": "tracepoint", "idx": 0},
            {"prog": "canned_direct_reclaim_end",   "kind": "tracepoint", "idx": 1},
            {"prog": "canned_memcg_reclaim_begin",  "kind": "tracepoint", "idx": 4},
            {"prog": "canned_memcg_reclaim_end",    "kind": "tracepoint", "idx": 5},
        ],
        target_subsystem="kernel_memory_mgmt",
    ),
    "tlb_shootdown": ProbeSpec(
        name="tlb_shootdown",
        source=CANNED_DIR / "tlb_shootdown.bpf.c",
        obj=HANDLER_OBJ_DIR / "tlb_shootdown.bpf.o",
        slots=[
            {"prog": "canned_tlb_flush",    "kind": "tracepoint", "idx": 6},
            {"prog": "canned_thp_collapse", "kind": "tracepoint", "idx": 7},
        ],
        target_subsystem="kernel_paging",
    ),
    # Day 8-9 / CS3: page-fault latency on the kernel slow path.
    # counters[0]=count, [1]=sum_ns, [2]=max_ns, [3..6] = 4-bucket histogram
    # (<1us / 1-10us / 10-100us / >=100us). Reproduces Valinor's PF-latency
    # ratio under THP-aggressor co-runner.
    "pf_latency": ProbeSpec(
        name="pf_latency",
        source=CANNED_DIR / "pf_latency.bpf.c",
        obj=HANDLER_OBJ_DIR / "pf_latency.bpf.o",
        slots=[
            {"prog": "canned_pf_lat_entry", "kind": "kprobe", "idx": 0},
            {"prog": "canned_pf_lat_exit",  "kind": "kprobe", "idx": 1},
        ],
        target_subsystem="kernel_paging",
    ),
}


# Default canned probe per perturbation id (B1 baseline lookup table).
DEFAULT_PROBE: dict[str, str] = {
    "p1_thp_off":      "tlb_shootdown",
    "p4_drop_caches":  "reclaim",
    "p10_thp_aggressor": "pf_latency",   # CS3: directly attributes the PF-latency tail
    "p11_force_compaction": "compaction",
}


def for_perturbation(pid: str) -> ProbeSpec | None:
    """Return the canned probe most aligned with `pid`'s expected subsystem."""
    name = DEFAULT_PROBE.get(pid)
    return PROBES.get(name) if name else None
