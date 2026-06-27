/* SPDX-License-Identifier: GPL-2.0 OR BSD-3-Clause */
/*
 * M4: Minor Page Fault Type Taxonomy
 *
 * Target sensor: PageFault (handle_mm_fault) + cpu_cycles perf counter
 *
 * Exercises 6 distinct page-fault code paths to compare per-fault CPU cost:
 *
 *   anon_write  — Anonymous write fault   (demand-zero + page allocation)
 *   zero_page   — Anonymous read fault    (maps shared zero page, no alloc)
 *   cow         — Copy-on-Write via fork  (prefault N + N COW = 2N faults)
 *   ksm         — KSM COW break           (setup 2N + ~N KSM breaks ≈ 3N)
 *   page_cache  — File minor fault        (mmap file in page cache, read)
 *   major       — File major fault        (mmap file evicted from cache, read)
 *
 * Usage:
 *   ./minor_page_fault_types --type <type> [--size SIZE] [--cpu CPU]
 */
#define _GNU_SOURCE
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdint.h>
#include <unistd.h>
#include <sys/mman.h>
#include <fcntl.h>
#include <errno.h>
#include <signal.h>
#include <semaphore.h>

#include "workload_utils.h"

/* ═══════════════════════════════════════════════════════════════════════ */
/* Helpers                                                                 */
/* ═══════════════════════════════════════════════════════════════════════ */

/*
 * Build a Fisher-Yates shuffled permutation of [0 .. n).
 * Defeats the kernel's fault-around prefetcher which installs PTEs for
 * an entire PMD-aligned window on sequential access, batching many
 * faults into one handle_mm_fault call and reducing the observable
 * fault count.  Caller must free() the returned array.
 */
static size_t *make_random_order(size_t n)
{
    size_t *order = malloc(n * sizeof(*order));
    if (!order) { perror("malloc shuffle"); return NULL; }
    for (size_t i = 0; i < n; i++)
        order[i] = i;
    for (size_t i = n - 1; i > 0; i--) {
        size_t j = (size_t)rand() % (i + 1);
        size_t tmp = order[i];
        order[i] = order[j];
        order[j] = tmp;
    }
    return order;
}

static char g_tmppath[256];

static void cleanup_tmpfile(void)
{
    if (g_tmppath[0]) unlink(g_tmppath);
}

/* Read a sysfs / procfs value into buf (trimmed). */
static int read_sysfs(const char *path, char *buf, size_t len)
{
    int fd = open(path, O_RDONLY);
    if (fd < 0) return -1;
    ssize_t n = read(fd, buf, len - 1);
    close(fd);
    if (n <= 0) return -1;
    buf[n] = '\0';
    if (n > 0 && buf[n - 1] == '\n') buf[n - 1] = '\0';
    return 0;
}

/*
 * Create a temp file of the given size, filled with 'A'.
 * Returns an open fd (at EOF) on success, -1 on failure.
 */
static int create_backing_file(size_t size)
{
    snprintf(g_tmppath, sizeof(g_tmppath),
             "/tmp/fault_types_%d.dat", getpid());
    atexit(cleanup_tmpfile);

    int fd = open(g_tmppath, O_CREAT | O_RDWR | O_TRUNC, 0600);
    if (fd < 0) { perror("open tmpfile"); return -1; }

    char *buf = malloc(WL_MB);
    if (!buf) { perror("malloc"); close(fd); return -1; }
    memset(buf, 'A', WL_MB);

    for (size_t off = 0; off < size; ) {
        size_t chunk = (size - off) > WL_MB ? WL_MB : (size - off);
        ssize_t w = write(fd, buf, chunk);
        if (w <= 0) { perror("write tmpfile"); free(buf); close(fd); return -1; }
        off += (size_t)w;
    }
    fsync(fd);
    free(buf);
    return fd;
}

/* ═══════════════════════════════════════════════════════════════════════ */
/* Type 1: Anonymous Write (demand-zero + page allocation)                 */
/* ═══════════════════════════════════════════════════════════════════════ */

static void run_anon_write(size_t size)
{
    size_t np = size / PAGE_SZ;
    void *p = mmap(NULL, size, PROT_READ | PROT_WRITE,
                   MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
    if (p == MAP_FAILED) { perror("mmap"); return; }
    madvise(p, size, MADV_NOHUGEPAGE);  /* force 4K pages; THP would give 512x fewer faults */
    volatile char *r = (volatile char *)p;

    size_t *order = make_random_order(np);
    if (!order) { munmap(p, size); return; }

    printf("[anon_write] Touching %zu pages (write, random order)...\n", np);
    uint64_t t0 = now_ns();
    for (size_t i = 0; i < np; i++)
        r[order[i] * PAGE_SZ] = 1;
    uint64_t t1 = now_ns();
    printf("[anon_write] %zu faults in %.3f ms  (%.0f ns/fault)\n",
           np, (t1 - t0) / 1e6, (double)(t1 - t0) / np);

    free(order);
    munmap(p, size);
}

/* ═══════════════════════════════════════════════════════════════════════ */
/* Type 2: Zero-Page Read (maps shared zero page, no allocation)           */
/* ═══════════════════════════════════════════════════════════════════════ */

static void run_zero_page(size_t size)
{
    size_t np = size / PAGE_SZ;
    void *p = mmap(NULL, size, PROT_READ,
                   MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
    if (p == MAP_FAILED) { perror("mmap"); return; }
    madvise(p, size, MADV_NOHUGEPAGE);  /* force 4K pages; THP would give 512x fewer faults */
    volatile char *r = (volatile char *)p;

    size_t *order = make_random_order(np);
    if (!order) { munmap(p, size); return; }

    printf("[zero_page] Touching %zu pages (read-only, random order)...\n", np);
    volatile char sink = 0;
    uint64_t t0 = now_ns();
    for (size_t i = 0; i < np; i++)
        sink = r[order[i] * PAGE_SZ];
    uint64_t t1 = now_ns();
    (void)sink;
    printf("[zero_page] %zu faults in %.3f ms  (%.0f ns/fault)\n",
           np, (t1 - t0) / 1e6, (double)(t1 - t0) / np);

    free(order);
    munmap(p, size);
}

/* ═══════════════════════════════════════════════════════════════════════ */
/* Type 3: Copy-on-Write — batch fork with anon_vma hierarchy              */
/*                                                                         */
/* Demonstrates anon_vma chain growth across a process hierarchy:          */
/*   1. Parent pre-faults a large anonymous region                         */
/*   2. 20 children are fork()'d in batch (no writes yet)                  */
/*      → each fork() links a new anon_vma_chain into the parent's         */
/*        anon_vma, growing the rmap tree that do_wp_page must traverse    */
/*   3. A shared semaphore barrier ensures all 20 children are alive       */
/*      before any writes begin                                            */
/*   4. Each child writes to its own unique page stripe, triggering CoW    */
/*      faults that must walk the now-deep anon_vma chain                  */
/*                                                                         */
/* Observe with:                                                           */
/*   cat /proc/<child_pid>/maps     — VMA boundaries                       */
/*   cat /proc/<child_pid>/smaps    — per-VMA RSS / shared / private       */
/* ═══════════════════════════════════════════════════════════════════════ */

#define MAX_CHILDREN 64
#define DEFAULT_COW_CHILDREN 20

struct child_barrier {
    sem_t ready;       /* children post here after fork */
    sem_t go;          /* parent posts here to release children */
};

/* ═══════════════════════════════════════════════════════════════════════ */
/* Multi-child forked workload (used by Analysis 11 for apples-to-apples */
/* comparison: all three fault types use identical process topology)      */
/*                                                                        */
/* For --children N > 0, the workload forks N children that spread across */
/* all CPUs.  The parent mmaps anonymous memory, optionally pre-faults it */
/* (CoW only), then children touch their stripe in parallel.              */
/*                                                                        */
/*   zero_page:  Parent mmaps PROT_READ, no pre-fault.                    */
/*               Children READ their stripe → do_anonymous_page (zero pg) */
/*   anon_write: Parent mmaps PROT_READ|PROT_WRITE, no pre-fault.         */
/*               Children WRITE their stripe → do_anonymous_page (alloc)  */
/*   cow:        Parent mmaps + writes every page (pre-fault).            */
/*               Children WRITE their stripe → do_wp_page (CoW copy)      */
/*                                                                        */
/* This ensures identical anon_vma chain depth, identical page-allocator  */
/* contention, and identical scheduler topology across all three types.   */
/* ═══════════════════════════════════════════════════════════════════════ */

enum fault_mode { MODE_ZERO_PAGE, MODE_ANON_WRITE, MODE_COW };

static const char *mode_tag(enum fault_mode m)
{
    switch (m) {
        case MODE_ZERO_PAGE:  return "zero_page";
        case MODE_ANON_WRITE: return "anon_write";
        case MODE_COW:        return "cow";
    }
    return "unknown";
}

static void run_forked_iterations(size_t size, int iterations,
                                  enum fault_mode mode, int n_children)
{
    const char *tag = mode_tag(mode);
    size_t np = size / PAGE_SZ;
    size_t pages_per_child = np / n_children;

    if (pages_per_child == 0) {
        fprintf(stderr, "[%s] Size too small for %d children "
                "(need at least %d pages)\n", tag, n_children, n_children);
        return;
    }

    struct child_barrier *barrier = mmap(NULL, sizeof(*barrier),
                                         PROT_READ | PROT_WRITE,
                                         MAP_SHARED | MAP_ANONYMOUS, -1, 0);
    if (barrier == MAP_FAILED) { perror("mmap barrier"); return; }

    int prot = (mode == MODE_ZERO_PAGE) ? PROT_READ
                                        : (PROT_READ | PROT_WRITE);
    void *p = mmap(NULL, size, prot, MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
    if (p == MAP_FAILED) {
        perror("mmap data");
        munmap(barrier, sizeof(*barrier));
        return;
    }
    madvise(p, size, MADV_NOHUGEPAGE);
    volatile char *r = (volatile char *)p;

    /* Phase 1: pre-fault every page in the parent (CoW only) */
    if (mode == MODE_COW) {
        printf("[%s] Phase 1: pre-faulting %zu pages in parent (pid %d)...\n",
               tag, np, getpid());
        for (size_t i = 0; i < np; i++)
            r[i * PAGE_SZ] = 1;
        printf("[%s]   pre-fault complete\n", tag);

        if (getenv("COW_SYNC_PREFAULT")) {
            printf("[%s] COW_SYNC_PREFAULT set — pausing (SIGSTOP) for "
                   "profiler attach.\n", tag);
            printf("[%s]   Resume with: kill -CONT %d\n", tag, getpid());
            fflush(stdout);
            raise(SIGSTOP);
            printf("[%s] Resumed — profiler should now be attached.\n", tag);
        }
    }

    /* Iteration loop: fork + fault + reap */
    size_t grand_total = 0;

    for (int iter = 0; iter < iterations; iter++) {
        if (iterations > 1)
            printf("\n--- %s iteration %d / %d ---\n", tag, iter + 1, iterations);

        sem_init(&barrier->ready, 1, 0);
        sem_init(&barrier->go,    1, 0);

        printf("[%s] Forking %d children...\n", tag, n_children);

        pid_t children[MAX_CHILDREN];
        int live = 0;

        for (int c = 0; c < n_children; c++) {
            pid_t pid = fork();
            if (pid < 0) { perror("fork"); break; }

            if (pid == 0) {
                /* ── Child path ────────────────────────────── */
                int my_id = c;
                size_t my_start = (size_t)my_id * pages_per_child;
                size_t my_end   = my_start + pages_per_child;

                /* Clear inherited CPU affinity so the scheduler
                   spreads children across cores. */
                long ncpus = sysconf(_SC_NPROCESSORS_ONLN);
                if (ncpus > 0) {
                    cpu_set_t all;
                    CPU_ZERO(&all);
                    for (long i = 0; i < ncpus; i++)
                        CPU_SET(i, &all);
                    sched_setaffinity(0, sizeof(all), &all);
                }

                sem_post(&barrier->ready);
                sem_wait(&barrier->go);

                uint64_t t0 = now_ns();
                if (mode == MODE_ZERO_PAGE) {
                    volatile char sink = 0;
                    for (size_t i = my_start; i < my_end; i++)
                        sink = r[i * PAGE_SZ];
                    (void)sink;
                } else {
                    /* MODE_ANON_WRITE and MODE_COW both write */
                    for (size_t i = my_start; i < my_end; i++)
                        r[i * PAGE_SZ] = (char)(my_id + 2);
                }
                uint64_t t1 = now_ns();

                if (iterations == 1) {
                    printf("[%s]   child %2d  pid=%-6d  %zu faults "
                           "in %.3f ms  (%.0f ns/fault)\n",
                           tag, my_id, getpid(), pages_per_child,
                           (t1 - t0) / 1e6,
                           (double)(t1 - t0) / pages_per_child);
                }
                _exit(0);
            }

            children[c] = pid;
            live++;
        }

        /* Wait for every child to reach the barrier */
        for (int c = 0; c < live; c++)
            sem_wait(&barrier->ready);

        uint64_t t_all_0 = now_ns();

        /* Wake all children at once */
        for (int c = 0; c < live; c++)
            sem_post(&barrier->go);

        /* Reap all children */
        for (int c = 0; c < live; c++) {
            int st;
            waitpid(children[c], &st, 0);
        }
        uint64_t t_all_1 = now_ns();

        size_t iter_faults = (size_t)live * pages_per_child;
        grand_total += iter_faults;

        printf("[%s]   iter %d: %zu faults in %.3f ms  "
               "(%d children x %zu pages)\n",
               tag, iter + 1, iter_faults, (t_all_1 - t_all_0) / 1e6,
               live, pages_per_child);

        sem_destroy(&barrier->ready);
        sem_destroy(&barrier->go);
    }

    printf("\n[%s] All %d iteration(s) complete\n", tag, iterations);
    printf("[%s]   total faults across all iterations: %zu\n",
           tag, grand_total);

    if (mode == MODE_COW) {
        if (getenv("COW_SYNC_PREFAULT"))
            printf("[%s]   profiler sees: ~%zu CoW faults only "
                   "(pre-faults excluded via sync)\n", tag, grand_total);
        else
            printf("[%s]   profiler sees: ~%zu prefault (anon) + %zu CoW "
                   "= %zu\n", tag, np, grand_total, np + grand_total);
    }

    munmap(barrier, sizeof(*barrier));
    munmap(p, size);
}

/* Legacy wrapper: A4 uses --type cow without --children, which calls this. */
static void run_cow_iterations(size_t size, int iterations)
{
    run_forked_iterations(size, iterations, MODE_COW, DEFAULT_COW_CHILDREN);
}

/* ═══════════════════════════════════════════════════════════════════════ */
/* Type 4: KSM COW Break (merge identical pages, then write to break)      */
/* ═══════════════════════════════════════════════════════════════════════ */

static void ksm_restore(const char *sv_run, const char *sv_sleep,
                         const char *sv_scan)
{
    cg_write("/sys/kernel/mm/ksm/run", sv_run);
    cg_write("/sys/kernel/mm/ksm/sleep_millisecs", sv_sleep);
    cg_write("/sys/kernel/mm/ksm/pages_to_scan", sv_scan);
}

static void run_ksm(size_t size)
{
    if (access("/sys/kernel/mm/ksm/run", W_OK) != 0) {
        fprintf(stderr, "[ksm] KSM not available "
                "(/sys/kernel/mm/ksm/run not writable)\n");
        return;
    }

    size_t np = size / PAGE_SZ;

    /* Save current KSM settings */
    char sv_run[16] = "0", sv_sleep[16] = "200", sv_scan[16] = "100";
    read_sysfs("/sys/kernel/mm/ksm/run", sv_run, sizeof(sv_run));
    read_sysfs("/sys/kernel/mm/ksm/sleep_millisecs",
               sv_sleep, sizeof(sv_sleep));
    read_sysfs("/sys/kernel/mm/ksm/pages_to_scan",
               sv_scan, sizeof(sv_scan));

    /* Enable aggressive KSM scanning */
    cg_write("/sys/kernel/mm/ksm/pages_to_scan", "4096");
    cg_write("/sys/kernel/mm/ksm/sleep_millisecs", "10");
    cg_write("/sys/kernel/mm/ksm/run", "1");

    /* Allocate two regions and fill with identical content */
    void *p1 = mmap(NULL, size, PROT_READ | PROT_WRITE,
                    MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
    void *p2 = mmap(NULL, size, PROT_READ | PROT_WRITE,
                    MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
    if (p1 == MAP_FAILED || p2 == MAP_FAILED) {
        perror("mmap");
        if (p1 != MAP_FAILED) munmap(p1, size);
        if (p2 != MAP_FAILED) munmap(p2, size);
        ksm_restore(sv_run, sv_sleep, sv_scan);
        return;
    }

    printf("[ksm] Filling 2 × %zu pages with identical content...\n", np);
    memset(p1, 'K', size);
    memset(p2, 'K', size);
    madvise(p1, size, MADV_MERGEABLE);
    madvise(p2, size, MADV_MERGEABLE);

    /* Poll pages_sharing until merge converges (up to 60 s) */
    printf("[ksm] Waiting for KSM merge...\n");
    unsigned long sharing = 0;
    for (int attempt = 0; attempt < 600; attempt++) {
        usleep(100000); /* 100 ms */
        char buf[32];
        if (read_sysfs("/sys/kernel/mm/ksm/pages_sharing",
                       buf, sizeof(buf)) == 0)
            sharing = strtoul(buf, NULL, 10);
        if (sharing >= np) break;
        if (attempt % 50 == 0)
            printf("[ksm]   pages_sharing = %lu / %zu\n", sharing, np);
    }
    printf("[ksm] Merge done: pages_sharing = %lu\n", sharing);
    if (sharing < np / 2)
        fprintf(stderr, "[ksm] WARNING: <50%% merged; results may be noisy\n");

    /* Pause KSM scanner to avoid interference during measurement */
    cg_write("/sys/kernel/mm/ksm/run", "0");

    /* Write to p2 → KSM-break COW faults */
    volatile char *r = (volatile char *)p2;
    printf("[ksm] Breaking KSM on %zu pages...\n", np);
    uint64_t t0 = now_ns();
    for (size_t i = 0; i < np; i++)
        r[i * PAGE_SZ] = 'X';
    uint64_t t1 = now_ns();
    printf("[ksm] %.3f ms  (%.0f ns/write, ~%lu actual KSM breaks)\n",
           (t1 - t0) / 1e6, (double)(t1 - t0) / np, sharing);
    printf("[ksm] Total profiler faults ≈ %zu (setup) + %lu (breaks) ≈ %lu\n",
           2 * np, sharing, 2 * np + sharing);

    munmap(p1, size);
    munmap(p2, size);
    ksm_restore(sv_run, sv_sleep, sv_scan);
}

/* ═══════════════════════════════════════════════════════════════════════ */
/* Type 5: Page-Cache Minor Fault (mmap file already in page cache)        */
/* ═══════════════════════════════════════════════════════════════════════ */

static void run_page_cache(size_t size)
{
    size_t np = size / PAGE_SZ;
    int fd = create_backing_file(size);
    if (fd < 0) return;

    /*
     * Guarantee page-cache residency: write() already populates the page
     * cache, but do an explicit read() pass as belt-and-suspenders so
     * every page is hot.
     */
    lseek(fd, 0, SEEK_SET);
    char tmp[4096];
    for (size_t off = 0; off < size; off += sizeof(tmp)) {
        ssize_t rd = read(fd, tmp, sizeof(tmp));
        if (rd <= 0) break;
    }

    void *p = mmap(NULL, size, PROT_READ, MAP_SHARED, fd, 0);
    close(fd);
    if (p == MAP_FAILED) { perror("mmap"); return; }

    /*
     * MADV_RANDOM sets VM_RAND_READ on the VMA, which makes the kernel
     * skip filemap_map_pages() (the "fault-around" optimisation).
     * Without this, a single handle_mm_fault installs PTEs for an entire
     * 2 MB PMD-aligned window of cached pages, so 512 MB would produce
     * only ~256 handle_mm_fault calls instead of 131 072.
     */
    madvise(p, size, MADV_RANDOM);
    volatile char *r = (volatile char *)p;

    printf("[page_cache] Reading %zu pages from page cache...\n", np);
    volatile char sink = 0;
    uint64_t t0 = now_ns();
    for (size_t i = 0; i < np; i++)
        sink = r[i * PAGE_SZ];
    uint64_t t1 = now_ns();
    (void)sink;
    printf("[page_cache] %zu faults in %.3f ms  (%.0f ns/fault)\n",
           np, (t1 - t0) / 1e6, (double)(t1 - t0) / np);

    munmap(p, size);
}

/* ═══════════════════════════════════════════════════════════════════════ */
/* Type 6: Major Fault (mmap file with page cache evicted)                 */
/* ═══════════════════════════════════════════════════════════════════════ */

static void run_major(size_t size)
{
    size_t np = size / PAGE_SZ;
    int fd = create_backing_file(size);
    if (fd < 0) return;

    /* Evict this file's pages from the page cache */
    posix_fadvise(fd, 0, (off_t)size, POSIX_FADV_DONTNEED);
    close(fd);

    /* Belt-and-suspenders: system-wide drop of clean page cache */
    sync();
    {
        int dc = open("/proc/sys/vm/drop_caches", O_WRONLY);
        if (dc >= 0) {
            ssize_t w = write(dc, "1", 1);
            (void)w; /* best-effort; needs root */
            close(dc);
        }
    }
    sleep(1); /* let I/O settle */

    /* Re-open and mmap with readahead disabled */
    fd = open(g_tmppath, O_RDONLY);
    if (fd < 0) { perror("open"); return; }
    posix_fadvise(fd, 0, (off_t)size, POSIX_FADV_DONTNEED);

    void *p = mmap(NULL, size, PROT_READ, MAP_SHARED, fd, 0);
    close(fd);
    if (p == MAP_FAILED) { perror("mmap"); return; }
    madvise(p, size, MADV_RANDOM); /* suppress readahead */
    volatile char *r = (volatile char *)p;

    printf("[major] Reading %zu pages (page cache evicted)...\n", np);
    volatile char sink = 0;
    uint64_t t0 = now_ns();
    for (size_t i = 0; i < np; i++)
        sink = r[i * PAGE_SZ];
    uint64_t t1 = now_ns();
    (void)sink;
    printf("[major] %zu faults in %.3f ms  (%.0f ns/fault)\n",
           np, (t1 - t0) / 1e6, (double)(t1 - t0) / np);

    munmap(p, size);
}

/* ═══════════════════════════════════════════════════════════════════════ */
/* CLI                                                                     */
/* ═══════════════════════════════════════════════════════════════════════ */

static void usage(const char *prog)
{
    fprintf(stderr,
            "Usage: %s --type <type> [--size SIZE] [--cpu CPU] "
            "[--iterations N] [--children N]\n\n"
            "Types:\n"
            "  anon_write  — Anonymous write (demand-zero + alloc)\n"
            "  zero_page   — Anonymous read  (shared zero page)\n"
            "  cow         — Copy-on-Write via fork\n"
            "  ksm         — KSM COW break\n"
            "  page_cache  — File minor fault (page in cache)\n"
            "  major       — File major fault (page evicted)\n\n"
            "Options:\n"
            "  --iterations N  Repeat the workload N times (default: 1).\n"
            "  --children N    Fork N children that spread across CPUs.\n"
            "                  Each child touches its own page stripe.\n"
            "                  Use for apples-to-apples comparison across\n"
            "                  fault types (same topology, same contention).\n"
            "                  Supported for: anon_write, zero_page, cow.\n"
            "                  Default: 0 (single-process, A4 compat).\n",
            prog);
    exit(1);
}

typedef void (*run_fn)(size_t);

int main(int argc, char **argv)
{
    const char *type = NULL;
    size_t size = 512 * WL_MB;
    int cpu = -1;
    int iterations = 1;
    int children = 0;

    for (int i = 1; i < argc; i++) {
        if (!strcmp(argv[i], "--type") && i + 1 < argc)
            type = argv[++i];
        else if (!strcmp(argv[i], "--size") && i + 1 < argc)
            size = parse_size(argv[++i]);
        else if (!strcmp(argv[i], "--cpu") && i + 1 < argc)
            cpu = atoi(argv[++i]);
        else if (!strcmp(argv[i], "--iterations") && i + 1 < argc)
            iterations = atoi(argv[++i]);
        else if (!strcmp(argv[i], "--children") && i + 1 < argc)
            children = atoi(argv[++i]);
        else
            usage(argv[0]);
    }

    if (!type) usage(argv[0]);
    if (iterations < 1) iterations = 1;
    if (children < 0) children = 0;
    if (children > MAX_CHILDREN) children = MAX_CHILDREN;
    size = (size / PAGE_SZ) * PAGE_SZ; /* page-align */

    if (cpu >= 0) pin_cpu(cpu);

    printf("=== minor_page_fault_types: type=%s  size=%zuM  cpu=%d  "
           "iterations=%d  children=%d ===\n",
           type, size / WL_MB, cpu, iterations, children);

    /*
     * Multi-child path (--children N > 0): all three fault types use the
     * same fork topology for apples-to-apples comparison (Analysis 11).
     */
    if (children > 0) {
        enum fault_mode mode;
        if      (!strcmp(type, "anon_write")) mode = MODE_ANON_WRITE;
        else if (!strcmp(type, "zero_page"))  mode = MODE_ZERO_PAGE;
        else if (!strcmp(type, "cow"))        mode = MODE_COW;
        else {
            fprintf(stderr, "--children is only supported for "
                    "anon_write, zero_page, cow\n");
            return 1;
        }
        run_forked_iterations(size, iterations, mode, children);
        return 0;
    }

    /* Single-process path (A4 backwards compatibility) */
    if (!strcmp(type, "cow")) {
        run_cow_iterations(size, iterations);
    } else {
        run_fn fn = NULL;
        if      (!strcmp(type, "anon_write"))  fn = run_anon_write;
        else if (!strcmp(type, "zero_page"))   fn = run_zero_page;
        else if (!strcmp(type, "ksm"))         fn = run_ksm;
        else if (!strcmp(type, "page_cache"))  fn = run_page_cache;
        else if (!strcmp(type, "major"))       fn = run_major;
        else { fprintf(stderr, "Unknown type: %s\n", type); usage(argv[0]); }

        for (int iter = 0; iter < iterations; iter++) {
            if (iterations > 1)
                printf("\n--- iteration %d / %d ---\n", iter + 1, iterations);
            fn(size);
        }

        if (iterations > 1)
            printf("\n=== All %d iterations complete ===\n", iterations);
    }

    return 0;
}
