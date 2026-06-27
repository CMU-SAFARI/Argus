#!/usr/bin/env bash
# P10: THP-aggressor co-runner. Spawns benchmarks/thp_aggressor as a long-
# running background process pinned to E-cores (cores 8-15 on Raptor Lake-S
# i9-14900K) so it doesn't fight the victim workload for the P-cores that
# AGENTICBPF_PIN_CPUS=0-7 reserves for measurement. The aggressor hammers
# THP allocations, which over time fragments the huge-page pool and forces
# the victim's mmap/page-fault path to do compaction work and contend on
# mmap_lock / PMD locks - the Valinor THP-tax effect.
#
# Expected subsystem flag (from manifest): kernel_paging.
#
# Usage:
#   p10_thp_aggressor.sh apply    # spawn aggressor, write PID file
#   p10_thp_aggressor.sh revert   # kill aggressor, remove PID file
#   p10_thp_aggressor.sh status   # check whether the aggressor is running
#
# Override the aggressor's CPU pin via P10_AGGRESSOR_CPU (default: 8, an
# E-core on i9-14900K). Override its run cap via P10_AGGRESSOR_DURATION
# (default: 3600s; the revert step kills it well before then in practice).
#
# PID file: /tmp/agenticbpf-p10.pid
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
AGGR="$REPO_ROOT/benchmarks/thp_aggressor/thp_aggressor"
PID_FILE=/tmp/agenticbpf-p10.pid
LOG_FILE=/tmp/agenticbpf-p10.log

CPU="${P10_AGGRESSOR_CPU:-8}"
DURATION="${P10_AGGRESSOR_DURATION:-3600}"
THP_SIZE="${P10_AGGRESSOR_THP_SIZE:-512M}"
FRAG_SIZE="${P10_AGGRESSOR_FRAG_SIZE:-1G}"

is_running() {
    [[ -f "$PID_FILE" ]] || return 1
    local pid
    pid="$(cat "$PID_FILE")"
    # Use /proc/<pid> presence rather than `kill -0`: an unprivileged user
    # checking on a root-owned daemon would get EPERM from `kill -0` and
    # falsely conclude "not running". /proc is readable by all, so this
    # works regardless of UID.
    [[ -d "/proc/$pid" ]]
}

case "${1:-}" in
    apply)
        if is_running; then
            echo "P10: aggressor already running (pid=$(cat $PID_FILE))"
            exit 0
        fi
        if [[ ! -x "$AGGR" ]]; then
            echo "P10: $AGGR not found or not executable. Build with:" >&2
            echo "    make -C benchmarks/thp_aggressor" >&2
            exit 1
        fi
        # Force-clear any stale log file (may be owned by a different UID
        # from a prior unprivileged smoke run; the next redirect would EACCES
        # otherwise even though we're root, since some FS configurations deny
        # truncation of files we don't own). Use rm so this works regardless
        # of who owns the existing file.
        rm -f "$LOG_FILE" "$PID_FILE"
        # Detach into its own session so SIGINT to the orchestrator doesn't
        # cascade and kill it before revert runs.
        setsid "$AGGR" --duration "$DURATION" --cpu "$CPU" \
                       --thp-size "$THP_SIZE" --frag-size "$FRAG_SIZE" \
                       > "$LOG_FILE" 2>&1 < /dev/null &
        echo $! > "$PID_FILE"
        sleep 1   # give it a moment to start the fragmentation pass
        if ! is_running; then
            echo "P10: aggressor died at startup; check $LOG_FILE" >&2
            tail -20 "$LOG_FILE" >&2 || true
            rm -f "$PID_FILE"
            exit 1
        fi
        echo "P10: aggressor running (pid=$(cat $PID_FILE), cpu=$CPU, log=$LOG_FILE)"
        ;;
    revert)
        if is_running; then
            pid="$(cat "$PID_FILE")"
            kill -TERM "$pid" 2>/dev/null || true
            # Wait up to 5 s for graceful exit, then SIGKILL.
            for _ in 1 2 3 4 5; do
                kill -0 "$pid" 2>/dev/null || break
                sleep 1
            done
            kill -KILL "$pid" 2>/dev/null || true
            echo "P10: aggressor stopped (pid=$pid)"
        else
            echo "P10: aggressor not running"
        fi
        rm -f "$PID_FILE"
        ;;
    status)
        if is_running; then
            echo "P10: running (pid=$(cat $PID_FILE))"
        else
            echo "P10: not running"
        fi
        ;;
    *)
        echo "Usage: $0 {apply|revert|status}" >&2
        exit 2
        ;;
esac
