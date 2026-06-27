"""Shared result record + scoring for the Argus sweep harness.

`AgentRunResult` is the uniform per-run record every baseline produces, so the
sweep CLI and the figure scripts can grade them all the same way. `score_path`
is the depth-aware ternary scorer (1.0 / 0.5 / 0.0) that grades a diagnosed
kernel-code-path against the manifest's accepted paths for a (perturbation,
victim) cell.

Both are consumed by the agent loop (`agent.py`), the sweep CLI (`cli.py`), and
the paper plotters (`paper/plot_hallucination*.py`, `plot_correctness_mode.py`).
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class AgentRunResult:
    baseline: str            # run label: "B2" (=Argus) | "B-LLM-Prior"[-Tree[-Probes-Every-Level]] | "B-NR"
    benchmark: str
    perturbation: str
    expected_subsystem: list[str]   # accept-any-of L1; len>=1
    diagnosed_subsystems: list[str]  # L1 head of diagnosed_path; legacy field
    correct: bool                    # legacy L1-only: bool(set(expected) & set(diagnosed_subsystems))
    workload_ok: bool
    wall_s: float
    z_scores: dict[str, float]
    hint: str
    canned_probe: str | None = None
    counters: list[int] = field(default_factory=list)
    # Depth-aware fields. Default to L1-only behaviour when no deeper path was
    # submitted.
    diagnosed_path: list[str] = field(default_factory=list)  # full path agent submitted, L1 to deepest
    expected_path: list[list[str]] = field(default_factory=list)  # any-of: list of acceptable path-prefixes
    score: float = 0.0       # 1.0 (deep-correct) | 0.5 (L1-correct, depth wrong/missing) | 0.0 (L1 wrong)
    depth_reached: int = 0   # len(diagnosed_path)


def score_path(diagnosed: list[str], expected: list[str],
               expected_paths: list[list[str]]) -> float:
    """Ternary scoring of a diagnosed root-to-leaf code path:
       1.0 -- diagnosed_path lies ON the trajectory of one expected_path.
              Two cases qualify:
                (a) diagnosed extends/equals an expected_path (went at
                    least as deep, prefix matches at every step);
                (b) diagnosed is a strict prefix of an expected_path
                    (stopped short of the canonical leaf, but every step
                    taken is on the correct trajectory).
              Both are correct: the agent never left the accepted branch.
       0.5 -- L1 of diagnosed is in expected (legacy L1-only correctness)
              but the path itself is neither a prefix nor an extension of
              any expected_path (i.e., agent stepped onto a sibling at
              some depth).
       0.0 -- L1 wrong (or no diagnosis).
    """
    if not diagnosed:
        return 0.0
    diag_l1 = diagnosed[0]
    l1_correct = diag_l1 in expected
    if not expected_paths:
        return 1.0 if l1_correct else 0.0
    for exp_path in expected_paths:
        if not exp_path:
            continue
        # (a) diagnosed extends or equals exp_path
        if len(diagnosed) >= len(exp_path) and \
           tuple(diagnosed[:len(exp_path)]) == tuple(exp_path):
            return 1.0
        # (b) diagnosed is a strict prefix of exp_path
        if len(diagnosed) < len(exp_path) and \
           tuple(exp_path[:len(diagnosed)]) == tuple(diagnosed):
            return 1.0
    return 0.5 if l1_correct else 0.0
