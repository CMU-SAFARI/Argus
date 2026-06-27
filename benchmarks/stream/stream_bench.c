/* SPDX-License-Identifier: GPL-2.0 OR BSD-3-Clause */
/* STREAM benchmark - sequential memory accesses (LLC + memory controller).
 *
 * Self-contained, no external utility headers. Three streaming kernels
 * (Copy, Scale, Add) over an array sized to fit comfortably in RAM but
 * exceed L3 several times so the perturbations actually move the needle.
 *
 * Usage: ./stream_bench [--array-mib N] [--iterations N] [--seconds T]
 */

#define _GNU_SOURCE
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdint.h>
#include <time.h>

static double now_s(void) {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return ts.tv_sec + ts.tv_nsec * 1e-9;
}

static volatile double g_sink = 0.0;

int main(int argc, char **argv) {
    size_t array_mib  = 256;          /* 256 MiB ~= 32M doubles per array */
    int    iterations = 0;            /* 0 = run until --seconds */
    double seconds    = 5.0;

    for (int i = 1; i < argc; i++) {
        if (!strcmp(argv[i], "--array-mib") && i + 1 < argc)  array_mib  = atoll(argv[++i]);
        else if (!strcmp(argv[i], "--iterations") && i + 1 < argc) iterations = atoi(argv[++i]);
        else if (!strcmp(argv[i], "--seconds") && i + 1 < argc) seconds = atof(argv[++i]);
        else if (!strcmp(argv[i], "--help")) {
            fprintf(stderr, "Usage: %s [--array-mib N] [--iterations N] [--seconds T]\n", argv[0]);
            return 0;
        }
    }

    size_t n = array_mib * (size_t)1024 * 1024 / sizeof(double);
    double *a = malloc(n * sizeof(double));
    double *b = malloc(n * sizeof(double));
    double *c = malloc(n * sizeof(double));
    if (!a || !b || !c) {
        fprintf(stderr, "alloc failed (%zu MiB x 3)\n", array_mib);
        return 1;
    }

    /* Touch all pages once - separates initial-fault cost from steady state. */
    for (size_t i = 0; i < n; i++) { a[i] = 1.0; b[i] = 2.0; c[i] = 0.0; }

    double t0 = now_s();
    int it = 0;
    while ((iterations == 0 ? now_s() - t0 < seconds : it < iterations)) {
        const double scalar = 3.0;
        for (size_t i = 0; i < n; i++) c[i] = a[i];               /* Copy  */
        for (size_t i = 0; i < n; i++) b[i] = scalar * c[i];      /* Scale */
        for (size_t i = 0; i < n; i++) c[i] = a[i] + b[i];        /* Add   */
        it++;
    }
    double t1 = now_s();
    g_sink = c[0] + c[n / 2] + c[n - 1];

    double bytes = (double)it * 3.0 * 3.0 * (double)n * (double)sizeof(double);
    /* "work_units" = total stream operations across all kernels - used for
     * per-unit normalization in the orchestrator. */
    double work_units = (double)it * 3.0 * (double)n;
    fprintf(stdout, "stream: iters=%d wall_s=%.3f gb_per_s=%.2f work_units=%.0f sink=%.1f\n",
            it, t1 - t0, bytes / 1e9 / (t1 - t0), work_units, g_sink);

    free(a); free(b); free(c);
    return 0;
}
