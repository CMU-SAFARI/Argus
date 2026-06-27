#!/usr/bin/env bash
# PF-type victim: page_cache (entry pf_page_cache).
# Shared binary lives in ../pf_types/. work_units = pages mapped.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
SIZE_MB="${PF_SIZE_MB:-256}"
"$HERE/../pf_types/minor_page_fault_types" --type page_cache --size "${SIZE_MB}M" --iterations 1
echo "work_units=$((SIZE_MB * 256))"
