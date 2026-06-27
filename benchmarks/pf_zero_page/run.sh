#!/usr/bin/env bash
# PF-type victim: zero_page (entry pf_zero_page).
# Shared binary lives in ../pf_types/. work_units = pages mapped.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
SIZE_MB="${PF_SIZE_MB:-256}"
"$HERE/../pf_types/minor_page_fault_types" --type zero_page --size "${SIZE_MB}M" --iterations 1
echo "work_units=$((SIZE_MB * 256))"
