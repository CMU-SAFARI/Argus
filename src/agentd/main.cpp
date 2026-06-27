/* agentd - AgenticBPF daemon (Day 3).
 *
 * - Loads & attaches the dispatcher BPF object
 * - Exposes JSON-over-AF_UNIX RPC for handler load/attach/output/detach
 */

#include <atomic>
#include <csignal>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <sched.h>
#include <unistd.h>

#include <bpf/libbpf.h>

#include "dispatcher.skel.h"
#include "handler_loader.h"
#include "ipc_server.h"

static std::atomic<bool> g_stop{false};

static void on_sigint(int) { g_stop.store(true); }

static int libbpf_print(enum libbpf_print_level lvl, const char *fmt, va_list args) {
    if (lvl == LIBBPF_DEBUG) return 0;
    return vfprintf(stderr, fmt, args);
}

int main(int argc, char **argv) {
    const char *sock_path = "/tmp/agentd.sock";
    for (int i = 1; i < argc; i++) {
        if (!std::strcmp(argv[i], "--help") || !std::strcmp(argv[i], "-h")) {
            std::printf("agentd - AgenticBPF daemon\n"
                        "Usage: sudo %s [--socket PATH]\n"
                        "  --socket PATH   AF_UNIX path for IPC (default %s)\n",
                        argv[0], sock_path);
            return 0;
        }
        if (!std::strcmp(argv[i], "--socket") && i + 1 < argc) {
            sock_path = argv[++i];
        }
    }

    libbpf_set_print(libbpf_print);
    std::signal(SIGINT, on_sigint);
    std::signal(SIGTERM, on_sigint);
    std::signal(SIGPIPE, SIG_IGN);

    /* Pin agentd's affinity to the workload's expected CPU set. On Intel
     * hybrid CPUs (e.g. i9-14900K), `cpu_core/...` PMU events only resolve
     * on P-cores; if agentd's affinity includes E-cores (8-15), perf-stat
     * processes spawned by agentd inherit the wider mask and the kernel
     * silently zeroes those events on the E-core CPUs. taskset on the
     * workload's cmd line isn't enough -- the parent's affinity also
     * matters. Setting agentd's own affinity to the same set as
     * AGENTICBPF_PIN_CPUS keeps event resolution correct. */
    {
        const char *pin_env = ::getenv("AGENTICBPF_PIN_CPUS");
        std::string pin = pin_env ? pin_env : "0-7";
        if (!pin.empty()) {
            cpu_set_t mask;
            CPU_ZERO(&mask);
            /* Parse comma-separated ranges like "0-7,12,16-19". */
            size_t i = 0;
            while (i < pin.size()) {
                size_t j = pin.find(',', i);
                if (j == std::string::npos) j = pin.size();
                std::string tok = pin.substr(i, j - i);
                size_t dash = tok.find('-');
                int lo, hi;
                if (dash == std::string::npos) {
                    lo = hi = std::atoi(tok.c_str());
                } else {
                    lo = std::atoi(tok.substr(0, dash).c_str());
                    hi = std::atoi(tok.substr(dash + 1).c_str());
                }
                for (int c = lo; c <= hi && c < CPU_SETSIZE; c++) CPU_SET(c, &mask);
                i = j + 1;
            }
            if (sched_setaffinity(0, sizeof(mask), &mask) != 0) {
                std::fprintf(stderr, "[agentd] sched_setaffinity(%s) failed: %s\n",
                             pin.c_str(), std::strerror(errno));
            } else {
                std::fprintf(stderr, "[agentd] affinity pinned to %s\n", pin.c_str());
            }
        }
    }

    /* 1. Load + attach the dispatcher. */
    struct dispatcher_bpf *skel = dispatcher_bpf__open_and_load();
    if (!skel) {
        std::fprintf(stderr, "[agentd] failed to open/load dispatcher skeleton\n");
        return 1;
    }
    if (dispatcher_bpf__attach(skel)) {
        std::fprintf(stderr, "[agentd] failed to attach dispatcher\n");
        dispatcher_bpf__destroy(skel);
        return 1;
    }
    int kp_fd = bpf_map__fd(skel->maps.kprobe_dispatch);
    int tp_fd = bpf_map__fd(skel->maps.tp_dispatch);
    std::fprintf(stderr, "[agentd] dispatcher attached "
                         "(kprobe_dispatch_fd=%d, tp_dispatch_fd=%d)\n",
                         kp_fd, tp_fd);

    /* 2. Spin up the registry + IPC server. */
    agentd::HandlerRegistry reg(kp_fd, tp_fd);
    agentd::IpcServer ipc(sock_path, reg);
    if (ipc.start()) {
        dispatcher_bpf__destroy(skel);
        return 1;
    }
    std::fprintf(stderr, "[agentd] ready - send SIGINT to exit\n");

    /* 3. Serve until signaled. */
    ipc.run(g_stop);

    std::fprintf(stderr, "[agentd] shutting down\n");
    dispatcher_bpf__destroy(skel);
    return 0;
}
