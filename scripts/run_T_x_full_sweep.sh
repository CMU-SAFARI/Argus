#!/usr/bin/env bash
# Master launcher for the T_x rollout: smoke tests -> Argus full sweep ->
# baselines on the new perturbation -> CS#1 rerun -> overhead experiment.
# Stops if any phase fails.
#
# PRE-LAUNCH CHECKLIST (one-time, run by hand):
#   1. agentd must be running the NEW binary (post-T_x changes). Restart:
#         sudo pkill -f '/build/agentd' ; sleep 1
#         sudo nohup ./build/agentd > /tmp/agentd.log 2>&1 &
#   2. Existing multiplex references will be auto-rebuilt on first descent
#      (their schema is now missing mu_log/sigma_log). This adds ~10 min
#      per (benchmark, parent) on the first encounter; the master sweep
#      below absorbs that cost in its early reps.
#   3. Old sweep JSONs are NOT deleted -- the new sweep JSONs gain a fresh
#      timestamp and the plotters pick up the latest mtime per cell. To
#      force a clean cut, delete files matching:
#         rm results/sweeps/{B2,B-NR,B-LLM-Prior,B-LLM-Prior-Tree,B-LLM-Prior-Tree-Probes-Every-Level}_*_$(date +%Y%m%d)_*.json
#
# Launch (autonomous, survives SSH disconnect):
#   chmod +x scripts/run_T_x_full_sweep.sh
#   setsid nohup ./scripts/run_T_x_full_sweep.sh </dev/null >/dev/null 2>&1 &
#   disown
#
# Progress one-liners:
#   ps -p $(cat /tmp/agenticbpf-T_x-master.pid) >/dev/null && echo ALIVE || echo DEAD
#   tail -f $(ls -t /tmp/agenticbpf-T_x-*.log | head -1)
#   ls results/sweeps/B2_*_$(date +%Y%m%d)_*.json 2>/dev/null | wc -l   # Argus reps done
#   ls results/sweeps/B-*_*_p_cache_thrash_$(date +%Y%m%d)_*.json 2>/dev/null | wc -l
set -euo pipefail
cd "$(dirname "$0")/.."
source .venv/bin/activate
export AGENTICBPF_AGENT_MODE=prod

MASTER_LOG="/tmp/agenticbpf-T_x-master-$(date +%Y%m%d_%H%M%S).log"
echo $$ > /tmp/agenticbpf-T_x-master.pid
echo "pid: $$  log: $MASTER_LOG"

VICTIMS=(gups stream pf_anon_write llama_cpp fork_storm)
ALL_PERTS=(p1_thp_off p4_drop_caches p10_thp_aggressor p_cache_thrash)
NEW_PERT=p_cache_thrash
BASELINES=(B-NR B-LLM-Prior B-LLM-Prior-Tree B-LLM-Prior-Tree-Probes-Every-Level)
REPS=3
MODEL=gemini-2.5-flash

{
echo "================================================================"
echo "T_x ROLLOUT MASTER LAUNCHER"
echo "started $(date)"
echo "================================================================"
echo "victims:    ${VICTIMS[*]}"
echo "perts:      ${ALL_PERTS[*]} (Argus over all; baselines over $NEW_PERT only)"
echo "baselines:  ${BASELINES[*]}"
echo "reps/cell:  $REPS"
echo "----------------------------------------------------------------"

echo
echo "================================================================"
echo "PHASE D: smoke tests (5 cells, gate the full sweep)"
echo "================================================================"
bash scripts/run_smoke_tests.sh
SMOKE_RC=$?
if [[ $SMOKE_RC -ne 0 ]]; then
    echo "smoke tests FAILED -- halting full sweep"
    exit 2
fi

echo
echo "================================================================"
echo "PHASE E1: Argus across all (victim x perturbation) cells"
echo "  (metric changed -> all cells rerun)"
echo "================================================================"
for V in "${VICTIMS[@]}"; do
    for P in "${ALL_PERTS[@]}"; do
        echo
        echo "##### Argus: $V x $P (reps=$REPS) #####"
        python -m orchestrator.agentctl.cli sweep \
            --baseline B2 \
            --benchmark "$V" --perturbation "$P" \
            --reps "$REPS" --model "$MODEL" 2>&1 | tail -10
        sleep 3
    done
done

echo
echo "================================================================"
echo "PHASE E2: 4 baselines on new perturbation x 5 victims"
echo "================================================================"
for B in "${BASELINES[@]}"; do
    for V in "${VICTIMS[@]}"; do
        echo
        echo "##### $B: $V x $NEW_PERT (reps=$REPS) #####"
        python -m orchestrator.agentctl.cli sweep \
            --baseline "$B" \
            --benchmark "$V" --perturbation "$NEW_PERT" \
            --reps "$REPS" --model "$MODEL" 2>&1 | tail -10
        sleep 3
    done
done

echo
echo "================================================================"
echo "PHASE F: CS#1 heatmap rerun on motivation victims"
echo "================================================================"
python scripts/cs1_pf_types_matrix.py \
    --parent kernel_paging \
    --victims gups,stream,pf_anon_write,llama_cpp,fork_storm \
    --references gups,stream,pf_anon_write,llama_cpp,fork_storm \
    --out paper/figures/cs1_matrix_kernel_paging_motivation.csv 2>&1 | tail -20
python scripts/cs1_pf_types_matrix.py \
    --parent kernel_paging.anon_fault \
    --victims gups,stream,pf_anon_write,llama_cpp,fork_storm \
    --references gups,stream,pf_anon_write,llama_cpp,fork_storm \
    --out paper/figures/cs1_matrix_kernel_paging_anon_fault_motivation.csv 2>&1 | tail -20
# Re-render
python paper/plot_cs1_heatmap.py \
    --csv paper/figures/cs1_matrix_kernel_paging_motivation.csv \
    --bold-labels 2>&1 | tail
python paper/plot_cs1_heatmap.py \
    --csv paper/figures/cs1_matrix_kernel_paging_anon_fault_motivation.csv \
    --bold-labels 2>&1 | tail

echo
echo "================================================================"
echo "PHASE C: eBPF measurement-overhead experiment"
echo "================================================================"
REPS=5 bash scripts/run_overhead_experiment.sh 2>&1 | tail -30
python paper/plot_ftrace_overhead_cpu_v2.py 2>&1 | tail

echo
echo "================================================================"
echo "PHASE G: re-render hallucination + TtD figures (auto-discover new sweeps)"
echo "================================================================"
python paper/plot_hallucination.py 2>&1 | tail -5
python paper/plot_hallucination_per_cell.py 2>&1 | tail -5
python paper/plot_ttd_medians.py 2>&1 | tail -5

echo
echo "================================================================"
echo "DONE $(date)"
echo "================================================================"
} 2>&1 | tee -a "$MASTER_LOG"
