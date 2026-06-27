/* Loads agent-authored .bpf.o files at runtime, inserts their kprobe/tp
 * programs into the dispatcher's PROG_ARRAY slots, and reads back the
 * agent_output map at the end of a run.
 *
 * One handler at a time for v1 (matches execution_plan.md M3).
 */
#pragma once

#include <cstdint>
#include <memory>
#include <string>
#include <vector>

#include <bpf/libbpf.h>

#include "profiler_types.h"

namespace agentd {

enum class SlotKind { Kprobe, Tracepoint };

struct ProgInfo {
    std::string name;        /* e.g. "seed_mm_fault_entry" */
    std::string sec_name;    /* e.g. "kprobe/handle_mm_fault" */
    SlotKind kind;
    int prog_fd = -1;        /* owned by the bpf_object */
};

struct AttachedSlot {
    SlotKind kind;
    uint32_t idx;
};

class Handler {
public:
    Handler(uint64_t id, struct bpf_object *obj);
    ~Handler();
    Handler(const Handler &) = delete;
    Handler &operator=(const Handler &) = delete;

    uint64_t id() const { return id_; }
    const std::vector<ProgInfo> &progs() const { return progs_; }

    /* Find the agent_output map FD; returns -1 if absent. */
    int agent_output_fd() const;

    /* Track a slot we've inserted into so we can clear it on detach. */
    void record_slot(SlotKind k, uint32_t idx) { attached_.push_back({k, idx}); }
    const std::vector<AttachedSlot> &attached_slots() const { return attached_; }

private:
    uint64_t id_;
    struct bpf_object *obj_;     /* owned */
    std::vector<ProgInfo> progs_;
    std::vector<AttachedSlot> attached_;
};

class HandlerRegistry {
public:
    /* Owns the dispatcher's PROG_ARRAY map fds (passed in from main). */
    HandlerRegistry(int kprobe_dispatch_fd, int tp_dispatch_fd);

    /* Load a .bpf.o. On verifier failure returns nullptr and sets err. */
    std::shared_ptr<Handler> load(const std::string &obj_path, std::string &err);

    /* Insert program FD into PROG_ARRAY[slot]. Returns 0 on success, -errno otherwise. */
    int attach_slot(Handler &h, const std::string &prog_name, SlotKind kind, uint32_t slot);

    /* Clear all slots this handler occupies and forget the handler. */
    void detach_and_remove(Handler &h);

    std::shared_ptr<Handler> by_id(uint64_t id) const;

private:
    int kprobe_dispatch_fd_;
    int tp_dispatch_fd_;
    uint64_t next_id_ = 1;
    /* v1: at most one handler at a time, but use a map for forward compat. */
    std::vector<std::shared_ptr<Handler>> handlers_;
};

} // namespace agentd
