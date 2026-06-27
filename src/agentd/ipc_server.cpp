#include "ipc_server.h"

#include <cerrno>
#include <cstdio>
#include <cstring>
#include <string>
#include <sys/select.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/un.h>
#include <unistd.h>

#include <bpf/bpf.h>

#include "json.h"
#include "multiplex_runner.h"
#include "profiler_types.h"
#include "workload_runner.h"

namespace agentd {

namespace {

using namespace agentd::json;

Value make_ok(Value result) {
    Object o;
    o.emplace("ok", Value(true));
    o.emplace("result", std::move(result));
    return Value(std::move(o));
}

Value make_err(const std::string &error, const std::string &detail = "") {
    Object o;
    o.emplace("ok", Value(false));
    o.emplace("error", Value(error));
    if (!detail.empty()) o.emplace("detail", Value(detail));
    return Value(std::move(o));
}

SlotKind parse_kind(const std::string &s) {
    if (s == "kp" || s == "kprobe") return SlotKind::Kprobe;
    return SlotKind::Tracepoint;
}

const char *kind_str(SlotKind k) {
    return (k == SlotKind::Kprobe) ? "kprobe" : "tracepoint";
}

/* Sum agent_output across all CPU entries of a BPF_MAP_TYPE_ARRAY. */
bool dump_agent_output(int map_fd, uint64_t out[AGENT_OUTPUT_COUNTERS]) {
    for (int i = 0; i < AGENT_OUTPUT_COUNTERS; i++) out[i] = 0;
    /* Walk all keys (CPU indices 0..max_entries-1). bpf_map__max_entries
     * is not directly available from the fd alone, so iterate up to a
     * generous cap and stop on lookup failure. */
    struct agent_output_block blk;
    for (uint32_t key = 0; key < 1024; key++) {
        if (bpf_map_lookup_elem(map_fd, &key, &blk) != 0) break;
        for (int i = 0; i < AGENT_OUTPUT_COUNTERS; i++) out[i] += blk.counters[i];
    }
    return true;
}

} // namespace

IpcServer::IpcServer(const std::string &sock_path, HandlerRegistry &reg)
    : sock_path_(sock_path), reg_(reg) {}

IpcServer::~IpcServer() {
    if (listen_fd_ >= 0) ::close(listen_fd_);
    /* Intentionally NOT unlink()ing sock_path_ here. If a new agentd has
     * already started and bound the same path while we were shutting down,
     * unlink() would race-delete the FS entry of the successor's socket --
     * the kernel-side listen fd survives but no client can connect()
     * because the path has no inode. The next IpcServer::start() does its
     * own unlink to clear stale state, so leaking a dead socket file
     * across an unclean stop is harmless. */
}

int IpcServer::start() {
    listen_fd_ = ::socket(AF_UNIX, SOCK_STREAM | SOCK_CLOEXEC, 0);
    if (listen_fd_ < 0) {
        fprintf(stderr, "[ipc] socket: %s\n", strerror(errno));
        return -1;
    }
    ::unlink(sock_path_.c_str()); /* clear any stale socket */
    sockaddr_un addr{};
    addr.sun_family = AF_UNIX;
    if (sock_path_.size() >= sizeof(addr.sun_path)) {
        fprintf(stderr, "[ipc] sock path too long\n");
        return -1;
    }
    std::strncpy(addr.sun_path, sock_path_.c_str(), sizeof(addr.sun_path) - 1);
    if (::bind(listen_fd_, reinterpret_cast<sockaddr *>(&addr), sizeof(addr)) < 0) {
        fprintf(stderr, "[ipc] bind %s: %s\n", sock_path_.c_str(), strerror(errno));
        return -1;
    }
    /* World-RW so a non-root agentctl can talk to a root agentd if desired.
     * Tighten later if we add a per-user socket dir. */
    ::chmod(sock_path_.c_str(), 0666);
    if (::listen(listen_fd_, 8) < 0) {
        fprintf(stderr, "[ipc] listen: %s\n", strerror(errno));
        return -1;
    }
    fprintf(stderr, "[ipc] listening on %s\n", sock_path_.c_str());
    return 0;
}

void IpcServer::run(std::atomic<bool> &stop) {
    while (!stop.load()) {
        fd_set rfds;
        FD_ZERO(&rfds);
        FD_SET(listen_fd_, &rfds);
        timeval tv{1, 0};
        int n = ::select(listen_fd_ + 1, &rfds, nullptr, nullptr, &tv);
        if (n < 0) {
            if (errno == EINTR) continue;
            fprintf(stderr, "[ipc] select: %s\n", strerror(errno));
            break;
        }
        if (n == 0) continue;
        int cfd = ::accept4(listen_fd_, nullptr, nullptr, SOCK_CLOEXEC);
        if (cfd < 0) {
            if (errno == EINTR || errno == EAGAIN) continue;
            fprintf(stderr, "[ipc] accept: %s\n", strerror(errno));
            continue;
        }
        handle_client(cfd);
        ::close(cfd);
    }
}

void IpcServer::handle_client(int cfd) {
    /* Read one full line, dispatch, write one reply line, close. */
    std::string buf;
    char tmp[4096];
    while (true) {
        ssize_t n = ::read(cfd, tmp, sizeof(tmp));
        if (n <= 0) break;
        buf.append(tmp, n);
        auto pos = buf.find('\n');
        if (pos != std::string::npos) {
            std::string line = buf.substr(0, pos);
            std::string reply = handle_request(line) + "\n";
            ::write(cfd, reply.data(), reply.size());
            return;
        }
        if (buf.size() > (1u << 20)) {
            auto err = encode(make_err("request too large")) + "\n";
            ::write(cfd, err.data(), err.size());
            return;
        }
    }
}

std::string IpcServer::handle_request(const std::string &line) {
    Value req;
    std::string perr;
    if (!decode(line, req, perr)) {
        return encode(make_err("parse error", perr));
    }
    if (!req.is_obj()) return encode(make_err("expected object"));
    const Object &o = req.as_obj();
    const std::string &op = obj_str(o, "op");
    const Object *args = obj_obj(o, "args");
    Object empty;
    if (!args) args = &empty;

    if (op == "ping") {
        Object r; r.emplace("pong", Value(true));
        return encode(make_ok(Value(std::move(r))));
    }

    if (op == "handler.load") {
        std::string path = obj_str(*args, "obj_path");
        if (path.empty()) return encode(make_err("missing obj_path"));
        std::string verifier_log;
        auto h = reg_.load(path, verifier_log);
        if (!h) return encode(make_err("load failed", verifier_log));
        Object r;
        r.emplace("handler_id", Value(static_cast<int64_t>(h->id())));
        Array progs;
        for (auto &p : h->progs()) {
            Object pi;
            pi.emplace("name", Value(p.name));
            pi.emplace("sec", Value(p.sec_name));
            pi.emplace("kind", Value(std::string(kind_str(p.kind))));
            progs.push_back(Value(std::move(pi)));
        }
        r.emplace("programs", Value(std::move(progs)));
        return encode(make_ok(Value(std::move(r))));
    }

    if (op == "handler.attach") {
        uint64_t hid = static_cast<uint64_t>(obj_int(*args, "handler_id"));
        auto h = reg_.by_id(hid);
        if (!h) return encode(make_err("unknown handler_id"));
        const Array *slots = obj_arr(*args, "slots");
        if (!slots) return encode(make_err("missing slots"));
        int attached = 0;
        for (auto &s : *slots) {
            if (!s.is_obj()) return encode(make_err("slot not an object"));
            const Object &so = s.as_obj();
            std::string prog = obj_str(so, "prog");
            SlotKind kind = parse_kind(obj_str(so, "kind"));
            uint32_t idx = static_cast<uint32_t>(obj_int(so, "idx", -1));
            int rc = reg_.attach_slot(*h, prog, kind, idx);
            if (rc) return encode(make_err("attach failed",
                                           "prog=" + prog + " kind=" + kind_str(kind) +
                                           " idx=" + std::to_string(idx) +
                                           " errno=" + std::to_string(-rc)));
            attached++;
        }
        Object r; r.emplace("attached", Value(static_cast<int64_t>(attached)));
        return encode(make_ok(Value(std::move(r))));
    }

    if (op == "handler.output") {
        uint64_t hid = static_cast<uint64_t>(obj_int(*args, "handler_id"));
        auto h = reg_.by_id(hid);
        if (!h) return encode(make_err("unknown handler_id"));
        int fd = h->agent_output_fd();
        if (fd < 0) return encode(make_err("agent_output map not present"));
        uint64_t counters[AGENT_OUTPUT_COUNTERS];
        if (!dump_agent_output(fd, counters)) return encode(make_err("dump failed"));
        Array a;
        for (int i = 0; i < AGENT_OUTPUT_COUNTERS; i++)
            a.push_back(Value(static_cast<int64_t>(counters[i])));
        Object r; r.emplace("counters", Value(std::move(a)));
        return encode(make_ok(Value(std::move(r))));
    }

    if (op == "workload.run") {
        const Array *argv_arr = obj_arr(*args, "argv");
        if (!argv_arr || argv_arr->empty())
            return encode(make_err("missing or empty argv"));
        std::vector<std::string> argv;
        for (auto &a : *argv_arr) {
            if (!a.is_str()) return encode(make_err("argv entries must be strings"));
            argv.push_back(a.as_str());
        }
        std::string cwd = obj_str(*args, "cwd");
        WorkloadRunResult res = run_workload(argv, cwd);
        Object r;
        r.emplace("ok", Value(res.ok));
        r.emplace("exit_code", Value(static_cast<int64_t>(res.exit_code)));
        r.emplace("wall_s", Value(static_cast<int64_t>(res.wall_s * 1e9))); /* return ns */
        Object fv;
        Object fv_active_obj;
        for (int i = 0; i < FeatureVector::N; i++) {
            fv.emplace(fv_label(i), Value(static_cast<int64_t>(res.fv.v[i])));
            fv_active_obj.emplace(fv_label(i), Value(fv_active(i)));
        }
        r.emplace("fv", Value(std::move(fv)));
        r.emplace("fv_active", Value(std::move(fv_active_obj)));
        r.emplace("stdout_tail", Value(res.stdout_tail));
        if (!res.error.empty()) r.emplace("error", Value(res.error));
        return encode(make_ok(Value(std::move(r))));
    }

    if (op == "handler.detach") {
        uint64_t hid = static_cast<uint64_t>(obj_int(*args, "handler_id"));
        auto h = reg_.by_id(hid);
        if (!h) return encode(make_err("unknown handler_id"));
        reg_.detach_and_remove(*h);
        Object r; r.emplace("detached", Value(true));
        return encode(make_ok(Value(std::move(r))));
    }

    if (op == "multiplex.run") {
        /* Atomic load+attach+exec+read+detach for an orchestrator-rendered
         * multiplex .bpf.o. See src/agentd/multiplex_runner.h. */
        std::string obj_path = obj_str(*args, "obj_path");
        const Array *argv_arr = obj_arr(*args, "workload_argv");
        if (obj_path.empty() || !argv_arr || argv_arr->empty())
            return encode(make_err("missing obj_path or workload_argv"));
        std::vector<std::string> argv;
        for (auto &a : *argv_arr) {
            if (!a.is_str()) return encode(make_err("workload_argv entries must be strings"));
            argv.push_back(a.as_str());
        }
        std::string cwd = obj_str(*args, "cwd");
        MultiplexResult mr = run_multiplex(obj_path, argv, cwd);
        Object res;
        res.emplace("ok", Value(mr.ok));
        res.emplace("wall_s", Value(static_cast<int64_t>(mr.wall_s * 1e9)));
        res.emplace("exit_code", Value(static_cast<int64_t>(mr.exit_code)));
        res.emplace("stdout_tail", Value(mr.stdout_tail));
        res.emplace("n_programs", Value(static_cast<int64_t>(mr.n_programs)));
        res.emplace("n_attached", Value(static_cast<int64_t>(mr.n_attached)));
        if (!mr.attach_errors.empty()) {
            Array ae;
            for (auto &e : mr.attach_errors) ae.push_back(Value(e));
            res.emplace("attach_errors", Value(std::move(ae)));
        }
        Array c;
        for (auto v : mr.counters) c.push_back(Value(static_cast<int64_t>(v)));
        res.emplace("counters", Value(std::move(c)));
        /* T_x metric: per-slot accumulated kernel-time nanoseconds.
         * Emitted only when the BPF object was built with the
         * kretprobe-pair template (i.e. orchestrator/agentctl/multiplex.py
         * post-T_x). Absent for legacy objects -- orchestrator handles
         * that by falling back to count-only z-scoring per slot. */
        if (!mr.sum_ns.empty()) {
            Array ns;
            for (auto v : mr.sum_ns) ns.push_back(Value(static_cast<int64_t>(v)));
            res.emplace("sum_ns", Value(std::move(ns)));
        }
        if (!mr.error.empty()) res.emplace("error", Value(mr.error));
        return encode(make_ok(Value(std::move(res))));
    }

    return encode(make_err("unknown op", op));
}

} // namespace agentd
