/* SPDX-License-Identifier: GPL-2.0 OR BSD-3-Clause */
/* Canned probe: compaction. Tracks kcompactd / direct-compaction activity
 * via mm_compaction_begin / mm_compaction_end tracepoints.
 *
 * Subsystem: kernel_memory_mgmt (with secondary signal for dram_contention).
 * Used as the B1 baseline for P11 (force_compaction) and as a kcompactd
 * attribution probe in CS_compact.
 *
 * agent_output layout (16 u64 per CPU):
 *   [0] compaction_begin events
 *   [1] compaction_end events
 *   [2] cumulative compaction-region wall-time (ns)
 *   [3..15] reserved (zero)
 *
 * Plugs into the dispatcher's tp_dispatch slots via TIDX_COMPACTION_BEGIN/END.
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

struct {
    __uint(type, BPF_MAP_TYPE_HASH);
    __uint(max_entries, 4096);
    __type(key, __u64);   /* pid_tgid */
    __type(value, __u64); /* entry timestamp ns */
} compaction_starts SEC(".maps");

SEC("tp/compaction/mm_compaction_begin")
int canned_compaction_begin(void *ctx)
{
    __u64 tid = bpf_get_current_pid_tgid();
    __u64 ts = bpf_ktime_get_ns();
    bpf_map_update_elem(&compaction_starts, &tid, &ts, BPF_ANY);

    __u32 cpu = bpf_get_smp_processor_id();
    struct agent_output_block *blk = bpf_map_lookup_elem(&agent_output, &cpu);
    if (blk)
        __sync_fetch_and_add(&blk->counters[0], 1);
    return 0;
}

SEC("tp/compaction/mm_compaction_end")
int canned_compaction_end(void *ctx)
{
    __u64 tid = bpf_get_current_pid_tgid();
    __u64 *tsp = bpf_map_lookup_elem(&compaction_starts, &tid);
    if (!tsp)
        return 0;
    __u64 dt = bpf_ktime_get_ns() - *tsp;
    bpf_map_delete_elem(&compaction_starts, &tid);

    __u32 cpu = bpf_get_smp_processor_id();
    struct agent_output_block *blk = bpf_map_lookup_elem(&agent_output, &cpu);
    if (blk) {
        __sync_fetch_and_add(&blk->counters[1], 1);
        __sync_fetch_and_add(&blk->counters[2], dt);
    }
    return 0;
}
