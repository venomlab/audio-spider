"""GraphView smoke tests — need GTK display + GooCanvas typelib."""
from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.gtk


@pytest.fixture(scope="module")
def _gtk():
    if os.environ.get("AUDIO_SPIDER_SKIP_GUI"):
        pytest.skip("AUDIO_SPIDER_SKIP_GUI set")
    if not os.environ.get("DISPLAY") and not os.environ.get("WAYLAND_DISPLAY"):
        pytest.skip("no display available")
    import gi
    gi.require_version("Gtk", "3.0")
    gi.require_version("GooCanvas", "2.0")
    from gi.repository import GooCanvas, Gtk  # noqa: F401
    if not Gtk.init_check():
        pytest.skip("Gtk.init_check() failed")
    return Gtk


def _populated_model():
    from audio_spider.graph_model import (
        Edge, GraphModel, Node, NodeKind, Port, PortKind,
    )
    model = GraphModel()
    src = Node(
        id="mic_a", kind=NodeKind.HW_SOURCE, label="Mic A",
        ports=[Port(id="out", kind=PortKind.SOURCE_OUT, label="out")],
        x=50.0, y=50.0,
    )
    sink = Node(
        id="vmic1", kind=NodeKind.NULL_SINK, label="VMic 1",
        ports=[
            Port(id="in", kind=PortKind.SINK_IN, label="in"),
            Port(id="monitor", kind=PortKind.MONITOR_OUT, label="monitor"),
        ],
        x=400.0, y=50.0,
    )
    model.add_node(src)
    model.add_node(sink)
    model.add_edge(Edge(
        id="e1", src_node="mic_a", src_port="out",
        dst_node="vmic1", dst_port="in", kind="loopback",
    ))
    return model


def test_initial_population(_gtk):
    from audio_spider.graph_view import GraphView
    model = _populated_model()
    view = GraphView(model)
    assert view.node_item("mic_a") is not None
    assert view.node_item("vmic1") is not None
    assert view.edge_item("e1") is not None


def test_signals_mirror_model_changes(_gtk):
    from audio_spider.graph_model import (
        Edge, Node, NodeKind, Port, PortKind,
    )
    from audio_spider.graph_view import GraphView
    model = _populated_model()
    view = GraphView(model)

    # add another node + edge after view exists
    new_sink = Node(
        id="vmic2", kind=NodeKind.NULL_SINK, label="VMic 2",
        ports=[
            Port(id="in", kind=PortKind.SINK_IN, label="in"),
            Port(id="monitor", kind=PortKind.MONITOR_OUT, label="monitor"),
        ],
    )
    model.add_node(new_sink)
    assert view.node_item("vmic2") is not None

    model.add_edge(Edge(
        id="e2", src_node="mic_a", src_port="out",
        dst_node="vmic2", dst_port="in", kind="loopback",
    ))
    assert view.edge_item("e2") is not None

    # move node — edge path should refresh
    model.move_node("mic_a", 80.0, 200.0)
    item = view.node_item("mic_a")
    assert item is not None
    # NodeItem reads node.x/y on next port_position call — verify via path
    edge_item = view.edge_item("e1")
    assert edge_item is not None

    # remove edge then node
    model.remove_edge("e1")
    assert view.edge_item("e1") is None
    model.remove_node("vmic2")
    assert view.node_item("vmic2") is None
    # incident edge e2 should have been auto-removed by model
    assert view.edge_item("e2") is None


def test_clear_drops_all(_gtk):
    from audio_spider.graph_view import GraphView
    model = _populated_model()
    view = GraphView(model)
    model.clear()
    assert view.node_item("mic_a") is None
    assert view.node_item("vmic1") is None
    assert view.edge_item("e1") is None


def test_node_item_port_position_is_absolute(_gtk):
    from audio_spider.graph_view import GraphView
    from audio_spider.node_items import NODE_WIDTH
    model = _populated_model()
    view = GraphView(model)
    src_item = view.node_item("mic_a")
    sink_item = view.node_item("vmic1")
    assert src_item is not None and sink_item is not None
    sx, sy = src_item.port_position("out")
    dx, dy = sink_item.port_position("in")
    # source out-port is on the right edge of mic_a (x=50, width=200) → ≈ 250
    assert sx == pytest.approx(50.0 + NODE_WIDTH, abs=0.1)
    # sink in-port is on the left edge of vmic1 (x=400) → ≈ 400
    assert dx == pytest.approx(400.0, abs=0.1)


def test_bezier_between_starts_and_ends_at_endpoints():
    from audio_spider.node_items import bezier_between
    s = bezier_between(10.0, 20.0, 100.0, 200.0)
    assert s.startswith("M 10.0,20.0")
    assert s.endswith("100.0,200.0")
    assert "C " in s


# --- port context menu --------------------------------------------------


def _menu_item_labels(menu) -> list[str]:
    """Walk a Gtk.Menu, return text of each leaf MenuItem (including submenus)."""
    from gi.repository import Gtk
    labels: list[str] = []
    for child in menu.get_children():
        if isinstance(child, Gtk.SeparatorMenuItem):
            labels.append("---")
            continue
        text = child.get_label()
        if text:
            labels.append(text)
        sub = child.get_submenu()
        if sub is not None:
            for sub_child in sub.get_children():
                sub_text = sub_child.get_label()
                if sub_text:
                    labels.append(f"  > {sub_text}")
    return labels


def _populated_view_with_controller(_gtk):
    """Build a small graph wired through a FakePA + Controller + GraphView."""
    from pathlib import Path
    from audio_spider.config import Config
    from audio_spider.controller import Controller
    from audio_spider.graph_model import GraphModel
    from audio_spider.graph_view import GraphView
    from audio_spider.tests.test_controller import FakePA

    pa = FakePA()
    pa.add_hw_source("mic_a", "Mic A")
    pa.add_hw_source("mic_b", "Mic B")
    pa.add_hw_sink("speakers", "Speakers")
    cfg = Config()
    model = GraphModel()
    ctrl = Controller(pa, cfg, model, config_path=Path("/tmp/test_port_menu.json"))
    ctrl.initial_sync()
    ctrl.request_create_null_sink("vmic1", "Virtual Mic 1")
    view = GraphView(model, controller=ctrl)
    return view, model, ctrl


def test_port_menu_on_source_out_offers_connect_targets(_gtk):
    from gi.repository import Gtk
    from audio_spider.drag_state import PortHit
    view, model, ctrl = _populated_view_with_controller(_gtk)
    menu = Gtk.Menu()
    appended = view._build_port_menu(menu, PortHit("mic_a", "out"))
    assert appended
    labels = _menu_item_labels(menu)
    # the SINK_IN port of vmic1 must show up as a connect target
    assert any("Virtual Mic 1" in l and "in" in l for l in labels), labels


def test_port_menu_on_source_out_shows_disconnect_for_existing(_gtk):
    from gi.repository import Gtk
    from audio_spider.drag_state import PortHit
    view, model, ctrl = _populated_view_with_controller(_gtk)
    ctrl.request_connect("mic_a", "out", "vmic1", "in")
    menu = Gtk.Menu()
    view._build_port_menu(menu, PortHit("mic_a", "out"))
    labels = _menu_item_labels(menu)
    assert any(l.startswith("Disconnect from") for l in labels), labels
    # connect submenu still present but does NOT list vmic1.in anymore
    sub_labels = [l for l in labels if l.startswith("  > ")]
    assert not any("Virtual Mic 1" in l for l in sub_labels), sub_labels


def test_port_menu_on_sink_in_offers_disconnect_only(_gtk):
    from gi.repository import Gtk
    from audio_spider.drag_state import PortHit
    view, model, ctrl = _populated_view_with_controller(_gtk)
    ctrl.request_connect("mic_a", "out", "vmic1", "in")
    ctrl.request_connect("mic_b", "out", "vmic1", "in")
    menu = Gtk.Menu()
    view._build_port_menu(menu, PortHit("vmic1", "in"))
    labels = _menu_item_labels(menu)
    disconnects = [l for l in labels if l.startswith("Disconnect from")]
    assert len(disconnects) == 2, labels
    # no Connect submenu on a sink-in port (the natural flow is drag-from-output)
    assert not any(l == "Connect to" for l in labels), labels


def test_port_menu_on_empty_combine_members_offers_add(_gtk):
    """A freshly-created speaker group with no members shows only the
    'Add member' submenu listing the hardware sinks."""
    from gi.repository import Gtk
    from audio_spider.drag_state import PortHit
    view, model, ctrl = _populated_view_with_controller(_gtk)
    ctrl.request_create_combine_sink("vout", description="Speaker Group")
    menu = Gtk.Menu()
    view._build_port_menu(menu, PortHit("vout", "members"))
    labels = _menu_item_labels(menu)
    assert any(l == "Add member" for l in labels), labels
    sub_labels = [l for l in labels if l.startswith("  > ")]
    # the hw sink in the fixture is "Speakers"
    assert any("Speakers" in l for l in sub_labels), sub_labels


def test_port_menu_on_combine_members_shows_remove_for_existing(_gtk):
    from gi.repository import Gtk
    from audio_spider.drag_state import PortHit
    view, model, ctrl = _populated_view_with_controller(_gtk)
    ctrl.request_create_combine_sink("vout", ["speakers"], description="Group")
    menu = Gtk.Menu()
    view._build_port_menu(menu, PortHit("vout", "members"))
    labels = _menu_item_labels(menu)
    assert any(l.startswith("Remove member: ") for l in labels), labels


def test_port_menu_add_member_invokes_controller(_gtk):
    from gi.repository import Gtk
    from audio_spider.drag_state import PortHit
    view, model, ctrl = _populated_view_with_controller(_gtk)
    ctrl.request_create_combine_sink("vout", description="Group")
    menu = Gtk.Menu()
    view._build_port_menu(menu, PortHit("vout", "members"))
    add_root = next(c for c in menu.get_children() if c.get_label() == "Add member")
    submenu = add_root.get_submenu()
    target = next(
        c for c in submenu.get_children()
        if c.get_label() and "Speakers" in c.get_label()
    )
    target.emit("activate")
    members = {
        e.dst_node for e in model.edges() if e.kind == "combine-member"
    }
    assert "speakers" in members


def test_port_menu_remove_member_invokes_controller(_gtk):
    from gi.repository import Gtk
    from audio_spider.drag_state import PortHit
    view, model, ctrl = _populated_view_with_controller(_gtk)
    ctrl.request_create_combine_sink("vout", ["speakers"], description="Group")
    menu = Gtk.Menu()
    view._build_port_menu(menu, PortHit("vout", "members"))
    remove_item = next(
        c for c in menu.get_children()
        if c.get_label() and c.get_label().startswith("Remove member: ")
    )
    remove_item.emit("activate")
    assert not any(
        e.kind == "combine-member" and e.dst_node == "speakers"
        for e in model.edges()
    )


def test_port_menu_disconnect_invokes_controller(_gtk):
    from gi.repository import Gtk
    from audio_spider.drag_state import PortHit
    view, model, ctrl = _populated_view_with_controller(_gtk)
    ctrl.request_connect("mic_a", "out", "vmic1", "in")
    edge_before = list(model.edges())[0]

    menu = Gtk.Menu()
    view._build_port_menu(menu, PortHit("mic_a", "out"))
    disconnect_item = next(
        c for c in menu.get_children()
        if c.get_label() and c.get_label().startswith("Disconnect from")
    )
    disconnect_item.emit("activate")

    assert model.find_edge(edge_before.id) is None


def test_port_menu_connect_invokes_controller(_gtk):
    from gi.repository import Gtk
    from audio_spider.drag_state import PortHit
    view, model, ctrl = _populated_view_with_controller(_gtk)
    menu = Gtk.Menu()
    view._build_port_menu(menu, PortHit("mic_a", "out"))
    connect_root = next(
        c for c in menu.get_children() if c.get_label() == "Connect to"
    )
    submenu = connect_root.get_submenu()
    target_item = next(
        c for c in submenu.get_children()
        if c.get_label() and "Virtual Mic 1" in c.get_label()
    )
    target_item.emit("activate")
    edges = [e for e in model.edges() if e.kind == "loopback"]
    assert any(
        e.src_node == "mic_a" and e.dst_node == "vmic1" for e in edges
    ), edges
