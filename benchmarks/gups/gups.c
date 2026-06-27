/* SPDX-License-Identifier: GPL-2.0 OR BSD-3-Clause */
/* GUPS - Giga Updates Per Second.
 *
 * HPCC-style random-access kernel. Sized to bust the dTLB on 4 KiB pages
 * (STLB reach ~6 MiB at 4 K) but fit comfortably under 2 MiB pages (STLB
 * reach ~3 GiB at 2 M). With THP=madvise the kernel may promote -> few TLB
 * misses; with THP=never every access misses.
 *
 * Each iteration does N random read-modify-write updates to a 256 MiB
 * (default) array of u64. xorshift PRNG keeps the inner loop ALU-light.
 *
 * Usage: ./gups [--array-mib N] [--iterations N] [--updates-per-iter N]
 */

#define _GNU_SOURCE
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdint.h>
#include <time.h>
#include <sys/mman.h>

static double now_s(void) {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return ts.tv_sec + ts.tv_nsec * 1e-9;
}

static inline uint64_t xs64(uint64_t *s) {
    uint64_t x = *s;
    x ^= x << 13;
    x ^= x >> 7;
    x ^= x << 17;
    *s = x;
    return x;
}

int main(int argc, char **argv) {
    size_t array_mib = 256;
    int iterations = 4;
    /* Updates per iteration. With 256 MiB / 8 B = 32M slots, 64M updates per
     * iteration ~= 2x the slot count -> well-mixed coverage. */
    long updates = 64L * 1024 * 1024;

    for (int i = 1; i < argc; i++) {
        if (!strcmp(argv[i], "--array-mib") && i + 1 < argc) array_mib = atoll(argv[++i]);
        else if (!strcmp(argv[i], "--iterations") && i + 1 < argc) iterations = atoi(argv[++i]);
        else if (!strcmp(argv[i], "--updates-per-iter") && i + 1 < argc) updates = atoll(argv[++i]);
        else if (!strcmp(argv[i], "--help")) {
            fprintf(stderr,
                "Usage: %s [--array-mib N] [--iterations N] [--updates-per-iter N]\n"
                "  Random updates on a u64 array. Defaults: 256 MiB, 4 iters, 64M updates/iter.\n",
                argv[0]);
            return 0;
        }
    }

    size_t n = array_mib * (size_t)1024 * 1024 / sizeof(uint64_t);
    size_t bytes = ((n * sizeof(uint64_t)) + (2u << 20) - 1) & ~((2u << 20) - 1);
    /* mmap the buffer 2 MiB-aligned and ask the kernel for huge pages. With
     * THP=always or MADV_HUGEPAGE, this is what lets P1 (THP=never) actually
     * change anything. With THP=never globally, MADV_HUGEPAGE is a no-op. */
    uint64_t *a = mmap(NULL, bytes, PROT_READ | PROT_WRITE,
                       MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
    if (a == MAP_FAILED) { perror("mmap"); return 1; }
    if (madvise(a, bytes, MADV_HUGEPAGE) != 0) {
        /* Non-fatal; on THP=never this returns EINVAL on some kernels. */
        /* Don't print - we deliberately run under THP=never sometimes. */
    }

    /* Touch every page. Under THP=always or MADV_HUGEPAGE+THP=madvise, the
     * kernel tries to back this with 2 MiB pages immediately. */
    for (size_t i = 0; i < n; i++) a[i] = i;
    /* Give khugepaged a moment to coalesce. */
    struct timespec sl = {0, 100 * 1000 * 1000};
    nanosleep(&sl, NULL);

    uint64_t mask = 1;
    while ((mask << 1) <= n) mask <<= 1;
    mask -= 1;   /* largest power-of-two mask <= n-1; we'll bound-check */

    uint64_t seed = 0xdeadbeef12345678ULL;
    double t0 = now_s();
    long total_updates = 0;
    for (int it = 0; it < iterations; it++) {
        for (long u = 0; u < updates; u++) {
            uint64_t r = xs64(&seed);
            size_t idx = (r & mask);
            if (idx >= n) idx -= mask >> 1;   /* bound-trim cheaply */
            a[idx] ^= r;
        }
        total_updates += updates;
    }
    double t1 = now_s();
    /* prevent DCE */
    volatile uint64_t sink = 0;
    for (size_t i = 0; i < n; i += 1024 * 1024) sink ^= a[i];

    double gups = total_updates / (t1 - t0) / 1e9;
    fprintf(stdout,
            "gups: iters=%d updates=%ld wall_s=%.3f gups=%.4f work_units=%ld sink=%lu\n",
            iterations, total_updates, t1 - t0, gups, total_updates, (unsigned long)sink);

    munmap(a, bytes);
    return 0;
}
