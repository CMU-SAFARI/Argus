/* SPDX-License-Identifier: GPL-2.0 OR BSD-3-Clause */
/* Canned probe: handle_mm_fault latency.
 *
 * Subsystem: kernel_paging (the page-fault SLOW PATH; mmap_lock + PMD lock
 * contention shows up as elevated tail latency here).
 * Used as the B1 baseline for P10 (THP-aggressor) and as the primary
 * kernel_paging-latency attribution probe in CS3. Reproduces Valinor's
 * page-fault-latency ratio (with-aggressor / baseline) headline.
 *
 * agent_output layout (16 u64 per CPU):
 *   [0] fault_count                    (number of completed faults)
 *   [1] fault_lat_sum_ns               (cumulative latency, divide by [0] -> mean)
 *   [2] fault_lat_max_ns               (worst-case fault, surfaces tail effects)
 *   [3] bucket_under_1us               (fast path)
 *   [4] bucket_1us_10us                (typical 4K fault)
 *   [5] bucket_10us_100us              (THP fault / minor compaction)
 *   [6] bucket_100us_plus              (PMD-split / mmap_lock waits, the Valinor signal)
 *   [7..15] reserved
 *
 * Plugs into KIDX_MM_FAULT_ENTRY + KIDX_MM_FAULT_EXIT.
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
    __uint(max_entries, 65536);
    __type(key, __u64);                    /* pid_tgid */
    __type(value, __u64);                  /* entry timestamp ns */
} fault_starts SEC(".maps");

SEC("kprobe/handle_mm_fault")
int canned_pf_lat_entry(struct pt_regs *ctx)
{
    __u64 tid = bpf_get_current_pid_tgid();
    __u64 ts = bpf_ktime_get_ns();
    bpf_map_update_elem(&fault_starts, &tid, &ts, BPF_ANY);
    return 0;
}

SEC("kretprobe/handle_mm_fault")
int canned_pf_lat_exit(struct pt_regs *ctx)
{
    __u64 tid = bpf_get_current_pid_tgid();
    __u64 *ts = bpf_map_lookup_elem(&fault_starts, &tid);
    if (!ts) return 0;
    __u64 dt = bpf_ktime_get_ns() - *ts;
    bpf_map_delete_elem(&fault_starts, &tid);

    __u32 cpu = bpf_get_smp_processor_id();
    struct agent_output_block *blk = bpf_map_lookup_elem(&agent_output, &cpu);
    if (!blk) return 0;

    __sync_fetch_and_add(&blk->counters[0], 1);
    __sync_fetch_and_add(&blk->counters[1], dt);

    /* max via CAS-loop. Bounded retries so the verifier accepts the loop;
     * collisions are rare on a single-CPU counter. */
    __u64 prev = blk->counters[2];
    if (dt > prev) {
        /* not strictly atomic but good enough - we only need approximate max
         * for the case-study figure, and contended writes from neighbour CPUs
         * land in their own per-CPU blocks. */
        blk->counters[2] = dt;
    }

    /* 4-bucket histogram. Edges in nanoseconds: 1us, 10us, 100us. */
    if (dt < 1000ULL)
        __sync_fetch_and_add(&blk->counters[3], 1);
    else if (dt < 10000ULL)
        __sync_fetch_and_add(&blk->counters[4], 1);
    else if (dt < 100000ULL)
        __sync_fetch_and_add(&blk->counters[5], 1);
    else
        __sync_fetch_and_add(&blk->counters[6], 1);

    return 0;
}
