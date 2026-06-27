/* SPDX-License-Identifier: GPL-2.0 OR BSD-3-Clause */
/* Canned probe: reclaim. Tracks direct-reclaim and memcg-reclaim activity
 * via vmscan tracepoints.
 *
 * Subsystem: kernel_memory_mgmt.
 * Used as the B1 baseline for P4 (drop_caches) and other reclaim-pressure
 * perturbations.
 *
 * agent_output layout (16 u64 per CPU):
 *   [0] direct_reclaim_begin events
 *   [1] direct_reclaim_end   events
 *   [2] cumulative direct-reclaim wall-time (ns)
 *   [3] memcg_reclaim_begin events
 *   [4] memcg_reclaim_end   events
 *   [5] cumulative memcg-reclaim wall-time (ns)
 *   [6..15] reserved
 *
 * Plugs into TIDX_RECLAIM_BEGIN/END + TIDX_MEMCG_RECLAIM_BEGIN/END.
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
    __type(key, __u64);
    __type(value, __u64);
} direct_starts SEC(".maps");

struct {
    __uint(type, BPF_MAP_TYPE_HASH);
    __uint(max_entries, 4096);
    __type(key, __u64);
    __type(value, __u64);
} memcg_starts SEC(".maps");

static __always_inline void bump(int idx_delta, __u64 dt_ns)
{
    __u32 cpu = bpf_get_smp_processor_id();
    struct agent_output_block *blk = bpf_map_lookup_elem(&agent_output, &cpu);
    if (!blk) return;
    __sync_fetch_and_add(&blk->counters[idx_delta], 1);
    __sync_fetch_and_add(&blk->counters[idx_delta + 1], dt_ns);
}

SEC("tp/vmscan/mm_vmscan_direct_reclaim_begin")
int canned_direct_reclaim_begin(void *ctx)
{
    __u64 tid = bpf_get_current_pid_tgid();
    __u64 ts = bpf_ktime_get_ns();
    bpf_map_update_elem(&direct_starts, &tid, &ts, BPF_ANY);

    __u32 cpu = bpf_get_smp_processor_id();
    struct agent_output_block *blk = bpf_map_lookup_elem(&agent_output, &cpu);
    if (blk) __sync_fetch_and_add(&blk->counters[0], 1);
    return 0;
}

SEC("tp/vmscan/mm_vmscan_direct_reclaim_end")
int canned_direct_reclaim_end(void *ctx)
{
    __u64 tid = bpf_get_current_pid_tgid();
    __u64 *tsp = bpf_map_lookup_elem(&direct_starts, &tid);
    if (!tsp) return 0;
    __u64 dt = bpf_ktime_get_ns() - *tsp;
    bpf_map_delete_elem(&direct_starts, &tid);

    __u32 cpu = bpf_get_smp_processor_id();
    struct agent_output_block *blk = bpf_map_lookup_elem(&agent_output, &cpu);
    if (blk) {
        __sync_fetch_and_add(&blk->counters[1], 1);
        __sync_fetch_and_add(&blk->counters[2], dt);
    }
    return 0;
}

SEC("tp/vmscan/mm_vmscan_memcg_reclaim_begin")
int canned_memcg_reclaim_begin(void *ctx)
{
    __u64 tid = bpf_get_current_pid_tgid();
    __u64 ts = bpf_ktime_get_ns();
    bpf_map_update_elem(&memcg_starts, &tid, &ts, BPF_ANY);

    __u32 cpu = bpf_get_smp_processor_id();
    struct agent_output_block *blk = bpf_map_lookup_elem(&agent_output, &cpu);
    if (blk) __sync_fetch_and_add(&blk->counters[3], 1);
    return 0;
}

SEC("tp/vmscan/mm_vmscan_memcg_reclaim_end")
int canned_memcg_reclaim_end(void *ctx)
{
    __u64 tid = bpf_get_current_pid_tgid();
    __u64 *tsp = bpf_map_lookup_elem(&memcg_starts, &tid);
    if (!tsp) return 0;
    __u64 dt = bpf_ktime_get_ns() - *tsp;
    bpf_map_delete_elem(&memcg_starts, &tid);

    __u32 cpu = bpf_get_smp_processor_id();
    struct agent_output_block *blk = bpf_map_lookup_elem(&agent_output, &cpu);
    if (blk) {
        __sync_fetch_and_add(&blk->counters[4], 1);
        __sync_fetch_and_add(&blk->counters[5], dt);
    }
    return 0;
}
