"""compare_to_reference: shared by all three baselines and the ReAct agent.

Given a per-work-unit OFV and a saved reference (mu, sigma), returns:
  - z_scores per metric
  - flagged_subsystems list (from M4 detector)
  - hint string steering the next ReAct decision (used by B2 only, but
    computed for all baselines for log uniformity)

The hint depends on the agent's stated hypothesis:
  - flagged=[]               -> "no metric exceeded +/-3sigma; consider a
                                 different hook or hypothesis"
  - flagged disjoint from H  -> "data flagged X but you hypothesized H;
                                 author a probe targeted at X to cross-validate,
                                 do NOT submit yet"
  - flagged superset of H    -> "you have evidence for your hypothesis;
                                 submit_diagnosis is appropriate"

This module also exposes compare_multiplex(...) for the depth >= 2 path,
which applies the same z-score + pivot/submit hint logic to multiplex-probe
slot counters instead of the 13-metric perf-stat FV. The mechanism is
identical (3-sigma threshold against an idle reference, with a zero-
variance fallback for slots whose idle baseline is mu==sigma==0); only the
signal source changes.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

from .detector import METRIC_TO_SUBSYSTEM, explain, flag, flag_metrics
from .feature_vector import LABELS, N
from .reference import ReferenceProfile

# Pairs of subsystems known to be physically coupled (one kernel code path
# physically calls the other), where a strict-superset over the pair is
# almost always a genuine joint bottleneck rather than two independent
# signals. The agent's prompt instructs (a) pivot-and-cross-validate as the
# default when this coupling is what drove the strict superset.
COUPLED_PAIRS: list[frozenset[str]] = [
    frozenset({"kernel_paging", "kernel_memory_mgmt"}),
]


@dataclass
class CompareResult:
    z_scores: dict[str, float]
    flagged_subsystems: list[str]
    hint: str

    def to_dict(self) -> dict:
        return asdict(self)


def compare(rp: ReferenceProfile,
            ofv_rates: list[float],
            agent_hypothesis: list[str] | None = None,
            k: float = 3.0) -> CompareResult:
    """Compute z-scores, flagged subsystems, and a guidance hint."""
    active = getattr(rp, "active", None)
    benchmark = getattr(rp, "benchmark", None)
    flagged = flag(rp.mu, rp.sigma, ofv_rates, k=k, active=active,
                   benchmark=benchmark)
    rows = explain(rp.mu, rp.sigma, ofv_rates, k=k, active=active,
                   benchmark=benchmark)
    z_scores = {label: round(z, 2) for (label, z, _marker) in rows}

    if not flagged:
        hint = ("no metric exceeded +/-{}sigma. The probe ran cleanly but the "
                "workload's per-work-unit rates did not deviate from idle. "
                "Either the perturbation has no effect on this benchmark, or "
                "your probe instrumented a code path that is not stressed. "
                "Consider a different hook or hypothesis.").format(k)
    elif agent_hypothesis is None:
        hint = ("flagged subsystems: {}. No agent hypothesis was provided "
                "alongside the comparison; this is fine for B0/B1 baselines "
                "but the ReAct agent should always provide its hypothesis "
                "for hint-aware steering.").format(flagged)
    else:
        hyp_set = set(agent_hypothesis)
        flagged_set = set(flagged)
        extra = sorted(flagged_set - hyp_set)
        missing = sorted(hyp_set - flagged_set)

        if flagged_set == hyp_set and hyp_set:
            # Exact match: probe was designed for what the data flagged.
            hint = ("flagged subsystems {} match your hypothesis exactly. "
                    "Your probe is conclusive about every flagged subsystem; "
                    "submit_diagnosis with this exact set is appropriate."
                   ).format(flagged)
        elif flagged_set >= hyp_set and hyp_set and extra:
            # Strict superset: hypothesis supported, but data flagged extras
            # the current probe did NOT measure. Strict cross-validation
            # forbids submitting unprobed claims.
            hint = ("flagged subsystems are {}. Your hypothesis {} is "
                    "supported by your probe, but the data ALSO flagged "
                    "{} which your current probe was NOT designed to "
                    "measure. Per the strict cross-validation rule you have "
                    "two options: (a) author one or more new probes "
                    "targeted at {} (one hypothesis pivot per extra "
                    "subsystem, observe each, then submit the union), OR "
                    "(b) submit_diagnosis with only your hypothesized "
                    "subsystems {} -- do NOT claim {} without a probe that "
                    "measured it directly."
                   ).format(flagged, sorted(hyp_set), extra,
                            extra[0], sorted(hyp_set), extra)

            # Coupled-metric detection: if one or more single FV metrics
            # drove BOTH the hypothesized and the extra subsystem (because
            # the metric is N-to-many in METRIC_TO_SUBSYSTEM), AND the
            # hypothesis ∪ extra forms a known coupled pair, the strict
            # superset is almost certainly a joint bottleneck rather than
            # two separate signals. Surface this and steer toward (a).
            flagged_idx = flag_metrics(rp.mu, rp.sigma, ofv_rates, k=k)
            shared = [(i, METRIC_TO_SUBSYSTEM[i]) for i in flagged_idx
                      if len(METRIC_TO_SUBSYSTEM[i]) > 1
                      and set(METRIC_TO_SUBSYSTEM[i]) & hyp_set
                      and set(METRIC_TO_SUBSYSTEM[i]) & set(extra)]
            if shared and any(
                hyp_set | set(extra) >= pair for pair in COUPLED_PAIRS
            ):
                shared_labels = ", ".join(LABELS[i] for i, _ in shared)
                hint += (" NOTE: the strict superset was driven by "
                         "metric(s) [{}] which the detector maps to "
                         "MULTIPLE subsystems including both your "
                         "hypothesis and the extra. {} and {} are "
                         "physically coupled (the page-fault handler "
                         "calls into the allocator), so a single "
                         "metric deviation here typically indicates a "
                         "joint bottleneck rather than two independent "
                         "ones. PREFER OPTION (a): pivot once to author "
                         "an extra probe on {} and submit the union. "
                         "This costs one pivot but produces a more "
                         "accurate diagnosis than submit-narrow."
                        ).format(shared_labels, sorted(hyp_set)[0],
                                 extra[0], extra[0])
        else:
            # Disjoint or partial: data points elsewhere from your hypothesis.
            hint = ("data flagged {} but you hypothesized {}.").format(
                flagged, sorted(hyp_set))
            if missing:
                hint += (" Your hypothesized subsystems were NOT flagged: "
                         "{}.").format(missing)
            if extra:
                hint += (" The data flagged {} unexpectedly. Author a NEW "
                         "probe targeted at {} to cross-validate, then "
                         "submit. Do NOT submit on this probe alone -- it "
                         "was not designed to be conclusive about {}."
                        ).format(extra, extra[0], extra[0])

    return CompareResult(z_scores=z_scores, flagged_subsystems=flagged, hint=hint)


# ---- Multiplex (depth >= 2) comparison ------------------------------------

@dataclass
class MultiplexCompareResult:
    """Output of compare_multiplex().

    parent_path: the dot-joined L1[.L2] node we descended into.
    z_scores:    {child_name: z}  -- now the T_x log-space z when available,
                  otherwise the count-only z (backward-compat fallback).
    flagged:     children whose primary z exceeds threshold OR
                 (mu==sigma==0 and observed > 0).
    hint:        same pivot/submit narrative as L1, retargeted to children.

    T_x decomposition (populated when the reference has mu_log / sigma_log):
    z_count_scores:    per-child z on count_rate alone.
    z_log_scores:      per-child z on log2(sum_ns/wu + eps).
    sum_ns_per_wu:     per-child observed sum_ns/wu (linear ns/work_unit).
    metric:            "T_x_log" when log-space gating drove flagged,
                       "count" when falling back to count-only mode.
    """
    parent_path: str
    labels: list[str]
    z_scores: dict[str, float]
    flagged: list[str]
    hint: str
    z_count_scores: dict[str, float] = field(default_factory=dict)
    z_log_scores: dict[str, float] = field(default_factory=dict)
    sum_ns_per_wu: dict[str, float] = field(default_factory=dict)
    metric: str = "count"

    def to_dict(self) -> dict:
        return asdict(self)


def _flag_slots(mu: list[float], sigma: list[float],
                observed: list[float], k: float = 3.0) -> tuple[list[int], list[float]]:
    """Per-slot flagging logic. Mirrors detector.flag_metrics: z>k (POSITIVE
    only -- anomalously high vs idle, not just deviating in either direction),
    AND zero-variance slots (mu==sigma==0) flag if observed > 0. Suppressed
    activity (negative z) is reported in the z column but never flagged as
    a bottleneck. Returns (flagged_indices, z_per_slot)."""
    flagged: list[int] = []
    zs: list[float] = []
    for i in range(len(mu)):
        if sigma[i] <= 0:
            zs.append(0.0)
            if mu[i] == 0.0 and observed[i] > 0.0:
                flagged.append(i)
            continue
        z = (observed[i] - mu[i]) / sigma[i]
        zs.append(z)
        if z > k:
            flagged.append(i)
    return flagged, zs


def _flag_slots_log(mu_log: list[float], sigma_log: list[float],
                    observed_sum_ns_per_wu: list[float],
                    k: float = 3.0,
                    epsilon: float = 1.0,
                    abs_floor_ns_per_wu: float = 0.0,
                    rel_floor_frac: float = 0.01,
                    sigma_log_floor: float = 0.01) -> tuple[list[int], list[float]]:
    """Per-slot T_x flagging in log space.

    z_log = (log2(sum_ns/wu + eps) - mu_log) / sigma_log

    Returns (flagged_indices, z_log_per_slot).

    Threshold raised to k=3.0 to match count-only k=3.0 (initial k=2.0
    proved too loose: structural-null cells fired at z_log~=2.0 on
    background noise, especially before victim-PID filtering eliminated
    aggressor contamination from system-wide kprobes).

    Relative noise floor (added 2026-05-15 after a structural-null
    smoke regressed when tlb_shootdown z_log=3.80 fired on per-teardown
    noise at 1.47 ns/wu next to anon_fault at 600 ns/wu): a slot is
    flagged only when

        obs >= rel_floor_frac * max(observed)   (default 1% of dominant)
        AND z > k

    The relative floor suppresses slots that are noise-amplified by
    tight idle variance but contribute negligibly compared to the
    dominant signal in this descent. Absolute floor defaults to 0:
    workload work_unit definitions vary by orders of magnitude (gups
    counts 16M updates/run, pf_anon_write counts ~65k anon-write
    faults), so any absolute per-WU floor that suits one workload is
    too strict or too lax for another.

    Sigma floor (added 2026-05-15 after stream/sync_rcu showed
    sigma_log=2e-5 producing z_log>3 on near-zero per-IRQ noise --
    rcu_sched_clock_irq fires "as victim" in IRQ context, so the
    PID filter doesn't reject it, but the actual contribution is
    nanoseconds): clamp sigma_log at sigma_log_floor (default 0.01
    in log2 space ~= 1.4% in linear). Slots whose idle reference has
    pathologically tight variance can no longer claim 3-sigma
    significance on jitter alone. Headline cells are unaffected --
    their idle sigma_log is already >= 0.02."""
    import math
    flagged: list[int] = []
    zs: list[float] = []
    dominant = max(observed_sum_ns_per_wu) if observed_sum_ns_per_wu else 0.0
    floor = max(abs_floor_ns_per_wu, rel_floor_frac * dominant)
    for i in range(len(mu_log)):
        obs = max(observed_sum_ns_per_wu[i], 0.0)
        obs_log = math.log2(obs + epsilon)
        if sigma_log[i] <= 0:
            zs.append(0.0)
            # Zero-variance fallback: if the slot was silent at idle but
            # produced observable kernel time under the perturbation, flag --
            # subject to the same noise floor.
            if obs_log > mu_log[i] and obs >= floor:
                flagged.append(i)
            continue
        # Clamp sigma_log to a minimum: prevents pathologically tight idle
        # variance from inflating noise into 3-sigma signal.
        sigma_eff = max(sigma_log[i], sigma_log_floor)
        z = (obs_log - mu_log[i]) / sigma_eff
        zs.append(z)
        if z > k and obs >= floor:
            flagged.append(i)
    return flagged, zs


def compare_multiplex(mx_ref: dict,
                      observed_rates: list[float],
                      parent_path: tuple[str, ...] | str = "",
                      agent_hypothesis: list[str] | None = None,
                      k: float = 3.0,
                      observed_sum_ns_per_wu: list[float] | None = None,
                      k_log: float = 3.0,
                      epsilon: float = 1.0) -> MultiplexCompareResult:
    """Compare per-slot observed rates against an idle multiplex reference.

    `mx_ref` is the dict at rp.extra["multiplex"][_path_key(parent_path)] --
    keys: labels, mu, sigma, cov, raw_rates, n_runs. New keys (T_x):
    mu_log, sigma_log, mu_sum_ns, sigma_sum_ns. When the new keys are
    present AND `observed_sum_ns_per_wu` is provided, gating happens in
    log space (T_x) and the count-based z is kept as a side channel for
    decomposition / rationale. When either is missing, falls back to
    legacy count-only gating (preserves backward compatibility with
    references built before the T_x extension).

    `observed_rates` is the per-slot observed count-rate (count / work_units).
    Must match mx_ref["labels"] in length and order.

    `observed_sum_ns_per_wu` is the per-slot kernel-ns per work_unit
    captured by the kprobe+kretprobe pair (the T_x linear). Same length
    as labels. Optional for legacy callers.

    `agent_hypothesis` is the list of child names the agent (or caller)
    expects to be the bottleneck; the hint is generated relative to it.
    If None, the hint is descriptive only.

    `k` is the threshold for count-z (legacy/fallback).
    `k_log` is the threshold for T_x log-z (the new primary gate).
    `epsilon` is the additive floor inside log2(.) to avoid log2(0).

    The pivot/submit hint logic is the same shape as the L1 compare():
       flagged == [] -> no signal at this depth, pivot or descend elsewhere
       flagged disjoint from hyp -> data points elsewhere, pivot
       flagged strict superset of hyp -> hypothesis supported but extras
            unprobed; submit-narrow OR pivot to cover extras
       flagged == hyp -> conclusive, submit
    """
    labels = list(mx_ref["labels"])
    mu = list(mx_ref["mu"])
    sigma = list(mx_ref["sigma"])
    if len(observed_rates) != len(labels):
        raise ValueError(
            f"observed_rates length {len(observed_rates)} != "
            f"reference labels length {len(labels)} for {parent_path}"
        )
    # Count-only z always computed (for decomposition / legacy).
    count_flagged_idx, count_zs = _flag_slots(mu, sigma, observed_rates, k=k)

    # T_x log-space gating when reference + observed support it.
    mu_log = mx_ref.get("mu_log")
    sigma_log = mx_ref.get("sigma_log")
    use_log = (
        mu_log is not None
        and sigma_log is not None
        and observed_sum_ns_per_wu is not None
        and len(observed_sum_ns_per_wu) == len(labels)
    )
    if use_log:
        log_flagged_idx, log_zs = _flag_slots_log(
            list(mu_log), list(sigma_log),
            list(observed_sum_ns_per_wu),
            k=k_log, epsilon=epsilon)
        flagged_idx = log_flagged_idx
        primary_zs = log_zs
        metric = "T_x_log"
    else:
        flagged_idx = count_flagged_idx
        primary_zs = count_zs
        log_zs = [0.0] * len(labels)
        metric = "count"

    flagged = [labels[i] for i in flagged_idx]
    z_scores = {labels[i]: round(primary_zs[i], 2) for i in range(len(labels))}
    z_count_scores = {labels[i]: round(count_zs[i], 2) for i in range(len(labels))}
    z_log_scores = {labels[i]: round(log_zs[i], 2) for i in range(len(labels))}
    sum_ns_dict: dict[str, float] = {}
    if observed_sum_ns_per_wu is not None:
        sum_ns_dict = {
            labels[i]: round(float(observed_sum_ns_per_wu[i]), 3)
            for i in range(len(labels))
        }

    pp_str = parent_path if isinstance(parent_path, str) else ".".join(parent_path)

    if not flagged:
        hint = (
            f"no slot exceeded +/-{k}sigma at {pp_str}. The multiplex probe "
            f"caught no significant deviation from the idle reference. "
            f"Either the perturbation has no L2 effect inside {pp_str}, or "
            f"the observed slots are within natural variance. Consider "
            f"descending into a different parent, or submitting the L1 "
            f"diagnosis without a deeper claim."
        )
    elif agent_hypothesis is None:
        hint = (
            f"flagged children: {flagged}. No hypothesis was provided; "
            f"this is informational. The agent should call compare_multiplex "
            f"with a hypothesis to get pivot/submit guidance."
        )
    else:
        hyp_set = set(agent_hypothesis)
        flagged_set = set(flagged)
        extra = sorted(flagged_set - hyp_set)
        missing = sorted(hyp_set - flagged_set)

        if flagged_set == hyp_set and hyp_set:
            hint = (
                f"flagged children {flagged} match your hypothesis exactly "
                f"at {pp_str}. submit_diagnosis with the path "
                f"{pp_str}.{flagged[0]} (or each child path) is appropriate."
            )
        elif flagged_set >= hyp_set and hyp_set and extra:
            hint = (
                f"flagged children at {pp_str} are {flagged}. Your "
                f"hypothesis {sorted(hyp_set)} is supported, but the data "
                f"ALSO flagged {extra}. Two options: (a) submit the union "
                f"path-set, OR (b) submit only your hypothesised children "
                f"{sorted(hyp_set)} (narrow). Unlike at L1 these children "
                f"are siblings under the same parent so submit-union is "
                f"the more informative choice."
            )
        else:
            hint = (
                f"data flagged {flagged} at {pp_str} but you hypothesised "
                f"{sorted(hyp_set)}."
            )
            if missing:
                hint += f" Your hypothesised children were NOT flagged: {missing}."
            if extra:
                hint += (
                    f" The data flagged {extra} unexpectedly. Re-evaluate "
                    f"the hypothesis: the active L2 path under {pp_str} is "
                    f"{extra[0]}, not what you predicted."
                )

    return MultiplexCompareResult(
        parent_path=pp_str,
        labels=labels,
        z_scores=z_scores,
        flagged=flagged,
        hint=hint,
        z_count_scores=z_count_scores,
        z_log_scores=z_log_scores,
        sum_ns_per_wu=sum_ns_dict,
        metric=metric,
    )
