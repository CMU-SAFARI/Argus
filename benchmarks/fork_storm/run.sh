#!/usr/bin/env bash
# fork_storm: spawn N short-lived child processes and wait.
# Designed to surface PID allocation + task_struct alloc + RCU-sched
# grace-period stalls as the dominant signature. The LLM's prior is
# likely to attribute "fork-heavy workload" to kernel_scheduler, but
# the measured hot path is sync_rcu (and slab churn for task_struct
# free via RCU callbacks).
set -euo pipefail
N="${FORK_N:-2000}"
ITERS="${FORK_ITERS:-10}"

for _ in $(seq 1 "$ITERS"); do
    for _ in $(seq 1 "$N"); do
        ( : ) &
    done
    wait
done
echo "work_units=$((N * ITERS))"
