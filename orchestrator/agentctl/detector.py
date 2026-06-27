"""L1 subsystem detector (post hierarchical-detection rearchitecture).

Given a reference distribution (mu, sigma) in per-work-unit rates and an
observed FV under perturbation (also normalized to per-work-unit rates), flag
each metric whose deviation exceeds k*sigma, then cluster flagged metrics
into L1 subsystems. This is the depth-1 stage of the L1->L2->L3 BFS loop;
the agent recurses into each flagged L1 with multiplexed probes (depth >= 2).

L1 catalogue (8 subsystems; amdgpu capability-gated at runtime):
  - kernel_paging       : fault-handler entry, dTLB pressure, TLB shootdown
  - kernel_memory_mgmt  : page allocator, reclaim/kswapd
  - kernel_scheduler    : sched_switch, preemption
  - block_io            : block-layer completions
  - network_stack       : tx + rx softirq paths
  - vfs_filesystems     : read/write syscall paths
  - sync_rcu            : RCU grace-period activity, lock contention
  - amdgpu (gated)      : HIP command submission ioctl

A metric may map to MORE THAN ONE L1 subsystem when its deviation legitimately
implicates several kernel paths. Notable couplings:
  - page_faults  -> kernel_paging + kernel_memory_mgmt
                    (fault handler entry rate AND allocator slow-path entry)
  - tlb_flush    -> kernel_paging
                    (mm-flush + IPI-shootdown both live in arch/x86/mm/tlb.c)
  - lock_contention -> sync_rcu
                    (mutex/rwsem/spinlock contention is the L1 signal; L2 splits)
The detector flags every implicated subsystem; the agent's L2 multiplexed
probe disambiguates which is actually hot.

cpu_cycles (metric 7) is the denominator and is intentionally absent from
METRIC_TO_SUBSYSTEM: large |z| on cpu_cycles alone never flags a subsystem.

Inactive (capability-gated) metrics are skipped entirely: if `active[i]` is
false the metric never contributes to a flag, even if mu/sigma look anomalous.
"""

from __future__ import annotations

from collections.abc import Sequence
from .feature_vector import LABELS, N

SUBSYSTEMS = [
    "kernel_paging",
    "kernel_memory_mgmt",
    "kernel_scheduler",
    "block_io",
    "network_stack",
    "vfs_filesystems",
    "sync_rcu",
    "amdgpu",
]

# Metrics excluded from flagging AND from the z-score report the agent sees.
# Currently empty. Note: rcu_utilization was previously masked because it
# fires as a generic "kernel is busy" proxy under any non-trivial load and
# was producing false sync_rcu attributions on memory/paging-dominated
# cells. It is unmasked again to support fork_storm and other RCU-heavy
# novel victims where sync_rcu is the legitimate bottleneck.
MASKED_METRICS: frozenset[str] = frozenset()

# Per-benchmark metric masks. Some FV slots fire on a victim only as a
# downstream symptom of a different bottleneck. The clearest example is
# block_rq_complete on pure-memory benchmarks (gups, stream, pf_anon_write):
# under p4_drop_caches the binary's text/data pages get evicted, and the
# next run page-faults them in -> kernel_paging.file_fault.major is the
# real cause, but the FV signal is block_rq_complete (the resulting disk
# read). The detector's static mapping (block_rq_complete -> block_io) then
# misattributes to block_io. These benchmarks don't legitimately exercise
# block I/O, so block_rq_complete is masked for them. pf_major and
# pf_page_cache DO use major file faults / page-cache I/O legitimately and
# are not masked.
BENCHMARK_METRIC_MASKS: dict[str, frozenset[str]] = {
    "gups":          frozenset({"block_rq_complete"}),
    "stream":        frozenset({"block_rq_complete"}),
    "pf_anon_write": frozenset({"block_rq_complete"}),
}

# Index -> tuple of L1 subsystem names. Indices match feature_vector.LABELS.
METRIC_TO_SUBSYSTEM: dict[int, tuple[str, ...]] = {
    0:  ("kernel_paging",),                              # dtlb_load_misses
    1:  ("kernel_paging",),                              # dtlb_store_misses
    2:  ("kernel_paging", "kernel_memory_mgmt"),         # page_faults: handler entry + allocator slow-path
    3:  ("kernel_memory_mgmt",),                         # kmem_alloc
    4:  ("kernel_memory_mgmt",),                         # kswapd_wake
    5:  ("kernel_scheduler",),                           # sched_switch
    6:  ("kernel_scheduler",),                           # context_switches
    # 7 (cpu_cycles) intentionally absent -- denominator only.
    8:  ("block_io",),                                   # block_rq_complete
    9:  ("network_stack",),                              # net_dev_xmit (tx)
    10: ("network_stack",),                              # net_dev_rx
    11: ("vfs_filesystems",),                            # vfs_read
    12: ("vfs_filesystems",),                            # vfs_write
    13: ("sync_rcu",),                                   # rcu_utilization
    14: ("sync_rcu",),                                   # lock_contention (mutex/rwsem/spinlock)
    15: ("kernel_paging",),                              # tlb_flush (TLB shootdown / mm flush)
    16: ("amdgpu",),                                     # amdgpu_irq (gated)
}


def _is_masked(label: str, benchmark: str | None) -> bool:
    """True if `label` is either globally masked OR masked for the specific
    benchmark via BENCHMARK_METRIC_MASKS."""
    if label in MASKED_METRICS:
        return True
    if benchmark and label in BENCHMARK_METRIC_MASKS.get(benchmark, frozenset()):
        return True
    return False


def flag_metrics(mu: Sequence[float], sigma: Sequence[float],
                 ofv_rates: Sequence[float], k: float = 3.0,
                 active: Sequence[bool] | None = None,
                 benchmark: str | None = None) -> list[int]:
    """Return indices i where |ofv_rates[i] - mu[i]| > k*sigma[i].

    Only metrics that map to a subsystem and are active on this host are
    eligible for flagging. Denominator metrics (cpu_cycles) and gated-but-
    absent metrics (e.g. amdgpu_irq on a non-AMD host) never flag.

    Zero-variance fallback: if both mu and sigma are 0 on a metric (idle-zero
    counter -- e.g. block_rq_complete on a tmpfs-backed victim), any strictly
    positive observed rate counts as a flag. Without this, the I/O and
    network metrics could never trigger on victims whose baseline doesn't
    already exercise those paths.
    """
    flagged: list[int] = []
    for i in range(N):
        if i not in METRIC_TO_SUBSYSTEM:
            continue
        if _is_masked(LABELS[i], benchmark):
            continue
        if active is not None and not active[i]:
            continue
        s = sigma[i]
        if s <= 0:
            if mu[i] == 0.0 and ofv_rates[i] > 0.0:
                flagged.append(i)
            continue
        # POSITIVE-only flagging for bottleneck localization. A slot firing
        # less than idle (negative z) means the workload is doing LESS of
        # that activity than baseline, not that the activity is a bottleneck.
        # Lab-observed false positives under stream x p4_drop_caches: slab
        # allocator activity is suppressed (z~-44), but abs() flagged it
        # anyway -> agent submitted slab_fast as the L2 bottleneck.
        if (ofv_rates[i] - mu[i]) > k * s:
            flagged.append(i)
    return flagged


def flag(mu: Sequence[float], sigma: Sequence[float],
         ofv_rates: Sequence[float], k: float = 3.0,
         active: Sequence[bool] | None = None,
         benchmark: str | None = None,
         top_k: int = 3) -> list[str]:
    """Return the top-K L1 subsystems ranked by their max child z-score.
    Only subsystems with at least one POSITIVE-z eligible metric qualify
    (negative-z slots are never bottleneck candidates).
    Sorted descending by max-child-z.
    Rationale: the fixed k=3 threshold previously used here produced ~50%
    miss-rate on borderline cells (e.g. gups x p10_thp_aggressor where
    page_faults z hovers near 3, flipping the diagnosis with noise).
    Ranking is robust to noise: the agent always sees the K most-anomalous
    L1s to consider descending into, regardless of magnitude.
    `k` is kept in the signature for backwards-compatibility but is no
    longer used at this layer. Threshold semantics still apply at L2/L3
    multiplex (compare._flag_slots) where siblings under one parent
    benefit from a hard cutoff.
    """
    # Per-subsystem max eligible z-score.
    sub_max_z: dict[str, float] = {}
    for i in range(N):
        if i not in METRIC_TO_SUBSYSTEM:
            continue
        if _is_masked(LABELS[i], benchmark):
            continue
        if active is not None and not active[i]:
            continue
        s = sigma[i]
        if s <= 0:
            # Zero-variance positive deviation = implicit "infinity" z.
            if mu[i] == 0.0 and ofv_rates[i] > 0.0:
                z = float("inf")
            else:
                continue  # mu>0,sigma=0 with no positive observation: no signal
        else:
            z = (ofv_rates[i] - mu[i]) / s
            if z <= 0:
                continue  # positive-only
        for sub in METRIC_TO_SUBSYSTEM[i]:
            if z > sub_max_z.get(sub, -float("inf")):
                sub_max_z[sub] = z
    # Rank descending; tie-break alphabetically for determinism.
    ranked = sorted(sub_max_z.items(), key=lambda kv: (-kv[1], kv[0]))
    return [sub for sub, _ in ranked[:top_k]]


def explain(mu: Sequence[float], sigma: Sequence[float],
            ofv_rates: Sequence[float], k: float = 3.0,
            active: Sequence[bool] | None = None,
            benchmark: str | None = None
            ) -> list[tuple[str, float, str]]:
    """Per-metric (label, z-score, flag-marker) for human-readable output.

    The flag-marker is shown only for metrics that map to a subsystem AND
    are active. Inactive (gated) metrics report z=0 and a `-` marker.
    """
    rows: list[tuple[str, float, str]] = []
    for i in range(N):
        is_active = True if active is None else bool(active[i])
        if not is_active:
            rows.append((LABELS[i], 0.0, "-"))
            continue
        if _is_masked(LABELS[i], benchmark):
            # Hide from the agent entirely: zero the reported z so it can't
            # pattern-match on raw magnitude, and use marker "m" so logs
            # still show the slot existed but was masked.
            rows.append((LABELS[i], 0.0, "m"))
            continue
        s = sigma[i]
        z = (ofv_rates[i] - mu[i]) / s if s > 0 else 0.0
        eligible = i in METRIC_TO_SUBSYSTEM
        # Only mark slots as "flagged" (asterisk) when they are anomalously
        # HIGH vs idle. Suppressed activity (negative z) is reported in the
        # z column but never as a bottleneck flag. See flag_metrics for the
        # full rationale.
        if eligible and s > 0 and z > k:
            marker = "*"
        elif eligible and s <= 0 and mu[i] == 0.0 and ofv_rates[i] > 0.0:
            marker = "*"
        else:
            marker = " "
        rows.append((LABELS[i], z, marker))
    return rows
