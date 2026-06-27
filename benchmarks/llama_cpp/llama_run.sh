#!/usr/bin/env bash
# Real-workload benchmark: LLM inference on the local AMD gfx1201 GPU
# via llama.cpp's HIP/ROCm backend.
#
# Drives N_ITERATIONS prompts through llama-completion, accumulates the
# total tokens emitted, and prints `work_units=<total_tokens>` on stdout
# so AgenticBPF's normaliser can divide perf-stat counters by tokens.
#
# Why a shell wrapper rather than calling llama-completion directly:
# llama-completion writes its statistics to stderr, and AgenticBPF reads
# work_units from stdout. We need a tiny adapter that runs the binary,
# parses the `eval time = ... / N runs` line out of stderr, sums across
# iterations, and emits `work_units=<sum>` cleanly.
#
# Env overrides:
#   LLAMA_BIN          path to llama-completion (default: /home/vlnitu/llama.cpp/build/bin/llama-completion)
#   LLAMA_MODEL        path to .gguf (default: /home/vlnitu/llama3.1-8b.gguf)
#   LLAMA_NGL          GPU layers (default: 999 = full offload)
#   LLAMA_CTX          ctx-size (default: 4096; small to keep KV-cache footprint bounded)
#   N_ITERATIONS       prompts per run (default: 4)
#   N_PREDICT          tokens per prompt (default: 128)
#   SEED_BASE          per-iteration seed = SEED_BASE + i (default: 42)
set -euo pipefail

LLAMA_BIN="${LLAMA_BIN:-/home/vlnitu/llama.cpp/build-vulkan/bin/llama-completion}"
LLAMA_MODEL="${LLAMA_MODEL:-/home/vlnitu/llama3.1-8b.gguf}"
LLAMA_NGL="${LLAMA_NGL:-999}"
LLAMA_CTX="${LLAMA_CTX:-4096}"
N_ITERATIONS="${N_ITERATIONS:-4}"
N_PREDICT="${N_PREDICT:-128}"
SEED_BASE="${SEED_BASE:-42}"

# Fixed prompts so iterations differ only in seed, not in input length.
# Each prompt is short (reduces prompt-eval time and makes total wall
# more decode-dominated, which is what stresses the GPU+host kernel).
PROMPTS=(
    "Explain in three sentences how a TLB shootdown works in Linux."
    "Describe the role of kcompactd in the Linux memory subsystem."
    "What is the difference between minor and major page faults?"
    "How does the page allocator decide between buddy zones?"
    "Why does drop_caches=3 cause kernel allocator activity afterwards?"
    "What is the lifecycle of a transparent huge page in Linux?"
    "Compare madvise(MADV_HUGEPAGE) and THP=always semantically."
    "Explain how mmap_lock contention shows up in perf events."
)

if [[ ! -x "$LLAMA_BIN" ]]; then
    echo "error: llama-completion not found at $LLAMA_BIN" >&2
    exit 1
fi
if [[ ! -f "$LLAMA_MODEL" ]]; then
    echo "error: model file not found at $LLAMA_MODEL" >&2
    exit 1
fi

TOTAL_TOKENS=0
ERR_TMP="$(mktemp)"
trap 'rm -f "$ERR_TMP"' EXIT

# Pre-touch the model into page cache so all iterations load fast from
# memory rather than disk. Without this, the first iteration pays full
# disk-read cost (~5-10s for a 4.6 GB GGUF on SSD) while later iterations
# load from cache (~1-2s); the per-WU normalisation can't distinguish
# the two, so reference-profile z-scores explode under perturbation.
cat "$LLAMA_MODEL" > /dev/null 2>&1 || true

# Warmup iteration: load model, generate a few tokens, discard. Stabilises
# GPU memory state (VRAM allocator + driver caches) before the measured
# loop. Output is suppressed so it doesn't contribute to work_units.
"$LLAMA_BIN" \
    -m "$LLAMA_MODEL" \
    -p "warmup" \
    -n 8 \
    -ngl "$LLAMA_NGL" \
    -c "$LLAMA_CTX" \
    --seed 0 \
    --no-warmup \
    < /dev/null > /dev/null 2>&1 || true

for ((i=0; i<N_ITERATIONS; i++)); do
    PROMPT="${PROMPTS[$((i % ${#PROMPTS[@]}))]}"
    SEED=$((SEED_BASE + i))
    "$LLAMA_BIN" \
        -m "$LLAMA_MODEL" \
        -p "$PROMPT" \
        -n "$N_PREDICT" \
        -ngl "$LLAMA_NGL" \
        -c "$LLAMA_CTX" \
        --seed "$SEED" \
        --no-warmup \
        < /dev/null \
        > /dev/null 2> "$ERR_TMP"

    # Parse total tokens from llama-completion's perf summary, e.g.:
    #   common_perf_print:       total time =     307.15 ms /    31 tokens
    # Variable spacing makes a single regex brittle; do it in two steps:
    # find the line, then extract the trailing N before "tokens".
    TOKENS_THIS_RUN="$(grep 'total time' "$ERR_TMP" | grep -oE '[0-9]+ tokens' | grep -oE '[0-9]+' | head -1)"
    if [[ -z "$TOKENS_THIS_RUN" ]]; then
        echo "warning: could not parse token count from llama-completion stderr (iteration $i)" >&2
        echo "stderr tail:" >&2
        tail -5 "$ERR_TMP" >&2
        TOKENS_THIS_RUN=0
    fi
    TOTAL_TOKENS=$((TOTAL_TOKENS + TOKENS_THIS_RUN))
done

echo "iterations=$N_ITERATIONS tokens_per_iter_avg=$((TOTAL_TOKENS / N_ITERATIONS))"
echo "work_units=$TOTAL_TOKENS"
