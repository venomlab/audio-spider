"""Pure-Python drag state machine for GraphView.

Kept separate from any GTK/GooCanvas types so it can be unit-tested without
a display. Encodes the rules:

* Press on a port → start an "edge-pending" drag. On release over a
  compatible target port, emit a connect intent.
* Press on a node body → start a "node-drag" drag. On release, emit a
  move intent with the final position.
* Otherwise (blank canvas) → idle.

The state machine owns no graphics — it just translates pointer-input
"hits" into intents that GraphView turns into Gtk/canvas actions.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from .graph_model import GraphModel, PortKind


@dataclass(frozen=True)
class NodeHit:
    node_id: str


@dataclass(frozen=True)
class PortHit:
    node_id: str
    port_id: str


@dataclass(frozen=True)
class EdgeHit:
    edge_id: str


Hit = NodeHit | PortHit | EdgeHit | None


@dataclass
class NodeDrag:
    node_id: str
    grab_dx: float  # pointer offset within node when drag started
    grab_dy: float
    current_x: float
    current_y: float


@dataclass
class EdgeDrag:
    src_node_id: str
    src_port_id: str
    current_x: float
    current_y: float


DragState = NodeDrag | EdgeDrag | None


@dataclass(frozen=True)
class MoveNodeIntent:
    node_id: str
    x: float
    y: float


@dataclass(frozen=True)
class ConnectIntent:
    src_node_id: str
    src_port_id: str
    dst_node_id: str
    dst_port_id: str


Intent = MoveNodeIntent | ConnectIntent | None


_PRODUCER_PORTS = (
    PortKind.SOURCE_OUT, PortKind.MONITOR_OUT, PortKind.COMBINE_MEMBERS,
)


def can_connect(model: GraphModel, src: PortHit, dst: PortHit) -> bool:
    """Symmetric check: is a connection possible between these two ports?

    Permitted endpoint pairings (audio always flows producer → sink):
      * SOURCE_OUT or MONITOR_OUT → SINK_IN: loopback.
      * COMBINE_MEMBERS → SINK_IN: add member to Speaker Group.

    The predicate accepts either drag direction — caller may have dragged
    from sink to producer. DragMachine.end() normalizes the orientation
    before handing the intent to the controller.
    """
    if src.node_id == dst.node_id:
        return False
    src_node = model.find_node(src.node_id)
    dst_node = model.find_node(dst.node_id)
    if src_node is None or dst_node is None:
        return False
    src_port = next((p for p in src_node.ports if p.id == src.port_id), None)
    dst_port = next((p for p in dst_node.ports if p.id == dst.port_id), None)
    if src_port is None or dst_port is None:
        return False
    forward = (
        src_port.kind in _PRODUCER_PORTS and dst_port.kind == PortKind.SINK_IN
    )
    reverse = (
        src_port.kind == PortKind.SINK_IN and dst_port.kind in _PRODUCER_PORTS
    )
    return forward or reverse


class DragMachine:
    """Stateful but pure. Methods return intents (or None) when applicable."""

    def __init__(self, model: GraphModel) -> None:
        self._model = model
        self._state: DragState = None

    @property
    def state(self) -> DragState:
        return self._state

    def begin(
        self,
        hit: Hit,
        pointer_x: float,
        pointer_y: float,
        node_origin: tuple[float, float] | None = None,
    ) -> Literal["node", "edge", "ignore"]:
        if isinstance(hit, PortHit):
            self._state = EdgeDrag(
                src_node_id=hit.node_id,
                src_port_id=hit.port_id,
                current_x=pointer_x,
                current_y=pointer_y,
            )
            return "edge"
        if isinstance(hit, NodeHit):
            if node_origin is None:
                node_origin = (pointer_x, pointer_y)
            ox, oy = node_origin
            self._state = NodeDrag(
                node_id=hit.node_id,
                grab_dx=pointer_x - ox,
                grab_dy=pointer_y - oy,
                current_x=ox,
                current_y=oy,
            )
            return "node"
        self._state = None
        return "ignore"

    def update(self, pointer_x: float, pointer_y: float) -> None:
        if isinstance(self._state, NodeDrag):
            self._state.current_x = pointer_x - self._state.grab_dx
            self._state.current_y = pointer_y - self._state.grab_dy
        elif isinstance(self._state, EdgeDrag):
            self._state.current_x = pointer_x
            self._state.current_y = pointer_y

    def end(self, drop_hit: Hit) -> Intent:
        try:
            if isinstance(self._state, NodeDrag):
                return MoveNodeIntent(
                    node_id=self._state.node_id,
                    x=self._state.current_x,
                    y=self._state.current_y,
                )
            if isinstance(self._state, EdgeDrag):
                if not isinstance(drop_hit, PortHit):
                    return None
                src = PortHit(self._state.src_node_id, self._state.src_port_id)
                if not can_connect(self._model, src, drop_hit):
                    return None
                # If user dragged from a sink back to a producer, the visible
                # drag goes opposite to the audio flow. Flip src/dst so the
                # emitted intent always reads producer → sink.
                src_node = self._model.find_node(src.node_id)
                src_port = next(
                    (p for p in src_node.ports if p.id == src.port_id), None,
                )
                if src_port is not None and src_port.kind == PortKind.SINK_IN:
                    src, drop_hit = drop_hit, src
                return ConnectIntent(
                    src_node_id=src.node_id,
                    src_port_id=src.port_id,
                    dst_node_id=drop_hit.node_id,
                    dst_port_id=drop_hit.port_id,
                )
            return None
        finally:
            self._state = None

    def cancel(self) -> None:
        self._state = None
