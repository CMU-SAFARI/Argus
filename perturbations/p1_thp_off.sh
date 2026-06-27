#!/usr/bin/env bash
# P1: THP off. Forces 4 KiB pages -> ~512x more page faults for the same
# working set, plus dTLB pressure. Expected subsystem flag: tlb_paging.
#
# Usage:
#   p1_thp_off.sh apply    # echo never  > /sys/kernel/mm/transparent_hugepage/enabled
#   p1_thp_off.sh revert   # restore previous setting from saved snapshot
#   p1_thp_off.sh status   # print current value (no change)
#
# Snapshot file: /tmp/agenticbpf-p1.snapshot
set -euo pipefail

THP=/sys/kernel/mm/transparent_hugepage/enabled
SNAP=/tmp/agenticbpf-p1.snapshot

current() { awk -F'[][]' '{print $2}' "$THP"; }   # extracts bracketed mode

case "${1:-}" in
    apply)
        if [[ ! -f "$SNAP" ]]; then
            current > "$SNAP"
        fi
        echo never | tee "$THP" > /dev/null
        echo "P1: THP=$(current) (was $(cat "$SNAP"))"
        ;;
    revert)
        if [[ -f "$SNAP" ]]; then
            echo "$(cat "$SNAP")" | tee "$THP" > /dev/null
            rm -f "$SNAP"
        fi
        echo "P1: THP=$(current)"
        ;;
    status)
        echo "P1: THP=$(current)"
        ;;
    *)
        echo "Usage: $0 {apply|revert|status}" >&2
        exit 2
        ;;
esac
