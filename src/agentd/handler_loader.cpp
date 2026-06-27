#include "handler_loader.h"

#include <algorithm>
#include <cerrno>
#include <cstdio>
#include <cstring>
#include <unistd.h>

#include <bpf/bpf.h>

namespace agentd {

namespace {

/* Capture libbpf's per-call output to a string while loading.
 * libbpf's print fn is global, so we briefly install a thread-local sink. */
thread_local std::string *g_capture = nullptr;

int capture_print(enum libbpf_print_level lvl, const char *fmt, va_list args) {
    char buf[4096];
    int n = vsnprintf(buf, sizeof(buf), fmt, args);
    if (n > 0 && g_capture) g_capture->append(buf, std::min<size_t>(n, sizeof(buf) - 1));
    /* Also forward to stderr at INFO+ so users see progress. */
    if (lvl <= LIBBPF_INFO) {
        fprintf(stderr, "%s", buf);
    }
    return n;
}

SlotKind sec_kind(const std::string &sec) {
    if (sec.rfind("kprobe", 0) == 0 || sec.rfind("kretprobe", 0) == 0) return SlotKind::Kprobe;
    if (sec.rfind("tp/", 0) == 0 || sec.rfind("tracepoint", 0) == 0)   return SlotKind::Tracepoint;
    /* Default to Kprobe; caller will reject mismatched slot kinds anyway. */
    return SlotKind::Kprobe;
}

} // namespace

Handler::Handler(uint64_t id, struct bpf_object *obj) : id_(id), obj_(obj) {
    struct bpf_program *p;
    bpf_object__for_each_program(p, obj_) {
        ProgInfo info;
        info.name = bpf_program__name(p);
        info.sec_name = bpf_program__section_name(p);
        info.kind = sec_kind(info.sec_name);
        info.prog_fd = bpf_program__fd(p);
        progs_.push_back(std::move(info));
    }
}

Handler::~Handler() {
    if (obj_) bpf_object__close(obj_);
}

int Handler::agent_output_fd() const {
    struct bpf_map *m = bpf_object__find_map_by_name(obj_, "agent_output");
    return m ? bpf_map__fd(m) : -1;
}

HandlerRegistry::HandlerRegistry(int kprobe_dispatch_fd, int tp_dispatch_fd)
    : kprobe_dispatch_fd_(kprobe_dispatch_fd), tp_dispatch_fd_(tp_dispatch_fd) {}

std::shared_ptr<Handler> HandlerRegistry::load(const std::string &obj_path, std::string &err) {
    err.clear();

    struct bpf_object *obj = bpf_object__open_file(obj_path.c_str(), nullptr);
    if (!obj || libbpf_get_error(obj)) {
        err = "bpf_object__open_file failed: " + std::string(strerror(errno));
        if (obj) bpf_object__close(obj);
        return nullptr;
    }

    /* Capture verifier log into err during load. */
    g_capture = &err;
    auto *prev = libbpf_set_print(capture_print);
    int rc = bpf_object__load(obj);
    libbpf_set_print(prev);
    g_capture = nullptr;

    if (rc) {
        if (err.empty()) err = "bpf_object__load failed (errno=" + std::to_string(-rc) + ")";
        bpf_object__close(obj);
        return nullptr;
    }

    auto h = std::make_shared<Handler>(next_id_++, obj);
    handlers_.push_back(h);
    return h;
}

int HandlerRegistry::attach_slot(Handler &h, const std::string &prog_name,
                                 SlotKind kind, uint32_t slot) {
    int prog_fd = -1;
    for (auto &p : h.progs()) {
        if (p.name == prog_name) {
            if (p.kind != kind) {
                fprintf(stderr, "[handler] prog '%s' kind mismatch (sec=%s, requested kind=%d)\n",
                        prog_name.c_str(), p.sec_name.c_str(), (int)kind);
                return -EINVAL;
            }
            prog_fd = p.prog_fd;
            break;
        }
    }
    if (prog_fd < 0) {
        fprintf(stderr, "[handler] prog '%s' not found\n", prog_name.c_str());
        return -ENOENT;
    }

    int map_fd = (kind == SlotKind::Kprobe) ? kprobe_dispatch_fd_ : tp_dispatch_fd_;
    int rc = bpf_map_update_elem(map_fd, &slot, &prog_fd, BPF_ANY);
    if (rc) {
        fprintf(stderr, "[handler] PROG_ARRAY update failed: %s\n", strerror(errno));
        return -errno;
    }
    h.record_slot(kind, slot);
    return 0;
}

void HandlerRegistry::detach_and_remove(Handler &h) {
    for (auto &slot : h.attached_slots()) {
        int map_fd = (slot.kind == SlotKind::Kprobe) ? kprobe_dispatch_fd_ : tp_dispatch_fd_;
        bpf_map_delete_elem(map_fd, &slot.idx);
    }
    handlers_.erase(std::remove_if(handlers_.begin(), handlers_.end(),
                                   [&](const std::shared_ptr<Handler> &p) { return p.get() == &h; }),
                    handlers_.end());
}

std::shared_ptr<Handler> HandlerRegistry::by_id(uint64_t id) const {
    for (auto &h : handlers_) if (h->id() == id) return h;
    return nullptr;
}

} // namespace agentd
