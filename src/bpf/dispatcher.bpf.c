/* SPDX-License-Identifier: GPL-2.0 OR BSD-3-Clause */
/*
 * dispatcher.bpf.c
 *
 * Central Dispatcher for Dynamic Multi-Subsystem Hooking
 *
 * Architecture:
 *   This BPF object is the ONLY one that is auto-attached. It contains
 *   thin "stub" programs for every hook point in the system. Each stub
 *   does nothing except a bpf_tail_call() into a PROG_ARRAY map.
 *
 *   The actual sensor logic lives in separate BPF objects (loaded but
 *   NOT attached). User-space inserts their program FDs into the
 *   PROG_ARRAY maps. When a stub fires, it tail-calls into the sensor.
 * 
 *   If the PROG_ARRAY slot is empty, the tail-call silently fails and
 *   the stub returns 0 — effectively disabling the sensor with zero
 *   overhead.
 *
 *   To toggle a sensor at runtime, user-space simply adds or removes
 *   the handler FD from the PROG_ARRAY. No BPF reload, no reattach.
 *
 * Two PROG_ARRAY maps are needed because BPF requires all programs in
 * a PROG_ARRAY to be the same BPF program type:
 *
 *   kprobe_dispatch  — BPF_PROG_TYPE_KPROBE  (kprobe & kretprobe handlers)
 *   tp_dispatch      — BPF_PROG_TYPE_TRACEPOINT (tracepoint handlers)
 *
 * The OOM sensor uses tp_btf (BPF_PROG_TYPE_TRACING) for CO-RE access
 * to the victim's task_struct, so it is attached directly and toggled
 * via sensor_mask instead of a PROG_ARRAY slot.
 */

#include "vmlinux.h"
#include <bpf/bpf_helpers.h>
#include <bpf/bpf_tracing.h>

#include "profiler_types.h"

/* =========================================================================
 * PROG_ARRAY maps: the central registries of active sensor programs
 * ========================================================================= */

/* For kprobe / kretprobe handler programs */
struct {
    __uint(type, BPF_MAP_TYPE_PROG_ARRAY);
    __uint(max_entries, PROG_ARRAY_MAX_ENTRIES);
    __uint(key_size, sizeof(__u32));
    __uint(value_size, sizeof(__u32));
} kprobe_dispatch SEC(".maps");

/* For tracepoint handler programs */
struct {
    __uint(type, BPF_MAP_TYPE_PROG_ARRAY);
    __uint(max_entries, PROG_ARRAY_MAX_ENTRIES);
    __uint(key_size, sizeof(__u32));
    __uint(value_size, sizeof(__u32));
} tp_dispatch SEC(".maps");

/* =========================================================================
 * Kprobe stubs — Page Fault sensor (#0)
 * ========================================================================= */
SEC("kprobe/handle_mm_fault")
int dispatch_mm_fault_entry(struct pt_regs *ctx)
{
    bpf_tail_call(ctx, &kprobe_dispatch, KIDX_MM_FAULT_ENTRY);
    return 0;
}

SEC("kretprobe/handle_mm_fault")
int dispatch_mm_fault_exit(struct pt_regs *ctx)
{
    bpf_tail_call(ctx, &kprobe_dispatch, KIDX_MM_FAULT_EXIT);
    return 0;
}

/* =========================================================================
 * Kprobe stubs — Slab Allocator sensor (#1)
 * ========================================================================= */
SEC("kprobe/kmem_cache_alloc_noprof")
int dispatch_slab_entry(struct pt_regs *ctx)
{
    bpf_tail_call(ctx, &kprobe_dispatch, KIDX_SLAB_ENTRY);
    return 0;
}

SEC("kretprobe/kmem_cache_alloc_noprof")
int dispatch_slab_exit(struct pt_regs *ctx)
{
    bpf_tail_call(ctx, &kprobe_dispatch, KIDX_SLAB_EXIT);
    return 0;
}

/* =========================================================================
 * Kprobe stub — Page Cache sensor (#4)
 * ========================================================================= */
SEC("kprobe/folio_mark_accessed")
int dispatch_page_cache(struct pt_regs *ctx)
{
    bpf_tail_call(ctx, &kprobe_dispatch, KIDX_PAGE_CACHE);
    return 0;
}

/* =========================================================================
 * Tracepoint stubs — Direct Reclaim sensor (#2)
 * ========================================================================= */
SEC("tp/vmscan/mm_vmscan_direct_reclaim_begin")
int dispatch_reclaim_begin(void *ctx)
{
    bpf_tail_call(ctx, &tp_dispatch, TIDX_RECLAIM_BEGIN);
    return 0;
}

SEC("tp/vmscan/mm_vmscan_direct_reclaim_end")
int dispatch_reclaim_end(void *ctx)
{
    bpf_tail_call(ctx, &tp_dispatch, TIDX_RECLAIM_END);
    return 0;
}

/* =========================================================================
 * Tracepoint stubs — Memcg Reclaim (cgroup-triggered reclaim, same sensor)
 * ========================================================================= */
SEC("tp/vmscan/mm_vmscan_memcg_reclaim_begin")
int dispatch_memcg_reclaim_begin(void *ctx)
{
    bpf_tail_call(ctx, &tp_dispatch, TIDX_MEMCG_RECLAIM_BEGIN);
    return 0;
}

SEC("tp/vmscan/mm_vmscan_memcg_reclaim_end")
int dispatch_memcg_reclaim_end(void *ctx)
{
    bpf_tail_call(ctx, &tp_dispatch, TIDX_MEMCG_RECLAIM_END);
    return 0;
}

/* =========================================================================
 * Tracepoint stubs — Compaction sensor (#3)
 * ========================================================================= */
SEC("tp/compaction/mm_compaction_begin")
int dispatch_compaction_begin(void *ctx)
{
    bpf_tail_call(ctx, &tp_dispatch, TIDX_COMPACTION_BEGIN);
    return 0;
}

SEC("tp/compaction/mm_compaction_end")
int dispatch_compaction_end(void *ctx)
{
    bpf_tail_call(ctx, &tp_dispatch, TIDX_COMPACTION_END);
    return 0;
}

/* =========================================================================
 * Tracepoint stubs — TLB Shootdown sensor (#6)
 * ========================================================================= */
SEC("tp/tlb/tlb_flush")
int dispatch_tlb_flush(void *ctx)
{
    bpf_tail_call(ctx, &tp_dispatch, TIDX_TLB_FLUSH);
    return 0;
}

SEC("tp/huge_memory/mm_collapse_huge_page")
int dispatch_thp_collapse(void *ctx)
{
    bpf_tail_call(ctx, &tp_dispatch, TIDX_THP_COLLAPSE);
    return 0;
}

/* Note: Alloc Contention sensor (#7) is directly attached (not dispatched).
 * The controller resolves the kprobe target at attach time:
 *   __alloc_frozen_pages_noprof  (kernel ≥ 6.14, "frozen pages" allocator)
 *   __alloc_pages_noprof         (kernel < 6.14, legacy fallback)
 * See alloc_contention_sensor.bpf.c for details. */

char _license[] SEC("license") = "GPL";
