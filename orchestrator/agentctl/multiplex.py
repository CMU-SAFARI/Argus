"""Multiplexed eBPF probe template + builder.

A "multiplexed probe" is a single eBPF program that hooks N kernel functions
at once and reports a per-slot counter for each. It is the depth >= 2 signal
source for the L1->L2->L3 hierarchical detection rearchitecture (see
docs/hierarchical_subsystem_plan.md).

The template is rendered per-descent: when the agent descends into L1 = X,
we render a probe that hooks X's L2-children's kprobes, with each slot
mapped to one child. The agent-side compare_to_reference logic then
z-scores the slot counters against a depth-2 reference, producing the
flagged set of L2 children — same mechanism as the L1 detector, applied
to probe-derived counters instead of perf-stat metrics.

Phase 2 deliverables:
- MultiplexSpec dataclass (one slot per child, each slot maps to >=1 kprobe).
- render_multiplex_c() to emit valid .bpf.c source.
- compile_multiplex() to invoke clang -target bpf and produce a .bpf.o.

Phase 3 will add the agentd RPC to attach the rendered .bpf.o via libbpf
directly (bypassing the dispatcher), run a workload, and read back the
per-slot counters.

Kprobe overlap: if two sibling children list the same kprobe, both slots
fire simultaneously when that kprobe triggers. This is semantically
correct for "did the parent code path execute" but ambiguates which child
specifically. The renderer logs an overlap warning so the agent (and
later, plot scripts) can flag affected paths.
"""

from __future__ import annotations

import math
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from .ipc_client import AgentdClient
from .subsystem_tree import Node

REPO = Path(__file__).resolve().parents[2]
BUILD_DIR = REPO / "build" / "multiplex_obj"
SRC_INCLUDE = REPO / "src" / "include"
LIBBPF_INCLUDE = REPO / "build" / "libbpf" / "destdir" / "usr" / "include"
SKEL_DIR = REPO / "build" / "skel"


@dataclass
class MultiplexSpec:
    """One multiplexed probe spec.

    parent_path: the L1 (or L2) node we are *descending into* — its children
                 become the slots of this probe.
    slots:       ordered [(child_name, [probe, ...]), ...]. Slot index = list
                 index. Each slot will fire when ANY of its probes triggers.
                 A probe entry is either a bare symbol (attached as kprobe)
                 or 'tp:<cat>/<name>' (attached as tracepoint).
    """
    parent_path: tuple[str, ...]
    slots: list[tuple[str, list[str]]] = field(default_factory=list)

    @property
    def n_slots(self) -> int:
        return len(self.slots)

    @property
    def name(self) -> str:
        """Used for the output .bpf.o filename and as a C-friendly identifier."""
        if not self.parent_path:
            return "root_mx"
        return "_".join(self.parent_path) + "_mx"

    def overlapping_probes(self) -> dict[str, list[int]]:
        """Map probe -> list of slot indices that share it.
        Useful for logging "this probe has ambiguous slots"."""
        seen: dict[str, list[int]] = {}
        for i, (_, ps) in enumerate(self.slots):
            for p in ps:
                seen.setdefault(p, []).append(i)
        return {p: idxs for p, idxs in seen.items() if len(idxs) > 1}


def spec_for_descent(parent_path: tuple[str, ...], parent: Node) -> MultiplexSpec:
    """Build a MultiplexSpec that disambiguates `parent`'s children.

    Raises ValueError if `parent` has no children (i.e., is a leaf).
    """
    if parent.is_leaf:
        raise ValueError(
            f"cannot build multiplex spec for leaf {parent_path}: "
            f"no children to disambiguate"
        )
    slots = [(c.name, list(c.probes)) for c in parent.children]
    return MultiplexSpec(parent_path=parent_path, slots=slots)


def spec_for_root(tree: list[Node]) -> MultiplexSpec:
    """Build the root-level (L1) T_x multiplex spec.

    One slot per active L1 subsystem; each slot hooks the L1's
    `representative_probe` with a kprobe+kretprobe pair. This gives the
    agent an L1 z_log ranking driven by per-call kernel latency, in
    addition to the count-based perf-stat OFV.

    Cache-thrash-like perturbations (latency without count change) show
    up cleanly here but are invisible to perf-stat counts -- the
    motivation for adding L1 T_x. L1 nodes without a representative
    probe (tracepoint-only, e.g. amdgpu under ROCm 7.x) are skipped.
    """
    slots: list[tuple[str, list[str]]] = []
    for n in tree:
        if n.representative_probe:
            slots.append((n.name, [n.representative_probe]))
    if not slots:
        raise ValueError("no L1 subsystems define a representative_probe; "
                         "root-level T_x cannot be built on this host")
    return MultiplexSpec(parent_path=(), slots=slots)


# ---- C source rendering ---------------------------------------------------

_HEADER_TMPL = """\
/* AUTO-GENERATED. Do not hand-edit. Source of truth: orchestrator/agentctl/multiplex.py
 *
 * Multiplexed eBPF probe for descent into: {parent_path}
 * N_SLOTS = {n_slots}; one slot per child of the parent node.
 *
 * Slot layout:
{slot_layout}
 *
 * Output maps:
 *   mx_counters: BPF_MAP_TYPE_PERCPU_ARRAY, max_entries=N_SLOTS, value=u64.
 *                Each handler bumps its slot's count (entry, both kprobe & tp).
 *   mx_sum_ns:   BPF_MAP_TYPE_PERCPU_ARRAY, max_entries=N_SLOTS, value=u64.
 *                Per-slot accumulated nanoseconds, captured by kprobe+kretprobe
 *                pairs. Tracepoint-only slots stay at 0 here -- tracepoints
 *                are point-in-time events with no natural "exit" event.
 *   mx_entry_ts: BPF_MAP_TYPE_HASH, keyed by (pid_tgid, slot), value=u64 ns.
 *                Records kprobe-entry timestamps; cleared on kretprobe.
 *                Recursion approximation: nested entries overwrite the outer
 *                timestamp; the matching kretprobe measures the innermost
 *                call's duration. Acceptable for the kprobe targets we use
 *                (allocator, page-fault, lock paths -- not recursive in our
 *                workloads).
 */

#include "vmlinux.h"
#include <bpf/bpf_helpers.h>
#include <bpf/bpf_tracing.h>
#include <bpf/bpf_core_read.h>

#define MX_N_SLOTS {n_slots}
#define MX_TS_MAX  65536
#define MX_VICTIM_PIDS_MAX 64

struct {{
    __uint(type, BPF_MAP_TYPE_PERCPU_ARRAY);
    __uint(max_entries, MX_N_SLOTS);
    __type(key, __u32);
    __type(value, __u64);
}} mx_counters SEC(".maps");

struct {{
    __uint(type, BPF_MAP_TYPE_PERCPU_ARRAY);
    __uint(max_entries, MX_N_SLOTS);
    __type(key, __u32);
    __type(value, __u64);
}} mx_sum_ns SEC(".maps");

struct mx_ts_key {{
    __u64 pid_tgid;
    __u32 slot;
    __u32 _pad;     /* keep key 16-byte aligned for predictable hashing */
}};

struct {{
    __uint(type, BPF_MAP_TYPE_HASH);
    __uint(max_entries, MX_TS_MAX);
    __type(key, struct mx_ts_key);
    __type(value, __u64);
}} mx_entry_ts SEC(".maps");

/* Victim-PID filter: agentd populates this map with the workload's TGID
 * (and may add additional TGIDs of harness children) before signalling
 * the workload to exec. Every probe handler early-returns when the
 * current task's TGID -- and its parent's TGID -- are not in the map.
 * Empty map => no probe fires, so agentd MUST populate it. */
struct {{
    __uint(type, BPF_MAP_TYPE_HASH);
    __uint(max_entries, MX_VICTIM_PIDS_MAX);
    __type(key, __u32);
    __type(value, __u8);
}} mx_victim_pids SEC(".maps");

/* Returns 1 if the current task is the victim or a direct child of the
 * victim, 0 otherwise. Walks one parent level to cover harness-spawned
 * children (e.g. fork_storm spawns ~1000 kids -- their real_parent is
 * the workload binary). */
static __always_inline int mx_should_measure(void) {{
    __u32 tgid = (__u32)(bpf_get_current_pid_tgid() >> 32);
    if (bpf_map_lookup_elem(&mx_victim_pids, &tgid)) return 1;
    struct task_struct *task = (struct task_struct *)bpf_get_current_task();
    if (!task) return 0;
    struct task_struct *parent = BPF_CORE_READ(task, real_parent);
    if (!parent) return 0;
    __u32 ptgid = BPF_CORE_READ(parent, tgid);
    if (bpf_map_lookup_elem(&mx_victim_pids, &ptgid)) return 1;
    return 0;
}}

static __always_inline void mx_bump_count(__u32 slot) {{
    __u64 *p = bpf_map_lookup_elem(&mx_counters, &slot);
    if (p) __sync_fetch_and_add(p, 1);
}}

static __always_inline void mx_record_entry(__u32 slot) {{
    struct mx_ts_key k = {{}};
    k.pid_tgid = bpf_get_current_pid_tgid();
    k.slot = slot;
    __u64 now = bpf_ktime_get_ns();
    bpf_map_update_elem(&mx_entry_ts, &k, &now, BPF_ANY);
}}

static __always_inline void mx_record_exit(__u32 slot) {{
    struct mx_ts_key k = {{}};
    k.pid_tgid = bpf_get_current_pid_tgid();
    k.slot = slot;
    __u64 *entry = bpf_map_lookup_elem(&mx_entry_ts, &k);
    if (!entry) return;
    __u64 delta = bpf_ktime_get_ns() - *entry;
    bpf_map_delete_elem(&mx_entry_ts, &k);
    __u64 *sum = bpf_map_lookup_elem(&mx_sum_ns, &slot);
    if (sum) __sync_fetch_and_add(sum, delta);
}}

"""

_KPROBE_TMPL = """\
SEC("kprobe/{sym}")
int BPF_KPROBE(mx_p{slot}_{cident}) {{
    if (!mx_should_measure()) return 0;
    mx_bump_count({slot});
    mx_record_entry({slot});
    return 0;
}}
SEC("kretprobe/{sym}")
int BPF_KRETPROBE(mx_p{slot}_{cident}_ret) {{
    if (!mx_should_measure()) return 0;
    mx_record_exit({slot});
    return 0;
}}
"""

_TRACEPOINT_TMPL = """\
SEC("tp/{tp_path}")
int mx_p{slot}_tp_{cident}(void *ctx) {{
    /* Tracepoints fire on a single event with no natural "exit" hook,
     * so we cannot measure per-event latency -- count-only contribution. */
    if (!mx_should_measure()) return 0;
    mx_bump_count({slot});
    return 0;
}}
"""

_FOOTER = 'char _license[] SEC("license") = "GPL";\n'


def _c_ident(s: str) -> str:
    """Sanitize a kernel symbol to a valid C identifier suffix."""
    out = []
    for ch in s:
        if ch.isalnum() or ch == "_":
            out.append(ch)
        else:
            out.append("_")
    return "".join(out)


def render_multiplex_c(spec: MultiplexSpec) -> str:
    """Return .bpf.c source for `spec`. Probe entries with no prefix become
    SEC("kprobe/...") blocks; entries prefixed `tp:cat/name` become
    SEC("tp/cat/name") blocks."""
    if spec.n_slots == 0:
        raise ValueError(f"empty multiplex spec for {spec.parent_path}")

    layout_lines = [
        f" *   [{i}] {child_name:<24s} probes: {ps}"
        for i, (child_name, ps) in enumerate(spec.slots)
    ]
    header = _HEADER_TMPL.format(
        parent_path=" -> ".join(spec.parent_path),
        n_slots=spec.n_slots,
        slot_layout="\n".join(layout_lines),
    )

    sections: list[str] = []
    for slot_idx, (_child_name, probes) in enumerate(spec.slots):
        for p in probes:
            if p.startswith("tp:"):
                tp_path = p[len("tp:"):]            # e.g. 'vmscan/mm_vmscan_lru_shrink_inactive'
                sections.append(_TRACEPOINT_TMPL.format(
                    tp_path=tp_path, slot=slot_idx, cident=_c_ident(tp_path),
                ))
            else:
                sections.append(_KPROBE_TMPL.format(
                    sym=p, slot=slot_idx, cident=_c_ident(p),
                ))
    return header + "".join(sections) + "\n" + _FOOTER


# ---- Compile --------------------------------------------------------------

def compile_multiplex(spec: MultiplexSpec,
                      out_dir: Path | None = None,
                      clang: str = "clang") -> tuple[Path, Path]:
    """Render `spec` to .bpf.c and compile it to .bpf.o via clang -target bpf.

    Returns (c_path, obj_path). Raises CalledProcessError on compile failure.
    """
    out_dir = out_dir or BUILD_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    c_path = out_dir / f"{spec.name}.bpf.c"
    obj_path = out_dir / f"{spec.name}.bpf.o"
    c_path.write_text(render_multiplex_c(spec))

    # Same -I flags as the project Makefile's BPF_CFLAGS:
    cmd = [
        clang, "-g", "-O2", "-target", "bpf",
        "-I", str(SRC_INCLUDE),
        "-I", str(LIBBPF_INCLUDE),
        "-I", str(LIBBPF_INCLUDE / "bpf"),
        "-I", str(SKEL_DIR),
        "-c", str(c_path), "-o", str(obj_path),
    ]
    if shutil.which(clang) is None:
        raise FileNotFoundError(f"clang not found: {clang}")
    subprocess.run(cmd, check=True, capture_output=True)
    return c_path, obj_path


# ---- Inspection helpers ---------------------------------------------------

@dataclass
class MultiplexRun:
    """Result of one multiplex.run RPC. counters is per-slot, ordered to
    match spec.slots so [i] is child slots[i][0]. sum_ns is the parallel
    array of accumulated kprobe-to-kretprobe wall-nanoseconds per slot
    (zero for tracepoint-only slots)."""
    ok: bool
    spec: MultiplexSpec
    obj_path: Path
    wall_s: float
    exit_code: int
    stdout_tail: str
    n_programs: int
    n_attached: int
    attach_errors: list[str]
    counters: list[int]
    sum_ns: list[int] = field(default_factory=list)
    error: str = ""

    def per_slot(self) -> dict[str, int]:
        """Map child_name -> raw counter sum (across CPUs)."""
        return {name: c for (name, _), c in zip(self.spec.slots, self.counters)}

    def per_slot_per_wu(self, work_units_pattern: str = r"work_units=(\d+)") -> dict[str, float]:
        """Map child_name -> per-work-unit *count* rate.

        Preserved for backward compatibility with count-only consumers
        (e.g. legacy scoring pipeline). For the new T_x-based scoring,
        use per_slot_T_x_log / per_slot_T_x_linear_ns_per_wu.

        work_units is parsed from stdout_tail using the same regex pattern
        the reference profiler uses. Returns raw counts per slot if the
        pattern doesn't match.
        """
        m = re.search(work_units_pattern, self.stdout_tail)
        wu = float(m.group(1)) if m else 1.0
        return {name: c / max(wu, 1.0)
                for (name, _), c in zip(self.spec.slots, self.counters)}

    def _wu(self, work_units_pattern: str) -> float:
        m = re.search(work_units_pattern, self.stdout_tail)
        return float(m.group(1)) if m else 1.0

    def per_slot_sum_ns_per_wu(
        self,
        work_units_pattern: str = r"work_units=(\d+)",
    ) -> dict[str, float]:
        """Map child_name -> per-work-unit *nanoseconds* (linear T_x backstop).

        Returns 0.0 for slots whose probes are all tracepoints (no exit
        event captured) or whose kretprobes failed to attach. This is
        the human-readable form: 'kernel ns spent in this code path
        per useful operation'.
        """
        wu = self._wu(work_units_pattern)
        sum_ns = self.sum_ns or [0] * len(self.counters)
        return {name: float(s) / max(wu, 1.0)
                for (name, _), s in zip(self.spec.slots, sum_ns)}

    def per_slot_T_x_log(
        self,
        work_units_pattern: str = r"work_units=(\d+)",
        epsilon: float = 1.0,
    ) -> dict[str, float]:
        """Map child_name -> log2(sum_ns/wu + epsilon), the gating metric.

        T_x = count x mean_latency = total_ns -- so the per-work-unit
        version is just sum_ns/wu, and log2 of that (plus epsilon to
        avoid log(0) on tracepoint-only / unobserved slots) is the
        variance-stabilized scalar we z-score in log space.

        epsilon defaults to 1.0 ns/work_unit. Set it per-slot to ~1% of
        idle median in compare.py if calibration requires.
        """
        wu = self._wu(work_units_pattern)
        sum_ns = self.sum_ns or [0] * len(self.counters)
        return {name: math.log2(max(float(s), 0.0) / max(wu, 1.0) + epsilon)
                for (name, _), s in zip(self.spec.slots, sum_ns)}

    def per_slot_mean_latency_ns(self) -> dict[str, float]:
        """Map child_name -> mean nanoseconds per probe firing.

        Returns 0.0 when count is zero (slot never fired) or when the
        slot is tracepoint-only (sum_ns stays 0 because tracepoints have
        no exit event). Useful for the agent's rationale ('count was
        flat at z=0 but mean latency rose 3x: latency-driven bottleneck')."""
        sum_ns = self.sum_ns or [0] * len(self.counters)
        out: dict[str, float] = {}
        for (name, _), c, s in zip(self.spec.slots, self.counters, sum_ns):
            out[name] = float(s) / float(c) if c > 0 else 0.0
        return out


def run_multiplex_against(spec: MultiplexSpec,
                          workload_argv: list[str],
                          client: AgentdClient,
                          *,
                          out_dir: Path | None = None,
                          cwd: str = "") -> MultiplexRun:
    """End-to-end multiplex.run: compile spec to .bpf.o, send the
    multiplex.run RPC to agentd, wrap the reply in a MultiplexRun.

    Recompiles every call (cheap; clang takes ~100ms). The .bpf.o lives
    under build/multiplex_obj/<spec.name>.bpf.o; agentd must be able to
    read that path (default: same machine, world-readable build dir).
    """
    _c, obj_path = compile_multiplex(spec, out_dir=out_dir)
    rpc_args = {"obj_path": str(obj_path), "workload_argv": list(workload_argv)}
    if cwd:
        rpc_args["cwd"] = cwd
    r = client._rpc("multiplex.run", rpc_args)
    return MultiplexRun(
        ok=bool(r.get("ok")),
        spec=spec,
        obj_path=obj_path,
        wall_s=int(r.get("wall_s", 0)) / 1e9,
        exit_code=int(r.get("exit_code", -1)),
        stdout_tail=r.get("stdout_tail", ""),
        n_programs=int(r.get("n_programs", 0)),
        n_attached=int(r.get("n_attached", 0)),
        attach_errors=list(r.get("attach_errors", []) or []),
        counters=list(r.get("counters", []) or []),
        sum_ns=list(r.get("sum_ns", []) or []),
        error=r.get("error", ""),
    )


# ---- Inspection helpers ---------------------------------------------------

def list_sections(obj_path: Path) -> list[str]:
    """Return the list of SEC() section names in a compiled .bpf.o.

    Uses llvm-objdump if available, otherwise readelf -S. Does NOT require
    bpftool (which needs root for some inspection ops). The list is filtered
    to BPF section names: kprobe/*, kretprobe/*, tp/*, fentry/*, etc.
    """
    candidates = [["llvm-objdump", "-h", str(obj_path)],
                  ["objdump", "-h", str(obj_path)]]
    for cmd in candidates:
        if shutil.which(cmd[0]) is None:
            continue
        out = subprocess.run(cmd, capture_output=True, text=True)
        if out.returncode != 0:
            continue
        secs: list[str] = []
        for line in out.stdout.splitlines():
            for prefix in ("kprobe/", "kretprobe/", "tp/", "fentry/", "fexit/"):
                idx = line.find(prefix)
                if idx == -1:
                    continue
                tail = line[idx:].split()[0]
                # Drop sections referenced from a relocation table (objdump -h
                # often double-prints; dedupe at the caller).
                secs.append(tail)
                break
        return secs
    raise RuntimeError("neither llvm-objdump nor objdump available")
