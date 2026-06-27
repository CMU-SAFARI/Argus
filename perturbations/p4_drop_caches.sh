#!/usr/bin/env bash
# P4: drop_caches. One-shot - echoes 3 to /proc/sys/vm/drop_caches which
# clears pagecache, dentries, and inodes. Increases minor->major fault
# conversion rate and forces re-population, stressing the allocator/reclaim
# path. Expected subsystem flag: allocator_reclaim.
#
# Usage:
#   p4_drop_caches.sh apply    # sync && echo 3 > /proc/sys/vm/drop_caches
#   p4_drop_caches.sh revert   # no-op (drop_caches has no inverse)
#   p4_drop_caches.sh status
set -euo pipefail

DROP=/proc/sys/vm/drop_caches

case "${1:-}" in
    apply)
        sync
        echo 3 | tee "$DROP" > /dev/null
        echo "P4: caches dropped"
        ;;
    revert)
        echo "P4: revert is a no-op (caches re-populate naturally)"
        ;;
    status)
        echo "P4: $(cat "$DROP") (current value)"
        ;;
    *)
        echo "Usage: $0 {apply|revert|status}" >&2
        exit 2
        ;;
esac
