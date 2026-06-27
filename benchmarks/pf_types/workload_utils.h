/* SPDX-License-Identifier: GPL-2.0 OR BSD-3-Clause */
/*
 * workload_utils.h — Shared utilities for profiler microbenchmarks
 *
 * Header-only library providing:
 *   - CLI size parsing (512M, 2G, etc.)
 *   - System info (total/available RAM, CPU count)
 *   - Monotonic timing
 *   - Deterministic PRNG (xorshift64)
 *   - Cgroup v2 helpers for memory containment
 *
 * Each microbenchmark is compiled as a standalone binary, so static
 * globals in this header are safe (one translation unit per binary).
 */
#ifndef WORKLOAD_UTILS_H
#define WORKLOAD_UTILS_H

#ifndef _GNU_SOURCE
#define _GNU_SOURCE
#endif

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdint.h>
#include <unistd.h>
#include <errno.h>
#include <fcntl.h>
#include <signal.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <sched.h>
#include <time.h>

/* ═══════════════════════════════════════════════════════════════════════ */
/* Constants                                                               */
/* ═══════════════════════════════════════════════════════════════════════ */
#define PAGE_SZ  4096UL
#define WL_KB    (1024UL)
#define WL_MB    (1024UL * 1024UL)
#define WL_GB    (1024UL * 1024UL * 1024UL)

/* ═══════════════════════════════════════════════════════════════════════ */
/* Size parsing: accepts "512M", "2G", "4096K", or raw byte count         */
/* ═══════════════════════════════════════════════════════════════════════ */
static inline size_t parse_size(const char *s)
{
    char *end;
    double v = strtod(s, &end);
    if (v < 0) v = 0;
    switch (*end) {
    case 'K': case 'k': return (size_t)(v * WL_KB);
    case 'M': case 'm': return (size_t)(v * WL_MB);
    case 'G': case 'g': return (size_t)(v * WL_GB);
    default:            return (size_t)v;
    }
}

/* ═══════════════════════════════════════════════════════════════════════ */
/* System information                                                      */
/* ═══════════════════════════════════════════════════════════════════════ */
static inline size_t get_total_ram(void)
{
    long p = sysconf(_SC_PHYS_PAGES);
    long s = sysconf(_SC_PAGESIZE);
    return (p > 0 && s > 0) ? (size_t)p * (size_t)s : 4 * WL_GB;
}

static inline size_t get_available_ram(void)
{
    FILE *f = fopen("/proc/meminfo", "r");
    if (!f) return get_total_ram() / 2;
    char line[256];
    while (fgets(line, sizeof(line), f)) {
        size_t kb;
        if (sscanf(line, "MemAvailable: %zu kB", &kb) == 1) {
            fclose(f);
            return kb * WL_KB;
        }
    }
    fclose(f);
    return get_total_ram() / 2;
}

static inline int wl_num_cpus(void)
{
    int n = (int)sysconf(_SC_NPROCESSORS_ONLN);
    return n > 0 ? n : 1;
}

/* ═══════════════════════════════════════════════════════════════════════ */
/* CPU pinning                                                             */
/* ═══════════════════════════════════════════════════════════════════════ */
/*
 * Pin the calling thread to the given CPU. Pass -1 to skip pinning.
 * Returns 0 on success, -1 on error (with a message printed).
 */
static inline int pin_cpu(int cpu)
{
    if (cpu < 0) return 0;   /* no pinning requested */
    int ncpus = wl_num_cpus();
    if (cpu >= ncpus) {
        fprintf(stderr, "[pin_cpu] CPU %d out of range (0..%d)\n",
                cpu, ncpus - 1);
        return -1;
    }
    cpu_set_t set;
    CPU_ZERO(&set);
    CPU_SET(cpu, &set);
    if (sched_setaffinity(0, sizeof(set), &set) != 0) {
        perror("[pin_cpu] sched_setaffinity");
        return -1;
    }
    printf("[pin_cpu] pinned to CPU %d\n", cpu);
    return 0;
}

/* ═══════════════════════════════════════════════════════════════════════ */
/* Timing (monotonic, nanoseconds)                                         */
/* ═══════════════════════════════════════════════════════════════════════ */
static inline uint64_t now_ns(void)
{
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (uint64_t)ts.tv_sec * 1000000000ULL + (uint64_t)ts.tv_nsec;
}

/* ═══════════════════════════════════════════════════════════════════════ */
/* PRNG — xorshift64 (fast, deterministic, non-cryptographic)              */
/* ═══════════════════════════════════════════════════════════════════════ */
static inline uint64_t xorshift64(uint64_t *state)
{
    uint64_t x = *state;
    x ^= x << 13;
    x ^= x >> 7;
    x ^= x << 17;
    return (*state = x);
}

/* ═══════════════════════════════════════════════════════════════════════ *
 * Cgroup v2 memory containment                                            *
 *                                                                         *
 * Self-contain pattern (enter + auto-cleanup):                            *
 *   if (cg_setup(256 * WL_MB) == 0)                                      *
 *       // now inside a cgroup with 256 MB limit                          *
 *                                                                         *
 * Wrap-child pattern (OOM benchmark):                                     *
 *   const char *path = cg_create(64 * WL_MB);                            *
 *   cg_set_swap_max(path, 0);                                            *
 *   pid_t c = fork();                                                     *
 *   if (c == 0) { cg_enter(path); ... }                                  *
 *   waitpid(c, &st, 0);                                                   *
 *   cg_destroy(path);                                                     *
 * ═══════════════════════════════════════════════════════════════════════ */
#define CG_ROOT   "/sys/fs/cgroup"
#define CG_PREFIX "profiler-bench"

/* Global path, used by atexit handler */
static char g_cg_path[512];

static inline int cg_available(void)
{
    struct stat st;
    return stat(CG_ROOT "/cgroup.controllers", &st) == 0;
}

/* Write a string to a cgroup control file. */
static inline int cg_write(const char *path, const char *val)
{
    int fd = open(path, O_WRONLY);
    if (fd < 0) return -1;
    ssize_t n = write(fd, val, strlen(val));
    close(fd);
    return (n > 0) ? 0 : -1;
}

/* Create a cgroup directory and set memory.max. Does NOT enter it.
 * Returns g_cg_path on success, NULL on failure. */
static inline const char *cg_create(size_t mem_max)
{
    if (!cg_available()) {
        fprintf(stderr, "[cgroup] cgroup v2 not available at " CG_ROOT "\n");
        return NULL;
    }
    snprintf(g_cg_path, sizeof(g_cg_path),
             CG_ROOT "/" CG_PREFIX "-%d", getpid());

    if (mkdir(g_cg_path, 0755) != 0 && errno != EEXIST) {
        perror("[cgroup] mkdir");
        return NULL;
    }

    char p[576], v[64];
    snprintf(p, sizeof(p), "%s/memory.max", g_cg_path);
    snprintf(v, sizeof(v), "%zu", mem_max);
    if (cg_write(p, v) != 0) {
        perror("[cgroup] set memory.max");
        rmdir(g_cg_path);
        g_cg_path[0] = '\0';
        return NULL;
    }

    printf("[cgroup] created %s  memory.max=%zuM\n",
           g_cg_path, mem_max / WL_MB);
    return g_cg_path;
}

/* Move current process into the cgroup. */
static inline int cg_enter(const char *path)
{
    char p[576], v[32];
    snprintf(p, sizeof(p), "%s/cgroup.procs", path);
    snprintf(v, sizeof(v), "%d", getpid());
    return cg_write(p, v);
}

/* Optional: set memory.high for sustained reclaim without OOM. */
static inline int cg_set_high(const char *path, size_t bytes)
{
    char p[576], v[64];
    snprintf(p, sizeof(p), "%s/memory.high", path);
    snprintf(v, sizeof(v), "%zu", bytes);
    return cg_write(p, v);
}

/* Optional: limit or disable swap. Use bytes=0 to disable. */
static inline int cg_set_swap_max(const char *path, size_t bytes)
{
    char p[576], v[64];
    snprintf(p, sizeof(p), "%s/memory.swap.max", path);
    snprintf(v, sizeof(v), "%zu", bytes);
    return cg_write(p, v);
}

/* Mark cgroup for group OOM kill. */
static inline int cg_set_oom_group(const char *path)
{
    char p[576];
    snprintf(p, sizeof(p), "%s/memory.oom.group", path);
    return cg_write(p, "1");
}

/* Move self out of the cgroup and remove the directory. */
static inline void cg_cleanup(void)
{
    if (!g_cg_path[0]) return;
    char v[32];
    snprintf(v, sizeof(v), "%d", getpid());
    cg_write(CG_ROOT "/cgroup.procs", v);
    usleep(100000);   /* let kernel update cgroup membership */
    if (rmdir(g_cg_path) == 0)
        printf("[cgroup] cleaned up %s\n", g_cg_path);
    g_cg_path[0] = '\0';
}

/* Remove a cgroup directory (caller must ensure it's empty). */
static inline void cg_destroy(const char *path)
{
    if (!path || !path[0]) return;
    usleep(100000);
    if (rmdir(path) == 0)
        printf("[cgroup] destroyed %s\n", path);
    if (path == g_cg_path)
        g_cg_path[0] = '\0';
}

/*
 * Convenience: create cgroup + enter + register atexit cleanup.
 * Returns 0 on success, -1 on failure (benchmark runs unconstrained).
 */
static inline int cg_setup(size_t mem_max)
{
    const char *path = cg_create(mem_max);
    if (!path) return -1;
    cg_set_swap_max(path, 0);
    if (cg_enter(path) != 0) {
        perror("[cgroup] enter");
        rmdir(g_cg_path);
        g_cg_path[0] = '\0';
        return -1;
    }
    atexit(cg_cleanup);
    return 0;
}

#endif /* WORKLOAD_UTILS_H */
