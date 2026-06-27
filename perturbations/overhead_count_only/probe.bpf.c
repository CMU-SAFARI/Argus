/* overhead_count_only/probe.bpf.c
 *
 * Minimal one-function kprobe: counts entries to do_anonymous_page into
 * a single PERCPU_ARRAY slot. Used by the eBPF-overhead experiment as
 * the "count-only" measurement mode -- the cheapest BPF probe shape
 * we can construct.
 *
 * Comparison points in the overhead experiment:
 *   (1) no probe at all          -> baseline wall time
 *   (2) this kprobe              -> +1 BPF program, +1 PERCPU bump per call
 *   (3) overhead_count_latency   -> + kretprobe + 2x ktime + hashmap upd
 *   (4) ftrace function_graph    -> kernel-side tracer with full call graph
 */
#include "vmlinux.h"
#include <bpf/bpf_helpers.h>
#include <bpf/bpf_tracing.h>

struct {
    __uint(type, BPF_MAP_TYPE_PERCPU_ARRAY);
    __uint(max_entries, 1);
    __type(key, __u32);
    __type(value, __u64);
} ov_count SEC(".maps");

SEC("kprobe/do_anonymous_page")
int BPF_KPROBE(ov_cnt_do_anonymous_page) {
    __u32 z = 0;
    __u64 *p = bpf_map_lookup_elem(&ov_count, &z);
    if (p) __sync_fetch_and_add(p, 1);
    return 0;
}

char _license[] SEC("license") = "GPL";
