#!/usr/bin/env bash
# P15a: rebuild every benchmark's 17-slot L1 reference after the FV
# scope change in src/agentd/workload_runner.cpp lands in the running
# agentd binary.
#
# After this you'll also want to rebuild any L2 multiplex references
# you care about. Those are independent of the L1 FV scope (they
# attach kprobes/tracepoints directly), so technically only the L1
# refs are stale. But it's cheap to rebuild a few and safer than
# guessing.
#
# Procedure (the human steps):
#   1. cd <repo> ; make             # rebuild build/agentd
#   2. sudo pkill -f build/agentd   # stop old daemon
#   3. sudo build/agentd            # start new daemon (in another tty)
#   4. ./scripts/p15a_rebuild_all_references.sh
#
# Wall time: ~5 min per benchmark x 9 = ~45 min.

set -euo pipefail
cd "$(dirname "$0")/.."

PY=.venv/bin/python
N_REF_RUNS="${N_REF_RUNS:-30}"

# Order matters only for partial-failure recovery; fast/cheap benchmarks first.
BENCHES=(
    pf_anon_write
    pf_cow
    pf_major
    pf_page_cache
    pf_zero_page
    stream
    gups
    fork_storm
    llama_cpp
)

echo "[P15a] rebuilding L1 reference for ${#BENCHES[@]} benchmark(s) "
echo "       at n_runs=$N_REF_RUNS each. This takes ~5 min/benchmark."

ok=()
fail=()
for B in "${BENCHES[@]}"; do
    echo
    echo "[P15a] === $B ==="
    if $PY -m orchestrator.agentctl.cli reference "$B" --n-runs "$N_REF_RUNS" 2>&1 | tail -3; then
        ok+=("$B")
    else
        fail+=("$B")
        echo "[P15a]   $B FAILED (continuing)"
    fi
done

echo
echo "[P15a] === summary ==="
echo "[P15a] ok    (${#ok[@]}): ${ok[*]}"
echo "[P15a] fail  (${#fail[@]}): ${fail[*]:-(none)}"
if (( ${#fail[@]} > 0 )); then
    exit 1
fi
