/* SPDX-License-Identifier: GPL-2.0 OR BSD-3-Clause */
/*
 * thp_aggressor.c - THP aggressor workload (lifted from
 *   dynamic-profiler-ebpf/workloads/micro/thp_aggressor.c)
 *
 * Continuously forces Transparent Huge Page (THP) allocations for a
 * configurable duration. Designed to run on a dedicated CPU while a victim
 * workload (that only needs 4 KB pages) runs on other cores, demonstrating
 * the "THP tax" the victim pays through increased page-fault latency on
 * mmap_lock / PMD-lock contention - the Valinor result we reproduce in CS3.
 *
 * Operation:
 *   1. (optional, --frag-size) Create checkerboard fragmentation so THP
 *      allocations force compaction (zone->lock held for milliseconds).
 *   2. Loop until --duration expires:
 *      a. mmap a THP-sized region, madvise(MADV_HUGEPAGE)
 *      b. Touch every 2 MB stride - triggers THP allocation
 *      c. munmap the region
 *
 * Usage:
 *   ./thp_aggressor [--thp-size SIZE] [--duration SECS] [--cpu N]
 *                   [--frag-size SIZE]
 *
 * Inlines workload_utils.h's parse_size / pin_cpu / now_ns / WL_MB / PAGE_SZ
 * to keep this benchmark self-contained (the legacy header pulls in cgroup
 * helpers we don't need here).
 */
#define _GNU_SOURCE
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdint.h>
#include <unistd.h>
#include <signal.h>
#include <sched.h>
#include <time.h>
#include <sys/mman.h>

#define PAGE_SZ  4096UL
#define WL_KB    (1024UL)
#define WL_MB    (1024UL * 1024UL)
#define WL_GB    (1024UL * 1024UL * 1024UL)

static volatile int g_stop = 0;

static void on_signal(int sig)
{
    (void)sig;
    g_stop = 1;
}

static size_t parse_size(const char *s)
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

static int pin_cpu(int cpu)
{
    if (cpu < 0) return 0;
    cpu_set_t set;
    CPU_ZERO(&set);
    CPU_SET(cpu, &set);
    if (sched_setaffinity(0, sizeof(set), &set) != 0) {
        perror("[pin_cpu] sched_setaffinity");
        return -1;
    }
    fprintf(stderr, "[thp_aggressor] pinned to CPU %d\n", cpu);
    return 0;
}

static uint64_t now_ns(void)
{
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (uint64_t)ts.tv_sec * 1000000000ULL + (uint64_t)ts.tv_nsec;
}

static void usage(const char *prog)
{
    fprintf(stderr,
        "Usage: %s [OPTIONS]\n"
        "  --thp-size  SIZE   Per-round THP allocation (default: 512M)\n"
        "  --duration  SECS   How long to run (default: 60)\n"
        "  --cpu       N      Pin to CPU N (default: no pinning)\n"
        "  --frag-size SIZE   Fragment this much memory first (default: none)\n",
        prog);
}

int main(int argc, char **argv)
{
    size_t thp_size   = 512 * WL_MB;
    size_t frag_size  = 0;
    int    duration   = 60;
    int    cpu        = -1;

    for (int i = 1; i < argc; i++) {
        if (!strcmp(argv[i], "--thp-size") && i + 1 < argc)
            thp_size = parse_size(argv[++i]);
        else if (!strcmp(argv[i], "--frag-size") && i + 1 < argc)
            frag_size = parse_size(argv[++i]);
        else if (!strcmp(argv[i], "--duration") && i + 1 < argc)
            duration = atoi(argv[++i]);
        else if (!strcmp(argv[i], "--cpu") && i + 1 < argc)
            cpu = atoi(argv[++i]);
        else { usage(argv[0]); return 1; }
    }

    signal(SIGINT,  on_signal);
    signal(SIGTERM, on_signal);
    signal(SIGALRM, on_signal);

    pin_cpu(cpu);

    fprintf(stderr,
            "[thp_aggressor] thp=%zuM  frag=%zuM  duration=%ds  cpu=%d\n",
            thp_size / WL_MB, frag_size / WL_MB, duration, cpu);

    char *frag = NULL;
    if (frag_size > 0) {
        fprintf(stderr, "[thp_aggressor] creating fragmentation...\n");
        frag = (char *)mmap(NULL, frag_size, PROT_READ | PROT_WRITE,
                            MAP_ANONYMOUS | MAP_PRIVATE, -1, 0);
        if (frag == MAP_FAILED) { perror("mmap frag"); return 1; }

        madvise(frag, frag_size, MADV_NOHUGEPAGE);

        size_t npages = frag_size / PAGE_SZ;
        for (size_t i = 0; i < npages; i++)
            ((volatile char *)frag)[i * PAGE_SZ] = 0x42;

        for (size_t i = 0; i < npages; i += 2)
            madvise(frag + i * PAGE_SZ, PAGE_SZ, MADV_DONTNEED);

        fprintf(stderr, "[thp_aggressor] fragmented %zu MB (%zu anchor pages)\n",
                frag_size / WL_MB, npages / 2);
    }

    alarm(duration);
    uint64_t t0  = now_ns();
    int      round = 0;

    while (!g_stop) {
        char *thp = (char *)mmap(NULL, thp_size, PROT_READ | PROT_WRITE,
                                 MAP_ANONYMOUS | MAP_PRIVATE, -1, 0);
        if (thp == MAP_FAILED) {
            usleep(10000);
            continue;
        }
        madvise(thp, thp_size, MADV_HUGEPAGE);

        size_t stride = 2 * WL_MB;
        for (size_t off = 0; off < thp_size && !g_stop; off += stride)
            ((volatile char *)thp)[off] = 0xff;

        munmap(thp, thp_size);
        round++;

        if (round % 20 == 0) {
            double elapsed = (now_ns() - t0) / 1e9;
            fprintf(stderr, "[thp_aggressor] round=%d  elapsed=%.1fs\n",
                    round, elapsed);
        }
    }

    double total = (now_ns() - t0) / 1e9;
    fprintf(stderr, "[thp_aggressor] finished: %d rounds in %.1fs\n",
            round, total);

    if (frag)
        munmap(frag, frag_size);
    return 0;
}
