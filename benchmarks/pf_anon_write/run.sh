#!/usr/bin/env bash
# PF-type victim: anon_write (entry pf_anon_write).
# Shared binary lives in ../pf_types/. work_units = pages re-faulted across
# all iterations (each iteration spawns fresh children that COW-fault the
# parent's anonymous mapping, so faults multiply with iterations).
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
SIZE_MB="${PF_SIZE_MB:-256}"
ITERATIONS="${PF_ITERATIONS:-1}"
"$HERE/../pf_types/minor_page_fault_types" --type anon_write --size "${SIZE_MB}M" --iterations "$ITERATIONS"
echo "work_units=$((SIZE_MB * 256 * ITERATIONS))"
