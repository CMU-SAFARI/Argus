#!/usr/bin/env bash
# eBPF measurement-overhead experiment.
#
# Compares the wall-time-per-work-unit cost of four kernel-side
# measurement modes on the same gups x p1_thp_off workload (target
# function: do_anonymous_page, ~1-2 M calls/sec):
#
#   1) no probe    -- pure workload baseline
#   2) count-only  -- single BPF kprobe + PERCPU_ARRAY bump
#   3) count+lat   -- kprobe + kretprobe + 2x bpf_ktime_get_ns + hashmap upd
#   4) ftrace fg   -- function_graph tracer scoped to do_anonymous_page
#
# Output: results/overhead_experiment.csv
# Each row: mode, rep, wall_s, work_units, ns_per_wu
# Plotting: paper/plot_ftrace_overhead_cpu_v2.py picks this up to render fig_overhead.{pdf,png}
#
# Run as a regular user; SUDO is requested per-rep for modes 2/3/4 since
# (a) BPF object load needs CAP_BPF / CAP_PERFMON, (b) ftrace
# /sys/kernel/tracing writes need root.
#
# Reps default 5 per mode. Tunable via env REPS=10 ./scripts/run_overhead_experiment.sh
set -euo pipefail
cd "$(dirname "$0")/.."

REPS="${REPS:-5}"
LOG="/tmp/agenticbpf-overhead-$(date +%Y%m%d_%H%M%S).log"
OUT_CSV="results/overhead_experiment.csv"
mkdir -p results

echo "===== eBPF overhead experiment =====" | tee -a "$LOG"
echo "started $(date)" | tee -a "$LOG"
echo "  reps per mode: $REPS" | tee -a "$LOG"
echo "  output csv:    $OUT_CSV" | tee -a "$LOG"

echo "mode,rep,wall_s,work_units,ns_per_wu" > "$OUT_CSV"

run_workload() {
    # pf_anon_write under p1_thp_off (THP off forces 4 KB anon faults).
    # Swapped 2026-05-15 from gups: gups only faults during the initial
    # 256 MB array touch (~65k faults), then runs steady-state without
    # faulting, so do_anonymous_page barely fires during the measurement
    # window and ftrace/kretprobe overhead is invisible. pf_anon_write
    # touches PF_SIZE_MB worth of pages per iteration, each triggering
    # one anon_write fault. Each iteration forks fresh children that
    # COW-fault the parent's anon mapping, so faults multiply with
    # iterations. PF_SIZE_MB=1024 * PF_ITERATIONS=10 = ~2.6 M sustained
    # faults over ~3 s baseline -- enough to make the ring-buffer write
    # cost of ftrace function_graph clearly visible vs the hash-map
    # insert+lookup of the kprobe+kretprobe pair.
    local mode="$1" rep="$2"
    local t0 t1 wall ns_per_wu wu out
    t0=$(date +%s.%N)
    out=$(PF_SIZE_MB=1024 PF_ITERATIONS=10 ./benchmarks/pf_anon_write/run.sh 2>&1)
    t1=$(date +%s.%N)
    wall=$(echo "$t1 - $t0" | bc -l)
    wu=$(echo "$out" | grep -oE "work_units=[0-9]+" | tail -1 | cut -d= -f2)
    [[ -z "$wu" ]] && wu=1
    ns_per_wu=$(echo "$wall * 1000000000 / $wu" | bc -l)
    printf "  %-14s rep=%d wall=%.3fs wu=%s ns_per_wu=%.2f\n" \
        "$mode" "$rep" "$wall" "$wu" "$ns_per_wu" | tee -a "$LOG"
    echo "$mode,$rep,$wall,$wu,$ns_per_wu" >> "$OUT_CSV"
}

apply_p1() {
    # Inline p1: THP=never. Saving the prior setting so revert restores it.
    P1_PRIOR_THP="$(cat /sys/kernel/mm/transparent_hugepage/enabled 2>/dev/null | grep -oP '\[\K[^]]+' || echo always)"
    echo never | sudo tee /sys/kernel/mm/transparent_hugepage/enabled > /dev/null
}
revert_p1() {
    echo "$P1_PRIOR_THP" | sudo tee /sys/kernel/mm/transparent_hugepage/enabled > /dev/null
}

apply_p1
trap revert_p1 EXIT

echo "===== mode 1: no probe =====" | tee -a "$LOG"
for r in $(seq 1 "$REPS"); do
    run_workload "no_probe" "$r"
    sleep 1
done

echo "===== mode 2: count-only kprobe =====" | tee -a "$LOG"
PROBE=perturbations/overhead_count_only/probe.bpf.o
for r in $(seq 1 "$REPS"); do
    # Attach the BPF object via a tiny inline loader (bpftool prog loadall).
    sudo bpftool prog loadall "$PROBE" /sys/fs/bpf/ov_co \
        autoattach 2>/dev/null || true
    run_workload "count_only" "$r"
    sudo rm -rf /sys/fs/bpf/ov_co
    sleep 1
done

echo "===== mode 3: count+latency (kprobe+kretprobe+timestamp) =====" | tee -a "$LOG"
PROBE=perturbations/overhead_count_latency/probe.bpf.o
for r in $(seq 1 "$REPS"); do
    sudo bpftool prog loadall "$PROBE" /sys/fs/bpf/ov_lat \
        autoattach 2>/dev/null || true
    run_workload "count_latency" "$r"
    sudo rm -rf /sys/fs/bpf/ov_lat
    sleep 1
done

echo "===== mode 4: ftrace function_graph (UNFILTERED — all kernel functions) =====" | tee -a "$LOG"
TRACING=/sys/kernel/tracing
# Switched 2026-05-15 from filtered single-function trace to unfiltered
# function_graph. Rationale: modern kernels (6.x+) heavily optimize
# function_graph WITH a per-symbol filter via dynamic-ftrace text
# patching, so the filtered case is effectively free (~100 ns/event).
# The realistic "I want to see what's happening in the kernel" workflow
# does NOT pre-filter -- you trace everything and grep afterward. That's
# what this mode now measures.
# overwrite=1: with no filter, the workload generates millions of events
# per second; let the buffer cycle rather than fail writes. Buffer at
# 128 MB/CPU still gets pressure but won't OOM.
for r in $(seq 1 "$REPS"); do
    sudo bash -c "
        echo nop > $TRACING/current_tracer
        echo > $TRACING/set_ftrace_filter
        echo 131072 > $TRACING/buffer_size_kb 2>/dev/null || true
        echo 1 > $TRACING/options/overwrite 2>/dev/null || true
        echo function_graph > $TRACING/current_tracer
        echo 1 > $TRACING/tracing_on
    "
    run_workload "ftrace_fg" "$r"
    sudo bash -c "
        echo 0 > $TRACING/tracing_on
        echo nop > $TRACING/current_tracer
        echo > $TRACING/set_ftrace_filter
    "
    sleep 1
done

revert_p1
trap - EXIT

echo "===== done =====" | tee -a "$LOG"
echo "csv: $OUT_CSV" | tee -a "$LOG"
echo "log: $LOG" | tee -a "$LOG"
echo "render:" | tee -a "$LOG"
echo "  python paper/plot_ftrace_overhead_cpu_v2.py" | tee -a "$LOG"
