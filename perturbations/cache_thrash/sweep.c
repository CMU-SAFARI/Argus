/* cache_thrash/sweep -- userspace LLC-pollution co-runner.
 *
 * Sweeps a region 2x the size of the L3 cache, touching one u64 per
 * cache-line in a fixed-stride loop, until SIGTERM. No syscalls in the
 * hot loop. No fragmentation, no slab churn -- the only kernel effect
 * is the initial mmap()+page-fault to instantiate the region, after
 * which the entire run is userspace cache-bandwidth pollution.
 *
 * Used as the new p_cache_thrash perturbation to expose latency-driven
 * kernel bottlenecks invisible to count-only FV measurement: when the
 * victim's per-fault wall time rises because page-table walks now hit
 * cold LLC lines, count probes still see "1 fault per page" but the
 * T_x metric (kprobe + kretprobe pair) catches the per-call latency
 * amplification.
 *
 * Usage:
 *   ./sweep [--size-mb N] [--cpu N] [--stride-bytes N] [--quiet]
 *
 * Defaults read the host's L3 size from
 *   /sys/devices/system/cpu/cpu0/cache/index3/size
 * and pick 2x that. Stride defaults to 64 (cache-line).
 */
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <sched.h>
#include <signal.h>
#include <stdatomic.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <time.h>
#include <unistd.h>

static volatile sig_atomic_t stop = 0;
static void on_term(int signo) { (void)signo; stop = 1; }

static size_t read_l3_kb_default(void) {
    /* Parse a string like "36864K" from
     * /sys/devices/system/cpu/cpu0/cache/index3/size; fallback 32 MiB. */
    int fd = open("/sys/devices/system/cpu/cpu0/cache/index3/size", O_RDONLY);
    if (fd < 0) return 32u * 1024u;
    char buf[64] = {0};
    ssize_t n = read(fd, buf, sizeof(buf) - 1);
    close(fd);
    if (n <= 0) return 32u * 1024u;
    size_t v = 0;
    int multiplier = 1;
    for (ssize_t i = 0; i < n && buf[i]; i++) {
        char c = buf[i];
        if (c >= '0' && c <= '9') v = v * 10 + (c - '0');
        else if (c == 'K' || c == 'k') multiplier = 1;
        else if (c == 'M' || c == 'm') multiplier = 1024;
        else if (c == 'G' || c == 'g') multiplier = 1024 * 1024;
    }
    return v * multiplier > 0 ? v * multiplier : 32u * 1024u;
}

int main(int argc, char **argv) {
    size_t size_mb = 0;            /* 0 = auto from L3 */
    int cpu = -1;                  /* -1 = no pinning */
    size_t stride = 64;            /* cache-line */
    int quiet = 0;

    for (int i = 1; i < argc; i++) {
        if (!strcmp(argv[i], "--size-mb") && i + 1 < argc)
            size_mb = (size_t)atoll(argv[++i]);
        else if (!strcmp(argv[i], "--cpu") && i + 1 < argc)
            cpu = atoi(argv[++i]);
        else if (!strcmp(argv[i], "--stride-bytes") && i + 1 < argc)
            stride = (size_t)atoll(argv[++i]);
        else if (!strcmp(argv[i], "--quiet"))
            quiet = 1;
        else if (!strcmp(argv[i], "--help") || !strcmp(argv[i], "-h")) {
            fprintf(stderr,
                "usage: %s [--size-mb N] [--cpu N] [--stride-bytes N] [--quiet]\n",
                argv[0]);
            return 0;
        }
    }
    if (size_mb == 0) {
        size_t l3_kb = read_l3_kb_default();
        size_mb = (l3_kb * 2 + 1023) / 1024;
        if (size_mb < 16) size_mb = 16;   /* sanity floor */
    }

    if (cpu >= 0) {
        cpu_set_t mask;
        CPU_ZERO(&mask);
        CPU_SET(cpu, &mask);
        if (sched_setaffinity(0, sizeof(mask), &mask) != 0) {
            fprintf(stderr, "[cache_thrash] sched_setaffinity(cpu=%d): %s\n",
                    cpu, strerror(errno));
            /* non-fatal: continue unpinned */
        }
    }

    struct sigaction sa = {0};
    sa.sa_handler = on_term;
    sigaction(SIGTERM, &sa, NULL);
    sigaction(SIGINT, &sa, NULL);
    sigaction(SIGHUP, &sa, NULL);

    size_t size_bytes = size_mb * 1024u * 1024u;
    /* MAP_ANONYMOUS | MAP_PRIVATE -- we want our own pages, populated
     * eagerly via MAP_POPULATE to avoid faulting in the hot loop
     * (we want pure userspace pollution, not page-fault traffic). */
    void *buf = mmap(NULL, size_bytes, PROT_READ | PROT_WRITE,
                     MAP_PRIVATE | MAP_ANONYMOUS | MAP_POPULATE, -1, 0);
    if (buf == MAP_FAILED) {
        fprintf(stderr, "[cache_thrash] mmap(%zu MiB): %s\n",
                size_mb, strerror(errno));
        return 2;
    }
    /* MADV_HUGEPAGE encourages 2 MiB pages so the sweep itself stays
     * cheap in TLB. We want LLC pollution, not TLB pressure. */
    (void)madvise(buf, size_bytes, MADV_HUGEPAGE);

    if (!quiet) {
        fprintf(stderr,
                "[cache_thrash] running: size=%zu MiB, stride=%zu B, cpu=%d, pid=%d\n",
                size_mb, stride, cpu, getpid());
        fflush(stderr);
    }

    volatile uint64_t *p = (volatile uint64_t *)buf;
    size_t n_words = size_bytes / sizeof(uint64_t);
    size_t step = stride / sizeof(uint64_t);
    if (step == 0) step = 1;

    /* Hot loop. Each iteration writes one u64 per cache line, sweeping
     * the entire region. The volatile + the write side keep the
     * compiler from hoisting / unrolling away. */
    uint64_t tick = 0;
    while (!stop) {
        for (size_t i = 0; i < n_words; i += step) {
            p[i] = tick;
        }
        tick++;
        /* No syscalls. No yield. No sleep. Pin the CPU set hot. */
    }

    if (!quiet) {
        fprintf(stderr, "[cache_thrash] exiting after %lu sweeps\n",
                (unsigned long)tick);
    }
    munmap(buf, size_bytes);
    return 0;
}
