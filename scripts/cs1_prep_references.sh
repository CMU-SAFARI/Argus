#!/usr/bin/env bash
# CS#1 prerequisite: rebuild 17-slot L1 references + kernel_paging L2
# multiplex references for the PF-types victim suite. Run once before
# scripts/cs1_pf_types_matrix.py.
#
# pf_anon_write is already current; this script handles the other four.
# Wall time: ~5 min per benchmark (30 idle runs × ~3s each + multiplex
# build), so ~20 min total.

set -euo pipefail
cd "$(dirname "$0")/.."

PY=.venv/bin/python
N_REF_RUNS="${N_REF_RUNS:-30}"
VICTIMS=(pf_cow pf_major pf_page_cache pf_zero_page)

for V in "${VICTIMS[@]}"; do
    echo "[CS1-prep] === $V: 17-slot L1 reference (n_runs=$N_REF_RUNS) ==="
    $PY -m orchestrator.agentctl.cli reference "$V" --n-runs "$N_REF_RUNS"

    echo "[CS1-prep] === $V: kernel_paging L2 multiplex reference ==="
    $PY -m orchestrator.agentctl.cli prebuild-references "$V" \
        --parents kernel_paging --n-runs "$N_REF_RUNS"

    echo
done

echo "[CS1-prep] all references built. Now run scripts/cs1_pf_types_matrix.py."
