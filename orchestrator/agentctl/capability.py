"""Runtime capability detection for the L1->L2->L3 subsystem tree.

The subsystem manifest declares each L1 with a `capability` string. At
agentctl startup we detect which capabilities the current host satisfies
and filter the active subsystem list to those whose capability is present.
This lets the same codebase work on hosts with and without an AMD GPU,
and is forward-compatible for adding NVIDIA/Intel gates later.

The ``always`` capability is always present.

Detection mechanism:
- ``amdgpu``: at least one /sys/class/drm/card*/device/vendor reports the
  AMD vendor ID (0x1002), AND the amdgpu:amdgpu_iv tracepoint is
  exposed by the running kernel (so eBPF / perf can attach to it).

Result is cached per-process; capabilities don't change across the
lifetime of an agentctl invocation.
"""

from __future__ import annotations

from glob import glob
from pathlib import Path

_CAPS: set[str] | None = None


def _has_amd_gpu() -> bool:
    for f in glob("/sys/class/drm/card*/device/vendor"):
        try:
            if Path(f).read_text().strip() == "0x1002":
                return True
        except OSError:
            continue
    return False


def _has_tracepoint(rel: str) -> bool:
    """Returns True iff the kernel exposes the tracepoint at events/<rel>."""
    p1 = Path("/sys/kernel/tracing/events") / rel / "id"
    p2 = Path("/sys/kernel/debug/tracing/events") / rel / "id"
    return p1.exists() or p2.exists()


def detect_capabilities() -> set[str]:
    """Return the set of capabilities present on this host. Cached."""
    global _CAPS
    if _CAPS is not None:
        return _CAPS
    caps: set[str] = {"always"}
    # Phase15b: gate on amdgpu_iv (completion IRQs) rather than the legacy
    # amdgpu_cs_ioctl tracepoint, which doesn't fire for ROCm/HIP compute
    # workloads on modern KFD+doorbell stacks.
    if _has_amd_gpu() and _has_tracepoint("amdgpu/amdgpu_iv"):
        caps.add("amdgpu")
    _CAPS = caps
    return caps


def reset_cache() -> None:
    """Force re-detection on next call (test hook)."""
    global _CAPS
    _CAPS = None
