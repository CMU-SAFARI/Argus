/* overhead_count_latency/probe.bpf.c
 *
 * Single function (do_anonymous_page) with the full T_x measurement
 * shape: kprobe + kretprobe + 2 x bpf_ktime_get_ns + hash-map entry
 * timestamp + per-CPU sum accumulator. The "count+latency" mode of
 * the eBPF-overhead experiment. Same algebra as the multiplex
 * template but on a single function, so the probe-overhead delta is
 * directly comparable to count-only.
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

struct {
    __uint(type, BPF_MAP_TYPE_PERCPU_ARRAY);
    __uint(max_entries, 1);
    __type(key, __u32);
    __type(value, __u64);
} ov_sum_ns SEC(".maps");

struct {
    __uint(type, BPF_MAP_TYPE_HASH);
    __uint(max_entries, 65536);
    __type(key, __u64);
    __type(value, __u64);
} ov_entry_ts SEC(".maps");

SEC("kprobe/do_anonymous_page")
int BPF_KPROBE(ov_lat_entry) {
    __u32 z = 0;
    __u64 *p = bpf_map_lookup_elem(&ov_count, &z);
    if (p) __sync_fetch_and_add(p, 1);
    __u64 key = bpf_get_current_pid_tgid();
    __u64 now = bpf_ktime_get_ns();
    bpf_map_update_elem(&ov_entry_ts, &key, &now, BPF_ANY);
    return 0;
}

SEC("kretprobe/do_anonymous_page")
int BPF_KRETPROBE(ov_lat_exit) {
    __u64 key = bpf_get_current_pid_tgid();
    __u64 *entry = bpf_map_lookup_elem(&ov_entry_ts, &key);
    if (!entry) return 0;
    __u64 delta = bpf_ktime_get_ns() - *entry;
    bpf_map_delete_elem(&ov_entry_ts, &key);
    __u32 z = 0;
    __u64 *sum = bpf_map_lookup_elem(&ov_sum_ns, &z);
    if (sum) __sync_fetch_and_add(sum, delta);
    return 0;
}

char _license[] SEC("license") = "GPL";
