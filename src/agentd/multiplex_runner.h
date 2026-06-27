/* Multiplexed probe runner.
 *
 * Loads a standalone .bpf.o (rendered by orchestrator/agentctl/multiplex.py),
 * auto-attaches every SEC() block via libbpf (kprobes + tracepoints alike),
 * runs a workload, reads back the per-slot counters from the PERCPU_ARRAY
 * map named `mx_counters`, and detaches.
 *
 * This bypasses the dispatcher PROG_ARRAY (HandlerRegistry) because:
 *   - The dispatcher exists for *agent-authored* probes (slot-managed,
 *     sandboxed, only ~13 hooks in total).
 *   - Multiplex probes are *orchestrator-authored*; their hook targets are
 *     known at template-render time and span ~60 kernel functions across
 *     the L1->L2->L3 tree -- well beyond what the dispatcher carries.
 *
 * Lifecycle is atomic per call: the .bpf.o is loaded, attached, the
 * workload is execed, counters are read, then the object is closed and
 * all kprobes/tps are released. There is no "hold" state across calls.
 */
#pragma once

#include <cstdint>
#include <string>
#include <vector>

namespace agentd {

struct MultiplexResult {
    bool ok = false;
    std::string error;

    /* Per-slot counters, summed across CPUs. counters.size() == map's
     * max_entries. */
    std::vector<uint64_t> counters;

    /* Per-slot accumulated nanoseconds, captured by kprobe+kretprobe
     * pairs in the generated BPF program (see multiplex.py). Same shape
     * as counters[]. Zero for slots whose probes are all tracepoints
     * (tracepoints have no exit hook) or whose kretprobes failed to
     * attach. Used to derive T_x = log2(sum_ns/work_unit + eps) on the
     * orchestrator side. */
    std::vector<uint64_t> sum_ns;

    /* Attachment summary -- useful when some kprobes/tps fail to bind
     * (e.g. symbol present in kallsyms but kprobe blacklisted). */
    int n_programs = 0;
    int n_attached = 0;
    std::vector<std::string> attach_errors;  /* human-readable per failure */

    /* Workload outcome -- bundled because the workload runs while the
     * probe is attached, and the caller wants both signals at once. */
    int exit_code = -1;
    double wall_s = 0.0;
    std::string stdout_tail;
};

MultiplexResult run_multiplex(const std::string &obj_path,
                              const std::vector<std::string> &workload_argv,
                              const std::string &cwd = "");

} // namespace agentd
