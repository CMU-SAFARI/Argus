"""Ftrace-based calibration for LLM-generated leaf microbenchmarks.

Each leaf in the static subsystem tree (`subsystems.yaml`) declares one
or more kernel probe targets that the multiplex hooks at descent time.
A correctly authored microbench should:
  - Fire its target leaf's probes >= THRESH_TARGET times per work unit.
  - Fire sibling leaves' probes < THRESH_NONTARGET times per work unit
    (so the multiplex z-score is unambiguous).

This module runs a candidate microbench under the function tracer with
a filter restricted to the leaf's targets + its siblings' targets, then
parses /sys/kernel/tracing/trace to count per-symbol hits.

Tracepoint targets ('tp:cat/name') are ignored by the function-tracer
filter -- we only verify kprobe-style bare-symbol targets here. The
calibration's hit/miss counts therefore reflect *function-call*
boundaries; tracepoint-only leaves cannot be calibrated this way and
must rely on hand-eyeballing the multiplex output.

Usage:
  from ubench_calibrate import calibrate
  result = calibrate(workload_argv=["./benchmarks/pf_anon_minor/run.sh"],
                     work_units_pattern=r"work_units=([0-9]+)",
                     target_symbols=["do_anonymous_page"],
                     sibling_symbols=["wp_page_copy", "filemap_fault",
                                       "do_swap_page"],
                     thresh_target=10.0,
                     thresh_nontarget=2.0)
  print(result.passed, result.target_hits_per_wu, result.max_nontarget_per_wu)
"""
from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

TRACING_ROOT = Path("/sys/kernel/tracing")


@dataclass
class CalibrationResult:
    passed: bool
    target_hits_per_wu: float
    max_nontarget_per_wu: float
    nontarget_breakdown: dict[str, float]
    target_breakdown: dict[str, float]
    work_units: int
    raw_total_target: int
    raw_total_nontarget: int
    error: str = ""


def _sudo_write(path: Path, text: str) -> None:
    """Write `text` to `path` via `sudo tee`. ftrace control files all
    need root; we use tee so the redirection happens as root rather than
    as the unprivileged shell."""
    subprocess.run(
        ["sudo", "tee", str(path)],
        input=text.encode(), check=True,
        stdout=subprocess.DEVNULL,
    )


def _sudo_read(path: Path) -> str:
    return subprocess.run(
        ["sudo", "cat", str(path)],
        check=True, capture_output=True,
    ).stdout.decode("utf-8", errors="replace")


def _set_filter(symbols: list[str]) -> None:
    """Replace the ftrace function filter with exactly `symbols`. Symbols
    not in available_filter_functions are silently dropped by the kernel;
    we let them be (the calibration's hit count will reflect what
    actually matched)."""
    # Clearing first ensures stale filters from a prior run don't pollute.
    _sudo_write(TRACING_ROOT / "set_ftrace_filter", "")
    if symbols:
        _sudo_write(TRACING_ROOT / "set_ftrace_filter", "\n".join(symbols))


def _toggle_tracing(on: bool) -> None:
    _sudo_write(TRACING_ROOT / "tracing_on", "1" if on else "0")


def _set_current_tracer(name: str) -> None:
    _sudo_write(TRACING_ROOT / "current_tracer", name)


def _clear_trace() -> None:
    _sudo_write(TRACING_ROOT / "trace", "")


def _read_trace() -> str:
    return _sudo_read(TRACING_ROOT / "trace")


def _count_symbol_hits(trace_text: str, symbols: list[str]) -> dict[str, int]:
    """Count how many times each symbol appears as a function-trace event.

    ftrace function-tracer line format (typical):
        <task-pid>     [cpu] .... ts: function <- caller
    We match a symbol by anchoring on " function <- " preceded by the
    name. Whitespace-padded to avoid matching prefix-shared names
    (`do_anonymous_page` vs `do_anonymous_page_cont`).
    """
    counts: dict[str, int] = {s: 0 for s in symbols}
    for line in trace_text.splitlines():
        for s in symbols:
            # Match "  <symbol> <- caller" anywhere in the line, with whole-word
            # boundaries on the symbol side. ftrace pads with spaces.
            if re.search(rf"\s{re.escape(s)}\s+<-\s", line):
                counts[s] += 1
    return counts


def _extract_work_units(stdout: str, pattern: str) -> int:
    m = re.search(pattern, stdout)
    if not m:
        return 0
    try:
        return int(m.group(1))
    except (ValueError, IndexError):
        return 0


def calibrate(workload_argv: list[str],
              work_units_pattern: str,
              target_symbols: list[str],
              sibling_symbols: list[str],
              thresh_target: float = 10.0,
              thresh_nontarget: float = 2.0,
              cwd: str | None = None) -> CalibrationResult:
    """Run `workload_argv` under ftrace function tracer; verify the
    target leaf's symbols dominate vs siblings.

    Returns CalibrationResult with `passed=True` iff:
      target_hits/work_units >= thresh_target
      max(sibling_hits)/work_units < thresh_nontarget

    Both thresholds are tunable per leaf if a particular leaf is
    intrinsically noisier (e.g. `flush_tlb_mm_range` may have a large
    background rate from system daemons).
    """
    # Drop tp: prefixed targets -- function tracer can't filter on them.
    target_kprobes = [s for s in target_symbols if not s.startswith("tp:")]
    sibling_kprobes = [s for s in sibling_symbols if not s.startswith("tp:")]
    all_filter = list({*target_kprobes, *sibling_kprobes})

    if not target_kprobes:
        return CalibrationResult(
            passed=False, target_hits_per_wu=0.0, max_nontarget_per_wu=0.0,
            nontarget_breakdown={}, target_breakdown={}, work_units=0,
            raw_total_target=0, raw_total_nontarget=0,
            error="no kprobe-style target symbols (all are tracepoints); "
                  "calibration requires at least one bare kernel symbol",
        )

    try:
        _set_current_tracer("nop")  # disable any prior tracer
        _clear_trace()
        _set_filter(all_filter)
        _set_current_tracer("function")
        _toggle_tracing(True)
        try:
            r = subprocess.run(workload_argv, capture_output=True,
                               text=True, cwd=cwd, timeout=300)
            stdout = r.stdout or ""
            stderr = r.stderr or ""
        finally:
            _toggle_tracing(False)
        _set_current_tracer("nop")
        trace_text = _read_trace()
        # Cleanup: leave the filter empty so subsequent ftrace use isn't
        # constrained by our list.
        _set_filter([])
    except subprocess.CalledProcessError as e:
        return CalibrationResult(
            passed=False, target_hits_per_wu=0.0, max_nontarget_per_wu=0.0,
            nontarget_breakdown={}, target_breakdown={}, work_units=0,
            raw_total_target=0, raw_total_nontarget=0,
            error=f"ftrace control failed: {e}",
        )
    except subprocess.TimeoutExpired:
        return CalibrationResult(
            passed=False, target_hits_per_wu=0.0, max_nontarget_per_wu=0.0,
            nontarget_breakdown={}, target_breakdown={}, work_units=0,
            raw_total_target=0, raw_total_nontarget=0,
            error="workload timed out (>300s)",
        )

    if r.returncode != 0:
        return CalibrationResult(
            passed=False, target_hits_per_wu=0.0, max_nontarget_per_wu=0.0,
            nontarget_breakdown={}, target_breakdown={}, work_units=0,
            raw_total_target=0, raw_total_nontarget=0,
            error=f"workload exited {r.returncode}: {stderr[-200:]}",
        )

    wu = _extract_work_units(stdout, work_units_pattern)
    if wu == 0:
        return CalibrationResult(
            passed=False, target_hits_per_wu=0.0, max_nontarget_per_wu=0.0,
            nontarget_breakdown={}, target_breakdown={}, work_units=0,
            raw_total_target=0, raw_total_nontarget=0,
            error=f"no work_units in stdout (pattern={work_units_pattern!r}); "
                  f"stdout tail: {stdout[-200:]}",
        )

    counts = _count_symbol_hits(trace_text, all_filter)
    target_breakdown = {s: counts.get(s, 0) / wu for s in target_kprobes}
    nontarget_breakdown = {s: counts.get(s, 0) / wu for s in sibling_kprobes}
    raw_target = sum(counts.get(s, 0) for s in target_kprobes)
    raw_nontarget = sum(counts.get(s, 0) for s in sibling_kprobes)
    target_per_wu = raw_target / wu
    max_nontarget_per_wu = (max(nontarget_breakdown.values())
                             if nontarget_breakdown else 0.0)

    passed = (target_per_wu >= thresh_target
              and max_nontarget_per_wu < thresh_nontarget)

    return CalibrationResult(
        passed=passed,
        target_hits_per_wu=target_per_wu,
        max_nontarget_per_wu=max_nontarget_per_wu,
        nontarget_breakdown=nontarget_breakdown,
        target_breakdown=target_breakdown,
        work_units=wu,
        raw_total_target=raw_target,
        raw_total_nontarget=raw_nontarget,
    )
