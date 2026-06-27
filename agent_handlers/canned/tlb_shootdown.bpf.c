/* SPDX-License-Identifier: GPL-2.0 OR BSD-3-Clause */
/* Canned probe: TLB shootdowns + THP collapse.
 *
 * Subsystem: kernel_paging.
 * Used as the B1 baseline for P1 (THP=never) and P10 (THP-aggressor),
 * and as the primary kernel-paging attribution probe in CS3.
 *
 * agent_output layout (16 u64 per CPU):
 *   [0] tlb_flush events
 *   [1] thp_collapse events
 *   [2..15] reserved
 *
 * Plugs into TIDX_TLB_FLUSH + TIDX_THP_COLLAPSE.
 */

#include "vmlinux.h"
#include <bpf/bpf_helpers.h>
#include <bpf/bpf_tracing.h>
#include "profiler_types.h"

char _license[] SEC("license") = "GPL";

struct {
    __uint(type, BPF_MAP_TYPE_ARRAY);
    __uint(max_entries, 256);
    __type(key, __u32);
    __type(value, struct agent_output_block);
} agent_output SEC(".maps");

SEC("tp/tlb/tlb_flush")
int canned_tlb_flush(void *ctx)
{
    __u32 cpu = bpf_get_smp_processor_id();
    struct agent_output_block *blk = bpf_map_lookup_elem(&agent_output, &cpu);
    if (blk)
        __sync_fetch_and_add(&blk->counters[0], 1);
    return 0;
}

SEC("tp/huge_memory/mm_collapse_huge_page")
int canned_thp_collapse(void *ctx)
{
    __u32 cpu = bpf_get_smp_processor_id();
    struct agent_output_block *blk = bpf_map_lookup_elem(&agent_output, &cpu);
    if (blk)
        __sync_fetch_and_add(&blk->counters[1], 1);
    return 0;
}
