"""Tool functions exposed to the ReAct agent.

Each tool is a thin Python function with type-annotated args + return; the
google-genai SDK auto-builds the JSON schema from the signatures. Each
function also returns a dict (NOT raises) on every error path so the model
sees the failure as data and can decide how to recover.

Lifecycle: created fresh per cell with `make_tools(...)`, which closes over
the AgentdClient + benchmark + perturbation + reference profile + a session
state dict that tracks the in-flight handler_id and the agent's most-recent
stated hypothesis.
"""

from __future__ import annotations

import os
import shlex
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .compare import compare, compare_multiplex
from .feature_vector import LABELS
from .ipc_client import AgentdClient, IpcError
from .multiplex import (compile_multiplex, run_multiplex_against, spec_for_root,
                        spec_for_descent)
from .perturb import Perturbation
from .reference import (ReferenceProfile, extract_work_units, normalize,
                        reference_for_descent)
from .subsystem_tree import find as find_node, load_tree
from .workload import Workload

REPO = Path(__file__).resolve().parents[2]


@dataclass
class SessionState:
    """Mutable state shared across tool calls in a single cell."""
    benchmark: str
    perturbation: str
    rp: ReferenceProfile
    handler_id: int | None = None
    last_compile_ok: bool | None = None
    last_attach_ok: bool | None = None
    last_run: dict | None = None
    last_ofv_rates: list[float] | None = None
    last_compare: dict | None = None
    current_hypothesis: list[str] = field(default_factory=list)
    # Cumulative descent log: each successful descend() pushes
    # {parent_path, observed, z_scores, flagged, hint}. The agent
    # may descend multiple times per cell (different L1s, or L2->L3).
    descents: list[dict] = field(default_factory=list)
    # Top-3 L1 subsystem names by z-score from the most recent ranking
    # signal (preferred: root-level descend([]); fallback:
    # compare_to_reference). Used by descend() to enforce top-k discipline:
    # depth-1 L1 descents must target one of these. Empty set means
    # "no ranking yet" -- the agent must call compare_to_reference or
    # descend([]) first.
    top_k_l1s: set[str] = field(default_factory=set)
    runs_dir: Path = field(default_factory=lambda: REPO / "agent_handlers" / "runs")
    cell_id: str = ""
    pivot: int = 0
    verifier_retry: int = 0
    # If the perturbation is an aggressor-style co-runner, run_b2 applies
    # it ONCE at rep entry and keeps it running across all tool calls.
    # When True, run_workload skips the per-call `with Perturbation(...)`
    # so the aggressor isn't churned (start/stop) per observation.
    skip_inner_perturb: bool = False
    # Monotonic counter incremented on every compile_ebpf call. Lets multi-
    # probe cross-validation (option-(a) from the four-state hint) write each
    # probe to a distinct file rather than clobbering candidate.bpf.c.
    compile_idx: int = 0


def _bpf_cflags() -> list[str]:
    """Mirror Makefile BPF_CFLAGS so on-demand candidate compiles match the
    canned/seed builds. We hard-code instead of shelling out to make so a
    bad PATH or stale Makefile doesn't break candidate compilation."""
    inc = REPO / "src" / "include"
    libbpf_inc = REPO / "build" / "libbpf" / "destdir" / "usr" / "include"
    return [
        "-g", "-O2", "-target", "bpf",
        "-D__TARGET_ARCH_x86",
        f"-I{inc}",
        f"-I{libbpf_inc}",
        f"-I{libbpf_inc / 'bpf'}",
        "-Wall", "-Wno-unused-function",
    ]


def make_tools(client: AgentdClient, state: SessionState) -> dict[str, Any]:
    """Build the 7 tool functions, each closing over `client` + `state`."""

    # ---- compile_ebpf ----------------------------------------------------
    def compile_ebpf(code: str, hypothesis: list[str]) -> dict:
        """Compile an eBPF C program with clang -target bpf. The agent must
        ALSO state its current hypothesis -- one or more L1 subsystem names
        from the active subsystem tree (kernel_paging, kernel_memory_mgmt,
        kernel_scheduler, block_io, network_stack, vfs_filesystems, sync_rcu,
        amdgpu) -- so subsequent compare_to_reference calls can produce a
        hint relative to it. amdgpu is only listed when the host has an
        AMD GPU (capability-gated).

        Returns:
          {ok: True, obj_path}             on success
          {ok: False, error}               on compile failure
        """
        state.current_hypothesis = list(hypothesis)
        # Per-call counter so multi-probe cross-validation (option-a from the
        # strict-superset hint) produces distinct files rather than
        # overwriting candidate.bpf.c on each authoring round.
        state.compile_idx += 1
        cell_dir = state.runs_dir / state.cell_id / f"pivot{state.pivot}" / f"retry{state.verifier_retry}"
        cell_dir.mkdir(parents=True, exist_ok=True)
        src = cell_dir / f"candidate_{state.compile_idx:03d}.bpf.c"
        obj = cell_dir / f"candidate_{state.compile_idx:03d}.bpf.o"
        src.write_text(code)
        cmd = ["clang", *_bpf_cflags(), "-c", str(src), "-o", str(obj)]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
        except subprocess.TimeoutExpired:
            state.last_compile_ok = False
            return {"ok": False, "error": "clang timeout (20s)"}
        if r.returncode != 0:
            state.last_compile_ok = False
            return {"ok": False, "error": (r.stderr or r.stdout)[-3000:]}
        # llvm-strip is best-effort; not fatal.
        subprocess.run(["llvm-strip", "-g", str(obj)], capture_output=True)
        state.last_compile_ok = True
        return {"ok": True, "obj_path": str(obj)}

    # ---- attach_ebpf -----------------------------------------------------
    def attach_ebpf(obj_path: str, slots: list[dict]) -> dict:
        """Load an .bpf.o into the kernel via agentd, run the verifier, and
        attach its programs into the dispatcher's PROG_ARRAY at the named
        slots. `slots` is a list of {prog: str, kind: "kprobe"|"tracepoint",
        idx: int}. Verifier rejection counts as TRIGGER A (same hypothesis,
        fix the code).

        Returns:
          {ok: True, handler_id, programs: [{name, sec, kind}]} on success
          {ok: False, verifier_log}                              on rejection
        """
        try:
            handler_id, progs = client.load_handler(obj_path)
        except IpcError as e:
            state.last_attach_ok = False
            return {"ok": False, "verifier_log": (e.detail or e.error)[-3000:]}
        try:
            client.attach(handler_id, slots)
        except IpcError as e:
            client.detach(handler_id)
            state.last_attach_ok = False
            return {"ok": False, "verifier_log": (e.detail or e.error)[-3000:]}
        state.handler_id = handler_id
        state.last_attach_ok = True
        return {
            "ok": True,
            "handler_id": handler_id,
            "programs": [
                {"name": p.name, "sec": p.sec, "kind": p.kind} for p in progs
            ],
        }

    # ---- run_workload ----------------------------------------------------
    def run_workload() -> dict:
        """Run the cell's benchmark (`{state.benchmark}`) under perturbation
        `{state.perturbation}`. Returns the perf-stat feature vector + wall
        time + stdout tail. Callable with OR without a probe attached:
        without a probe, you still get the perf-stat FV (preferred first
        action — observe before hypothesizing). With a probe attached, the
        same call also feeds counters into agent_output for collect_ofv()."""
        w = Workload(state.benchmark)
        pre_pid = w.spec.get("pre_perturbation")
        pre_ctx = Perturbation(pre_pid) if pre_pid else None
        if pre_ctx:
            pre_ctx.__enter__()
        try:
            if state.perturbation == "none" or state.skip_inner_perturb:
                # skip_inner_perturb: the aggressor was already applied by
                # run_b2 at rep entry and stays running across all tool calls.
                r = w.run(client)
            else:
                with Perturbation(state.perturbation):
                    r = w.run(client)
        finally:
            if pre_ctx:
                pre_ctx.__exit__(None, None, None)
        if not r.ok:
            return {"ok": False, "error": r.error[-2000:]}
        pattern = w.spec.get("work_units_pattern", r"work_units=([0-9]+)")
        wu = extract_work_units(r.stdout_tail, pattern)
        rates = normalize(r.fv, wu)
        state.last_run = {"wall_s": r.wall_s, "wu": wu}
        state.last_ofv_rates = rates
        return {
            "ok": True,
            "wall_s": round(r.wall_s, 3),
            "perf_fv": dict(zip(LABELS, r.fv.v)),
            "perf_fv_per_work_unit": {LABELS[i]: round(rates[i], 6) for i in range(len(rates))},
            "stdout_tail": r.stdout_tail[-500:],
        }

    # ---- collect_ofv -----------------------------------------------------
    def collect_ofv() -> dict:
        """Read the agent_output map (16 u64 counters per CPU, summed). Use
        only AFTER run_workload; the eBPF probe must have been attached and
        actively collecting during the run."""
        if state.handler_id is None:
            return {"ok": False, "error": "no handler attached"}
        try:
            counters = client.output(state.handler_id)
        except IpcError as e:
            return {"ok": False, "error": e.error}
        nz = [(i, v) for i, v in enumerate(counters) if v > 0]
        return {"ok": True, "counters": counters, "nonzero": nz}

    # ---- compare_to_reference -------------------------------------------
    def compare_to_reference() -> dict:
        """Compare the most recent run_workload's perf-stat output to the
        saved idle reference. Returns z-scores, the flagged subsystem list,
        and a HINT that steers the next decision:

          flagged=[]                          -> hypothesis was wrong; pivot
                                                 to a different hook/subsystem
          flagged disjoint from your hypoth   -> author NEW probe at flagged
                                                 subsystem to cross-validate
          flagged superset of your hypoth     -> submit_diagnosis is appropriate

        The agent's currently-stated hypothesis (set in compile_ebpf) is used
        to compute the hint. Ignore the hint at your own risk; it encodes
        the strict cross-validation rule from the system prompt.
        """
        if state.last_ofv_rates is None:
            return {"ok": False, "error": "call run_workload first"}
        res = compare(state.rp, state.last_ofv_rates,
                      agent_hypothesis=state.current_hypothesis or None)
        state.last_compare = {
            "z_scores": res.z_scores,
            "flagged": res.flagged_subsystems,
            "hint": res.hint,
        }
        # Refresh top-k L1 ranking from compare_to_reference's z-scores.
        # A subsequent root-level descend() may overwrite this with a
        # T_x-based ranking; either source is acceptable to descend().
        if res.z_scores:
            top3 = sorted(res.z_scores.items(),
                          key=lambda kv: abs(float(kv[1])), reverse=True)[:3]
            state.top_k_l1s = {k for k, _ in top3}
        return {
            "ok": True,
            "z_scores": res.z_scores,
            "flagged_subsystems": res.flagged_subsystems,
            "hint": res.hint,
        }

    # ---- detach ----------------------------------------------------------
    def detach() -> dict:
        """Detach + close the currently-attached probe so a new one can be
        loaded. Required between hypothesis pivots."""
        if state.handler_id is None:
            return {"ok": True, "note": "no handler to detach"}
        try:
            client.detach(state.handler_id)
        except IpcError as e:
            return {"ok": False, "error": e.error}
        state.handler_id = None
        return {"ok": True}

    # ---- descend ---------------------------------------------------------
    def descend(parent_path: list[str], hypothesis: list[str]) -> dict:
        """Run a multiplex eBPF probe that hooks all children of `parent_path`
        at once and returns per-slot z-scores against an idle depth-N
        reference. Use this AFTER you have an L1 diagnosis from
        compare_to_reference: descend into a flagged L1 subsystem to find
        out WHICH L2 child of that subsystem is actually hot.

        `parent_path` is the dot-path to a non-leaf node in the static
        subsystem tree. Examples:
          []                                  - L1 T_x: one slot per
                                                 active L1 subsystem,
                                                 each hooking the L1's
                                                 representative kprobe.
                                                 Use as the FIRST descent
                                                 to catch latency-driven
                                                 bottlenecks that the
                                                 perf-stat OFV misses
                                                 (count-blind, e.g.
                                                 cache pollution).
          ['kernel_paging']                   - depth-2 detection
          ['kernel_paging', 'anon_fault']     - depth-3 (only valid for
                                                 subsystems with max_depth=3)
          ['kernel_memory_mgmt', 'reclaim']   - depth-3 inside mm
        Use exactly the names from the static subsystem tree (the system
        prompt enumerates them).

        `hypothesis` is the list of CHILD names you predict will be flagged
        (e.g. for parent_path=['kernel_paging'] you might guess
        ['anon_fault']). Used to compute the pivot/submit hint relative to
        your prediction. Same semantics as compile_ebpf's hypothesis but
        about children of `parent_path` rather than top-level subsystems.

        The depth-N reference is built lazily on first descent into
        (benchmark, parent_path) -- this triggers ~30 idle reruns of the
        workload and is slow on first call (5-30s for cheap workloads,
        more for heavy ones). Subsequent descents into the same path are
        cached.

        Returns:
          {ok: True, parent_path, labels, z_scores, flagged, hint, n_attached}
          {ok: False, error}
        """
        try:
            tree = load_tree()
            parent_tup = tuple(parent_path)
            if not parent_tup:
                # Root-level L1 T_x: one slot per active L1 subsystem,
                # hooked via that L1's representative kprobe.
                spec = spec_for_root(tree)
            else:
                parent = find_node(tree, parent_tup)
                if parent is None:
                    return {"ok": False, "error":
                        f"parent_path {parent_path} not in active subsystem tree"}
                if parent.is_leaf:
                    return {"ok": False, "error":
                        f"{parent_path} is a leaf (no children to descend into)"}
                # Top-k discipline (added 2026-05-16): a depth-1 L1 descent
                # is only legal if the L1 is among the top-3 L1s by anomaly
                # strength, computed from the most recent ranking signal
                # (compare_to_reference's z_scores, or a prior root-level
                # descend's T_x z_log_scores). Depth-2+ descents (e.g.
                # ['kernel_paging', 'anon_fault']) are unaffected -- their
                # parent L1 has already passed the gate at depth-1.
                if (len(parent_tup) == 1
                        and state.top_k_l1s
                        and parent_tup[0] not in state.top_k_l1s):
                    return {"ok": False, "error":
                        f"descend rejected: {parent_tup[0]!r} is not in the "
                        f"top-3 L1s by anomaly strength "
                        f"({sorted(state.top_k_l1s)}). Descend into one of "
                        f"those first. The top-3 comes from the most recent "
                        f"compare_to_reference or root-level descend([])."}
                spec = spec_for_descent(parent_tup, parent)
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}

        # Lazy build/load the depth-N idle reference for (benchmark, parent).
        try:
            mx_ref = reference_for_descent(client, state.benchmark, parent_tup,
                                           verbose=False)
        except Exception as e:
            return {"ok": False,
                    "error": f"depth-{len(parent_tup)+1} reference build "
                             f"failed: {type(e).__name__}: {e}"}

        # Run multiplex.run on the perturbed cell (same perturbation as
        # this whole session). multiplex_runner.cpp does the libbpf
        # auto-attach + workload exec + PERCPU_ARRAY readback in one RPC.
        w = Workload(state.benchmark)
        argv = w._argv()
        pre_pid = w.spec.get("pre_perturbation")
        pre_ctx = Perturbation(pre_pid) if pre_pid else None
        if pre_ctx:
            pre_ctx.__enter__()
        try:
            if state.perturbation == "none" or state.skip_inner_perturb:
                mx_run = run_multiplex_against(spec, argv, client)
            else:
                with Perturbation(state.perturbation):
                    mx_run = run_multiplex_against(spec, argv, client)
        except Exception as e:
            return {"ok": False, "error": f"multiplex.run: {type(e).__name__}: {e}"}
        finally:
            if pre_ctx:
                pre_ctx.__exit__(None, None, None)

        if not mx_run.ok:
            return {"ok": False,
                    "error": f"multiplex run failed: {mx_run.error or 'workload exit nonzero'}",
                    "attach_errors": mx_run.attach_errors}

        observed = list(mx_run.per_slot_per_wu().values())
        # T_x: per-slot kernel-ns per work_unit, captured via the
        # kprobe+kretprobe pairs in the new multiplex template. May be
        # an all-zeros list on legacy BPF objects or for slots whose
        # probes are tracepoint-only; compare_multiplex handles that
        # case by falling back to count-only z.
        observed_sum_ns = list(
            mx_run.per_slot_sum_ns_per_wu().values()
        ) if mx_run.sum_ns else None
        try:
            cmp = compare_multiplex(
                mx_ref, observed,
                parent_path=parent_tup,
                agent_hypothesis=list(hypothesis or []) or None,
                observed_sum_ns_per_wu=observed_sum_ns,
            )
        except Exception as e:
            return {"ok": False, "error": f"compare_multiplex: {type(e).__name__}: {e}"}

        record = {
            "parent_path": list(parent_tup),
            "observed_per_wu": dict(zip(cmp.labels, observed)),
            "z_scores": cmp.z_scores,
            "flagged": cmp.flagged,
            "hint": cmp.hint,
            "n_attached": mx_run.n_attached,
            "n_programs": mx_run.n_programs,
            "attach_errors": mx_run.attach_errors,
            # T_x decomposition + metric tag (informational; the agent
            # can cite z_count vs z_log when explaining its commit).
            "metric": cmp.metric,
            "z_count_scores": cmp.z_count_scores,
            "z_log_scores": cmp.z_log_scores,
            "sum_ns_per_wu": cmp.sum_ns_per_wu,
        }
        state.descents.append(record)
        # After a successful root-level descent, refresh top-k from its
        # T_x ranking (this overrides the perf-stat top-k that
        # compare_to_reference set). For non-root descents, leave top_k_l1s
        # alone -- the agent should still consider all L1s the original
        # ranking endorsed.
        if not parent_tup:
            z = cmp.z_log_scores or cmp.z_scores or {}
            if z:
                top3 = sorted(z.items(),
                              key=lambda kv: abs(float(kv[1])), reverse=True)[:3]
                state.top_k_l1s = {k for k, _ in top3}
        return {"ok": True, **record}

    # ---- submit_diagnosis ------------------------------------------------
    # This one is special: when called, the loop terminates (Submitted).
    # We still return a dict for the FunctionResponse contract.
    def submit_diagnosis(path: list[str], rationale: str) -> dict:
        """TERMINAL. Call this exactly once when you have evidence for a
        diagnosis. `path` is the deepest tree path your evidence supports,
        as a list of node names from L1 down. Examples:

          ['kernel_paging']                       - L1-only diagnosis
                                                     (no descent or descent
                                                     showed no flagged child)
          ['kernel_paging', 'anon_fault']         - L2 diagnosis
          ['kernel_paging', 'anon_fault','minor'] - L3 diagnosis

        Use the deepest path your descent evidence supports. Submitting at
        L1 when L2 was conclusive under-reports your insight; submitting at
        L2 without descent evidence claims more than your data shows.

        `rationale` is a 1-3 sentence explanation in plain English citing
        the kernel code path and the z-scores from compare_to_reference
        (L1) and descend() (L2/L3) that drove your conclusion.

        After this returns, the orchestrator scores the diagnosis against
        ground truth (which the agent does not see) and ends the cell."""
        p = list(path)
        rat = rationale
        # Deterministic abstain enforcement (added 2026-05-15). The system
        # prompt instructs the agent to submit ["none"] when every
        # descent returned flagged=[], but LLMs are non-deterministic
        # about this -- occasionally they still submit the perf-stat
        # top L1 anyway. When the agent ran at least one multiplex
        # descent and every one returned flagged=[], override the
        # submitted path to ["none"]. This makes structural-null cells
        # reproducible across reps.
        all_descents_empty = (
            len(state.descents) > 0
            and all(not d.get("flagged") for d in state.descents)
        )
        if all_descents_empty and len(p) <= 1 and p != ["none"]:
            rat = (f"[abstain-override: {len(state.descents)} descents all "
                   f"flagged=[]; agent submitted {path}] " + rationale)
            p = ["none"]

        # Deterministic depth-mismatch handling (added 2026-05-16, refined
        # 2026-05-16). The agent submits at L1; L2 descent into that L1
        # returned flagged=[] (every L2 child below the relative-noise
        # floor). Two cases:
        #
        #   (a) The L1 itself was NOT flagged by the root descent. Then
        #       there's no evidence at any level for this subsystem ->
        #       abstain. Catches process-startup outliers / aggressor leak
        #       producing a stray L1 spike that doesn't pass the L1
        #       noise floor.
        #
        #   (b) The L1 WAS flagged by the root descent but L2 leaves are
        #       all below the relative floor. Then L1 is strong evidence
        #       of subsystem-level activity even though the probe set
        #       can't decompose it (e.g., THP-aggressor's effect lives in
        #       mmap_lock / direct_compaction code paths that aren't in
        #       the L2 multiplex). KEEP the L1 commit -- abstaining here
        #       would discard real evidence.
        if p != ["none"] and len(p) == 1:
            sub_descents = [d for d in state.descents
                            if list(d.get("parent_path") or []) == [p[0]]]
            if sub_descents and all(not d.get("flagged") for d in sub_descents):
                # Check whether the root L1 descent flagged this subsystem.
                root_descent = next(
                    (d for d in state.descents
                     if not (d.get("parent_path") or [])),
                    None,
                )
                root_flagged = list(
                    (root_descent or {}).get("flagged") or []
                )
                if p[0] not in root_flagged:
                    rat = (f"[depth-mismatch-abstain: L1={p[0]} submitted "
                           f"but L1 not in root_flagged={root_flagged} and "
                           f"{len(sub_descents)} sub-descents all "
                           f"flagged=[]] " + rat)
                    p = ["none"]
                # else: L1 was flagged; KEEP the depth-1 commit as the
                # honest "subsystem activity present, leaf not decomposable
                # with current probe set" answer.

        # Deterministic flagged-leaf enforcement (added 2026-05-15). The
        # prompt instructs the agent to commit to a slot only if it
        # appears in the descent's `flagged` list (raw z_log_scores are
        # informational and may include slots blocked by the noise
        # floor). LLMs occasionally still pick the highest-z slot
        # regardless. When the agent's submitted leaf is NOT in the
        # corresponding descent's flagged list, override to the
        # highest-z_log slot that IS flagged. Preserves prefix (L1, L2)
        # so partial credit isn't lost.
        if p != ["none"] and len(p) >= 2 and state.descents:
            parent_tup = list(p[:-1])
            leaf = p[-1]
            for d in reversed(state.descents):
                if d.get("parent_path") == parent_tup:
                    flagged = list(d.get("flagged", []) or [])
                    if flagged and leaf not in flagged:
                        z_log_scores = d.get("z_log_scores") or {}
                        best = max(
                            flagged,
                            key=lambda s: float(z_log_scores.get(s, 0.0)),
                        )
                        rat = (f"[flagged-leaf-override: agent picked "
                               f"{leaf!r} at parent={parent_tup}, but "
                               f"flagged={flagged}; using {best!r}] " + rat)
                        p = parent_tup + [best]
                    break

        return {
            "ok": True,
            "submitted": True,
            "path": p,
            "flagged_subsystems": p[:1],  # legacy compat: L1 head
            "rationale": rat,
        }

    return {
        "compile_ebpf":          compile_ebpf,
        "attach_ebpf":           attach_ebpf,
        "run_workload":          run_workload,
        "collect_ofv":           collect_ofv,
        "compare_to_reference":  compare_to_reference,
        "descend":               descend,
        "detach":                detach,
        "submit_diagnosis":      submit_diagnosis,
    }
