from __future__ import annotations

import pytest

from audio_spider.graph_model import (
    Edge,
    GraphModel,
    Node,
    NodeKind,
    Port,
    PortKind,
)


def _node(node_id: str, kind: NodeKind = NodeKind.HW_SOURCE,
          port_kind: PortKind = PortKind.SOURCE_OUT) -> Node:
    return Node(
        id=node_id,
        kind=kind,
        label=node_id,
        ports=[Port(id="p", kind=port_kind, label="p")],
    )


def _edge(edge_id: str, src: str, dst: str) -> Edge:
    return Edge(
        id=edge_id,
        src_node=src, src_port="p",
        dst_node=dst, dst_port="p",
        kind="loopback",
    )


class Recorder:
    def __init__(self, model: GraphModel) -> None:
        self.events: list[tuple[str, ...]] = []
        for signal in ("node-added", "node-removed", "node-moved",
                       "node-changed", "edge-added", "edge-removed", "cleared"):
            model.connect(signal, self._make_handler(signal))

    def _make_handler(self, signal: str):
        def handler(_model, *args):
            self.events.append((signal, *args))
        return handler


def test_add_node_emits_signal():
    m = GraphModel()
    rec = Recorder(m)
    m.add_node(_node("a"))
    assert rec.events == [("node-added", "a")]
    assert [n.id for n in m.nodes()] == ["a"]


def test_add_duplicate_node_raises():
    m = GraphModel()
    m.add_node(_node("a"))
    with pytest.raises(KeyError):
        m.add_node(_node("a"))


def test_add_edge_requires_existing_nodes():
    m = GraphModel()
    m.add_node(_node("a"))
    with pytest.raises(KeyError, match="dst node missing"):
        m.add_edge(_edge("e", "a", "missing"))
    with pytest.raises(KeyError, match="src node missing"):
        m.add_edge(_edge("e", "missing", "a"))


def test_remove_node_drops_incident_edges():
    m = GraphModel()
    m.add_node(_node("a"))
    m.add_node(_node("b", NodeKind.HW_SINK, PortKind.SINK_IN))
    m.add_node(_node("c"))
    m.add_edge(_edge("e_ab", "a", "b"))
    m.add_edge(_edge("e_cb", "c", "b"))
    rec = Recorder(m)

    m.remove_node("b")

    assert [n.id for n in m.nodes()] == ["a", "c"]
    assert m.edges() == []
    assert ("edge-removed", "e_ab") in rec.events
    assert ("edge-removed", "e_cb") in rec.events
    assert ("node-removed", "b") in rec.events


def test_remove_missing_node_is_noop():
    m = GraphModel()
    rec = Recorder(m)
    m.remove_node("ghost")  # should not raise, no events
    assert rec.events == []


def test_move_node_updates_coords_and_emits():
    m = GraphModel()
    n = _node("a")
    m.add_node(n)
    rec = Recorder(m)
    m.move_node("a", 42.0, 17.0)
    assert n.x == 42.0
    assert n.y == 17.0
    assert rec.events == [("node-moved", "a")]


def test_update_node_replaces_in_place():
    m = GraphModel()
    m.add_node(_node("a"))
    new = Node(id="a", kind=NodeKind.NULL_SINK, label="renamed", ports=[])
    rec = Recorder(m)
    m.update_node(new)
    assert m.find_node("a") is new
    assert rec.events == [("node-changed", "a")]


def test_clear_drops_everything():
    m = GraphModel()
    m.add_node(_node("a"))
    m.add_node(_node("b", NodeKind.HW_SINK, PortKind.SINK_IN))
    m.add_edge(_edge("e", "a", "b"))
    rec = Recorder(m)
    m.clear()
    assert m.nodes() == []
    assert m.edges() == []
    assert ("cleared",) in rec.events


def test_find_returns_none_for_unknown():
    m = GraphModel()
    assert m.find_node("ghost") is None
    assert m.find_edge("ghost") is None
