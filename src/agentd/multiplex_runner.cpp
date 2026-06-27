#include "multiplex_runner.h"

#include <algorithm>
#include <cerrno>
#include <chrono>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <unistd.h>
#include <signal.h>
#include <sys/prctl.h>
#include <sys/wait.h>

#include <bpf/bpf.h>
#include <bpf/libbpf.h>

namespace agentd {

namespace {

/* Read all of fd into a string, then close it. Same pattern as workload_runner. */
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

/* Fork+exec the workload, return wall-time + exit_code + stdout tail.
 * Mirrors workload_runner.cpp's child setup (PR_SET_DUMPABLE, optional
 * chdir) but without the perf-stat wrapping. We deliberately do NOT pin
 * via taskset here -- pinning is what the perf-stat path needs to keep
 * cpu_core/ events resolving on hybrid CPUs; for the multiplex path the
 * eBPF probes attach on every CPU regardless. */
struct ChildResult {
    bool ok = false;
    int exit_code = -1;
    double wall_s = 0.0;
    std::string stdout_tail;
    std::string error;
};

/* Fork the workload, but block it pre-execvp so the caller can stamp
 * its TGID into the mx_victim_pids BPF map BEFORE any user code runs.
 * Otherwise the probes fire on every process system-wide (the original
 * RC1 contamination bug).
 *
 * Synchronization: a one-byte pipe. Child closes the write end, blocks
 * on read(); parent populates the BPF map then writes one byte to
 * release the child. */
ChildResult exec_bare(const std::vector<std::string> &argv, const std::string &cwd,
                      int victim_pids_fd) {
    ChildResult r;
    if (argv.empty()) { r.error = "empty argv"; return r; }

    int out_pipe[2], err_pipe[2], sync_pipe[2];
    if (::pipe(out_pipe) || ::pipe(err_pipe) || ::pipe(sync_pipe)) {
        r.error = std::string("pipe: ") + std::strerror(errno);
        return r;
    }

    auto t0 = std::chrono::steady_clock::now();
    pid_t pid = ::fork();
    if (pid < 0) {
        r.error = std::string("fork: ") + std::strerror(errno);
        ::close(out_pipe[0]); ::close(out_pipe[1]);
        ::close(err_pipe[0]); ::close(err_pipe[1]);
        ::close(sync_pipe[0]); ::close(sync_pipe[1]);
        return r;
    }
    if (pid == 0) {
        /* child */
        ::close(out_pipe[0]); ::close(err_pipe[0]);
        ::close(sync_pipe[1]);  /* child reads */
        /* Block until the parent has stamped our PID into the BPF map.
         * Otherwise the probes attach but never fire for us (filter
         * rejects everything) on the early instructions of execvp. */
        char go;
        ssize_t n = ::read(sync_pipe[0], &go, 1);
        ::close(sync_pipe[0]);
        if (n != 1) {
            std::fprintf(stderr, "[mx-child] sync read failed (n=%zd)\n", n);
            ::_exit(126);
        }
        ::dup2(out_pipe[1], 1); ::dup2(err_pipe[1], 2);
        ::close(out_pipe[1]); ::close(err_pipe[1]);
        /* Same dumpable=1 reset as the perf-stat workload path -- some
         * kprobe types misattribute on dumpable=0 children. */
        ::prctl(PR_SET_DUMPABLE, 1, 0, 0, 0);
        if (!cwd.empty() && ::chdir(cwd.c_str()) != 0) {
            std::fprintf(stderr, "[mx-child] chdir(%s) failed: %s\n",
                         cwd.c_str(), std::strerror(errno));
            ::_exit(127);
        }
        std::vector<char *> raw;
        for (auto &s : argv) raw.push_back(const_cast<char *>(s.c_str()));
        raw.push_back(nullptr);
        ::execvp(argv[0].c_str(), raw.data());
        std::fprintf(stderr, "[mx-child] execvp(%s) failed: %s\n",
                     argv[0].c_str(), std::strerror(errno));
        ::_exit(127);
    }
    /* parent */
    ::close(out_pipe[1]); ::close(err_pipe[1]);
    ::close(sync_pipe[0]);  /* parent writes */

    /* Stamp the child's TGID into the BPF filter map. The probes already
     * check both current->tgid AND current->real_parent->tgid, so any
     * direct children spawned by the workload (e.g. fork_storm's ~1000
     * kids) will pass the filter too. */
    if (victim_pids_fd >= 0) {
        uint32_t key = static_cast<uint32_t>(pid);
        uint8_t val = 1;
        if (bpf_map_update_elem(victim_pids_fd, &key, &val, BPF_ANY) != 0) {
            std::fprintf(stderr, "[mx] mx_victim_pids insert pid=%u failed: %s\n",
                         key, std::strerror(errno));
        }
    }
    /* Release the child to execvp. */
    char go = 'x';
    (void)::write(sync_pipe[1], &go, 1);
    ::close(sync_pipe[1]);

    std::string out = slurp(out_pipe[0]);
    /* Drain stderr too so the child doesn't block on a full pipe. */
    (void)slurp(err_pipe[0]);
    int status = 0;
    ::waitpid(pid, &status, 0);
    auto t1 = std::chrono::steady_clock::now();

    r.wall_s = std::chrono::duration<double>(t1 - t0).count();
    r.exit_code = WIFEXITED(status) ? WEXITSTATUS(status) : -1;
    r.ok = (r.exit_code == 0);
    if (out.size() > 1024) r.stdout_tail = out.substr(out.size() - 1024);
    else r.stdout_tail = out;
    return r;
}

/* Sum a u64-valued PERCPU_ARRAY across all CPUs into out[]. */
bool dump_percpu_u64(int map_fd, int n_slots, std::vector<uint64_t> &out) {
    int n_cpus = libbpf_num_possible_cpus();
    if (n_cpus <= 0) return false;
    out.assign(n_slots, 0);
    std::vector<uint64_t> percpu(n_cpus);
    for (int i = 0; i < n_slots; i++) {
        uint32_t key = static_cast<uint32_t>(i);
        if (bpf_map_lookup_elem(map_fd, &key, percpu.data()) != 0) {
            std::fprintf(stderr, "[mx] PERCPU_ARRAY lookup slot=%d failed: %s\n",
                         i, std::strerror(errno));
            return false;
        }
        uint64_t sum = 0;
        for (int c = 0; c < n_cpus; c++) sum += percpu[c];
        out[i] = sum;
    }
    return true;
}

} // namespace

MultiplexResult run_multiplex(const std::string &obj_path,
                              const std::vector<std::string> &workload_argv,
                              const std::string &cwd) {
    MultiplexResult r;

    /* 1. Open + load the .bpf.o. */
    struct bpf_object *obj = bpf_object__open_file(obj_path.c_str(), nullptr);
    if (!obj || libbpf_get_error(obj)) {
        r.error = "bpf_object__open_file failed: " + std::string(std::strerror(errno));
        if (obj) bpf_object__close(obj);
        return r;
    }
    if (bpf_object__load(obj)) {
        r.error = "bpf_object__load failed: " + std::string(std::strerror(errno));
        bpf_object__close(obj);
        return r;
    }

    /* 2. Auto-attach every program. libbpf parses SEC("kprobe/...") /
     *    SEC("tp/...") / etc. and attaches accordingly. We collect the
     *    returned bpf_link*s for later cleanup. A single attachment
     *    failure is non-fatal: the multiplex still produces useful data
     *    from the probes that did attach. */
    std::vector<struct bpf_link *> links;
    struct bpf_program *prog;
    bpf_object__for_each_program(prog, obj) {
        r.n_programs++;
        const char *sec = bpf_program__section_name(prog);
        const char *pname = bpf_program__name(prog);
        struct bpf_link *l = bpf_program__attach(prog);
        if (!l || libbpf_get_error(l)) {
            int e = -static_cast<int>(libbpf_get_error(l));
            r.attach_errors.push_back(std::string(pname) + " (" + sec + "): "
                                      + std::strerror(e ? e : errno));
            continue;
        }
        links.push_back(l);
        r.n_attached++;
    }

    /* 3. Locate the mx_counters PERCPU_ARRAY map (and optionally mx_sum_ns
     *    -- present when the renderer was built with the T_x extension,
     *    absent on legacy objects). */
    struct bpf_map *map = bpf_object__find_map_by_name(obj, "mx_counters");
    int map_fd = map ? bpf_map__fd(map) : -1;
    int n_slots = map ? static_cast<int>(bpf_map__max_entries(map)) : 0;
    if (map_fd < 0 || n_slots <= 0) {
        r.error = "mx_counters PERCPU_ARRAY not found in object (or zero entries)";
        for (auto *l : links) if (l) bpf_link__destroy(l);
        bpf_object__close(obj);
        return r;
    }

    struct bpf_map *map_ns = bpf_object__find_map_by_name(obj, "mx_sum_ns");
    int map_ns_fd = map_ns ? bpf_map__fd(map_ns) : -1;

    /* mx_victim_pids: present when built with the PID-filter template.
     * Required for correct attribution -- absent means the .bpf.o predates
     * the filter and would measure system-wide activity, which we no
     * longer want. We still proceed (fd<0 disables the stamp), but log it. */
    struct bpf_map *map_vp = bpf_object__find_map_by_name(obj, "mx_victim_pids");
    int map_vp_fd = map_vp ? bpf_map__fd(map_vp) : -1;
    if (map_vp_fd < 0) {
        std::fprintf(stderr, "[mx] WARNING mx_victim_pids map not found in %s "
                              "-- probes will fire system-wide (legacy template)\n",
                     obj_path.c_str());
    }

    /* 4. Run the workload. exec_bare blocks the child between fork() and
     *    execvp() so we can stamp its TGID into mx_victim_pids first;
     *    otherwise the filter would reject everything during early exec. */
    ChildResult cr = exec_bare(workload_argv, cwd, map_vp_fd);
    r.exit_code = cr.exit_code;
    r.wall_s = cr.wall_s;
    r.stdout_tail = cr.stdout_tail;
    if (!cr.error.empty()) {
        r.error = "workload: " + cr.error;
        /* still try to read counters -- the probes may have caught some
         * activity before fork() failed */
    }

    /* 5. Read the counters. */
    if (!dump_percpu_u64(map_fd, n_slots, r.counters)) {
        if (r.error.empty()) r.error = "PERCPU_ARRAY readback failed";
    }
    /* sum_ns is optional: present only with the T_x-extended template.
     * On failure or absence we leave sum_ns empty and let the orchestrator
     * fall back to count-only scoring for this slot. */
    if (map_ns_fd >= 0) {
        if (!dump_percpu_u64(map_ns_fd, n_slots, r.sum_ns)) {
            std::fprintf(stderr, "[mx] mx_sum_ns readback failed; "
                                  "falling back to count-only for this run\n");
            r.sum_ns.clear();
        }
    }

    /* 6. Cleanup. */
    for (auto *l : links) if (l) bpf_link__destroy(l);
    bpf_object__close(obj);

    /* "ok" means: workload exited 0 AND we read counters successfully.
     * Partial attach (some probes failed) is NOT an error here -- the
     * caller can inspect attach_errors and decide. */
    r.ok = cr.ok && r.error.empty();
    return r;
}

} // namespace agentd
