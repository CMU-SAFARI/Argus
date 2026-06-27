"""B2: ReAct loop with multi-turn tool calling (provider-agnostic).

Two nested retry counters:
  - MAX_VERIFIER_RETRIES (=5) per hypothesis. Triggered by compile_ebpf or
    attach_ebpf returning ok=False. Same hypothesis, fix the code.
  - MAX_HYPOTHESIS_PIVOTS (=3) per cell. Triggered by the inner loop exiting
    without a submit_diagnosis call (probe ran but agent decided to pivot).

Four termination paths:
  Submitted          -- agent called submit_diagnosis(...)
  VerifierBlocked    -- inner loop exhausted; code never loaded
  NoConvergence      -- outer loop exhausted; no submit_diagnosis ever called
  ModelGaveUp        -- model returned text without a tool call

Plus orthogonal first-try-acceptance (compile + attach succeeded on attempt #1).

Provider selection is done in `chat_backend.build_backend(...)`. Defaults:
  AGENTICBPF_AGENT_MODE=prod    -> Google Gemini (Vertex)
  AGENTICBPF_AGENT_MODE=debug   -> local Ollama (qwen2.5-coder:32b)
Each backend's SDK is lazy-imported, so B0/B1 baselines never pay any LLM
SDK cost.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from .agent_baselines import AgentRunResult, score_path
from .agent_tools import SessionState, make_tools
from .canned_probes import PROBES
from .chat_backend import ToolResult, build_backend
from .ipc_client import AgentdClient
from .perturb import Perturbation, expected_paths, expected_subsystem
from .reference import ReferenceProfile

# Perturbations that spawn a long-running co-runner process (vs. instant
# config knob flips). For these we apply ONCE at rep entry and keep the
# aggressor running across every run_workload/descend call -- otherwise
# each tool call would pay the aggressor's cold-start (e.g. p20 reloading
# a 4.6 GB GGUF every 5 s) and short victims would finish before the
# aggressor is producing load. Config-style perturbations (p1_thp_off,
# p4_drop_caches) stay per-call so they apply immediately before each
# measurement.
AGGRESSOR_PERTURBATIONS: frozenset[str] = frozenset({
    "p10_thp_aggressor",
    "p11_force_compaction",
    "p12_dd_io",
    "p13_netem_loopback",
    "p14_madvise_storm",
    "p20_llm_aggressor",
})

REPO = Path(__file__).resolve().parents[2]
PROMPTS_DIR = REPO / "orchestrator" / "configs" / "prompts"


@dataclass
class AttemptLog:
    pivot: int
    verifier_retry: int
    tool_name: str
    tool_args_summary: str
    ok: bool
    error: str = ""
    elapsed_s: float = 0.0


def _load_system_prompt() -> str:
    p = PROMPTS_DIR / "system.md"
    if not p.exists():
        raise FileNotFoundError(
            f"system prompt missing: {p}\nDay 6 must write this file before "
            f"running B2."
        )
    return p.read_text()


def _load_few_shot() -> str:
    """Inline two canned probes verbatim into the prompt so the model has
    structural templates to follow."""
    parts = []
    for name in ("tlb_shootdown", "reclaim"):
        spec = PROBES[name]
        parts.append(f"### Few-shot example: {name} (target subsystem: {spec.target_subsystem})\n")
        parts.append(f"```c\n{spec.source.read_text()}\n```\n")
    return "\n".join(parts)


_BNR_PROMPT_PREFIX = """**MODE: NO-REFERENCE (B-NR)**

You are running WITHOUT an idle-baseline reference profile. The
following tools are NOT AVAILABLE in this mode and have been removed:

  - compare_to_reference  (no idle baseline exists; no z-scores)
  - descend               (multiplex L2/L3 descent depends on a depth-N
                           reference and is therefore disabled)

Diagnose the bottleneck from raw counters alone:

  - run_workload returns `perf_fv` (absolute counts) and
    `perf_fv_per_work_unit` (normalized rates). Use these directly.
  - collect_ofv returns your own probe's counters after a run with the
    probe attached.

Form your hypothesis from prior knowledge of which counters dominate
in each kernel subsystem (e.g. high `page_faults` -> kernel_paging,
high `kmem_alloc` -> kernel_memory_mgmt). Author and attach a probe to
cross-validate. Call submit_diagnosis once you are confident; you can
only submit ONE diagnosis per cell.

Ignore any instructions below that reference compare_to_reference,
descend, z-scores, mu/sigma, or depth-N references. They do not apply
in B-NR mode.
"""


def _load_subsystem_tree_text() -> str:
    """Render subsystems.yaml as an indented text tree for in-prompt use.

    Output format (depth-indented, 2 spaces per level):
        kernel_paging
          anon_fault
            minor
            cow
            thp_collapse
          file_fault
            ...

    The tree is the authoritative list of legal L1/L2/L3 path components.
    Fed to B-LLM-Prior-Tree so the LLM is constrained to a finite,
    enumerated vocabulary instead of inventing kernel-function names.
    """
    import yaml
    repo = Path(__file__).resolve().parents[2]
    tree = yaml.safe_load((repo / "orchestrator" / "configs" /
                           "subsystems.yaml").read_text())
    lines: list[str] = []

    def walk(node: dict, depth: int) -> None:
        lines.append("  " * depth + node["name"])
        for c in (node.get("children") or []):
            walk(c, depth + 1)

    for l1 in tree.get("subsystems", []):
        walk(l1, 0)
    return "\n".join(lines)


_BLP_TREE_PROMPT_PREFIX = """**MODE: LLM-PRIOR DEEP-GUESS + TREE (B-LLM-Prior-Tree)**

You are running WITHOUT an idle-baseline reference profile. The
following tools are NOT AVAILABLE in this mode and have been removed:

  - compare_to_reference
  - descend

This baseline measures the LLM's a priori knowledge of kernel-subsystem
behavior when constrained to a fixed vocabulary. You MUST submit the
deepest plausible path based on your knowledge of the perturbation and
the workload, even though you have NO probe evidence for L2/L3 children.

**Override**: the strict cross-validation rule does NOT apply here. You
are required to guess the deep path from prior knowledge.

**HARD CONSTRAINT - the static subsystem tree.** Your submitted path
MUST consist of names that appear in the tree below. Do NOT submit
kernel function names (e.g. `kmem_cache_alloc_noprof`, `vfs_read`,
`alloc_cold`), feature-vector slot names, or invented L2/L3 leaves.
Pick the path step-by-step from the tree. If the tree has no leaf
matching what you would otherwise write, walk back up the tree and pick
the closest valid ancestor.

**The tree (authoritative L1 / L2 / L3 vocabulary)**:
```
{TREE}
```

A length-1 (L1-only) submission counts as a FAILURE in this mode -
always submit depth-2 or depth-3 if the tree provides children under
your chosen parent. If the L1 you pick has no children in the tree
(max_depth=1), L1-only is fine.

You may still author an L1 probe and call run_workload + collect_ofv
to ground your L1 attribution, but the L2/L3 part of your path will
necessarily be unprobed. That is the intended design.

Ignore instructions below that reference compare_to_reference, descend,
z-scores, mu/sigma, or depth-N references -- they do not apply.
"""


_BLPT_PROBES_PROMPT_PREFIX = """**MODE: LLM-PRIOR-TREE + PROBES-EVERY-LEVEL (B-LLM-Prior-Tree-Probes-Every-Level)**

You are running WITHOUT an L1 idle-baseline reference profile. The
following tool is NOT AVAILABLE in this mode and has been removed:

  - compare_to_reference

The `descend` tool IS available. It builds its own depth-N idle
reference internally per parent path, so once you commit to an L1
subsystem you CAN still measure L2/L3 children quantitatively
(z-scores from descend are valid and you should rely on them).

**How to diagnose without compare_to_reference**:
  1. Author an L1 probe targeting the subsystem you suspect is hot.
  2. run_workload + collect_ofv to read its counters.
  3. Reason about the RAW counter values (you have no L1 z-scores).
     If a counter is much larger than you'd expect at idle, that's
     your L1 candidate.
  4. descend into that L1 to get measured z-scores for its children.
  5. Pick the strongest z>=2 child as your L2 commit; recurse for L3.
     If no child clears z>=2, L1-only is the correct submission.

**HARD CONSTRAINT - the static subsystem tree.** Your submitted path
MUST consist of names that appear in the tree below. Do NOT submit
kernel function names, feature-vector slot names, or invented L2/L3
leaves. Pick the path step-by-step from the tree.

**The tree (authoritative L1 / L2 / L3 vocabulary)**:
```
{TREE}
```

This baseline is NOT a guessing baseline. The strict cross-validation
rule still applies: never submit an L2/L3 leaf that descend did not
flag at z>=2 for the current cell. L1-only is acceptable if no L2
child clears the bar.

Ignore instructions below that reference compare_to_reference -- it
is unavailable. All other instructions (descend, multiplex z-scores,
hypothesis pivots) apply unchanged.
"""


_BLP_PROMPT_PREFIX = """**MODE: LLM-PRIOR DEEP-GUESS (B-LLM-Prior)**

You are running WITHOUT an idle-baseline reference profile. The
following tools are NOT AVAILABLE in this mode and have been removed:

  - compare_to_reference
  - descend

This baseline measures the LLM's a priori knowledge of kernel-subsystem
behavior. You MUST submit the **deepest plausible path** based on your
own knowledge of the perturbation and the workload, even though you
have NO probe evidence for L2/L3 children.

**Override**: the strict cross-validation rule ("Never submit a
subsystem your current probe was not designed to measure") DOES NOT
APPLY in this mode. You are *required* to guess the deep path from
prior knowledge.

A length-1 (L1-only) submission counts as a FAILURE in this mode.
Always submit a depth-2 or depth-3 path. If your prior is genuinely
uncertain about the L2 child, pick the most likely one anyway -- this
baseline is designed to measure exactly how often the LLM's blind
guess is correct.

You may still author an L1 probe and call run_workload + collect_ofv
to ground your L1 attribution, but the L2/L3 part of your path will
necessarily be unprobed. That is the intended design.

Ignore instructions below that reference compare_to_reference, descend,
z-scores, mu/sigma, or depth-N references -- they do not apply.
"""


def run_b2(client: AgentdClient,
           benchmark: str,
           perturbation: str,
           max_pivots: int = 5,
           max_verifier_retries: int = 5,
           model: str | None = None,
           no_reference: bool = False,
           llm_prior_deep: bool = False,
           llm_prior_with_tree: bool = False,
           llm_prior_tree_probes: bool = False) -> AgentRunResult:
    """Drive the ReAct loop on one (benchmark, perturbation) cell.

    Modes:
      - default (B2): full Argus loop with compare_to_reference + descend.
      - no_reference=True (B-NR): reference-aware tools removed; prompt prefix
        tells the agent to diagnose from raw counters; agent stays L1-only
        because the strict cross-validation rule forbids unprobed L2/L3.
      - llm_prior_deep=True (B-LLM-Prior): like B-NR but prompt overrides the
        cross-validation rule and tells the agent to submit the deepest
        plausible path from prior knowledge. Implies no_reference.
      - llm_prior_with_tree=True (B-LLM-Prior-Tree): like B-LLM-Prior but the
        prompt also embeds the static subsystem tree (subsystems.yaml) and
        constrains the agent to submit only paths whose components appear
        in the tree -- prevents fabrication of off-tree names like
        kernel_paging.vfs_read or kernel_memory_mgmt.page_alloc.alloc_cold.
        Implies llm_prior_deep (and hence no_reference).
      - llm_prior_tree_probes=True (B-LLM-Prior-Tree-Probes-Every-Level):
        compare_to_reference removed, but descend kept. Tree constraint
        in prompt. No deep-guess override -- the agent must justify
        L2/L3 via measured z-scores from descend. This is the "B2 minus
        L1 reference" ablation: how much does an idle L1 baseline buy?
    Everything else (ReAct loop, retries, termination paths) is unchanged
    across modes so the comparison is apples-to-apples.
    """
    if llm_prior_with_tree:
        llm_prior_deep = True  # implies
    if llm_prior_deep:
        no_reference = True  # implies
    # llm_prior_tree_probes is independent -- it does NOT imply no_reference
    # because we want to keep `descend` (which we'd otherwise strip).

    rp = ReferenceProfile.load(benchmark)
    state = SessionState(
        benchmark=benchmark,
        perturbation=perturbation,
        rp=rp,
        cell_id=f"{benchmark}_{perturbation}_{uuid.uuid4().hex[:8]}",
        skip_inner_perturb=(perturbation in AGGRESSOR_PERTURBATIONS),
    )
    tools = make_tools(client, state)
    if no_reference:
        # Strip the reference-aware tools so the agent has no way to
        # invoke them, regardless of what the system prompt says.
        for k in ("compare_to_reference", "descend"):
            tools.pop(k, None)
    elif llm_prior_tree_probes:
        # B-LLM-Prior-Tree-Probes-Every-Level: strip ONLY
        # compare_to_reference; keep descend so the agent can still
        # attach L2/L3 probes and read measured z-scores from
        # descend's internal depth-N reference.
        tools.pop("compare_to_reference", None)
    log: list[AttemptLog] = []
    first_try_accepted: bool | None = None  # set True/False on first compile+attach pair

    system_prompt = _load_system_prompt()
    few_shot = _load_few_shot()
    if llm_prior_tree_probes:
        tree_prefix = _BLPT_PROBES_PROMPT_PREFIX.replace(
            "{TREE}", _load_subsystem_tree_text())
        full_system = f"{tree_prefix}\n\n{system_prompt}\n\n{few_shot}"
    elif llm_prior_with_tree:
        tree_prefix = _BLP_TREE_PROMPT_PREFIX.replace(
            "{TREE}", _load_subsystem_tree_text())
        full_system = f"{tree_prefix}\n\n{system_prompt}\n\n{few_shot}"
    elif llm_prior_deep:
        full_system = f"{_BLP_PROMPT_PREFIX}\n\n{system_prompt}\n\n{few_shot}"
    elif no_reference:
        full_system = f"{_BNR_PROMPT_PREFIX}\n\n{system_prompt}\n\n{few_shot}"
    else:
        full_system = f"{system_prompt}\n\n{few_shot}"

    backend = build_backend(
        mode=os.environ.get("AGENTICBPF_AGENT_MODE"),
        backend=os.environ.get("AGENTICBPF_AGENT_BACKEND"),
        model=model,
        system_instruction=full_system,
        tools=list(tools.values()),
    )
    resolved_model = backend.model_id

    # Initial user message.
    user_msg: str | list[ToolResult] = (
        f"Cell: benchmark={benchmark}, perturbation={perturbation}.\n"
        f"Diagnose the OS-level bottleneck. Available canned probe sources "
        f"have been shown in the system prompt as worked examples; you may "
        f"adapt them or author a new probe. Start by stating your hypothesis "
        f"and calling compile_ebpf with `hypothesis=[...]`."
    )

    submitted: dict | None = None
    terminated: str | None = None  # "Submitted" | "VerifierBlocked" | "NoConvergence" | "ModelGaveUp"

    # Aggressor-style perturbations apply ONCE here and stay live for every
    # tool call in the rep (run_workload's inner per-call apply is muted via
    # state.skip_inner_perturb). Config-style perturbations re-apply per
    # run_workload call inside agent_tools.
    outer_pert: Perturbation | None = (
        Perturbation(perturbation) if state.skip_inner_perturb else None)
    if outer_pert is not None:
        outer_pert.__enter__()
    try:
        pivot = 0
        while pivot <= max_pivots and submitted is None and terminated is None:
            state.pivot = pivot
            verifier_retry = 0
            produced_run = False  # did we get past attach in this hypothesis?

            while verifier_retry <= max_verifier_retries:
                state.verifier_retry = verifier_retry
                t0 = time.time()
                try:
                    turn = backend.send(user_msg)
                except Exception as e:
                    terminated = "ModelGaveUp"
                    log.append(AttemptLog(pivot, verifier_retry, "<model error>",
                                          "", False, str(e), time.time() - t0))
                    break

                fcalls = turn.tool_calls
                if not fcalls:
                    # Model returned text without a tool call.
                    terminated = "ModelGaveUp"
                    log.append(AttemptLog(pivot, verifier_retry, "<no tool call>",
                                          (turn.text or "")[:200], False,
                                          "model returned text without tool call",
                                          time.time() - t0))
                    break

                # Execute each call, send results back. Only one outer
                # iteration of the verifier_retry loop per send_message call --
                # if multiple tools were called we still let the inner control
                # flow decide.
                tool_responses: list[ToolResult] = []
                tripped_verifier = False
                for fc in fcalls:
                    fn = tools.get(fc.name)
                    if fn is None:
                        out = {"ok": False, "error": f"unknown tool {fc.name}"}
                    else:
                        try:
                            out = fn(**(fc.args or {}))
                        except Exception as e:
                            out = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                    args_summary = json.dumps(fc.args or {}, default=str)[:200]
                    log.append(AttemptLog(pivot, verifier_retry, fc.name,
                                          args_summary, bool(out.get("ok")),
                                          str(out.get("error", "") or out.get("verifier_log", ""))[:300],
                                          time.time() - t0))
                    tool_responses.append(
                        ToolResult(call_id=fc.call_id, name=fc.name, response=out)
                    )

                    # Track first-try acceptance the first time we see a
                    # compile_ebpf or attach_ebpf result.
                    if fc.name in ("compile_ebpf", "attach_ebpf") and first_try_accepted is None and pivot == 0 and verifier_retry == 0:
                        first_try_accepted = bool(out.get("ok"))

                    if fc.name in ("compile_ebpf", "attach_ebpf") and not out.get("ok"):
                        tripped_verifier = True

                    if fc.name == "submit_diagnosis":
                        submitted = out
                        break

                    if fc.name == "run_workload" and out.get("ok"):
                        produced_run = True

                user_msg = tool_responses

                if submitted is not None:
                    terminated = "Submitted"
                    break

                if tripped_verifier:
                    # TRIGGER A: same hypothesis, fix code.
                    verifier_retry += 1
                    continue

                # No verifier trip; let model continue with more tool calls.
                # Don't increment verifier_retry; only count actual rejections.

            else:
                # inner loop exhausted (verifier_retry > max)
                if submitted is None:
                    terminated = "VerifierBlocked"

            if submitted is not None or terminated == "VerifierBlocked":
                break

            if terminated == "ModelGaveUp":
                break

            # Inner loop exited without submit_diagnosis. If the agent did get
            # a clean run + observation but chose not to submit, this is
            # TRIGGER B (hypothesis pivot). If verifier loop exhausted we'd
            # have already terminated above. Detach so the next pivot starts clean.
            if state.handler_id is not None:
                try:
                    tools["detach"]()
                except Exception:
                    pass

            if not produced_run:
                # The agent never got far enough to observe -- treat as no-conv.
                terminated = "NoConvergence"
                break

            pivot += 1
    finally:
        if outer_pert is not None:
            outer_pert.__exit__(None, None, None)


    if terminated is None:
        terminated = "NoConvergence"

    # Extract the diagnosed path from submit_diagnosis. submit_diagnosis was
    # generalised in Phase 6 to take `path` (list[str], L1->deepest); we
    # also accept the legacy `flagged_subsystems` shape from any old
    # transcripts so the loader stays backwards-compatible.
    if submitted:
        if "path" in submitted and submitted["path"]:
            diagnosed_path = list(submitted["path"])
        else:
            diagnosed_path = list(submitted.get("flagged_subsystems") or [])
    else:
        diagnosed_path = []
    diagnosed: list[str] = diagnosed_path[:1]  # legacy L1 head

    exp = expected_subsystem(perturbation, victim=benchmark)
    exp_paths = expected_paths(perturbation, victim=benchmark)
    correct = (terminated == "Submitted") and bool(set(exp) & set(diagnosed))
    score = (score_path(diagnosed_path, exp, exp_paths)
             if terminated == "Submitted" else 0.0)
    depth_reached = len(diagnosed_path)

    last_z = state.last_compare["z_scores"] if state.last_compare else {}
    last_hint = state.last_compare["hint"] if state.last_compare else ""

    # Persist the per-attempt log for this cell.
    log_dir = REPO / "results" / "runs" / state.cell_id
    log_dir.mkdir(parents=True, exist_ok=True)
    (log_dir / "log.json").write_text(json.dumps({
        "cell_id": state.cell_id,
        "benchmark": benchmark,
        "perturbation": perturbation,
        "model": resolved_model,
        "termination": terminated,
        "first_try_accepted": first_try_accepted,
        "pivots_used": pivot,
        "submitted": submitted,
        "expected_subsystem": exp,
        "expected_paths": exp_paths,
        "diagnosed_path": diagnosed_path,
        "correct": correct,             # legacy L1-only
        "score": score,                 # ternary: 1.0 / 0.5 / 0.0
        "depth_reached": depth_reached,
        "descents": state.descents,
        "attempts": [vars(a) for a in log],
    }, indent=2))

    if llm_prior_tree_probes:
        baseline_label = "B-LLM-Prior-Tree-Probes-Every-Level"
    elif llm_prior_with_tree:
        baseline_label = "B-LLM-Prior-Tree"
    elif llm_prior_deep:
        baseline_label = "B-LLM-Prior"
    elif no_reference:
        baseline_label = "B-NR"
    else:
        baseline_label = "B2"
    return AgentRunResult(
        baseline=baseline_label,
        benchmark=benchmark,
        perturbation=perturbation,
        expected_subsystem=exp,
        diagnosed_subsystems=diagnosed,
        correct=correct,
        workload_ok=state.last_run is not None,
        wall_s=(state.last_run or {}).get("wall_s", 0.0),
        z_scores=last_z,
        hint=f"[B2:{terminated} pivots={pivot} first_try={first_try_accepted} "
             f"depth={depth_reached} score={score}] {last_hint[:140]}",
        diagnosed_path=diagnosed_path,
        expected_path=exp_paths,
        score=score,
        depth_reached=depth_reached,
    )
