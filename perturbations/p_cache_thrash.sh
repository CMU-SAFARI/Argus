#!/usr/bin/env bash
# P_CACHE_THRASH: LLC-pollution co-runner. Spawns the cache_thrash
# userspace sweep pinned to a sibling CPU (default: cpu 8, an E-core
# on i9-14900K -- same convention as p10_thp_aggressor). The sweep
# touches a region 2x L3 size at cache-line stride, polluting LLC and
# pre-fetching nothing useful. No allocator churn, no fragmentation,
# no syscalls in the hot loop -- the only kernel effect on the victim
# is *cold cache lines on page-table walks*, which raises per-page-fault
# wall time without changing the fault count.
#
# Designed to expose latency-driven kernel bottlenecks that the
# count-only FV misses but the new T_x = log2(count x latency) metric
# captures via the kprobe + kretprobe pair in the multiplex template.
#
# Expected subsystem flag (from manifest): kernel_paging.anon_fault.minor
# on most victims (latency-driven, not count-driven).
#
# Usage:
#   p_cache_thrash.sh apply     # spawn sweep, write PID file
#   p_cache_thrash.sh revert    # SIGTERM sweep, remove PID file
#   p_cache_thrash.sh status    # check whether the sweep is running
#
# Env overrides:
#   P_CACHE_THRASH_CPU       (default 8)       CPU to pin sweep to
#   P_CACHE_THRASH_SIZE_MB   (default auto=2*L3 from /sys)
#   P_CACHE_THRASH_STRIDE_B  (default 64)      stride bytes
#
# PID file: /tmp/agenticbpf-p_cache_thrash.pid
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SWEEP="$REPO_ROOT/perturbations/cache_thrash/sweep"
PID_FILE=/tmp/agenticbpf-p_cache_thrash.pid
LOG_FILE=/tmp/agenticbpf-p_cache_thrash.log

CPU="${P_CACHE_THRASH_CPU:-8}"
STRIDE_B="${P_CACHE_THRASH_STRIDE_B:-64}"
SIZE_MB_OVERRIDE="${P_CACHE_THRASH_SIZE_MB:-}"

is_running() {
    [[ -f "$PID_FILE" ]] || return 1
    local pid
    pid="$(cat "$PID_FILE")"
    [[ -d "/proc/$pid" ]]
}

case "${1:-}" in
    apply)
        if is_running; then
            echo "p_cache_thrash: already running (pid=$(cat $PID_FILE))"
            exit 0
        fi
        if [[ ! -x "$SWEEP" ]]; then
            echo "p_cache_thrash: $SWEEP not found or not executable. Build with:" >&2
            echo "    cc -O2 -Wall perturbations/cache_thrash/sweep.c -o perturbations/cache_thrash/sweep" >&2
            exit 1
        fi
        rm -f "$LOG_FILE" "$PID_FILE"
        ARGS=(--cpu "$CPU" --stride-bytes "$STRIDE_B")
        if [[ -n "$SIZE_MB_OVERRIDE" ]]; then
            ARGS+=(--size-mb "$SIZE_MB_OVERRIDE")
        fi
        setsid "$SWEEP" "${ARGS[@]}" > "$LOG_FILE" 2>&1 < /dev/null &
        echo $! > "$PID_FILE"
        sleep 1
        if ! is_running; then
            echo "p_cache_thrash: sweep died at startup; check $LOG_FILE" >&2
            tail -20 "$LOG_FILE" >&2 || true
            rm -f "$PID_FILE"
            exit 1
        fi
        echo "p_cache_thrash: running (pid=$(cat $PID_FILE), cpu=$CPU, log=$LOG_FILE)"
        ;;
    revert)
        if is_running; then
            pid="$(cat "$PID_FILE")"
            kill -TERM "$pid" 2>/dev/null || true
            for _ in 1 2 3 4 5; do
                kill -0 "$pid" 2>/dev/null || break
                sleep 1
            done
            kill -KILL "$pid" 2>/dev/null || true
            echo "p_cache_thrash: stopped (pid=$pid)"
        else
            echo "p_cache_thrash: not running"
        fi
        rm -f "$PID_FILE"
        ;;
    status)
        if is_running; then
            echo "p_cache_thrash: running (pid=$(cat $PID_FILE))"
        else
            echo "p_cache_thrash: not running"
        fi
        ;;
    *)
        echo "Usage: $0 {apply|revert|status}" >&2
        exit 2
        ;;
esac
