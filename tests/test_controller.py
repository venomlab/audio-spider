from __future__ import annotations

from pathlib import Path

import pytest

from audio_spider import config as config_module
from audio_spider.config import Config, ConfigModule
from audio_spider.controller import (
    PORT_COMBINE_MEMBERS,
    PORT_MONITOR_OUT,
    PORT_SINK_IN,
    PORT_SOURCE_OUT,
    Controller,
)
from audio_spider.errors import PABackendError, ValidationError
from audio_spider.graph_model import GraphModel, NodeKind
from audio_spider.pa_backend import PAModule, PASink, PASource


class FakePA:
    """Stand-in for PABackend used by Controller.

    Mirrors the subset of the real interface the Controller calls. State is
    a flat module list plus implicit-derived sinks/sources (null-sinks add
    a sink + monitor source; combine-sinks add a sink only; loopbacks add
    nothing topological).
    """

    def __init__(self) -> None:
        self.sources: list[PASource] = []
        self.sinks: list[PASink] = []
        self.modules: list[PAModule] = []
        self._next_idx = 100
        self._subscriber = None

    # introspection
    def list_sources(self):
        return list(self.sources)

    def list_sinks(self):
        return list(self.sinks)

    def list_modules(self):
        return list(self.modules)

    # mutation
    def load_null_sink(self, name: str, description: str | None = None) -> int:
        idx = self._allocate_idx()
        self.modules.append(PAModule(
            index=idx, name="module-null-sink",
            argument=f"sink_name={name}",
        ))
        self.sinks.append(PASink(
            name=name, index=idx + 1000,
            description=description or name, owner_module=idx,
        ))
        self.sources.append(PASource(
            name=f"{name}.monitor", index=idx + 2000,
            description=f"Monitor of {name}",
            is_monitor=True, monitor_of_sink=name,
        ))
        return idx

    def load_combine_sink(self, name, members, description=None) -> int:
        idx = self._allocate_idx()
        arg = f"sink_name={name}"
        if members:
            arg += f" slaves={','.join(members)}"
        self.modules.append(PAModule(
            index=idx, name="module-combine-sink", argument=arg,
        ))
        self.sinks.append(PASink(
            name=name, index=idx + 1000,
            description=description or name, owner_module=idx,
        ))
        return idx

    def load_loopback(self, source, sink, latency_msec=1) -> int:
        idx = self._allocate_idx()
        self.modules.append(PAModule(
            index=idx, name="module-loopback",
            argument=f"source={source} sink={sink} latency_msec={latency_msec}",
        ))
        return idx

    def unload(self, module_index: int) -> None:
        target = next((m for m in self.modules if m.index == module_index), None)
        if target is None:
            raise PABackendError(f"unload module {module_index}: not found")
        self.modules.remove(target)
        # sinks owned by this module disappear
        self.sinks = [s for s in self.sinks if s.owner_module != module_index]
        # monitor sources of removed sinks disappear
        sink_names = {s.name for s in self.sinks}
        self.sources = [
            s for s in self.sources
            if not s.is_monitor or s.monitor_of_sink in sink_names
        ]

    def subscribe(self, callback) -> None:
        self._subscriber = callback

    def close(self) -> None:
        pass

    # helpers
    def _allocate_idx(self) -> int:
        idx = self._next_idx
        self._next_idx += 1
        return idx

    def add_hw_source(self, name: str, description: str = "") -> None:
        self.sources.append(PASource(
            name=name, index=self._allocate_idx(),
            description=description or name,
            is_monitor=False, monitor_of_sink=None,
        ))

    def add_hw_sink(self, name: str, description: str = "") -> None:
        self.sinks.append(PASink(
            name=name, index=self._allocate_idx(),
            description=description or name, owner_module=None,
        ))


@pytest.fixture
def fake_pa() -> FakePA:
    pa = FakePA()
    pa.add_hw_source("mic_a", "Microphone A")
    pa.add_hw_source("mic_b", "Microphone B")
    pa.add_hw_sink("speakers", "Speakers")
    pa.add_hw_sink("headphones", "Headphones")
    return pa


@pytest.fixture
def ctrl(tmp_path: Path, fake_pa: FakePA):
    cfg = Config()
    cfg_path = tmp_path / "config.json"
    model = GraphModel()
    c = Controller(fake_pa, cfg, model, config_path=cfg_path)
    return c, fake_pa, model, cfg, cfg_path


def test_initial_sync_builds_hw_nodes(ctrl):
    c, pa, model, cfg, _ = ctrl
    c.initial_sync()
    nodes = {n.id: n for n in model.nodes()}
    assert "mic_a" in nodes
    assert "mic_b" in nodes
    assert "speakers" in nodes
    assert "headphones" in nodes
    assert nodes["mic_a"].kind == NodeKind.HW_SOURCE
    assert nodes["speakers"].kind == NodeKind.HW_SINK
    assert ".monitor" not in " ".join(nodes)


def test_initial_sync_reconciles_config(tmp_path: Path, fake_pa: FakePA):
    cfg = Config(modules=[
        ConfigModule(id="vmic1", kind="null-sink",
                     params={"name": "vmic1", "description": "Virtual Mic"}),
    ])
    model = GraphModel()
    c = Controller(fake_pa, cfg, model, config_path=tmp_path / "c.json")
    report = c.initial_sync()
    assert "vmic1" in report.created
    nodes = {n.id: n for n in model.nodes()}
    assert "vmic1" in nodes
    assert nodes["vmic1"].kind == NodeKind.NULL_SINK
    # ports
    port_kinds = {p.id for p in nodes["vmic1"].ports}
    assert PORT_SINK_IN in port_kinds
    assert PORT_MONITOR_OUT in port_kinds


def test_request_create_null_sink_emits_node_and_saves_config(ctrl):
    c, pa, model, cfg, cfg_path = ctrl
    c.initial_sync()
    c.request_create_null_sink("vmic1", "Combined Mic")

    nodes = {n.id: n for n in model.nodes()}
    assert "vmic1" in nodes
    assert nodes["vmic1"].kind == NodeKind.NULL_SINK

    saved = config_module.load(cfg_path)
    assert any(m.id == "vmic1" and m.kind == "null-sink" for m in saved.modules)


def test_request_connect_creates_loopback_and_edge(ctrl):
    c, pa, model, cfg, cfg_path = ctrl
    c.initial_sync()
    c.request_create_null_sink("vmic1")
    c.request_connect("mic_a", PORT_SOURCE_OUT, "vmic1", PORT_SINK_IN)
    edges = list(model.edges())
    assert len(edges) == 1
    assert edges[0].src_node == "mic_a"
    assert edges[0].dst_node == "vmic1"
    assert edges[0].kind == "loopback"
    # cfg saved with loopback entry
    saved = config_module.load(cfg_path)
    assert any(m.kind == "loopback" for m in saved.modules)


def test_request_connect_via_monitor_resolves_pa_source(ctrl):
    c, pa, model, cfg, _ = ctrl
    c.initial_sync()
    c.request_create_null_sink("vmic1")
    c.request_create_null_sink("vmic2")
    c.request_connect("vmic1", PORT_MONITOR_OUT, "vmic2", PORT_SINK_IN)
    # the underlying PA loopback should reference "vmic1.monitor", not "vmic1"
    lb = next(m for m in pa.list_modules() if m.name == "module-loopback")
    assert "source=vmic1.monitor" in lb.argument


def test_request_connect_rejects_incompatible_ports(ctrl):
    c, pa, model, cfg, _ = ctrl
    c.initial_sync()
    c.request_create_null_sink("vmic1")
    # SINK_IN → SOURCE_OUT is wrong direction
    with pytest.raises(ValidationError):
        c.request_connect("vmic1", PORT_SINK_IN, "mic_a", PORT_SOURCE_OUT)


def test_request_delete_edge_unloads_loopback(ctrl):
    c, pa, model, cfg, cfg_path = ctrl
    c.initial_sync()
    c.request_create_null_sink("vmic1")
    c.request_connect("mic_a", PORT_SOURCE_OUT, "vmic1", PORT_SINK_IN)
    edge = list(model.edges())[0]
    c.request_delete_edge(edge.id)
    assert model.edges() == []
    assert not any(m.name == "module-loopback" for m in pa.list_modules())
    saved = config_module.load(cfg_path)
    assert not any(m.kind == "loopback" for m in saved.modules)


def test_request_delete_node_refuses_hardware(ctrl):
    c, pa, model, cfg, _ = ctrl
    c.initial_sync()
    errors: list[str] = []
    c.connect("error", lambda _src, msg: errors.append(msg))
    c.request_delete_node("mic_a")
    assert any("hardware" in e for e in errors)
    assert model.find_node("mic_a") is not None


def test_request_delete_node_unloads_virtual_sink(ctrl):
    c, pa, model, cfg, cfg_path = ctrl
    c.initial_sync()
    c.request_create_null_sink("vmic1")
    c.request_delete_node("vmic1")
    assert model.find_node("vmic1") is None
    assert not any(m.name == "module-null-sink" for m in pa.list_modules())
    saved = config_module.load(cfg_path)
    assert not any(m.kind == "null-sink" for m in saved.modules)


def test_delete_null_sink_cascades_incoming_loopbacks(ctrl):
    """Deleting a null-sink must also unload every mic→sink loopback,
    not leave them as orphans."""
    c, pa, model, cfg, cfg_path = ctrl
    c.initial_sync()
    c.request_create_null_sink("vsink1")
    c.request_connect("mic_a", "out", "vsink1", "in")
    c.request_connect("mic_b", "out", "vsink1", "in")
    # sanity
    assert any(m.name == "module-loopback" for m in pa.list_modules())

    c.request_delete_node("vsink1")

    assert model.find_node("vsink1") is None
    assert not any(m.name == "module-loopback" for m in pa.list_modules())
    assert not any(m.name == "module-null-sink" for m in pa.list_modules())
    # placeholder MUST NOT appear — no orphan loopbacks survive
    assert not any(
        n.kind.value == "missing" for n in model.nodes()
    )


def test_delete_null_sink_cascades_outgoing_monitor_loopbacks(ctrl):
    """A loopback from sink.monitor → another sink must also be unloaded when
    the source sink is deleted."""
    c, pa, model, cfg, _ = ctrl
    c.initial_sync()
    c.request_create_null_sink("vsink1")
    c.request_create_null_sink("vsink2")
    # monitor of vsink1 flows into vsink2 (chain)
    c.request_connect("vsink1", "monitor", "vsink2", "in")
    assert any(m.name == "module-loopback" for m in pa.list_modules())

    c.request_delete_node("vsink1")

    assert not any(m.name == "module-loopback" for m in pa.list_modules())
    # vsink2 still around (it was the destination of the deleted loopback)
    assert model.find_node("vsink2") is not None


def test_delete_speaker_group_does_not_leave_orphan(ctrl):
    """Combine-sink + a loopback into it → deleting the group cleans up both."""
    c, pa, model, cfg, _ = ctrl
    c.initial_sync()
    c.request_create_combine_sink("vout", ["speakers"], description="Group")
    c.request_connect("mic_a", "out", "vout", "in")

    c.request_delete_node("vout")

    assert model.find_node("vout") is None
    assert not any(m.name == "module-loopback" for m in pa.list_modules())
    assert not any(m.name == "module-combine-sink" for m in pa.list_modules())
    assert not any(n.kind.value == "missing" for n in model.nodes())


def test_request_move_node_persists_layout(ctrl):
    c, pa, model, cfg, cfg_path = ctrl
    c.initial_sync()
    c.request_move_node("mic_a", 123.0, 456.0)
    node = model.find_node("mic_a")
    assert node.x == 123.0 and node.y == 456.0
    saved = config_module.load(cfg_path)
    assert saved.layout["mic_a"] == {"x": 123.0, "y": 456.0}


def test_saved_layout_is_restored_on_next_sync(ctrl, tmp_path: Path):
    c, pa, model, cfg, cfg_path = ctrl
    c.initial_sync()
    c.request_move_node("mic_a", 999.0, 111.0)
    # second controller reading the persisted config
    cfg2 = config_module.load(cfg_path)
    model2 = GraphModel()
    c2 = Controller(pa, cfg2, model2, config_path=cfg_path)
    c2.initial_sync()
    node = model2.find_node("mic_a")
    assert (node.x, node.y) == (999.0, 111.0)


def test_combine_member_edge_is_rendered(ctrl):
    c, pa, model, cfg, _ = ctrl
    c.initial_sync()
    c.request_create_combine_sink("vout", ["speakers", "headphones"])
    edges = list(model.edges())
    members = {(e.src_node, e.dst_node) for e in edges if e.kind == "combine-member"}
    assert members == {("vout", "speakers"), ("vout", "headphones")}


def test_create_combine_sink_empty(ctrl):
    """Speaker group can be created with no members and added to later."""
    c, pa, model, cfg, cfg_path = ctrl
    c.initial_sync()
    c.request_create_combine_sink("vout", description="Group")
    node = model.find_node("vout")
    assert node is not None
    assert not any(e.kind == "combine-member" and e.src_node == "vout"
                   for e in model.edges())
    saved = config_module.load(cfg_path)
    combine_cfg = next(m for m in saved.modules if m.id == "vout")
    assert combine_cfg.params.get("members", []) == []


def test_add_combine_member_extends_slaves(ctrl):
    c, pa, model, cfg, cfg_path = ctrl
    c.initial_sync()
    c.request_create_combine_sink("vout")
    c.request_add_combine_member("vout", "speakers")
    edges = [e for e in model.edges() if e.kind == "combine-member"]
    assert len(edges) == 1
    assert edges[0].src_node == "vout"
    assert edges[0].dst_node == "speakers"
    saved = config_module.load(cfg_path)
    combine_cfg = next(m for m in saved.modules if m.id == "vout")
    assert combine_cfg.params["members"] == ["speakers"]


def test_add_combine_member_is_idempotent(ctrl):
    c, pa, model, cfg, _ = ctrl
    c.initial_sync()
    c.request_create_combine_sink("vout", ["speakers"])
    pre_modules = list(pa.list_modules())
    c.request_add_combine_member("vout", "speakers")
    # combine-member listing unchanged
    members = [e.dst_node for e in model.edges() if e.kind == "combine-member"]
    assert members == ["speakers"]
    # no extra modules created (other than the rebuild dance)
    post_modules = list(pa.list_modules())
    assert len(post_modules) == len(pre_modules)


def test_add_combine_member_rejects_self(ctrl):
    from audio_spider.errors import ValidationError
    c, pa, model, cfg, _ = ctrl
    c.initial_sync()
    c.request_create_combine_sink("vout")
    with pytest.raises(ValidationError):
        c.request_add_combine_member("vout", "vout")


def test_remove_combine_member_keeps_empty_combine(ctrl):
    c, pa, model, cfg, cfg_path = ctrl
    c.initial_sync()
    c.request_create_combine_sink("vout", ["speakers"])
    c.request_remove_combine_member("vout", "speakers")
    # combine-sink still exists, just empty
    assert model.find_node("vout") is not None
    assert not any(e.kind == "combine-member" for e in model.edges())
    saved = config_module.load(cfg_path)
    combine_cfg = next(m for m in saved.modules if m.id == "vout")
    assert combine_cfg.params["members"] == []


def test_remove_combine_member_noop_for_unknown(ctrl):
    c, pa, model, cfg, _ = ctrl
    c.initial_sync()
    c.request_create_combine_sink("vout", ["speakers"])
    c.request_remove_combine_member("vout", "headphones")
    members = [e.dst_node for e in model.edges() if e.kind == "combine-member"]
    assert members == ["speakers"]


def test_request_connect_from_combine_members_adds_member(ctrl):
    """Dragging from a combine's `members` port → speaker.in must add member."""
    c, pa, model, cfg, _ = ctrl
    c.initial_sync()
    c.request_create_combine_sink("vout")
    c.request_connect("vout", "members", "speakers", "in")
    members = {e.dst_node for e in model.edges() if e.kind == "combine-member"}
    assert "speakers" in members


def test_combine_member_edge_delete_removes_only_that_member(ctrl):
    """Right-click → delete on a combine-member edge unloads & reloads the
    combine-sink without that single member."""
    c, pa, model, cfg, _ = ctrl
    c.initial_sync()
    c.request_create_combine_sink(
        "vout", ["speakers", "headphones"], description="Group",
    )
    edge = next(
        e for e in model.edges()
        if e.kind == "combine-member" and e.dst_node == "headphones"
    )
    c.request_delete_edge(edge.id)
    remaining_members = {
        e.dst_node for e in model.edges() if e.kind == "combine-member"
    }
    assert remaining_members == {"speakers"}


def test_reload_config_picks_up_external_edits(ctrl):
    c, pa, model, cfg, cfg_path = ctrl
    c.initial_sync()
    # write a config with a null-sink directly to disk, as if user edited it
    import json
    cfg_path.write_text(json.dumps({
        "version": 1,
        "modules": [
            {"id": "vmic_external", "kind": "null-sink",
             "params": {"name": "vmic_external"}},
        ],
        "layout": {},
        "window": {"w": 1200, "h": 800, "start_minimized": False},
    }))
    report = c.reload_config()
    assert "vmic_external" in report.created
    assert model.find_node("vmic_external") is not None


def test_reload_config_leaves_existing_modules_alone(ctrl):
    """Modules in PA but absent from the new config must remain loaded."""
    c, pa, model, cfg, cfg_path = ctrl
    c.initial_sync()
    c.request_create_null_sink("vmic_pre")
    # now wipe the config (still has vmic_pre on disk from previous request;
    # overwrite it with an empty one)
    import json
    cfg_path.write_text(json.dumps({
        "version": 1, "modules": [], "layout": {},
        "window": {"w": 1200, "h": 800, "start_minimized": False},
    }))
    c.reload_config()
    # vmic_pre still loaded in PA and visible as a node
    assert any(s.name == "vmic_pre" for s in pa.list_sinks())
    assert model.find_node("vmic_pre") is not None


def test_null_sink_lands_in_null_sink_column(ctrl):
    """Newly-created null-sinks sit left of center."""
    from audio_spider.controller import (
        AUTO_LAYOUT_COL_HW_SOURCE,
        AUTO_LAYOUT_COL_NULL_SINK,
        AUTO_LAYOUT_COL_COMBINE_SINK,
        AUTO_LAYOUT_COL_HW_SINK,
    )
    c, pa, model, cfg, _ = ctrl
    c.initial_sync()
    c.request_create_null_sink("vsink1", "My Null Sink")
    node = model.find_node("vsink1")
    # ordering: hw_source < null_sink < combine_sink < hw_sink
    assert AUTO_LAYOUT_COL_HW_SOURCE < AUTO_LAYOUT_COL_NULL_SINK
    assert AUTO_LAYOUT_COL_NULL_SINK < AUTO_LAYOUT_COL_COMBINE_SINK
    assert AUTO_LAYOUT_COL_COMBINE_SINK < AUTO_LAYOUT_COL_HW_SINK
    assert node.x == AUTO_LAYOUT_COL_NULL_SINK


def test_combine_sink_lands_in_speaker_group_column(ctrl):
    from audio_spider.controller import AUTO_LAYOUT_COL_COMBINE_SINK
    c, pa, model, cfg, _ = ctrl
    c.initial_sync()
    c.request_create_combine_sink("vout1", ["speakers"], description="Group A")
    node = model.find_node("vout1")
    assert node.x == AUTO_LAYOUT_COL_COMBINE_SINK


def test_hw_sink_exposes_monitor_port(ctrl):
    """Every sink (incl. hardware) must show a monitor-out port so that
    loopbacks originating from sink.monitor are renderable."""
    from audio_spider.graph_model import PortKind
    c, pa, model, cfg, _ = ctrl
    c.initial_sync()
    speakers = model.find_node("speakers")
    port_kinds = {p.kind for p in speakers.ports}
    assert PortKind.SINK_IN in port_kinds
    assert PortKind.MONITOR_OUT in port_kinds


def test_existing_loopback_from_hw_sink_monitor_renders(ctrl, fake_pa):
    """A loopback created by pactl (outside our app) from speakers.monitor
    must show up as an edge once we sync."""
    from audio_spider.pa_backend import PAModule
    fake_pa.modules.append(PAModule(
        index=4242, name="module-loopback",
        argument="source=speakers.monitor sink=headphones",
    ))
    c, pa, model, cfg, _ = ctrl
    c.initial_sync()
    matches = [
        e for e in model.edges()
        if e.kind == "loopback"
        and e.src_node == "speakers" and e.src_port == "monitor"
        and e.dst_node == "headphones" and e.dst_port == "in"
    ]
    assert len(matches) == 1, list(model.edges())


def test_existing_loopback_can_be_deleted_via_controller(ctrl, fake_pa):
    """Pre-existing loopbacks become edges with a backing module index — so
    request_delete_edge can unload them, same as edges we created."""
    from audio_spider.pa_backend import PAModule
    fake_pa.modules.append(PAModule(
        index=4242, name="module-loopback",
        argument="source=mic_a sink=speakers",
    ))
    c, pa, model, cfg, _ = ctrl
    c.initial_sync()
    edge = next(
        e for e in model.edges()
        if e.kind == "loopback" and e.src_node == "mic_a" and e.dst_node == "speakers"
    )
    assert edge.pa_module_index == 4242
    c.request_delete_edge(edge.id)
    # underlying PA module gone
    assert not any(m.index == 4242 for m in pa.list_modules())
    # edge gone from model
    assert not any(e.id == edge.id for e in model.edges())


def test_request_create_null_sink_persists_description(ctrl):
    """Description (friendly name) must reach PA, not be dropped."""
    c, pa, model, cfg, _ = ctrl
    c.initial_sync()
    c.request_create_null_sink("vmic1", description="My Virtual Mic")
    sink = next(s for s in pa.list_sinks() if s.name == "vmic1")
    assert sink.description == "My Virtual Mic"
    node = model.find_node("vmic1")
    assert node.label == "My Virtual Mic"


def test_orphan_loopback_creates_placeholder_node(ctrl, fake_pa):
    """A loopback whose sink no longer exists must still render — as a
    placeholder node so the user can see and remove the stray module."""
    from audio_spider.graph_model import NodeKind
    from audio_spider.pa_backend import PAModule
    fake_pa.modules.append(PAModule(
        index=4242, name="module-loopback",
        argument="source=mic_a sink=GhostSink latency_msec=1",
    ))
    c, pa, model, cfg, _ = ctrl
    c.initial_sync()
    ghost = model.find_node("GhostSink")
    assert ghost is not None
    assert ghost.kind == NodeKind.MISSING
    assert "missing" in ghost.label.lower()


def test_orphan_loopback_edge_renders(ctrl, fake_pa):
    """The edge from the real source to the missing sink must show up."""
    from audio_spider.pa_backend import PAModule
    fake_pa.modules.append(PAModule(
        index=4242, name="module-loopback",
        argument="source=mic_a sink=GhostSink",
    ))
    c, pa, model, cfg, _ = ctrl
    c.initial_sync()
    edges = [
        e for e in model.edges()
        if e.src_node == "mic_a" and e.dst_node == "GhostSink"
        and e.kind == "loopback"
    ]
    assert len(edges) == 1
    assert edges[0].pa_module_index == 4242


def test_orphan_loopback_can_be_deleted_via_edge(ctrl, fake_pa):
    """Right-click on the edge → delete should still work for orphan edges."""
    from audio_spider.pa_backend import PAModule
    fake_pa.modules.append(PAModule(
        index=4242, name="module-loopback",
        argument="source=mic_a sink=GhostSink",
    ))
    c, pa, model, cfg, _ = ctrl
    c.initial_sync()
    edge = next(e for e in model.edges() if e.dst_node == "GhostSink")
    c.request_delete_edge(edge.id)
    assert not any(m.index == 4242 for m in pa.list_modules())
    # placeholder disappears on the rebuild that delete triggered
    assert model.find_node("GhostSink") is None


def test_request_remove_orphan_unloads_all_referencing_loopbacks(ctrl, fake_pa):
    from audio_spider.pa_backend import PAModule
    fake_pa.modules.append(PAModule(
        index=1, name="module-loopback",
        argument="source=mic_a sink=GhostSink",
    ))
    fake_pa.modules.append(PAModule(
        index=2, name="module-loopback",
        argument="source=mic_b sink=GhostSink",
    ))
    c, pa, model, cfg, _ = ctrl
    c.initial_sync()
    assert model.find_node("GhostSink") is not None

    c.request_remove_orphan("GhostSink")

    assert not any(m.name == "module-loopback" for m in pa.list_modules())
    assert model.find_node("GhostSink") is None


def test_request_remove_orphan_ignores_non_missing_nodes(ctrl, fake_pa):
    """Calling remove_orphan on a real node must not unload anything."""
    from audio_spider.pa_backend import PAModule
    fake_pa.modules.append(PAModule(
        index=1, name="module-loopback",
        argument="source=mic_a sink=speakers",
    ))
    c, pa, model, cfg, _ = ctrl
    c.initial_sync()
    before = {m.index for m in pa.list_modules()}
    c.request_remove_orphan("speakers")
    after = {m.index for m in pa.list_modules()}
    assert before == after


def test_orphan_node_placement_in_virtual_column(ctrl, fake_pa):
    from audio_spider.controller import AUTO_LAYOUT_COL_NULL_SINK
    from audio_spider.pa_backend import PAModule
    fake_pa.modules.append(PAModule(
        index=4242, name="module-loopback",
        argument="source=mic_a sink=GhostSink",
    ))
    c, pa, model, cfg, _ = ctrl
    c.initial_sync()
    ghost = model.find_node("GhostSink")
    assert ghost.x == AUTO_LAYOUT_COL_NULL_SINK


def test_pa_change_events_do_not_rebuild_model(ctrl):
    """Volume tweaks and other 'change' events must not wipe the dragged node."""
    from audio_spider.pa_backend import PAEvent
    c, pa, model, cfg, _ = ctrl
    c.initial_sync()
    rebuild_count = 0
    original = c.rebuild_model

    def counting_rebuild():
        nonlocal rebuild_count
        rebuild_count += 1
        original()
    c.rebuild_model = counting_rebuild

    c._on_pa_event(PAEvent(facility="sink", type="change", index=1))
    c._on_pa_event(PAEvent(facility="source", type="change", index=2))
    assert rebuild_count == 0


def test_pa_new_event_triggers_rebuild(ctrl):
    from audio_spider.pa_backend import PAEvent
    c, pa, model, cfg, _ = ctrl
    c.initial_sync()
    rebuild_count = 0
    original = c.rebuild_model

    def counting_rebuild():
        nonlocal rebuild_count
        rebuild_count += 1
        original()
    c.rebuild_model = counting_rebuild

    c._on_pa_event(PAEvent(facility="module", type="new", index=99))
    assert rebuild_count == 1


def test_pause_rebuild_defers_pa_event(ctrl):
    """Events during a drag should be deferred, not snap-back the node."""
    from audio_spider.pa_backend import PAEvent
    c, pa, model, cfg, _ = ctrl
    c.initial_sync()
    rebuild_count = 0
    original = c.rebuild_model

    def counting_rebuild():
        nonlocal rebuild_count
        rebuild_count += 1
        original()
    c.rebuild_model = counting_rebuild

    c.pause_rebuild()
    c._on_pa_event(PAEvent(facility="module", type="new", index=99))
    c._on_pa_event(PAEvent(facility="module", type="remove", index=99))
    assert rebuild_count == 0  # no rebuilds while paused
    c.resume_rebuild()
    assert rebuild_count == 1  # one coalesced rebuild on resume


def test_pause_rebuild_skips_resume_when_no_events(ctrl):
    c, pa, model, cfg, _ = ctrl
    c.initial_sync()
    rebuild_count = 0
    original = c.rebuild_model

    def counting_rebuild():
        nonlocal rebuild_count
        rebuild_count += 1
        original()
    c.rebuild_model = counting_rebuild

    c.pause_rebuild()
    c.resume_rebuild()
    assert rebuild_count == 0


def test_error_signal_emitted_on_pa_failure(ctrl, monkeypatch):
    c, pa, model, cfg, _ = ctrl
    c.initial_sync()
    monkeypatch.setattr(
        pa, "load_null_sink",
        lambda *a, **kw: (_ for _ in ()).throw(PABackendError("nope")),
    )
    errors: list[str] = []
    c.connect("error", lambda _src, msg: errors.append(msg))
    with pytest.raises(PABackendError):
        c.request_create_null_sink("vmic_fail")
    assert any("nope" in e for e in errors)
