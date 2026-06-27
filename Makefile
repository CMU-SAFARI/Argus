# =============================================================================
# AgenticBPF - Top-level Makefile
# =============================================================================
# Builds:
#   1. libbpf from submodule (static library)
#   2. vmlinux.h via bpftool BTF dump
#   3. dispatcher.bpf.c -> dispatcher.skel.h (linked into agentd)
#   4. agentd: C++ daemon, exposes JSON IPC over AF_UNIX
#   5. Pattern rule for any .bpf.c -> .bpf.o (used for agent-authored handlers,
#      compiled on demand by the orchestrator with the same BPF_CFLAGS)
# =============================================================================

CLANG          ?= clang
CLANGXX        ?= clang++
BPFTOOL        ?= bpftool
LLVM_STRIP     ?= llvm-strip

ARCH           := $(shell uname -m | sed 's/x86_64/x86/' | sed 's/aarch64/arm64/')

ROOT_DIR       := $(abspath .)
SRC_DIR        := $(ROOT_DIR)/src
BPF_DIR        := $(SRC_DIR)/bpf
AGENTD_DIR     := $(SRC_DIR)/agentd
INCLUDE_DIR    := $(SRC_DIR)/include
BUILD_DIR      := $(ROOT_DIR)/build
OBJ_DIR        := $(BUILD_DIR)/obj
SKEL_DIR       := $(BUILD_DIR)/skel
HANDLER_OBJ_DIR:= $(BUILD_DIR)/handler_obj

LIBBPF_SRC     := $(ROOT_DIR)/third_party/libbpf/src
LIBBPF_BUILD   := $(BUILD_DIR)/libbpf
LIBBPF_DESTDIR := $(LIBBPF_BUILD)/destdir
LIBBPF_STATIC  := $(LIBBPF_DESTDIR)/usr/lib64/libbpf.a
LIBBPF_INCLUDE := $(LIBBPF_DESTDIR)/usr/include
VMLINUX_H      := $(INCLUDE_DIR)/vmlinux.h

# Exposed so orchestrator/agentctl/compile_bpf.py can reuse the exact flags
# (it parses `make -p` for BPF_CFLAGS).
export BPF_CFLAGS  := -g -O2 \
                      -target bpf \
                      -D__TARGET_ARCH_$(ARCH) \
                      -I$(INCLUDE_DIR) \
                      -I$(LIBBPF_INCLUDE) \
                      -I$(LIBBPF_INCLUDE)/bpf \
                      -Wall -Wno-unused-function

CXXFLAGS       := -g -O2 -std=c++20 \
                  -Wall -Wextra \
                  -I$(INCLUDE_DIR) \
                  -I$(LIBBPF_INCLUDE) \
                  -I$(LIBBPF_INCLUDE)/bpf \
                  -I$(SKEL_DIR)

LDFLAGS        := -lelf -lz -lzstd -lpthread

# --- Sources for agentd ---
AGENTD_CPP     := $(wildcard $(AGENTD_DIR)/*.cpp)
AGENTD_OBJS    := $(patsubst $(AGENTD_DIR)/%.cpp, $(OBJ_DIR)/%.o, $(AGENTD_CPP))

DISPATCHER_BPF := $(BPF_DIR)/dispatcher.bpf.c
DISPATCHER_OBJ := $(OBJ_DIR)/dispatcher.bpf.o
DISPATCHER_SKEL:= $(SKEL_DIR)/dispatcher.skel.h

TARGET         := $(BUILD_DIR)/agentd

.PHONY: all clean distclean libbpf vmlinux dispatcher

all: $(TARGET)
	@echo ""
	@echo "Build complete: $(TARGET)"
	@echo "Run with: sudo $(TARGET)"

# --- libbpf static library (from submodule) ---
$(LIBBPF_STATIC): $(wildcard $(LIBBPF_SRC)/*.[ch] $(LIBBPF_SRC)/Makefile)
	@mkdir -p $(LIBBPF_BUILD)
	$(MAKE) -C $(LIBBPF_SRC) \
	    BUILD_STATIC_ONLY=1 \
	    OBJDIR=$(LIBBPF_BUILD)/obj \
	    DESTDIR=$(LIBBPF_DESTDIR) \
	    INCLUDEDIR=/usr/include \
	    LIBDIR=/usr/lib64 \
	    UAPIDIR=/usr/include \
	    install

libbpf: $(LIBBPF_STATIC)

# --- vmlinux.h (BTF dump from running kernel) ---
$(VMLINUX_H):
	@mkdir -p $(INCLUDE_DIR)
	$(BPFTOOL) btf dump file /sys/kernel/btf/vmlinux format c > $@

vmlinux: $(VMLINUX_H)

# --- Dispatcher BPF object (linked into agentd via skeleton) ---
$(DISPATCHER_OBJ): $(DISPATCHER_BPF) $(LIBBPF_STATIC) $(VMLINUX_H) $(wildcard $(INCLUDE_DIR)/*.h)
	@mkdir -p $(OBJ_DIR)
	$(CLANG) $(BPF_CFLAGS) -c $< -o $@
	$(LLVM_STRIP) -g $@

$(DISPATCHER_SKEL): $(DISPATCHER_OBJ)
	@mkdir -p $(SKEL_DIR)
	$(BPFTOOL) gen skeleton $< > $@

dispatcher: $(DISPATCHER_SKEL)

# --- Generic handler compile target ---
# Used by orchestrator (and humans) to compile a single .bpf.c on demand:
#   make handler BPF_SRC=path/to/handler.bpf.c [HANDLER_OUT=...]
# The default output is build/handler_obj/<basename>.bpf.o.
HANDLER_OUT ?= $(HANDLER_OBJ_DIR)/$(notdir $(BPF_SRC:.bpf.c=.bpf.o))

.PHONY: handler
handler: $(LIBBPF_STATIC) $(VMLINUX_H)
	@if [ -z "$(BPF_SRC)" ]; then echo "ERROR: pass BPF_SRC=<path/to/file.bpf.c>" >&2; exit 1; fi
	@mkdir -p $(dir $(HANDLER_OUT))
	$(CLANG) $(BPF_CFLAGS) -c $(BPF_SRC) -o $(HANDLER_OUT)
	$(LLVM_STRIP) -g $(HANDLER_OUT)
	@echo "Built handler: $(HANDLER_OUT)"

# Build every canned probe under agent_handlers/canned/ in one shot.
# B1 baseline + sweeps need these built or they'll FileNotFoundError mid-run.
CANNED_SRCS := $(wildcard agent_handlers/canned/*.bpf.c)
CANNED_OBJS := $(patsubst agent_handlers/canned/%.bpf.c,$(HANDLER_OBJ_DIR)/%.bpf.o,$(CANNED_SRCS))

.PHONY: canned
canned: $(CANNED_OBJS)

$(HANDLER_OBJ_DIR)/%.bpf.o: agent_handlers/canned/%.bpf.c $(LIBBPF_STATIC) $(VMLINUX_H)
	@mkdir -p $(HANDLER_OBJ_DIR)
	$(CLANG) $(BPF_CFLAGS) -c $< -o $@
	$(LLVM_STRIP) -g $@
	@echo "Built canned probe: $@"

# --- C++ user-space objects ---
$(OBJ_DIR)/%.o: $(AGENTD_DIR)/%.cpp $(DISPATCHER_SKEL) $(LIBBPF_STATIC)
	@mkdir -p $(OBJ_DIR)
	$(CLANGXX) $(CXXFLAGS) -c $< -o $@

# --- Final agentd binary ---
$(TARGET): $(AGENTD_OBJS) $(LIBBPF_STATIC)
	@mkdir -p $(BUILD_DIR)
	$(CLANGXX) $(CXXFLAGS) $(AGENTD_OBJS) $(LIBBPF_STATIC) -o $@ $(LDFLAGS)

clean:
	rm -rf $(BUILD_DIR)

distclean: clean
	rm -f $(VMLINUX_H)
