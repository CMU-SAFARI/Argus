"""Loader + traversal helpers for the static L1->L2->L3 subsystem tree.

The tree is authored offline in orchestrator/configs/subsystems.yaml (see
docs/hierarchical_subsystem_plan.md). At load time we filter L1 nodes by
the host's detected capabilities (e.g., the amdgpu subtree is dropped on
non-AMD hosts).

Public API:
    load_tree() -> list[Node]            # capability-filtered L1 nodes
    find(tree, path) -> Node | None      # walk a (l1, l2, ...) path
    iter_leaves(tree) -> Iterator[(path, Node)]
    iter_descendants(tree, path) -> Iterator[(path, Node)]   # subtree starting at path

A "path" is a tuple[str, ...] of node names from L1 down. ("kernel_paging",)
selects the L1 node; ("kernel_paging", "anon_fault", "minor") selects the
L3 leaf.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .capability import detect_capabilities

REPO = Path(__file__).resolve().parents[2]
DEFAULT_TREE_PATH = REPO / "orchestrator" / "configs" / "subsystems.yaml"


@dataclass
class Node:
    name: str
    description: str = ""
    probes: list[str] = field(default_factory=list)  # entries are 'sym' (kprobe) or 'tp:cat/name'
    children: list["Node"] = field(default_factory=list)
    microbenchmark: str | None = None
    # Set on L1 nodes only (root of each subtree).
    capability: str = "always"
    max_depth: int = 1
    # Single kprobe-able symbol used at the root-level (L1) T_x multiplex:
    # one slot per active L1, each slot hooks this symbol with a kprobe+
    # kretprobe pair. Set on L1 nodes only; None means the subsystem is
    # tracepoint-only (e.g. amdgpu under ROCm 7.x) and is excluded from
    # the L1 T_x probe set. Distinct from `probes`, which is a list of
    # multiplex targets used inside subtrees.
    representative_probe: str | None = None

    @property
    def is_leaf(self) -> bool:
        return not self.children


def _node_from_dict(d: dict, *, is_root: bool = False) -> Node:
    # YAML field is `probes`; legacy `kprobes` is accepted as a fallback to
    # ease migration during Phase 2 wiring.
    raw = d.get("probes") if "probes" in d else d.get("kprobes", [])
    n = Node(
        name=d["name"],
        description=d.get("description", ""),
        probes=list(raw or []),
        microbenchmark=d.get("microbenchmark"),
    )
    if is_root:
        n.capability = d.get("capability", "always")
        n.max_depth = int(d.get("max_depth", 1))
        n.representative_probe = d.get("representative_probe")
    for child_d in d.get("children", []) or []:
        n.children.append(_node_from_dict(child_d))
    return n


def load_tree(path: Path | None = None,
              capabilities: set[str] | None = None) -> list[Node]:
    """Parse subsystems.yaml and return the active L1 nodes (filtered by capability).

    A subsystem is "active" iff its `capability` token is in the host's
    detected capability set. The default `always` token is always present.
    """
    p = path or DEFAULT_TREE_PATH
    raw = yaml.safe_load(p.read_text())
    caps = detect_capabilities() if capabilities is None else capabilities
    out: list[Node] = []
    for d in raw.get("subsystems", []) or []:
        n = _node_from_dict(d, is_root=True)
        if n.capability not in caps:
            continue
        out.append(n)
    return out


def find(tree: list[Node], path: tuple[str, ...]) -> Node | None:
    """Walk the tree to the node at `path`. Returns None if any segment misses."""
    if not path:
        return None
    cur = next((n for n in tree if n.name == path[0]), None)
    for seg in path[1:]:
        if cur is None:
            return None
        cur = next((c for c in cur.children if c.name == seg), None)
    return cur


def iter_descendants(tree: list[Node],
                     path: tuple[str, ...] = ()) -> Iterator[tuple[tuple[str, ...], Node]]:
    """Yield (path, node) for every node at or below `path` (BFS order)."""
    if path:
        root = find(tree, path)
        if root is None:
            return
        # Coerce to tuple so callers can pass a list and the queue's
        # `p + (c.name,)` concatenation still works.
        roots = [(tuple(path), root)]
    else:
        roots = [((n.name,), n) for n in tree]
    queue: list[tuple[tuple[str, ...], Node]] = list(roots)
    while queue:
        p, n = queue.pop(0)
        yield p, n
        for c in n.children:
            queue.append((p + (c.name,), c))


def iter_leaves(tree: list[Node],
                path: tuple[str, ...] = ()) -> Iterator[tuple[tuple[str, ...], Node]]:
    """Yield (path, node) for every leaf at or below `path`."""
    for p, n in iter_descendants(tree, path):
        if n.is_leaf:
            yield p, n


def child_probes(node: Node) -> list[tuple[str, str]]:
    """For a non-leaf node, return [(child_name, probe), ...] flattened over
    all probes of all children. Probe entries follow the same format as
    Node.probes -- bare symbol = kprobe, 'tp:<cat>/<name>' = tracepoint.
    The multiplexed-probe builder uses this to derive (slot_index, target)
    pairs."""
    out: list[tuple[str, str]] = []
    for c in node.children:
        for p in c.probes:
            out.append((c.name, p))
    return out
