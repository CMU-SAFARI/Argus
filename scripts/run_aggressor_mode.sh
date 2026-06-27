#!/usr/bin/env bash
# Case Study 1 (paper Fig 6): aggressor mode.
#
# Hold the aggressor perturbation fixed (M = p10_thp_aggressor) and sweep every
# victim that has an idle reference profile, K reps each. Each (victim, M) cell
# writes the same JSON shape as `cli sweep`, which paper/plot_correctness_mode.py
# renders into the per-victim diagnosis chart.
#
# Usage:
#   scripts/run_aggressor_mode.sh <perturbation> [reps] [baseline] [model]
# Defaults: reps=5, baseline=B2, model=gemini-2.5-flash.
#
# Victims are auto-discovered from results/reference/<bench>.json, so adding one
# is just `agentctl reference <bench> --n-runs N` and re-running this.
set -euo pipefail

PERT="${1:-}"
REPS="${2:-5}"
BASELINE="${3:-B2}"
MODEL="${4:-gemini-2.5-flash}"

if [[ -z "$PERT" ]]; then
    echo "usage: $0 <perturbation> [reps=5] [baseline=B2] [model=gemini-2.5-flash]" >&2
    echo "" >&2
    echo "available perturbations:" >&2
    grep -E '^p[0-9]+_[a-z_]+:' perturbations/manifest.yaml | sed 's/:.*$//' | sed 's/^/  /' >&2
    exit 2
fi

REPO="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO"

# B2 needs google-genai (only installed inside .venv). System python has
# no `google` module; activate the venv if it exists or fail loudly.
if [[ -f "$REPO/.venv/bin/activate" ]]; then
    # shellcheck disable=SC1091
    source "$REPO/.venv/bin/activate"
elif ! python -c "import google.genai" 2>/dev/null; then
    echo "error: .venv not found and google.genai not importable from system python" >&2
    echo "create it with:  python3 -m venv .venv && pip install -r requirements.txt" >&2
    exit 1
fi

# Discover victims = benchmarks with reference profiles built.
VICTIMS=()
for refpath in results/reference/*.json; do
    [[ -f "$refpath" ]] || continue
    bench="$(basename "$refpath" .json)"
    VICTIMS+=("$bench")
done

if [[ ${#VICTIMS[@]} -eq 0 ]]; then
    echo "error: no reference profiles found in results/reference/" >&2
    echo "build one with: agentctl reference <benchmark> --n-runs 30" >&2
    exit 1
fi

export AGENTICBPF_AGENT_MODE=prod

LOG="/tmp/agenticbpf-aggressor-${PERT}-$(date +%Y%m%d_%H%M%S).log"
echo "log: $LOG"
echo "aggressor: $PERT  victims: ${VICTIMS[*]}  reps: $REPS  baseline: $BASELINE  model: $MODEL"
echo ""

{
    for V in "${VICTIMS[@]}"; do
        echo ""
        echo "########## $BASELINE x $V x $PERT (aggressor mode, $REPS reps) ##########"
        python -m orchestrator.agentctl.cli sweep \
            --baseline "$BASELINE" \
            --benchmark "$V" \
            --perturbation "$PERT" \
            --reps "$REPS" \
            --model "$MODEL" 2>&1 | tail -25
    done
} | tee "$LOG"

echo ""
echo "done. render Case Study 1 (Fig 6) with:"
echo "  python paper/plot_correctness_mode.py"
