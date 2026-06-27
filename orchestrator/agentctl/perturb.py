"""Perturbation context manager - calls perturbations/<id>.sh apply/revert.

Always runs `revert` on exit (even on exception), modeled on bash `trap`.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
PERT_DIR = REPO / "perturbations"


def _normalise_expected(raw) -> list[str]:
    """expected_subsystem may be a single string or a list of strings.
    Always return a list[str] so callers can use set-membership scoring."""
    if isinstance(raw, str):
        return [raw]
    if isinstance(raw, list):
        return list(raw)
    return ["none"]


class Perturbation:
    def __init__(self, pid: str):
        self.pid = pid
        manifest = yaml.safe_load((PERT_DIR / "manifest.yaml").read_text())
        if pid not in manifest:
            raise KeyError(f"unknown perturbation: {pid}")
        self.spec = manifest[pid]
        self.script = PERT_DIR / self.spec["script"] if self.spec.get("script") else None
        self.expected_subsystem = _normalise_expected(
            self.spec.get("expected_subsystem", "none"))

    def _run(self, action: str) -> None:
        if self.script is None:
            return
        subprocess.run(["sudo", str(self.script), action], check=True)

    def __enter__(self) -> "Perturbation":
        self._run("apply")
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        try:
            self._run("revert")
        except subprocess.CalledProcessError:
            pass  # never let a revert failure mask a real error


def expected_subsystem(pid: str, victim: str | None = None) -> list[str]:
    """Return the set of acceptable L1 subsystem labels for (pid, victim).

    If the manifest has a `per_victim.<victim>.expected_subsystem` entry,
    that *overrides* the perturbation-level default. Otherwise falls back
    to the perturbation-level `expected_subsystem`.
    """
    manifest = yaml.safe_load((PERT_DIR / "manifest.yaml").read_text())
    spec = manifest.get(pid, {}) or {}
    if victim:
        per_victim = (spec.get("per_victim") or {}).get(victim)
        if per_victim and "expected_subsystem" in per_victim:
            return _normalise_expected(per_victim["expected_subsystem"])
    return _normalise_expected(spec.get("expected_subsystem", "none"))


def _parse_path_list(raw) -> list[list[str]]:
    """Normalise an `expected_path`-style field to list[list[str]]."""
    if raw is None:
        return []
    if isinstance(raw, str):
        raw = [raw]
    out: list[list[str]] = []
    for entry in raw:
        if isinstance(entry, str):
            out.append([s for s in entry.split(".") if s])
        elif isinstance(entry, list):
            out.append([str(s) for s in entry])
    return out


def expected_paths(pid: str, victim: str | None = None) -> list[list[str]]:
    """Acceptable depth-N paths for (perturbation, optional victim).

    Manifest schema:
        p4_drop_caches:
          expected_subsystem: kernel_memory_mgmt
          expected_path:                       # default for any victim
            - kernel_memory_mgmt.reclaim
            - kernel_paging.file_fault.major
          per_victim:                          # override block per victim
            fork_storm:
              expected_subsystem: [kernel_memory_mgmt, sync_rcu]
              expected_path:
                - kernel_memory_mgmt.slab_slow
                - sync_rcu.rcu_sched_grace_period

    If `victim` is provided and the per_victim block has an
    `expected_path` entry, that *replaces* the global expected_path
    entirely (strict-override semantics). This makes "the LLM submits its
    p4 reflex (file_fault.major) on a victim where that signal does not
    apply" score wrong rather than be credited via the global accept
    list. Otherwise falls back to the perturbation-level global.
    """
    manifest = yaml.safe_load((PERT_DIR / "manifest.yaml").read_text())
    spec = manifest.get(pid, {}) or {}
    if victim:
        per_victim = (spec.get("per_victim") or {}).get(victim)
        if per_victim and "expected_path" in per_victim:
            return _parse_path_list(per_victim["expected_path"])
    return _parse_path_list(spec.get("expected_path"))
