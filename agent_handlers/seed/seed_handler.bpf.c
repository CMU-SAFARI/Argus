/* SPDX-License-Identifier: GPL-2.0 OR BSD-3-Clause */
/* seed_handler.bpf.c - OpenEvolve initial program for STREAM x P1 (THP-off).
 *
 * Probes handle_mm_fault entry/exit, accumulates per-CPU into agent_output:
 *   counters[0] = fault count
 *   counters[1] = cumulative fault latency (ns)
 *
 * Designed to be plugged into KIDX_MM_FAULT_ENTRY / KIDX_MM_FAULT_EXIT slots
 * by agentd (Day 3).
 */

#include "vmlinux.h"
#include <bpf/bpf_helpers.h>
#include <bpf/bpf_tracing.h>
#include "profiler_types.h"

char _license[] SEC("license") = "GPL";

struct {
    __uint(type, BPF_MAP_TYPE_ARRAY);
    __uint(max_entries, 256);              /* support up to 256 CPUs */
    __type(key, __u32);
    __type(value, struct agent_output_block);
} agent_output SEC(".maps");

struct {
    __uint(type, BPF_MAP_TYPE_HASH);
    __uint(max_entries, 65536);
    __type(key, __u64);                    /* pid_tgid */
    __type(value, __u64);                  /* entry timestamp */
} fault_starts SEC(".maps");

SEC("kprobe/handle_mm_fault")
int seed_mm_fault_entry(struct pt_regs *ctx)
{
    __u64 tid = bpf_get_current_pid_tgid();
    __u64 ts = bpf_ktime_get_ns();
    bpf_map_update_elem(&fault_starts, &tid, &ts, BPF_ANY);
    return 0;
}

SEC("kretprobe/handle_mm_fault")
int seed_mm_fault_exit(struct pt_regs *ctx)
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
    return 0;
}
