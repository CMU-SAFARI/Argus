"""Feature Vector layout. Indices match agentd's workload_runner.cpp::kLabels.

Redesigned for the L1->L2->L3 hierarchical detection rearchitecture
(see docs/hierarchical_subsystem_plan.md):

  - Dropped: llc_load_misses, cache_misses, stalls_l3_miss (dram_contention removed).
  - Added: net_dev_rx, vfs_read, vfs_write, rcu_utilization, lock_contention,
    tlb_flush, amdgpu_irq.

The amdgpu_irq slot (index 16) is capability-gated on the C++ side: on
hosts without an AMD GPU, agentd reports `fv_active.amdgpu_irq == false`
and the count stays at 0. Detector / reference code must skip inactive slots
when computing z-scores.

Phase15c note (2026-05-17): the slot is bound to `amdgpu_cs_ioctl` (re-validated).
The Phase15b switch to `amdgpu_iv` was based on a measurement where (a) the GPU was
in low-power state during a short sample and (b) a bpftrace signed/unsigned
comparison bug masked the PID-attribution check. After pre-warming the GPU and
fixing the type cast: `amdgpu_cs_ioctl` fires at ~470 Hz under inference (gfx1201,
Mesa RADV Vulkan backend) AND is the only amdgpu tracepoint that runs in the
caller's PID context (the others - amdgpu_iv, amdgpu_sched_run_job, amdgpu_job_run
- fire in kworker/IRQ context and cannot be PID-attributed). The slot's scope is
therefore restored to WORKLOAD (was SYSTEM in Phase15b). The ROCm 7.x cs_ioctl
bypass note only applies to KFD compute submissions via userspace doorbells, NOT
to Vulkan or HIP graphics submissions - both still hit cs_ioctl. The slot index
is unchanged (16).
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Order MUST match src/agentd/workload_runner.cpp::kLabels
LABELS: list[str] = [
    "dtlb_load_misses",    # 0  TLB load pressure
    "dtlb_store_misses",   # 1  TLB store-side
    "page_faults",         # 2  fault-handler entry rate
    "kmem_alloc",          # 3  page allocator (kmem:mm_page_alloc)
    "kswapd_wake",         # 4  reclaim canary (vmscan:mm_vmscan_kswapd_wake)
    "sched_switch",        # 5  scheduler context-switch (sched:sched_switch)
    "context_switches",    # 6  preemption sw event
    "cpu_cycles",          # 7  denominator
    "block_rq_complete",   # 8  block:block_rq_complete
    "net_dev_xmit",        # 9  net:net_dev_xmit (tx)
    "net_dev_rx",          # 10 net:netif_receive_skb (rx softirq)
    "vfs_read",            # 11 syscalls:sys_enter_read
    "vfs_write",           # 12 syscalls:sys_enter_write
    "rcu_utilization",     # 13 rcu:rcu_utilization (grace-period activity)
    "lock_contention",     # 14 lock:contention_begin (mutex/rwsem/spinlock)
    "tlb_flush",           # 15 tlb:tlb_flush (TLB shootdown / mm flush)
    "amdgpu_irq",          # 16 amdgpu:amdgpu_cs_ioctl (GPU command-stream
                            #     submissions; gated; restored Phase15c 2026-05-17)
]
N = len(LABELS)

# Capability-gated FV slots: when the host doesn't expose the underlying
# tracepoint (e.g. no AMD GPU loaded), agentd reports the slot as inactive
# and Python-side consumers must skip z-scoring it.
GATED_LABELS: set[str] = {"amdgpu_irq"}


@dataclass
class FeatureVector:
    v: list[int] = field(default_factory=lambda: [0] * N)
    active: list[bool] = field(default_factory=lambda: [True] * N)

    @classmethod
    def from_dict(cls, d: dict, active: dict | None = None) -> "FeatureVector":
        fv = cls(v=[int(d.get(k, 0)) for k in LABELS])
        if active is not None:
            fv.active = [bool(active.get(k, True)) for k in LABELS]
        return fv

    def __getitem__(self, k):
        if isinstance(k, str):
            return self.v[LABELS.index(k)]
        return self.v[k]

    def is_active(self, idx_or_label) -> bool:
        i = idx_or_label if isinstance(idx_or_label, int) else LABELS.index(idx_or_label)
        return self.active[i]

    def as_dict(self) -> dict[str, int]:
        return dict(zip(LABELS, self.v))

    def __repr__(self) -> str:
        return "FV(" + ", ".join(f"{k}={x}" for k, x in zip(LABELS, self.v)) + ")"
