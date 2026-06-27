#!/usr/bin/env bash
# P0: THP=always. Forces transparent_hugepage=always system-wide so workloads
# that mmap + madvise(MADV_HUGEPAGE) actually receive 2 MiB pages. Used as
# a *pre-perturbation* for the reference build so that idle-state runs use
# real huge pages, giving P1 (THP=never) something to actually downgrade.
#
# Usage:
#   p0_thp_always.sh apply   # echo always > /sys/kernel/mm/transparent_hugepage/enabled
#   p0_thp_always.sh revert  # restore previous setting
#   p0_thp_always.sh status
#
# Snapshot file: /tmp/agenticbpf-p0.snapshot
set -euo pipefail

THP=/sys/kernel/mm/transparent_hugepage/enabled
SNAP=/tmp/agenticbpf-p0.snapshot

current() { awk -F'[][]' '{print $2}' "$THP"; }

case "${1:-}" in
    apply)
        if [[ ! -f "$SNAP" ]]; then
            current > "$SNAP"
        fi
        echo always | tee "$THP" > /dev/null
        echo "P0: THP=$(current) (was $(cat "$SNAP"))"
        ;;
    revert)
        if [[ -f "$SNAP" ]]; then
            echo "$(cat "$SNAP")" | tee "$THP" > /dev/null
            rm -f "$SNAP"
        fi
        echo "P0: THP=$(current)"
        ;;
    status)
        echo "P0: THP=$(current)"
        ;;
    *)
        echo "Usage: $0 {apply|revert|status}" >&2
        exit 2
        ;;
esac
