/* Runs a benchmark with `perf stat` wrapping it. Returns wall-clock and the
 * FV metric counts. perf-stat is invoked as a child process; its stderr
 * (CSV format) is parsed for counter values.
 *
 * The FV layout was redesigned for the L1->L2->L3 hierarchical detection
 * rearchitecture (see docs/hierarchical_subsystem_plan.md): 3 dram_contention
 * metrics dropped; metrics for vfs/network-rx/sync_rcu/locking/tlb-shootdown/
 * amdgpu added. Total 17 metric slots; the amdgpu slot (index 16) is
 * runtime-gated via fv_active() and fv_active_count() based on whether
 * amdgpu:amdgpu_iv is exposed by the running kernel (Phase15b: was
 * amdgpu_cs_ioctl, swapped because ROCm 7.x bypasses cs_ioctl).
 */
#pragma once

#include <array>
#include <cstdint>
#include <string>
#include <vector>

namespace agentd {

struct FeatureVector {
    static constexpr int N = 17;
    std::array<uint64_t, N> v{};   /* per-metric counts */
};

extern const char *fv_event_name(int idx);   /* perf event name for metric idx */
extern const char *fv_label(int idx);        /* short human label */
extern bool fv_active(int idx);              /* false for capability-gated slots whose event is absent */
extern int  fv_active_count();               /* number of slots where fv_active() == true */

struct WorkloadRunResult {
    bool ok = false;
    int exit_code = -1;
    double wall_s = 0.0;
    FeatureVector fv;
    std::string stdout_tail;     /* last ~1 KB of benchmark stdout */
    std::string error;
};

/* argv must NOT include `perf stat` - we add it. */
WorkloadRunResult run_workload(const std::vector<std::string> &argv,
                               const std::string &cwd = "");

} // namespace agentd
