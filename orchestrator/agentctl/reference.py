"""M2 - Reference profiler. Builds an Input Feature Vector from N idle runs.

For each metric m we compute:
  mu[m]    = mean of (count[m] / work_units) across runs
  sigma[m] = stddev of the same
  cov[m]   = sigma[m] / max(mu[m], eps)

Per-work-unit normalization is the apples-to-apples fix: a 16-iteration STREAM
under perturbation produces fewer raw counts than a 19-iteration baseline, but
per-work-unit rates are comparable.

The CoV gate (default threshold 0.15 on a subset of GATE_METRICS) rejects
benchmarks whose reference itself is too noisy to detect 3-sigma deviations
reliably.
"""

from __future__ import annotations

import json
import math
import re
import statistics
import time
from dataclasses import dataclass, field
from pathlib import Path

from .feature_vector import FeatureVector, LABELS, N
from .ipc_client import AgentdClient
from .perturb import Perturbation
from .workload import Workload, WorkloadResult

REPO = Path(__file__).resolve().parents[2]
REF_DIR = REPO / "results" / "reference"

# Metrics that must have CoV <= GATE_THR for the reference to be considered
# stable. After the FV redesign (drop of llc_load_misses, cache_misses,
# stalls_l3_miss), the gate is anchored on the remaining always-active OS
# tracepoints that fire on virtually every workload. dtlb_load_misses + page_faults
# stay because they're the high-signal canaries for paging-class perturbations.
# sched_switch / context_switches are excluded: on a quiet, single-threaded
# workload these are dominated by host-system noise (preemption from other
# processes), not workload signal.
GATE_METRICS = ["dtlb_load_misses", "page_faults"]
GATE_THR = 0.15
# Per-metric override for events that are structurally noisier than the
# default. dtlb_load_misses on Intel hybrid P-cores (Raptor Lake-S i9-14900K
# was the lab box where this was measured) sits around CoV 0.17 on idle GUPS:
# the per-core dTLB is small (~96 entries) and absolute counts are tiny
# (~3.2e-5/wu), so single-cache-line evictions dominate run-to-run variance.
GATE_THR_BY_METRIC = {
    "dtlb_load_misses": 0.20,
}
# Skip the CoV check on metrics whose mean absolute count per run is below
# this threshold (further safety net on top of GATE_METRICS selection).
GATE_MIN_COUNT_PER_RUN = 50
# Brief sleep between reference runs so khugepaged can finish coalescing
# 4K -> 2M pages from the previous run before the next run measures rates.
# Kept short to keep build_ifv reasonable (30 runs * 200ms = 6s overhead).
INTER_RUN_SETTLE_S = 0.2


@dataclass
class ReferenceProfile:
    benchmark: str
    n_runs: int
    mu: list[float]                  # per work unit
    sigma: list[float]               # per work unit
    cov: list[float]
    gate_pass: bool
    gate_failures: list[str] = field(default_factory=list)
    raw_rates: list[list[float]] = field(default_factory=list)  # per-run, per-metric
    # Per-metric active flag (False on this host = capability-gated event
    # absent, e.g. amdgpu_irq on a non-AMD host). Defaults to all-True
    # for legacy references that pre-date the gating field.
    active: list[bool] = field(default_factory=lambda: [True] * N)
    # Subfields for depth-N reference profiles; lazy-expanded on first
    # descent. Schema: extra["depth2"]["kernel_paging"] = {"labels":[...],
    # "mu":[...], "sigma":[...]}. Empty dict on legacy/L1-only references.
    extra: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "benchmark": self.benchmark,
            "n_runs": self.n_runs,
            "labels": LABELS,
            "active": self.active,
            "mu": self.mu,
            "sigma": self.sigma,
            "cov": self.cov,
            "gate_pass": self.gate_pass,
            "gate_failures": self.gate_failures,
            "raw_rates": self.raw_rates,
            "extra": self.extra,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "ReferenceProfile":
        labels = d.get("labels", [])
        mu = d.get("mu", [])
        if len(mu) != N or (labels and labels != LABELS):
            raise ValueError(
                f"reference profile for {d.get('benchmark', '?')} has "
                f"{len(mu)} metrics (labels={labels[:3]}...), but the current "
                f"FV has {N} metrics. The FV layout was changed by the "
                f"hierarchical-detection rearchitecture (see "
                f"docs/hierarchical_subsystem_plan.md). Re-run "
                f"`agentctl reference {d.get('benchmark', '<bench>')}` to "
                f"regenerate the idle baseline."
            )
        return cls(
            benchmark=d["benchmark"], n_runs=d["n_runs"],
            mu=mu, sigma=d["sigma"], cov=d["cov"],
            gate_pass=d["gate_pass"], gate_failures=d.get("gate_failures", []),
            raw_rates=d.get("raw_rates", []),
            active=d.get("active", [True] * N),
            extra=d.get("extra", {}),
        )

    def save(self, path: Path | None = None) -> Path:
        path = path or (REF_DIR / f"{self.benchmark}.json")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.as_dict(), indent=2))
        return path

    @classmethod
    def load(cls, benchmark: str) -> "ReferenceProfile":
        path = REF_DIR / f"{benchmark}.json"
        return cls.from_dict(json.loads(path.read_text()))


def extract_work_units(stdout_tail: str, pattern: str) -> float:
    """Pull `work_units=N` from the benchmark's stdout."""
    m = re.search(pattern, stdout_tail)
    if not m:
        return 1.0  # avoid div-by-zero; downstream rates will be raw counts
    return float(m.group(1))


def build_ifv(client: AgentdClient, benchmark: str, n_runs: int = 30,
              warmup: int = 2, gate_thr: float = GATE_THR,
              verbose: bool = True) -> ReferenceProfile:
    """Run `benchmark` n_runs times on an idle system, normalize counters per
    work unit, compute mu/sigma/cov, apply CoV gate.

    If the benchmark spec declares a `pre_perturbation`, it is applied around
    the entire reference run (and reverted on exit). This is for setup like
    THP=always that makes the workload's perf profile representative of what
    perturbations like P1 (THP=never) will subsequently downgrade from.
    """
    w = Workload(benchmark)
    pattern = w.spec.get("work_units_pattern", r"work_units=([0-9]+)")
    pre_pid = w.spec.get("pre_perturbation")

    pre_ctx = Perturbation(pre_pid) if pre_pid else None
    if pre_ctx:
        if verbose:
            print(f"[reference] applying pre-perturbation: {pre_pid}", flush=True)
        pre_ctx.__enter__()

    try:
        return _build_ifv_inner(client, w, pattern, n_runs, warmup,
                                gate_thr, verbose, benchmark)
    finally:
        if pre_ctx:
            pre_ctx.__exit__(None, None, None)
            if verbose:
                print(f"[reference] reverted pre-perturbation: {pre_pid}", flush=True)


def _build_ifv_inner(client: AgentdClient, w: Workload, pattern: str,
                     n_runs: int, warmup: int, gate_thr: float,
                     verbose: bool, benchmark: str) -> ReferenceProfile:
    # Warmup: discard the first few runs (page-cache warm-up, libc loads).
    for i in range(warmup):
        if verbose:
            print(f"[reference] warmup {i+1}/{warmup} ...", flush=True)
        w.run(client)

    rates: list[list[float]] = []   # rates[run][metric] = count / work_units
    abs_counts: list[list[int]] = []  # raw counts for absolute-count gating
    active: list[bool] = [True] * N   # last seen per-metric active flags from agentd
    for i in range(n_runs):
        if i > 0 and INTER_RUN_SETTLE_S > 0:
            time.sleep(INTER_RUN_SETTLE_S)
        r: WorkloadResult = w.run(client)
        if not r.ok:
            raise RuntimeError(f"benchmark failed (run {i}): {r.error}")
        wu = extract_work_units(r.stdout_tail, pattern)
        run_rates = [c / wu for c in r.fv.v]
        rates.append(run_rates)
        abs_counts.append(list(r.fv.v))
        active = list(r.fv.active)
        if verbose:
            print(f"[reference] run {i+1}/{n_runs} wall={r.wall_s:.2f}s wu={wu:.0f}",
                  flush=True)

    mu = [statistics.fmean(rates[r][m] for r in range(n_runs)) for m in range(N)]
    sigma = [statistics.pstdev((rates[r][m] for r in range(n_runs)), mu=mu[m])
             for m in range(N)]
    cov = [sigma[m] / max(mu[m], 1e-12) if mu[m] > 0 else 0.0 for m in range(N)]
    abs_mean = [statistics.fmean(abs_counts[r][m] for r in range(n_runs))
                for m in range(N)]

    gate_failures: list[str] = []
    for label in GATE_METRICS:
        idx = LABELS.index(label)
        if mu[idx] == 0:
            continue  # zero-rate metric (e.g. kswapd_wake on idle STREAM)
        if abs_mean[idx] < GATE_MIN_COUNT_PER_RUN:
            continue  # too few absolute events to make CoV meaningful
        thr = GATE_THR_BY_METRIC.get(label, gate_thr)
        if cov[idx] > thr:
            gate_failures.append(f"{label}: cov={cov[idx]:.3f} > {thr}")
    gate_pass = len(gate_failures) == 0

    return ReferenceProfile(benchmark=benchmark, n_runs=n_runs,
                            mu=mu, sigma=sigma, cov=cov,
                            gate_pass=gate_pass, gate_failures=gate_failures,
                            raw_rates=rates, active=active)


def normalize(fv: FeatureVector, work_units: float) -> list[float]:
    """Convert raw FV to per-work-unit rates."""
    return [c / max(work_units, 1.0) for c in fv.v]


# ---- Depth-N (multiplex) reference profiler -------------------------------
#
# A multiplex reference is the idle (μ, σ) per slot for one (benchmark,
# parent_path) pair, where parent_path identifies a non-leaf node in the
# subsystem tree. It augments the L1 perf-stat reference rather than
# replacing it; both live in the same `<bench>.json` file.
#
# Schema:
#   reference_profile.extra["multiplex"][".".join(parent_path)] = {
#       "labels":        [child_name, ...],            # one entry per slot
#       "n_runs":        int,
#       "mu":            [float, ...],                 # per-slot, per-work-unit
#       "sigma":         [float, ...],
#       "cov":           [float, ...],
#       "raw_rates":     [[per-slot rates per run], ...],
#       "n_attached":    [int, ...],                   # per-run libbpf attach count, sanity check
#       "n_programs":    int,                          # rendered SECs in the .bpf.o
#   }
#
# Lazy-extension: reference_for_descent() loads the JSON, returns the cached
# entry if present, else builds it by re-running the multiplex spec n_runs
# times on the idle workload, persists, and returns.

def _path_key(parent_path: tuple[str, ...]) -> str:
    return ".".join(parent_path)


def build_multiplex_reference(client: AgentdClient,
                              benchmark: str,
                              parent_path: tuple[str, ...],
                              n_runs: int = 30,
                              warmup: int = 2,
                              verbose: bool = True) -> dict:
    """Run the multiplex spec for `parent_path` on `benchmark` n_runs times
    on an idle system, return the {labels, mu, sigma, cov, raw_rates,
    n_attached, n_programs} dict ready to be inserted under
    rp.extra["multiplex"][_path_key(parent_path)].

    Does NOT persist. Use reference_for_descent() for the load-or-build
    flow that writes back to disk.
    """
    # Lazy imports to avoid a circular dep with multiplex.py (which imports
    # ipc_client which doesn't pull reference, but multiplex's runner could
    # in future).
    from .subsystem_tree import load_tree, find
    from .multiplex import spec_for_descent, spec_for_root, run_multiplex_against

    tree = load_tree()
    if not parent_path:
        # Root-level L1 T_x reference: one slot per active L1 subsystem.
        spec = spec_for_root(tree)
    else:
        parent = find(tree, parent_path)
        if parent is None:
            raise ValueError(f"parent path not found in active tree: {parent_path}")
        if parent.is_leaf:
            raise ValueError(f"parent path is a leaf (nothing to descend into): {parent_path}")
        spec = spec_for_descent(parent_path, parent)

    w = Workload(benchmark)
    argv = w._argv()
    pattern = w.spec.get("work_units_pattern", r"work_units=([0-9]+)")
    pre_pid = w.spec.get("pre_perturbation")
    pre_ctx = Perturbation(pre_pid) if pre_pid else None

    if pre_ctx:
        if verbose:
            print(f"[mxref] applying pre-perturbation: {pre_pid}", flush=True)
        pre_ctx.__enter__()
    try:
        # Warmup runs — same logic as the perf-stat reference, but with
        # the multiplex probe attached so the kernel has primed any
        # kprobe trampolines.
        for i in range(warmup):
            if verbose:
                print(f"[mxref] warmup {i+1}/{warmup} ({_path_key(parent_path)}) ...",
                      flush=True)
            run_multiplex_against(spec, argv, client)

        rates: list[list[float]] = []
        # T_x extension: also track per-slot per-WU nanoseconds (sum_ns)
        # across the idle runs. Empty list per-run if the BPF object
        # didn't carry mx_sum_ns (legacy build).
        sum_ns_rates: list[list[float]] = []
        attached_seen: list[int] = []
        for i in range(n_runs):
            if i > 0 and INTER_RUN_SETTLE_S > 0:
                time.sleep(INTER_RUN_SETTLE_S)
            r = run_multiplex_against(spec, argv, client)
            if not r.ok:
                raise RuntimeError(f"multiplex run {i} failed: {r.error or 'unknown'}")
            wu = extract_work_units(r.stdout_tail, pattern)
            run_rates = [c / max(wu, 1.0) for c in r.counters]
            rates.append(run_rates)
            run_sum_ns = (
                [s / max(wu, 1.0) for s in r.sum_ns]
                if r.sum_ns else [0.0] * spec.n_slots
            )
            sum_ns_rates.append(run_sum_ns)
            attached_seen.append(r.n_attached)
            if verbose:
                # Cheap human-readable hint: show the dominant slot's
                # mean latency in this run so it's easy to spot if
                # kretprobes are firing.
                total_ns = sum(r.sum_ns) if r.sum_ns else 0
                print(f"[mxref] run {i+1}/{n_runs} wall={r.wall_s:.2f}s "
                      f"wu={wu:.0f} attached={r.n_attached}/{r.n_programs} "
                      f"sum_ns_total={total_ns}",
                      flush=True)
    finally:
        if pre_ctx:
            pre_ctx.__exit__(None, None, None)
            if verbose:
                print(f"[mxref] reverted pre-perturbation: {pre_pid}", flush=True)

    n_slots = spec.n_slots
    mu = [statistics.fmean(rates[r][s] for r in range(n_runs)) for s in range(n_slots)]
    sigma = [statistics.pstdev((rates[r][s] for r in range(n_runs)), mu=mu[s])
             for s in range(n_slots)]
    cov = [sigma[s] / max(mu[s], 1e-12) if mu[s] > 0 else 0.0 for s in range(n_slots)]

    # T_x stats: linear and log space. epsilon defaults to 1.0 ns/WU
    # to avoid log2(0) on slots where the kprobe entry-side fired but
    # the kretprobe never matched (rare; happens when the function
    # exits via a path the kretprobe can't intercept, e.g. tail call).
    import math
    mu_sum_ns = [statistics.fmean(sum_ns_rates[r][s] for r in range(n_runs))
                 for s in range(n_slots)]
    sigma_sum_ns = [statistics.pstdev(
        (sum_ns_rates[r][s] for r in range(n_runs)), mu=mu_sum_ns[s])
        for s in range(n_slots)]
    log_rates = [[math.log2(sum_ns_rates[r][s] + 1.0) for s in range(n_slots)]
                 for r in range(n_runs)]
    mu_log = [statistics.fmean(log_rates[r][s] for r in range(n_runs))
              for s in range(n_slots)]
    sigma_log = [statistics.pstdev(
        (log_rates[r][s] for r in range(n_runs)), mu=mu_log[s])
        for s in range(n_slots)]

    return {
        "labels": [child_name for child_name, _ in spec.slots],
        "n_runs": n_runs,
        "mu": mu,
        "sigma": sigma,
        "cov": cov,
        "raw_rates": rates,
        # T_x metric extension. mu_sum_ns / sigma_sum_ns are linear
        # ns-per-work-unit; mu_log / sigma_log are log2(ns/WU + 1)
        # used for z-score gating (variance-stabilized).
        "mu_sum_ns": mu_sum_ns,
        "sigma_sum_ns": sigma_sum_ns,
        "mu_log": mu_log,
        "sigma_log": sigma_log,
        "raw_sum_ns": sum_ns_rates,
        "n_attached": attached_seen,
        "n_programs": spec.n_slots,  # actually #programs >= #slots; surface for parity
    }


def reference_for_descent(client: AgentdClient,
                          benchmark: str,
                          parent_path: tuple[str, ...],
                          n_runs: int = 30,
                          warmup: int = 2,
                          force_rebuild: bool = False,
                          verbose: bool = True) -> dict:
    """Load-or-build the multiplex reference for (benchmark, parent_path).

    Reads <bench>.json, returns the cached entry under
    extra["multiplex"][_path_key(parent_path)] if present, else builds it
    via build_multiplex_reference(), persists the augmented JSON, returns
    the entry.

    The L1 perf-stat reference must already exist (run `agentctl reference
    <bench>` first). This function refuses to create a fresh L1 reference
    on the user's behalf -- the L1 mu/sigma matter and shouldn't be
    sourced from a side-effect of someone calling depth-2.
    """
    rp = ReferenceProfile.load(benchmark)
    key = _path_key(parent_path)
    cache = rp.extra.setdefault("multiplex", {})
    if not force_rebuild and key in cache:
        if verbose:
            print(f"[mxref] cache hit: {benchmark} x {key}", flush=True)
        return cache[key]
    if verbose:
        print(f"[mxref] building: {benchmark} x {key} (n_runs={n_runs})",
              flush=True)
    entry = build_multiplex_reference(client, benchmark, parent_path,
                                      n_runs=n_runs, warmup=warmup,
                                      verbose=verbose)
    cache[key] = entry
    rp.save()
    if verbose:
        print(f"[mxref] saved {key} into {REF_DIR / (benchmark + '.json')}",
              flush=True)
    return entry
