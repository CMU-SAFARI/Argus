# Paper materials

Figure-generation scripts for the Argus paper, plus a few analysis helpers. The
figures themselves live in `figures/` (PDF for the paper, PNG for review).

Each generator reads result data from `results/` (sweep JSONs, the overhead
CSV, the CS2 matrix CSVs) and writes to `figures/`. To re-render every figure
from existing data: `./run_all_analysis.sh` (from the repo root, with `.venv`
active). To regenerate the data first, see [../ARTIFACT.md](../ARTIFACT.md).

## Figure generators

| Paper item | Generator | Output (`figures/`) | What it shows |
|---|---|---|---|
| **Fig 1** | `plot_hallucination_per_cell.py` | `fig_hallucination_per_cell.{pdf,png}` | hallucinated diagnoses per (victim, perturber) cell, all four baselines |
| **Fig 2** | `plot_hallucination.py` | `fig_hallucination.{pdf,png}` | aggregate hallucination rate per baseline (Argus 2% vs 45 / 52 / 32%) |
| **TtD** | `plot_ttd_medians.py` | `fig_ttd_medians.{pdf,png}` | median time-to-diagnosis per baseline (Argus 30.9 s) |
| **Fig 5** | `plot_ftrace_overhead_cpu_v2.py` | `fig_overhead.{pdf,png}` | per-call overhead: ftrace vs count-only vs kprobe+kretprobe |
| **Fig 6 (CS1)** | `plot_correctness_mode.py` | `fig5_correctness_*.{pdf,png}` | per-victim diagnosis under the THP aggressor (p10) |
| **Fig 7 (CS2)** | `plot_cs1_heatmap.py` | `cs1_heatmap*.{pdf,png}` | page-fault disambiguation: 5 perturbers × 5 references, L2 T_x z-scores |

> **Filename ≠ paper figure number.** These scripts predate the paper's final
> numbering. The on-disk names are historical: `fig5_correctness_*` is paper
> **Fig 6**, the `cs1_*` files are paper **CS2** (`fig_overhead` is the only one
> that lines up). Trust the *Generator* column, not the filename. See
> [../ARTIFACT.md](../ARTIFACT.md) for the full paper⇄code mapping.

## Analysis helpers

`cell_reliability.py`, `variance_summary.py`, `failure_taxonomy.py`,
`cost_profile.py`, `latex_table.py`, and `time_to_diagnosis.py` print
text summaries (confidence intervals, failure breakdowns, cost/phase tables)
over the same sweep data. They are diagnostic aids, not figure generators.
