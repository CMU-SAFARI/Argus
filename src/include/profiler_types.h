/* SPDX-License-Identifier: GPL-2.0 OR BSD-3-Clause */
#ifndef __AGENTICBPF_PROFILER_TYPES_H__
#define __AGENTICBPF_PROFILER_TYPES_H__

/* PROG_ARRAY / Dispatcher Configuration (shared with legacy profiler) */
#define PROG_ARRAY_MAX_ENTRIES  16

/* Kprobe dispatch slot indices (must match dispatcher.bpf.c SEC tags). */
#define KIDX_MM_FAULT_ENTRY      0
#define KIDX_MM_FAULT_EXIT       1
#define KIDX_SLAB_ENTRY          2
#define KIDX_SLAB_EXIT           3
#define KIDX_PAGE_CACHE          4
#define KIDX_ALLOC_ENTRY         5
#define KIDX_ALLOC_EXIT          6

/* Tracepoint dispatch slot indices. */
#define TIDX_RECLAIM_BEGIN       0
#define TIDX_RECLAIM_END         1
#define TIDX_COMPACTION_BEGIN    2
#define TIDX_COMPACTION_END      3
#define TIDX_MEMCG_RECLAIM_BEGIN 4
#define TIDX_MEMCG_RECLAIM_END   5
#define TIDX_TLB_FLUSH           6
#define TIDX_THP_COLLAPSE        7

/* Agent handler authoring contract: every agent-authored .bpf.c MUST declare
 * a single BPF_MAP_TYPE_ARRAY named "agent_output", key = u32 cpu, value =
 * struct agent_output_block. The orchestrator reads exactly this map at the
 * end of each candidate run to compute the OFV-side metrics. */
#define AGENT_OUTPUT_COUNTERS    16
struct agent_output_block {
    __u64 counters[AGENT_OUTPUT_COUNTERS];
};

#endif /* __AGENTICBPF_PROFILER_TYPES_H__ */
