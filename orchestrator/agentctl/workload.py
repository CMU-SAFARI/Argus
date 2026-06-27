"""Benchmark wrapper - resolves a benchmark id to argv via manifest.yaml,
calls agentd's workload.run RPC.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

from .feature_vector import FeatureVector
from .ipc_client import AgentdClient

REPO = Path(__file__).resolve().parents[2]
BENCH_DIR = REPO / "benchmarks"


@dataclass
class WorkloadResult:
    ok: bool
    exit_code: int
    wall_s: float
    fv: FeatureVector
    stdout_tail: str
    error: str = ""


class Workload:
    def __init__(self, name: str):
        manifest = yaml.safe_load((BENCH_DIR / "manifest.yaml").read_text())
        if name not in manifest:
            raise KeyError(f"unknown benchmark: {name}")
        self.spec = manifest[name]
        self.name = name

    def _argv(self, **overrides) -> list[str]:
        spec = {**self.spec, **overrides}
        binary = BENCH_DIR / self.name / spec["binary"]
        argv = [str(binary)]
        for arg in spec.get("args", []):
            argv.append(str(arg))
        return argv

    def run(self, client: AgentdClient, **overrides) -> WorkloadResult:
        argv = self._argv(**overrides)
        r = client._rpc("workload.run", {"argv": argv})
        return WorkloadResult(
            ok=bool(r.get("ok")),
            exit_code=int(r.get("exit_code", -1)),
            wall_s=int(r.get("wall_s", 0)) / 1e9,
            fv=FeatureVector.from_dict(r.get("fv", {}), r.get("fv_active")),
            stdout_tail=r.get("stdout_tail", ""),
            error=r.get("error", ""),
        )
