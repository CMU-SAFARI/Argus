#!/usr/bin/env bash
# PF-type victim: cow (entry pf_cow).
# Shared binary lives in ../pf_types/. work_units = pages mapped.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
SIZE_MB="${PF_SIZE_MB:-256}"
"$HERE/../pf_types/minor_page_fault_types" --type cow --size "${SIZE_MB}M" --iterations 1
echo "work_units=$((SIZE_MB * 256))"
