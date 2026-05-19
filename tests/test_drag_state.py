from __future__ import annotations

from audio_spider.drag_state import (
    ConnectIntent,
    DragMachine,
    EdgeDrag,
    EdgeHit,
    MoveNodeIntent,
    NodeDrag,
    NodeHit,
    PortHit,
    can_connect,
)
from audio_spider.graph_model import (
    GraphModel,
    Node,
    NodeKind,
    Port,
    PortKind,
)


def _mk_model() -> GraphModel:
    m = GraphModel()
    m.add_node(
        Node(
            id="mic",
            kind=NodeKind.HW_SOURCE,
            label="Mic",
            ports=[Port("out", PortKind.SOURCE_OUT, "out")],
        )
    )
    m.add_node(
        Node(
            id="vmic",
            kind=NodeKind.NULL_SINK,
            label="VMic",
            ports=[
                Port("in", PortKind.SINK_IN, "in"),
                Port("monitor", PortKind.MONITOR_OUT, "monitor"),
            ],
        )
    )
    m.add_node(
        Node(
            id="spk",
            kind=NodeKind.HW_SINK,
            label="Speakers",
            ports=[Port("in", PortKind.SINK_IN, "in")],
        )
    )
    return m


class TestCanConnect:
    def test_source_out_to_sink_in_ok(self) -> None:
        assert can_connect(_mk_model(), PortHit("mic", "out"), PortHit("vmic", "in"))

    def test_monitor_out_to_sink_in_ok(self) -> None:
        assert can_connect(_mk_model(), PortHit("vmic", "monitor"), PortHit("spk", "in"))

    def test_self_loop_rejected(self) -> None:
        assert not can_connect(_mk_model(), PortHit("vmic", "monitor"), PortHit("vmic", "in"))

    def test_sink_in_to_source_out_accepted(self) -> None:
        # Reverse-direction drag is allowed; orientation is normalized later.
        assert can_connect(_mk_model(), PortHit("vmic", "in"), PortHit("mic", "out"))

    def test_two_producers_rejected(self) -> None:
        # Both endpoints are outputs — no sink to terminate at.
        assert not can_connect(
            _mk_model(),
            PortHit("mic", "out"),
            PortHit("vmic", "monitor"),
        )

    def test_unknown_node_rejected(self) -> None:
        assert not can_connect(_mk_model(), PortHit("nope", "out"), PortHit("vmic", "in"))

    def test_unknown_port_rejected(self) -> None:
        assert not can_connect(_mk_model(), PortHit("mic", "ghost"), PortHit("vmic", "in"))


class TestDragMachine:
    def test_begin_on_port_starts_edge_drag(self) -> None:
        dm = DragMachine(_mk_model())
        kind = dm.begin(PortHit("mic", "out"), pointer_x=100.0, pointer_y=50.0)
        assert kind == "edge"
        assert isinstance(dm.state, EdgeDrag)
        assert dm.state.src_node_id == "mic"

    def test_begin_on_node_starts_node_drag(self) -> None:
        dm = DragMachine(_mk_model())
        kind = dm.begin(
            NodeHit("mic"),
            pointer_x=120.0,
            pointer_y=60.0,
            node_origin=(100.0, 50.0),
        )
        assert kind == "node"
        assert isinstance(dm.state, NodeDrag)
        assert dm.state.grab_dx == 20.0
        assert dm.state.grab_dy == 10.0

    def test_begin_on_edge_or_blank_resets(self) -> None:
        dm = DragMachine(_mk_model())
        dm.begin(NodeHit("mic"), 0, 0, node_origin=(0, 0))
        dm.begin(None, 0, 0)
        assert dm.state is None
        dm.begin(EdgeHit("e1"), 0, 0)
        assert dm.state is None

    def test_node_drag_update_tracks_grab_offset(self) -> None:
        dm = DragMachine(_mk_model())
        dm.begin(NodeHit("mic"), 120.0, 60.0, node_origin=(100.0, 50.0))
        dm.update(200.0, 200.0)
        # node origin = pointer - grab_offset = (200-20, 200-10) = (180, 190)
        assert dm.state is not None
        assert dm.state.current_x == 180.0
        assert dm.state.current_y == 190.0

    def test_node_drag_end_emits_move_intent(self) -> None:
        dm = DragMachine(_mk_model())
        dm.begin(NodeHit("mic"), 120.0, 60.0, node_origin=(100.0, 50.0))
        dm.update(300.0, 400.0)
        intent = dm.end(drop_hit=None)
        assert intent == MoveNodeIntent("mic", 280.0, 390.0)
        assert dm.state is None  # cleared after end

    def test_edge_drag_drop_on_compatible_port_emits_connect(self) -> None:
        dm = DragMachine(_mk_model())
        dm.begin(PortHit("mic", "out"), 0, 0)
        intent = dm.end(drop_hit=PortHit("vmic", "in"))
        assert intent == ConnectIntent("mic", "out", "vmic", "in")

    def test_edge_drag_from_sink_to_producer_normalizes_direction(self) -> None:
        """Reverse-direction drag (start on sink_in, drop on producer)
        produces a producer→sink intent, matching the controller's contract."""
        dm = DragMachine(_mk_model())
        dm.begin(PortHit("vmic", "in"), 0, 0)
        intent = dm.end(drop_hit=PortHit("mic", "out"))
        assert intent == ConnectIntent("mic", "out", "vmic", "in")

    def test_edge_drag_drop_on_incompatible_returns_none(self) -> None:
        dm = DragMachine(_mk_model())
        dm.begin(PortHit("mic", "out"), 0, 0)  # producer → producer is invalid
        intent = dm.end(drop_hit=PortHit("vmic", "monitor"))
        assert intent is None
        assert dm.state is None

    def test_edge_drag_drop_on_blank_returns_none(self) -> None:
        dm = DragMachine(_mk_model())
        dm.begin(PortHit("mic", "out"), 0, 0)
        assert dm.end(drop_hit=None) is None

    def test_cancel_clears_state(self) -> None:
        dm = DragMachine(_mk_model())
        dm.begin(PortHit("mic", "out"), 0, 0)
        dm.cancel()
        assert dm.state is None
