from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from gi.repository import GObject


class PortKind(Enum):
    SOURCE_OUT = "source_out"
    SINK_IN = "sink_in"
    MONITOR_OUT = "monitor_out"
    COMBINE_MEMBERS = "combine_members"


class NodeKind(Enum):
    HW_SOURCE = "hw_source"
    HW_SINK = "hw_sink"
    NULL_SINK = "null_sink"
    COMBINE_SINK = "combine_sink"
    MISSING = "missing"  # placeholder for endpoints referenced by an existing
                          # module but no longer present in PulseAudio


EdgeKind = str  # "loopback" | "combine-member"


@dataclass
class Port:
    id: str
    kind: PortKind
    label: str


@dataclass
class Node:
    id: str
    kind: NodeKind
    label: str
    ports: list[Port] = field(default_factory=list)
    x: float = 0.0
    y: float = 0.0
    pa_module_index: int | None = None
    backing_config_id: str | None = None


@dataclass
class Edge:
    id: str
    src_node: str
    src_port: str
    dst_node: str
    dst_port: str
    kind: EdgeKind
    pa_module_index: int | None = None


class GraphModel(GObject.Object):
    __gsignals__ = {
        "node-added": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
        "node-removed": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
        "node-moved": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
        "node-changed": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
        "edge-added": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
        "edge-removed": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
        "cleared": (GObject.SignalFlags.RUN_FIRST, None, ()),
    }

    def __init__(self) -> None:
        super().__init__()
        self._nodes: dict[str, Node] = {}
        self._edges: dict[str, Edge] = {}

    def add_node(self, node: Node) -> None:
        if node.id in self._nodes:
            raise KeyError(f"node already exists: {node.id}")
        self._nodes[node.id] = node
        self.emit("node-added", node.id)

    def remove_node(self, node_id: str) -> None:
        if node_id not in self._nodes:
            return
        # drop incident edges first so listeners see consistent state
        incident = [
            e.id for e in self._edges.values()
            if e.src_node == node_id or e.dst_node == node_id
        ]
        for edge_id in incident:
            self.remove_edge(edge_id)
        del self._nodes[node_id]
        self.emit("node-removed", node_id)

    def move_node(self, node_id: str, x: float, y: float) -> None:
        node = self._nodes[node_id]
        node.x = x
        node.y = y
        self.emit("node-moved", node_id)

    def update_node(self, node: Node) -> None:
        if node.id not in self._nodes:
            raise KeyError(node.id)
        self._nodes[node.id] = node
        self.emit("node-changed", node.id)

    def add_edge(self, edge: Edge) -> None:
        if edge.id in self._edges:
            raise KeyError(f"edge already exists: {edge.id}")
        if edge.src_node not in self._nodes:
            raise KeyError(f"src node missing: {edge.src_node}")
        if edge.dst_node not in self._nodes:
            raise KeyError(f"dst node missing: {edge.dst_node}")
        self._edges[edge.id] = edge
        self.emit("edge-added", edge.id)

    def remove_edge(self, edge_id: str) -> None:
        if edge_id not in self._edges:
            return
        del self._edges[edge_id]
        self.emit("edge-removed", edge_id)

    def clear(self) -> None:
        self._nodes.clear()
        self._edges.clear()
        self.emit("cleared")

    def nodes(self) -> list[Node]:
        return list(self._nodes.values())

    def edges(self) -> list[Edge]:
        return list(self._edges.values())

    def find_node(self, node_id: str) -> Node | None:
        return self._nodes.get(node_id)

    def find_edge(self, edge_id: str) -> Edge | None:
        return self._edges.get(edge_id)
