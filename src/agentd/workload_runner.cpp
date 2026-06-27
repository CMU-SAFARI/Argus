#include "workload_runner.h"

#include <cerrno>
#include <chrono>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <fstream>
#include <sstream>
#include <fcntl.h>
#include <signal.h>
#include <spawn.h>
#include <sys/prctl.h>
#include <sys/stat.h>
#include <sys/wait.h>
#include <unistd.h>

extern char **environ;

namespace agentd {

/* Feature Vector layout for the L1->L2->L3 hierarchical detection
 * rearchitecture. See docs/hierarchical_subsystem_plan.md.
 *
 * 3 dram_contention metrics dropped (llc_load_misses, cache_misses,
 * stalls_l3_miss). Added: net_dev_rx, vfs_read, vfs_write, rcu_utilization,
 * lock_contention, tlb_flush, amdgpu_irq. The amdgpu slot is
 * capability-gated at runtime via fv_active(): if the kernel does not
 * expose amdgpu:amdgpu_iv (no AMD GPU loaded), that slot is omitted
 * from the perf event list and stays at 0 in the FV. Phase15b: this
 * slot was amdgpu_cs_ioctl until we discovered ROCm 7.x bypasses it
 * via userspace doorbells; amdgpu_iv (completion IRQs) is the modern
 * kernel-visible signal of HIP compute activity.
 *
 * Each metric is annotated with its attribution category:
 *
 *   [OS-direct]      Tracepoint or sw event tied directly to a kernel
 *                    code path. Flagged with high confidence; no agent
 *                    eBPF probe needed to interpret it.
 *
 *   [OS-via-eBPF]    Hardware counter that a perf-stat aggregate cannot
 *                    attribute on its own. The agent's job is to author
 *                    a kprobe that decomposes the count by kernel hook.
 *
 *   [Denominator]    Used only for normalization. cpu-cycles only.
 *
 * Hardware-PMU events are explicitly qualified to cpu_core/ so they work on
 * Intel hybrid CPUs (e.g. i9-14900K, where unqualified hw events emit one line
 * per PMU type and our parser matches neither). On homogeneous CPUs the
 * cpu_core/ alias still resolves; lab-tested on Raptor Lake-S + kernel 6.17.
 *
 * Per-event collection scope. WORKLOAD = perf stat on the victim's process
 * tree. SYSTEM = perf stat -a (system-wide); needed for tracepoints that
 * fire in foreign PIDs.
 */
enum Scope : uint8_t { WORKLOAD = 0, SYSTEM = 1 };

static const char *kEvents[FeatureVector::N] = {
    "cpu_core/dTLB-load-misses/",   /* 0:  [OS-via-eBPF] TLB load pressure         */
    "cpu_core/dTLB-store-misses/",  /* 1:  [OS-via-eBPF] TLB store-side            */
    "page-faults",                  /* 2:  [OS-direct]   __handle_mm_fault path    */
    "kmem:mm_page_alloc",           /* 3:  [OS-direct]   page alloc                */
    "vmscan:mm_vmscan_kswapd_wake", /* 4:  [OS-direct]   kswapd wakeup             */
    "sched:sched_switch",           /* 5:  [OS-direct]   scheduler context-switch  */
    "context-switches",             /* 6:  [OS-direct]   sw event, preemption      */
    "cpu_core/cpu-cycles/",         /* 7:  [Denominator] not in any subsystem      */
    "block:block_rq_complete",      /* 8:  [OS-direct]   block-layer completions   */
    "net:net_dev_xmit",             /* 9:  [OS-direct]   tx packets (incl lo)      */
    "net:netif_receive_skb",        /* 10: [OS-direct]   rx softirq                */
    "syscalls:sys_enter_read",      /* 11: [OS-direct]   read() syscall rate       */
    "syscalls:sys_enter_write",     /* 12: [OS-direct]   write() syscall rate      */
    "rcu:rcu_utilization",          /* 13: [OS-direct]   RCU grace-period activity */
    "lock:contention_begin",        /* 14: [OS-direct]   mutex/rwsem/spinlock      */
    "tlb:tlb_flush",                /* 15: [OS-direct]   TLB shootdown / mm flush  */
    "amdgpu:amdgpu_cs_ioctl",       /* 16: [OS-direct, gated] AMDGPU command-stream
                                        submission rate. Restored 2026-05-17 after
                                        empirical re-validation: on a Mesa RADV
                                        Vulkan workload (llama.cpp -DGGML_VULKAN=ON,
                                        gfx1201 = AMD Radeon AI PRO R9700),
                                        amdgpu_cs_ioctl fires at ~470 Hz under
                                        inference and is the only amdgpu tracepoint
                                        that runs in the *caller's* PID context
                                        (verified by bpftrace: 648/648 events have
                                        tgid==llama-completion's PID). amdgpu_iv,
                                        amdgpu_sched_run_job, and amdgpu_ib_schedule
                                        all fire in kworker threads, so PID-attribution
                                        is impossible for them. The Phase15b switch
                                        to amdgpu_iv was based on a measurement that
                                        suffered from GPU-low-power-state + a
                                        bpftrace signed/unsigned comparison bug; the
                                        ROCm 7.x cs_ioctl-bypass note only applies
                                        to KFD compute submissions via userspace
                                        doorbells, NOT to Vulkan or HIP graphics
                                        paths -- both still hit cs_ioctl.          */
};
static const Scope kScope[FeatureVector::N] = {
    WORKLOAD,                /* dTLB-load-misses: victim's own pointer chase     */
    WORKLOAD,                /* dTLB-store-misses: victim-internal               */
    WORKLOAD,                /* page-faults: victim's own faults dominate        */
    WORKLOAD,                /* kmem:mm_page_alloc: victim-bound allocator path  */
    WORKLOAD,                /* kswapd_wake: victim triggers reclaim under pressure */
    SYSTEM,                  /* sched:sched_switch: external co-runner preempts  */
                              /*   victim; WORKLOAD only sees victim-on/off-CPU.  */
                              /*   SYSTEM catches all switches incl. co-runners'. */
    SYSTEM,                  /* context-switches: same rationale as sched_switch */
    WORKLOAD,                /* cpu-cycles: denominator (per-victim)             */
    SYSTEM,                  /* block_rq_complete: I/O aggressor fires in own PID */
    SYSTEM,                  /* net_dev_xmit: tx softirq is host-wide             */
    SYSTEM,                  /* netif_receive_skb: rx softirq is host-wide        */
    WORKLOAD,                /* sys_enter_read                                    */
    WORKLOAD,                /* sys_enter_write                                   */
    SYSTEM,                  /* rcu_utilization: kthread context (rcu_*)          */
    WORKLOAD,                /* lock:contention_begin                             */
    SYSTEM,                  /* tlb:tlb_flush: madvise/mprotect storms fire in    */
                              /*   storm's PID; WORKLOAD-scoped sees ~0 even when */
                              /*   storm is active. SYSTEM catches the IPI count. */
    WORKLOAD,                /* amdgpu_irq: post-Phase15c (2026-05-17) this slot is */
                              /*   bound to amdgpu_cs_ioctl, which runs in the       */
                              /*   *caller's* PID syscall context (verified). PID-   */
                              /*   filtered measurement therefore attributes GPU     */
                              /*   submission activity to the victim correctly --    */
                              /*   strictly more useful than the previous SYSTEM     */
                              /*   scope, which conflated victim and aggressor GPU.  */
};
static const char *kLabels[FeatureVector::N] = {
    "dtlb_load_misses",   "dtlb_store_misses", "page_faults",
    "kmem_alloc",         "kswapd_wake",
    "sched_switch",       "context_switches",  "cpu_cycles",
    "block_rq_complete",  "net_dev_xmit",      "net_dev_rx",
    "vfs_read",           "vfs_write",
    "rcu_utilization",    "lock_contention",   "tlb_flush",
    "amdgpu_irq",         /* was amdgpu_cs_ioctl pre-Phase15b; renamed when
                             we switched the underlying tracepoint from
                             amdgpu_cs_ioctl (ROCm-bypassed) to amdgpu_iv
                             (completion IRQs). Same FV slot index 16. */
};

/* Capability slots: each FV index has a tracing-events path whose presence
 * decides whether the event is included in the perf invocation. Empty
 * string = always-active (no gate). The check is "exists at /sys/kernel/
 * tracing/events/<path>/id" or the legacy debugfs path. Result is cached
 * on first call. */
static const char *kTracingPath[FeatureVector::N] = {
    "", "", "", "", "",                    /* 0..4: hw events + page_faults + kmem + vmscan, always present */
    "", "", "",                            /* 5..7: sched_switch + sw events, always present */
    "", "", "",                            /* 8..10: block + net (always present) */
    "", "",                                /* 11..12: syscalls (always present on enabled syscall tracing) */
    "", "", "",                            /* 13..15: rcu + lock + tlb (always present) */
    "amdgpu/amdgpu_cs_ioctl",              /* 16: gated -- requires amdgpu module loaded */
};

static bool tracing_event_exists(const char *rel) {
    if (!rel || !*rel) return true;
    /* Try modern /sys/kernel/tracing first, fall back to debugfs path. */
    std::string p1 = std::string("/sys/kernel/tracing/events/") + rel + "/id";
    std::string p2 = std::string("/sys/kernel/debug/tracing/events/") + rel + "/id";
    struct stat st;
    return ::stat(p1.c_str(), &st) == 0 || ::stat(p2.c_str(), &st) == 0;
}

bool fv_active(int idx) {
    if (idx < 0 || idx >= FeatureVector::N) return false;
    static int cache[FeatureVector::N] = {0}; /* 0 = unknown, 1 = active, -1 = inactive */
    if (cache[idx] == 0) {
        cache[idx] = tracing_event_exists(kTracingPath[idx]) ? 1 : -1;
    }
    return cache[idx] == 1;
}

int fv_active_count() {
    int n = 0;
    for (int i = 0; i < FeatureVector::N; i++) if (fv_active(i)) n++;
    return n;
}

const char *fv_event_name(int idx) { return (idx >= 0 && idx < FeatureVector::N) ? kEvents[idx] : ""; }
const char *fv_label(int idx)      { return (idx >= 0 && idx < FeatureVector::N) ? kLabels[idx] : ""; }

namespace {

/* Read all of fd into a string. Closes fd. */
std::string slurp(int fd) {
    std::string out;
    char buf[4096];
    while (true) {
        ssize_t n = ::read(fd, buf, sizeof(buf));
        if (n <= 0) break;
        out.append(buf, n);
    }
    ::close(fd);
    return out;
}

/* perf stat -x, output line: <count>,<unit>,<event>,<runtime>,<pct>,...
 * Some lines start with timestamp, or with <not counted>, or are headers.
 * We just look for our event name in field 3 (0-indexed 2). */
void parse_perf_csv(const std::string &csv, FeatureVector &fv) {
    std::istringstream is(csv);
    std::string line;
    while (std::getline(is, line)) {
        if (line.empty() || line[0] == '#') continue;
        /* Split on commas. */
        std::vector<std::string> fields;
        size_t start = 0;
        for (size_t i = 0; i <= line.size(); i++) {
            if (i == line.size() || line[i] == ',') {
                fields.push_back(line.substr(start, i - start));
                start = i + 1;
            }
        }
        if (fields.size() < 3) continue;
        const std::string &count = fields[0];
        const std::string &event = fields[2];
        for (int k = 0; k < FeatureVector::N; k++) {
            if (event == kEvents[k]) {
                /* "<not counted>" / "<not supported>" -> 0 */
                if (count.empty() || count[0] == '<') { fv.v[k] = 0; break; }
                try { fv.v[k] = static_cast<uint64_t>(std::stoll(count)); }
                catch (...) { fv.v[k] = 0; }
                break;
            }
        }
    }
}

} // namespace

WorkloadRunResult run_workload(const std::vector<std::string> &argv,
                               const std::string &cwd) {
    WorkloadRunResult r;
    if (argv.empty()) { r.error = "empty argv"; return r; }

    /* Build two evlists - workload-scoped vs system-wide.
     *
     * Workload-scoped: classic `perf stat -e <ev> -- victim`. Counts events
     * tied to the victim's process tree only. Right for HW PMU events and
     * tracepoints that fire on the victim's own PID.
     *
     * System-wide: classic `perf stat -a -e <ev> -- sleep <wall>`. Captures
     * tracepoints that fire on a foreign PID (an I/O aggressor's dd, a
     * network aggressor's tx softirq) but should still be attributed to
     * the victim's measurement window. We launch this in parallel with
     * the workload-scoped run, then SIGINT it once the victim exits.
     * perf stat catches SIGINT and prints final counters before exiting.
     */
    std::string ev_workload, ev_system;
    for (int i = 0; i < FeatureVector::N; i++) {
        if (!fv_active(i)) continue;   /* capability-gated event absent on this host */
        std::string &dst = (kScope[i] == SYSTEM) ? ev_system : ev_workload;
        if (!dst.empty()) dst += ',';
        dst += kEvents[i];
    }

    /* Pin perf+workload to a stable CPU set. Default is P-cores 0-7 on the
     * lab i9-14900K (Raptor Lake-S hybrid). On homogeneous CPUs the default
     * is harmless: cores 0-7 always exist on x86_64 boxes we target.
     * Override via AGENTICBPF_PIN_CPUS (e.g. "0-15" for an EPYC, "" to
     * disable pinning entirely). Pinning gives the reference profiler a
     * stable PMU type and avoids E-core/P-core scheduler bounce. */
    std::vector<std::string> cmd;
    const char *pin_env = ::getenv("AGENTICBPF_PIN_CPUS");
    std::string pin = pin_env ? pin_env : "0-7";
    if (!pin.empty()) {
        cmd = {"taskset", "-c", pin, "perf", "stat", "-x,", "-e", ev_workload, "--"};
    } else {
        cmd = {"perf", "stat", "-x,", "-e", ev_workload, "--"};
    }
    cmd.insert(cmd.end(), argv.begin(), argv.end());

    /* IMPORTANT ordering: launch system-wide perf-stat FIRST, before
     * creating workload pipes. If we created workload pipes first, the
     * system-wide perf-stat process would inherit their write ends, hold
     * them open, and our slurp() of the workload's pipe-read-end would
     * block forever waiting for EOF (since the system-wide perf-stat
     * outlives the workload). */

    /* Launch the system-wide perf stat *before* the workload, so it's
     * already collecting when the victim's first event fires. We invoke:
     *   perf stat -x, -a -e <SYS_EVS>
     * with NO trailing command. perf-stat then collects system-wide until
     * interrupted; on SIGINT it prints final counters to stderr and exits
     * 130. (Earlier we used `-- sleep 86400` but that hangs because perf
     * doesn't always propagate its termination signal to the child sleep,
     * so wait() blocks until the sleep itself finishes.) */
    pid_t sys_pid = -1;
    int sys_err_pipe[2] = {-1, -1};
    if (!ev_system.empty() && ::getenv("AGENTICBPF_DISABLE_SYSWIDE") == nullptr) {
        if (::pipe(sys_err_pipe) == 0) {
            std::vector<std::string> sys_cmd = {
                "perf", "stat", "-x,", "-a", "-e", ev_system
            };
            posix_spawn_file_actions_t sfa;
            posix_spawn_file_actions_init(&sfa);
            posix_spawn_file_actions_addclose(&sfa, sys_err_pipe[0]);
            /* Redirect stdout to /dev/null; we only care about stderr CSV. */
            posix_spawn_file_actions_addopen(&sfa, 1, "/dev/null", O_WRONLY, 0);
            posix_spawn_file_actions_adddup2(&sfa, sys_err_pipe[1], 2);
            posix_spawn_file_actions_addclose(&sfa, sys_err_pipe[1]);

            std::vector<char *> sys_raw;
            for (auto &s : sys_cmd) sys_raw.push_back(const_cast<char *>(s.c_str()));
            sys_raw.push_back(nullptr);

            int srrc = ::posix_spawnp(&sys_pid, "perf", &sfa, nullptr,
                                      sys_raw.data(), environ);
            posix_spawn_file_actions_destroy(&sfa);
            ::close(sys_err_pipe[1]);
            if (srrc != 0) {
                /* System-wide collection failed to launch; the workload run
                 * still proceeds with the workload-scoped events. */
                ::close(sys_err_pipe[0]);
                sys_err_pipe[0] = -1;
                sys_pid = -1;
            }
        }
    }

    /* Create workload pipes AFTER system-wide perf-stat is already running,
     * so that system-wide perf-stat doesn't inherit them. */
    int out_pipe[2], err_pipe[2];
    if (::pipe(out_pipe) || ::pipe(err_pipe)) {
        r.error = "pipe failed";
        if (sys_pid > 0) { ::kill(sys_pid, SIGINT); ::waitpid(sys_pid, nullptr, 0); }
        if (sys_err_pipe[0] >= 0) ::close(sys_err_pipe[0]);
        return r;
    }

    std::vector<char *> raw;
    for (auto &s : cmd) raw.push_back(const_cast<char *>(s.c_str()));
    raw.push_back(nullptr);

    /* Use fork()+exec() rather than posix_spawn. posix_spawn on glibc uses
     * vfork-like CLONE_VM, which has been observed to interact badly with
     * perf-event attribution: workload-side counters (dTLB misses, page
     * faults, kmem tracepoints) come back near-zero on long-running, heavy
     * workloads even though the perf-stat invocation looks identical to a
     * working shell command. fork() drops the CLONE_VM and the events
     * count correctly. */
    pid_t pid;
    auto t0 = std::chrono::steady_clock::now();
    pid = ::fork();
    if (pid < 0) {
        r.error = std::string("fork: ") + std::strerror(errno);
        ::close(out_pipe[0]); ::close(out_pipe[1]);
        ::close(err_pipe[0]); ::close(err_pipe[1]);
        if (sys_pid > 0) { ::kill(sys_pid, SIGTERM); ::waitpid(sys_pid, nullptr, 0); }
        if (sys_err_pipe[0] >= 0) ::close(sys_err_pipe[0]);
        return r;
    }
    if (pid == 0) {
        /* child */
        ::close(out_pipe[0]);
        ::close(err_pipe[0]);
        ::dup2(out_pipe[1], 1);
        ::dup2(err_pipe[1], 2);
        ::close(out_pipe[1]);
        ::close(err_pipe[1]);
        /* Restore PR_SET_DUMPABLE=1: when agentd has elevated capabilities
         * (CAP_BPF, CAP_PERFMON, ...), the kernel sets dumpable=0 on
         * agentd, which is inherited by all forked children. Some
         * perf_event paths (notably dTLB and tracepoint-on-task events)
         * silently report near-zero counts on dumpable=0 tasks because
         * /proc/pid is hardened. Resetting dumpable=1 in the child
         * BEFORE exec() makes perf-event attribution work normally. */
        ::prctl(PR_SET_DUMPABLE, 1, 0, 0, 0);
        if (!cwd.empty()) {
            if (::chdir(cwd.c_str()) != 0) {
                ::fprintf(stderr, "[agentd-child] chdir(%s) failed: %s\n",
                          cwd.c_str(), std::strerror(errno));
                ::_exit(127);
            }
        }
        ::execvp(cmd[0].c_str(), raw.data());
        ::fprintf(stderr, "[agentd-child] execvp(%s) failed: %s\n",
                  cmd[0].c_str(), std::strerror(errno));
        ::_exit(127);
    }
    /* parent */
    ::close(out_pipe[1]);
    ::close(err_pipe[1]);

    /* Read both pipes serially - fine for short runs (<60s). */
    std::string out = slurp(out_pipe[0]);
    std::string err = slurp(err_pipe[0]);
    int status = 0;
    ::waitpid(pid, &status, 0);
    auto t1 = std::chrono::steady_clock::now();

    /* Stop the system-wide collector now that the victim has exited.
     * SIGINT triggers perf stat's signal handler which prints final
     * counters to stderr; SIGTERM/SIGKILL would skip that path. */
    std::string sys_err;
    if (sys_pid > 0) {
        int kr = ::kill(sys_pid, SIGINT);
        ::fprintf(stderr, "[agentd] kill(%d, SIGINT) = %d errno=%d\n",
                  sys_pid, kr, errno);
        sys_err = slurp(sys_err_pipe[0]); /* blocks until perf stat closes stderr */
        ::fprintf(stderr, "[agentd] sys_err len=%zu\n", sys_err.size());
        ::waitpid(sys_pid, nullptr, 0);
    } else if (sys_err_pipe[0] >= 0) {
        ::close(sys_err_pipe[0]);
    }

    r.wall_s = std::chrono::duration<double>(t1 - t0).count();
    r.exit_code = WIFEXITED(status) ? WEXITSTATUS(status) : -1;
    r.ok = (r.exit_code == 0);

    /* Tail of stdout for the JSON reply. */
    if (out.size() > 1024) r.stdout_tail = out.substr(out.size() - 1024);
    else r.stdout_tail = out;

    /* Parse both CSVs into the same FV. The two evlists are disjoint so
     * neither overwrites the other. */
    ::fprintf(stderr, "[agentd] WORKLOAD perf stderr (%zu bytes):\n%s\n[agentd] END WORKLOAD\n",
              err.size(), err.c_str());
    ::fprintf(stderr, "[agentd] SYSTEM perf stderr (%zu bytes):\n%s\n[agentd] END SYSTEM\n",
              sys_err.size(), sys_err.c_str());
    parse_perf_csv(err, r.fv);
    if (!sys_err.empty()) parse_perf_csv(sys_err, r.fv);

    if (!r.ok && r.error.empty()) {
        /* Surface the perf+benchmark stderr on failure for debugging. */
        r.error = err.substr(0, 2048);
    }
    return r;
}

} // namespace agentd
