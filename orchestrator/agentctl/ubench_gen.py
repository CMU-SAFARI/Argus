"""LLM-driven leaf microbenchmark generator (Plan Step 4).

For each leaf in the static subsystem tree, this module:
  1. Asks an LLM (via chat_backend) to author C source + Makefile + run.sh
     that fires *that leaf's* kernel-probe targets per work unit while not
     firing sibling-leaf probes.
  2. Compiles the candidate.
  3. Runs ubench_calibrate.calibrate() under ftrace function tracer to
     verify hit/miss thresholds.
  4. On calibration failure, re-prompts with the failure summary; up to
     K=4 rounds.
  5. On success, writes the files under benchmarks/<microbench>/, optionally
     updates benchmarks/manifest.yaml.

The yield rate (accepted / generated) is the headline methodology
number for the paper.

Usage:
  from .ubench_gen import generate_for_leaf, generate_for_tree
  generate_for_leaf(parent_path=("kernel_paging", "anon_fault", "minor"),
                    benchmarks_root=Path("benchmarks"))

Or end-to-end (every leaf, idempotent):
  python -m orchestrator.agentctl.ubench_gen --parent kernel_paging \\
      --max-rounds 4

CLI flags:
  --parent <L1>            : only generate leaves under this L1
  --leaf <microbench_name> : only this leaf
  --dry-run                : print the prompts; don't call the LLM
  --max-rounds K           : retry budget per leaf (default 4)
  --thresh-target N        : target_hits/work_unit threshold (default 10)
  --thresh-nontarget N     : max sibling_hits/work_unit (default 2)
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from .chat_backend import build_backend
from .subsystem_tree import Node, find, iter_leaves, load_tree
from .ubench_calibrate import CalibrationResult, calibrate

REPO = Path(__file__).resolve().parents[2]
SYSTEM_INSTRUCTION = (
    "You are an expert Linux kernel engineer authoring tiny user-space "
    "C programs whose runtime is dominated by ONE specific kernel code "
    "path. The output must compile with `cc -O2 -Wall` on a stock Linux "
    "host and produce a short stdout that ends with a line "
    "`work_units=<N>` where <N> is an integer. Reply with THREE fenced "
    "code blocks in this order: a `c` block (the source), a `make` block "
    "(the Makefile), a `bash` block (run.sh). No prose between or after "
    "the blocks."
)


@dataclass
class LeafSpec:
    """All inputs needed to prompt the LLM for one leaf."""
    parent_path: tuple[str, ...]   # full path including the leaf name
    leaf_name: str                 # last element of parent_path
    microbench_name: str           # benchmark dir name
    target_kprobes: list[str]      # bare-symbol kprobes for the leaf
    target_tracepoints: list[str]  # tp:cat/name targets (informational)
    sibling_kprobes: list[str]     # bare-symbol kprobes for siblings
    description: str               # leaf node's `description` field
    parent_description: str        # parent node's `description` field


@dataclass
class GenResult:
    leaf: LeafSpec
    accepted: bool
    rounds: int
    last_calibration: CalibrationResult | None = None
    bench_dir: Path | None = None
    failure_log: list[str] = field(default_factory=list)


# --- prompt construction --------------------------------------------------


def _split_targets(probes: list[str]) -> tuple[list[str], list[str]]:
    """Return (kprobes, tracepoints). 'tp:<cat>/<name>' goes to the second."""
    kp, tp = [], []
    for p in probes:
        if p.startswith("tp:"):
            tp.append(p)
        else:
            kp.append(p)
    return kp, tp


def gather_siblings(tree: list[Node],
                    parent_path: tuple[str, ...]) -> list[str]:
    """Collect kprobe symbols from every leaf NOT under parent_path. The
    calibration uses these as the 'must not fire' set."""
    out: set[str] = set()
    for leaf_path, leaf in iter_leaves(tree, []):
        if tuple(leaf_path) == tuple(parent_path):
            continue
        kp, _ = _split_targets(list(leaf.probes or []))
        out.update(kp)
    # Drop the leaf's own kprobes if they happen to overlap with siblings
    # (they shouldn't, but be safe).
    return sorted(out)


def build_leaf_spec(tree: list[Node],
                    leaf_path: tuple[str, ...]) -> LeafSpec | None:
    leaf = find(tree, list(leaf_path))
    if leaf is None or not getattr(leaf, "microbenchmark", None):
        return None
    parent = find(tree, list(leaf_path[:-1])) if len(leaf_path) > 1 else None
    target_kp, target_tp = _split_targets(list(leaf.probes or []))
    sibling_kp = [s for s in gather_siblings(tree, leaf_path)
                  if s not in target_kp]
    return LeafSpec(
        parent_path=leaf_path,
        leaf_name=leaf_path[-1],
        microbench_name=leaf.microbenchmark,
        target_kprobes=target_kp,
        target_tracepoints=target_tp,
        sibling_kprobes=sibling_kp,
        description=leaf.description or "",
        parent_description=(parent.description or "") if parent else "",
    )


PROMPT_TEMPLATE = """\
Author a tiny user-space C program (plus Makefile + run.sh) whose runtime
is dominated by **{leaf_name}** -- an L{depth} leaf of the
{parent_path_str} subsystem tree.

Leaf description: {leaf_desc}
Parent context:   {parent_desc}

The benchmark MUST fire these kernel functions on every work unit (these
will be hooked by an ftrace function tracer to verify dominance):
  TARGET kprobes (must hit >= {thresh_target} times per work unit):
    {target_kprobes_list}
{tracepoint_note}

The benchmark MUST NOT noticeably fire these sibling-leaf functions
(must hit < {thresh_nontarget} times per work unit each):
  SIBLING kprobes:
    {sibling_kprobes_list}

Output requirements:
  - Reply with EXACTLY THREE fenced code blocks, in order:
      1. ```c        (the source: minor_page_fault_types.c-style style)
      2. ```make     (a Makefile that builds with `cc -O2 -Wall`)
      3. ```bash     (a run.sh that runs the binary and prints work_units)
  - No prose outside the blocks.
  - The binary must accept `--iterations N` (or similar) and print a final
    line `work_units=<N>` to stdout where N is the integer count.
  - The benchmark should complete in 1-10 seconds on a modern x86 host.
  - Use only stable userspace APIs (no /proc magic for state changes that
    require root, no module loading). The run.sh runs as the same user
    that invokes the orchestrator -- typically unprivileged.
  - Make the work pattern as monomorphic as possible: each iteration
    should do ONE syscall / one mmap / one page touch / etc., chosen so
    the kernel side hits the TARGET symbol and nothing else.
{calibration_note}
"""


def build_prompt(leaf: LeafSpec,
                  thresh_target: float,
                  thresh_nontarget: float,
                  prior_calibration: CalibrationResult | None) -> str:
    """Build the user-message prompt for one (re)prompt round."""
    depth = len(leaf.parent_path)
    parent_path_str = ".".join(leaf.parent_path)
    target_list = "\n    ".join(leaf.target_kprobes) or "(none)"
    sibling_list = ("\n    ".join(leaf.sibling_kprobes[:30])
                     or "(none — all leaves are siblings)")
    tp_note = (
        f"\n  (Note: this leaf also lists tracepoints {leaf.target_tracepoints}; "
        f"those are observed by the multiplex but cannot be filtered by the "
        f"function tracer, so the calibration only checks the kprobes above.)"
        if leaf.target_tracepoints else ""
    )
    calibration_note = ""
    if prior_calibration is not None:
        c = prior_calibration
        worst = ", ".join(
            f"{k}={v:.2f}/wu" for k, v in
            sorted(c.nontarget_breakdown.items(), key=lambda kv: -kv[1])[:5])
        calibration_note = (
            f"\nPRIOR CALIBRATION FAILED:\n"
            f"  target_hits/wu={c.target_hits_per_wu:.2f} "
            f"(needed >= {thresh_target})\n"
            f"  max_nontarget_hits/wu={c.max_nontarget_per_wu:.2f} "
            f"(needed < {thresh_nontarget})\n"
            f"  worst non-target firers: {worst or '(none)'}\n"
            f"  Adjust the workload so it hits the target symbol more "
            f"often per work unit AND avoids the non-target symbols above."
        )
    return PROMPT_TEMPLATE.format(
        leaf_name=leaf.leaf_name, depth=depth,
        parent_path_str=parent_path_str,
        leaf_desc=leaf.description, parent_desc=leaf.parent_description,
        thresh_target=thresh_target, thresh_nontarget=thresh_nontarget,
        target_kprobes_list=target_list,
        sibling_kprobes_list=sibling_list,
        tracepoint_note=tp_note,
        calibration_note=calibration_note,
    )


# --- response parsing -----------------------------------------------------


_BLOCK_RE = re.compile(r"```(\w*)\s*\n(.*?)```", re.DOTALL)


def parse_three_blocks(reply: str) -> tuple[str, str, str] | None:
    """Extract (c_source, makefile, run_sh) from the LLM reply. Returns
    None if any block is missing or out of order."""
    blocks = _BLOCK_RE.findall(reply)
    if len(blocks) < 3:
        return None
    # Keep first c, first makefile, first bash/sh -- in that order.
    c_src = next((body for tag, body in blocks
                  if tag.lower() in ("c", "cpp")), None)
    makefile = next((body for tag, body in blocks
                     if tag.lower() in ("make", "makefile")), None)
    run_sh = next((body for tag, body in blocks
                   if tag.lower() in ("bash", "sh", "shell")), None)
    if not (c_src and makefile and run_sh):
        return None
    return c_src.strip() + "\n", makefile.strip() + "\n", run_sh.strip() + "\n"


# --- file I/O + compile + calibrate ---------------------------------------


def _write_candidate(bench_dir: Path, c_src: str, makefile: str,
                      run_sh: str) -> tuple[Path, Path, Path]:
    bench_dir.mkdir(parents=True, exist_ok=True)
    c_path = bench_dir / f"{bench_dir.name}.c"
    mk_path = bench_dir / "Makefile"
    sh_path = bench_dir / "run.sh"
    c_path.write_text(c_src)
    mk_path.write_text(makefile)
    sh_path.write_text(run_sh)
    sh_path.chmod(0o755)
    return c_path, mk_path, sh_path


def _try_compile(bench_dir: Path) -> tuple[bool, str]:
    r = subprocess.run(["make", "-C", str(bench_dir)],
                        capture_output=True, text=True)
    return (r.returncode == 0,
            (r.stdout + "\n" + r.stderr).strip()[-1500:])


def _try_run_under_calibration(bench_dir: Path, leaf: LeafSpec,
                                 thresh_target: float,
                                 thresh_nontarget: float) -> CalibrationResult:
    run_sh = bench_dir / "run.sh"
    return calibrate(
        workload_argv=[str(run_sh)],
        work_units_pattern=r"work_units=([0-9]+)",
        target_symbols=leaf.target_kprobes,
        sibling_symbols=leaf.sibling_kprobes,
        thresh_target=thresh_target,
        thresh_nontarget=thresh_nontarget,
        cwd=str(bench_dir),
    )


# --- generator main loop --------------------------------------------------


def generate_for_leaf(leaf: LeafSpec,
                       benchmarks_root: Path,
                       *,
                       max_rounds: int = 4,
                       thresh_target: float = 10.0,
                       thresh_nontarget: float = 2.0,
                       dry_run: bool = False) -> GenResult:
    """Generate (or re-generate) one leaf microbenchmark, retrying on
    calibration failure up to max_rounds times."""
    bench_dir = benchmarks_root / leaf.microbench_name
    res = GenResult(leaf=leaf, accepted=False, rounds=0)
    if dry_run:
        prompt = build_prompt(leaf, thresh_target, thresh_nontarget, None)
        print(f"=== DRY-RUN: prompt for {leaf.microbench_name} ===")
        print(prompt)
        return res

    backend = build_backend(
        mode=os.environ.get("AGENTICBPF_AGENT_MODE"),
        backend=os.environ.get("AGENTICBPF_AGENT_BACKEND"),
        model=os.environ.get("AGENTICBPF_AGENT_MODEL"),
        system_instruction=SYSTEM_INSTRUCTION,
        tools=[],
    )

    prior: CalibrationResult | None = None
    for round_idx in range(1, max_rounds + 1):
        prompt = build_prompt(leaf, thresh_target, thresh_nontarget, prior)
        try:
            turn = backend.send(prompt)
        except Exception as e:
            res.failure_log.append(f"round {round_idx}: backend error {e}")
            res.rounds = round_idx
            break
        reply = (turn.text or "").strip()
        parsed = parse_three_blocks(reply)
        if not parsed:
            res.failure_log.append(
                f"round {round_idx}: could not parse 3 fenced blocks; "
                f"reply head: {reply[:300]}")
            res.rounds = round_idx
            continue
        c_src, makefile, run_sh = parsed
        # Wipe stale build artefacts before writing fresh source.
        if bench_dir.exists():
            shutil.rmtree(bench_dir)
        _write_candidate(bench_dir, c_src, makefile, run_sh)
        ok, build_log = _try_compile(bench_dir)
        if not ok:
            res.failure_log.append(
                f"round {round_idx}: compile failed:\n{build_log}")
            res.rounds = round_idx
            # Fall through to next round; future prompts will include the
            # calibration failure (compile is treated similar -- prior=None
            # since no calibration ran).
            prior = None
            continue
        cal = _try_run_under_calibration(
            bench_dir, leaf, thresh_target, thresh_nontarget)
        res.last_calibration = cal
        res.rounds = round_idx
        if cal.passed:
            res.accepted = True
            res.bench_dir = bench_dir
            return res
        res.failure_log.append(
            f"round {round_idx}: calibration failed; "
            f"target/wu={cal.target_hits_per_wu:.2f} "
            f"max_nontarget/wu={cal.max_nontarget_per_wu:.2f}")
        prior = cal
    return res


def generate_for_tree(parent_filter: str | None = None,
                       leaf_filter: str | None = None,
                       *,
                       max_rounds: int = 4,
                       thresh_target: float = 10.0,
                       thresh_nontarget: float = 2.0,
                       dry_run: bool = False,
                       benchmarks_root: Path | None = None) -> list[GenResult]:
    benchmarks_root = benchmarks_root or (REPO / "benchmarks")
    tree = load_tree()
    results: list[GenResult] = []
    for leaf_path, leaf_node in iter_leaves(tree, []):
        if not getattr(leaf_node, "microbenchmark", None):
            continue
        if parent_filter and (not leaf_path or leaf_path[0] != parent_filter):
            continue
        if leaf_filter and leaf_node.microbenchmark != leaf_filter:
            continue
        spec = build_leaf_spec(tree, tuple(leaf_path))
        if spec is None:
            continue
        print(f"\n[ubench-gen] {'.'.join(spec.parent_path)} -> "
              f"{spec.microbench_name}", flush=True)
        r = generate_for_leaf(
            spec, benchmarks_root,
            max_rounds=max_rounds,
            thresh_target=thresh_target,
            thresh_nontarget=thresh_nontarget,
            dry_run=dry_run,
        )
        status = "ACCEPTED" if r.accepted else "REJECTED"
        print(f"[ubench-gen]   {status} after {r.rounds} round(s)")
        if not r.accepted:
            for line in r.failure_log[-3:]:
                print(f"[ubench-gen]     {line}")
        results.append(r)
    accepted = sum(1 for r in results if r.accepted)
    print(f"\n[ubench-gen] yield: {accepted}/{len(results)} "
          f"({100*accepted/max(len(results),1):.0f}%)")
    return results


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--parent", default=None)
    p.add_argument("--leaf", default=None)
    p.add_argument("--max-rounds", type=int, default=4)
    p.add_argument("--thresh-target", type=float, default=10.0)
    p.add_argument("--thresh-nontarget", type=float, default=2.0)
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args(argv)
    rs = generate_for_tree(
        parent_filter=args.parent,
        leaf_filter=args.leaf,
        max_rounds=args.max_rounds,
        thresh_target=args.thresh_target,
        thresh_nontarget=args.thresh_nontarget,
        dry_run=args.dry_run,
    )
    return 0 if all(r.accepted for r in rs) else 1


if __name__ == "__main__":
    sys.exit(main())
