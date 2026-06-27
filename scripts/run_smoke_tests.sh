#!/usr/bin/env bash
# T_x smoke tests: 5 cells, ~15-20 min wall total. Returns non-zero
# (and the master launcher halts) if any of the four pass criteria fail.
#
# Cells:
#   1) gups x p1_thp_off          -> expect z_log>=2 AND z_count>=2 on
#                                    kernel_paging.anon_fault.minor
#                                    (count-driven; both metrics agree)
#   2) pf_anon_write x p4_drop_caches -> expect z_log>=2 AND z_count>=2
#                                    on kernel_memory_mgmt.slab_fast
#   3) pf_anon_write x p1_thp_off -> expect every L2 child z_log < 2
#                                    (structural null preserved)
#   4) stream x p10_thp_aggressor -> expect z_log>=2 on a kernel_memory_mgmt
#                                    or kernel_paging child
#   5) pf_anon_write x p_cache_thrash (NEW) -> z_log>=2 BUT z_count<1 on
#                                    kernel_paging.anon_fault.minor
#                                    (latency-only -- T_x sees what
#                                     counts miss; headline result)
#
# Outputs each rep's diagnostic + the cell aggregate. Tail the log to
# review by hand.
set -euo pipefail
cd "$(dirname "$0")/.."
source .venv/bin/activate
export AGENTICBPF_AGENT_MODE=prod
LOG="/tmp/agenticbpf-T_x-smoke-$(date +%Y%m%d_%H%M%S).log"
echo "smoke log: $LOG"

CELLS=(
    "gups            p1_thp_off"
    "pf_anon_write   p4_drop_caches"
    "pf_anon_write   p1_thp_off"
    "stream          p10_thp_aggressor"
    "pf_anon_write   p_cache_thrash"
)

{
echo "================================================================"
echo "T_x smoke tests"
echo "started $(date)"
echo "================================================================"

for line in "${CELLS[@]}"; do
    read -r V P <<<"$line"
    echo
    echo "##### smoke: Argus on $V x $P (reps=1, T_x metric) #####"
    python -m orchestrator.agentctl.cli sweep \
        --baseline B2 \
        --benchmark "$V" --perturbation "$P" \
        --reps 1 --model gemini-2.5-flash 2>&1 | tail -20
    sleep 2
done

echo
echo "================================================================"
echo "Smoke tests done $(date) -- verify by hand before launching sweep:"
echo "  ls -lt results/sweeps/B2_*_$(date +%Y%m%d)_*.json | head -5"
echo "  jq -r '.reps[0].diagnosed_path, .reps[0].descents[0].z_log_scores // \"no T_x data\"' results/sweeps/B2_pf_anon_write_p_cache_thrash_*.json | tail -10"
echo "================================================================"
} 2>&1 | tee -a "$LOG"
