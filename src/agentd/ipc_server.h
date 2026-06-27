/* AF_UNIX line-oriented JSON RPC server.
 *
 * Wire format: one JSON object per line (NDJSON).
 *
 * Request:  {"op":"...", "args":{...}}
 * Response: {"ok":true,  "result":{...}}
 *           {"ok":false, "error":"...", "detail":"..."}
 *
 * Operations:
 *   - "ping"            -> {pong: true}
 *   - "handler.load"    args={obj_path}        -> {handler_id, programs:[{name,sec,kind}]}
 *   - "handler.attach"  args={handler_id, slots:[{prog,kind,idx}]}  -> {attached: N}
 *   - "handler.output"  args={handler_id}      -> {counters:[16 u64s, summed across CPUs]}
 *   - "handler.detach"  args={handler_id}      -> {detached: true}
 *   - "workload.run"    args={argv,[cwd]}      -> {ok, exit_code, wall_s, fv, fv_active, stdout_tail}
 *   - "multiplex.run"   args={obj_path, workload_argv, [cwd]}
 *                                              -> {ok, exit_code, wall_s, stdout_tail,
 *                                                  n_programs, n_attached, attach_errors,
 *                                                  counters:[per-slot u64, summed across CPUs],
 *                                                  sum_ns:[per-slot kernel-ns, summed across CPUs]
 *                                                         (omitted when BPF object lacks mx_sum_ns map)}
 */
#pragma once

#include <atomic>
#include <string>

#include "handler_loader.h"

namespace agentd {

class IpcServer {
public:
    IpcServer(const std::string &sock_path, HandlerRegistry &reg);
    ~IpcServer();

    /* Bind & listen. Returns 0 on success. */
    int start();

    /* Block-serve until *stop becomes true. */
    void run(std::atomic<bool> &stop);

private:
    std::string sock_path_;
    HandlerRegistry &reg_;
    int listen_fd_ = -1;

    void handle_client(int cfd);
    std::string handle_request(const std::string &line);
};

} // namespace agentd
