"""AF_UNIX line-oriented JSON RPC client for agentd.

One request, one reply, one socket - the daemon closes after each reply.
"""

from __future__ import annotations

import json
import socket
from dataclasses import dataclass
from typing import Any


class IpcError(RuntimeError):
    def __init__(self, error: str, detail: str = ""):
        super().__init__(f"{error}: {detail}" if detail else error)
        self.error = error
        self.detail = detail


@dataclass
class ProgInfo:
    name: str
    sec: str
    kind: str  # "kprobe" | "tracepoint"


class AgentdClient:
    def __init__(self, sock_path: str = "/tmp/agentd.sock"):
        self.sock_path = sock_path

    def _rpc(self, op: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.connect(self.sock_path)
        try:
            req = json.dumps({"op": op, "args": args or {}}, separators=(",", ":"))
            s.sendall((req + "\n").encode())
            chunks: list[bytes] = []
            while True:
                buf = s.recv(8192)
                if not buf:
                    break
                chunks.append(buf)
                if b"\n" in buf:
                    break
        finally:
            s.close()
        line = b"".join(chunks).split(b"\n", 1)[0].decode()
        reply = json.loads(line)
        if not reply.get("ok"):
            raise IpcError(reply.get("error", "unknown"), reply.get("detail", ""))
        return reply.get("result", {})

    def ping(self) -> bool:
        return self._rpc("ping").get("pong") is True

    def load_handler(self, obj_path: str) -> tuple[int, list[ProgInfo]]:
        r = self._rpc("handler.load", {"obj_path": obj_path})
        return r["handler_id"], [ProgInfo(**p) for p in r["programs"]]

    def attach(self, handler_id: int, slots: list[dict[str, Any]]) -> int:
        """slots = [{"prog": "...", "kind": "kprobe"|"tracepoint", "idx": int}, ...]"""
        r = self._rpc("handler.attach", {"handler_id": handler_id, "slots": slots})
        return r["attached"]

    def output(self, handler_id: int) -> list[int]:
        r = self._rpc("handler.output", {"handler_id": handler_id})
        return r["counters"]

    def detach(self, handler_id: int) -> None:
        self._rpc("handler.detach", {"handler_id": handler_id})
