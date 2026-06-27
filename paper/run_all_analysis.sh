#!/usr/bin/env bash
# Re-render every paper figure from the result data already in results/.
# No agent runs and no root -- it only reads results/ and writes paper/figures/.
# To regenerate the underlying data first, see ARTIFACT.md ("Tier 2").
#
# Usage:  ./paper/run_all_analysis.sh        (run from the repo root)
cd "$(dirname "$0")/.."
PY="${PY:-.venv/bin/python}"; [ -x "$PY" ] || PY=python3

run() { echo; echo "== $1 =="; shift; "$PY" "$@" || echo "  (skipped -- missing data? see ARTIFACT.md)"; }

run "Fig 1: per-cell hallucination"      paper/plot_hallucination_per_cell.py
run "Fig 2: aggregate hallucination"     paper/plot_hallucination.py
run "TtD: median time-to-diagnosis"      paper/plot_ttd_medians.py
run "Fig 5: eBPF measurement overhead"   paper/plot_ftrace_overhead_cpu_v2.py
run "Fig 6 (CS1): aggressor mode"        paper/plot_correctness_mode.py

echo; echo "== Fig 7 (CS2): page-fault disambiguation =="
shopt -s nullglob
for csv in paper/figures/cs1_matrix_*.csv; do
    "$PY" paper/plot_cs1_heatmap.py --csv "$csv" --bold-labels || echo "  (skipped $csv)"
done

echo; echo "done -> paper/figures/"
