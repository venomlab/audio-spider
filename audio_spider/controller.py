from __future__ import annotations

import logging
import threading
from typing import TYPE_CHECKING

from gi.repository import GObject

from audio_spider import config as config_module
from audio_spider.config import Config, ConfigModule
from audio_spider.errors import PABackendError, ValidationError
from audio_spider.graph_model import (
    Edge,
    GraphModel,
    Node,
    NodeKind,
    Port,
    PortKind,
)
from audio_spider.reconcile import ReconcileReport, parse_module_args, reconcile

if TYPE_CHECKING:
    from pathlib import Path

    from audio_spider.pa_backend import PABackend, PAEvent, PAModule, PASink, PASource

log = logging.getLogger(__name__)

PORT_SOURCE_OUT = "out"
PORT_SINK_IN = "in"
PORT_MONITOR_OUT = "monitor"
PORT_COMBINE_MEMBERS = "members"

# Four-column auto-layout, left to right:
#   real mics (inputs)  →  null-sinks
#   combine-sinks (Speaker Groups)  →  real speakers (outputs)
# Audio flows left-to-right; virtual devices group with the corresponding
# real-device side so users find them where they expect.
AUTO_LAYOUT_COL_HW_SOURCE = 50.0
AUTO_LAYOUT_COL_NULL_SINK = 350.0
AUTO_LAYOUT_COL_COMBINE_SINK = 700.0
AUTO_LAYOUT_COL_HW_SINK = 1000.0
AUTO_LAYOUT_ROW_STEP = 130.0
AUTO_LAYOUT_ROW_START = 50.0


class Controller(GObject.Object):
    """Glues PABackend + Config + GraphModel together.

    All state mutations go through here: it's the single owner of the
    PA-model-config relationship. Both PA events and UI intents land on the
    GLib main thread (events via idle_add), so we don't need cross-thread
    locking for graph mutations, but we still use a re-entrant lock to guard
    config persistence.
    """

    __gsignals__ = {
        # emitted whenever a user-visible non-fatal error happens (string msg)
        "error": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
        # emitted after model rebuild from PA snapshot
        "sync-complete": (GObject.SignalFlags.RUN_FIRST, None, ()),
    }

    def __init__(
        self,
        pa: PABackend,
        cfg: Config,
        model: GraphModel,
        config_path: Path | None = None,
    ) -> None:
        super().__init__()
        self._pa = pa
        self._cfg = cfg
        self._model = model
        self._config_path = config_path
        self._save_lock = threading.RLock()
        # cfg-module-id <-> PA module index, for non-loopback configured modules
        self._cfg_module_index: dict[str, int] = {}
        # When the view is actively dragging a node, PA-event-driven rebuilds
        # would snap the dragged node back to its last persisted position
        # mid-gesture (since cfg.layout isn't updated until release). The view
        # holds this latch open during a drag.
        self._rebuild_paused = False
        self._missed_rebuild = False

    # ------------------------------------------------------------------
    # bootstrap

    def initial_sync(self) -> ReconcileReport:
        report = reconcile(self._cfg, self._pa)
        self._cfg_module_index = report.id_to_module_index()
        self._report_reconcile_errors(report)
        self.rebuild_model()
        self._pa.subscribe(self._on_pa_event)
        return report

    def reload_config(self) -> ReconcileReport:
        """Re-read the config file from disk and resync.

        Honors the non-destructive rule: modules in PA that have been removed
        from the new config remain loaded (the user can remove them manually
        via pactl or right-click delete). Only adds what's missing.
        """
        loaded = config_module.load(self._config_path)
        self._cfg.modules = loaded.modules
        self._cfg.layout = loaded.layout
        self._cfg.window = loaded.window
        report = reconcile(self._cfg, self._pa)
        self._cfg_module_index = report.id_to_module_index()
        self._report_reconcile_errors(report)
        self.rebuild_model()
        return report

    def _report_reconcile_errors(self, report: ReconcileReport) -> None:
        for cm_id, err in report.errors.items():
            log.error("reconcile error for %s: %s", cm_id, err)
            self._emit_error(f"reconcile {cm_id}: {err}")

    # ------------------------------------------------------------------
    # user intents

    def request_create_null_sink(
        self, name: str, description: str | None = None,
    ) -> int:
        try:
            idx = self._pa.load_null_sink(name, description)
        except PABackendError as e:
            self._emit_error(f"create null-sink: {e}")
            raise
        cm = ConfigModule(
            id=name,
            kind="null-sink",
            params={"name": name, **({"description": description} if description else {})},
        )
        self._upsert_config_module(cm)
        self._cfg_module_index[cm.id] = idx
        self._save_config()
        self.rebuild_model()
        return idx

    def request_create_combine_sink(
        self,
        name: str,
        members: list[str] | None = None,
        description: str | None = None,
    ) -> int:
        """Create a Speaker Group. `members` may be empty — add members later
        via `request_add_combine_member` (or by dragging from the group's
        members port).
        """
        members = list(members or [])
        try:
            idx = self._pa.load_combine_sink(name, members, description)
        except PABackendError as e:
            self._emit_error(f"create combine-sink: {e}")
            raise
        cm = ConfigModule(
            id=name,
            kind="combine-sink",
            params={
                "name": name,
                "members": members,
                **({"description": description} if description else {}),
            },
        )
        self._upsert_config_module(cm)
        self._cfg_module_index[cm.id] = idx
        self._save_config()
        self.rebuild_model()
        return idx

    def request_add_combine_member(
        self, combine_id: str, member_sink_id: str,
    ) -> None:
        """Add a real sink to a Speaker Group. Idempotent."""
        node = self._model.find_node(combine_id)
        if node is None or node.kind != NodeKind.COMBINE_SINK:
            msg = f"not a Speaker Group: {combine_id}"
            raise ValidationError(msg)
        if combine_id == member_sink_id:
            msg = "a Speaker Group cannot include itself"
            raise ValidationError(msg)
        current = self._read_combine_members(node)
        if member_sink_id in current:
            return
        self._reload_combine(node, [*current, member_sink_id])

    def request_remove_combine_member(
        self, combine_id: str, member_sink_id: str,
    ) -> None:
        """Drop a member from a Speaker Group. No-op if not a member."""
        node = self._model.find_node(combine_id)
        if node is None or node.kind != NodeKind.COMBINE_SINK:
            return
        current = self._read_combine_members(node)
        if member_sink_id not in current:
            return
        self._reload_combine(
            node, [m for m in current if m != member_sink_id],
        )

    def _read_combine_members(self, node: Node) -> list[str]:
        if node.pa_module_index is None:
            return []
        for module in self._pa.list_modules():
            if module.index != node.pa_module_index:
                continue
            slaves = parse_module_args(module.argument).get("slaves", "")
            return [s for s in slaves.split(",") if s] if slaves else []
        return []

    def _reload_combine(self, node: Node, new_members: list[str]) -> None:
        """Unload + reload the combine-sink so PA picks up new slaves.

        Loopbacks targeting this sink keep their module alive across the
        gap (PA pauses them until the named sink reappears), so we don't
        need to re-register them ourselves.
        """
        description = self._cfg_combine_description(node.id)
        old_idx = node.pa_module_index
        if old_idx is not None:
            try:
                self.pause_rebuild()
                self._pa.unload(old_idx)
            except PABackendError as e:
                self.resume_rebuild()
                self._emit_error(f"unload speaker group: {e}")
                raise
        try:
            new_idx = self._pa.load_combine_sink(
                node.id, new_members, description,
            )
        except PABackendError as e:
            self.resume_rebuild()
            self._emit_error(f"reload speaker group: {e}")
            raise
        self._cfg_module_index[node.id] = new_idx
        for cm in self._cfg.modules:
            if cm.id == node.id and cm.kind == "combine-sink":
                cm.params["members"] = list(new_members)
                break
        self._save_config()
        self.resume_rebuild(do_rebuild=True)

    def _cfg_combine_description(self, combine_id: str) -> str | None:
        for cm in self._cfg.modules:
            if cm.id == combine_id and cm.kind == "combine-sink":
                return cm.params.get("description")
        return None

    def request_connect(
        self,
        src_node_id: str,
        src_port_id: str,
        dst_node_id: str,
        dst_port_id: str,
    ) -> int | None:
        """Dispatch a port-to-port drop to the appropriate PA action.

        - SOURCE_OUT / MONITOR_OUT → SINK_IN: create a loopback.
        - COMBINE_MEMBERS → SINK_IN: add the destination sink as a member of
          the Speaker Group.
        """
        src_node = self._model.find_node(src_node_id)
        dst_node = self._model.find_node(dst_node_id)
        if src_node is None or dst_node is None:
            msg = "source or destination node not found"
            raise ValidationError(msg)
        src_port = next((p for p in src_node.ports if p.id == src_port_id), None)
        dst_port = next((p for p in dst_node.ports if p.id == dst_port_id), None)
        if src_port is None or dst_port is None:
            msg = "port not found"
            raise ValidationError(msg)
        if dst_port.kind != PortKind.SINK_IN:
            msg = "destination port must be SINK_IN"
            raise ValidationError(msg)

        if src_port.kind == PortKind.COMBINE_MEMBERS:
            self.request_add_combine_member(src_node.id, dst_node.id)
            return None

        if src_port.kind not in (PortKind.SOURCE_OUT, PortKind.MONITOR_OUT):
            msg = "source port must be SOURCE_OUT or MONITOR_OUT"
            raise ValidationError(msg)

        # Real PA source name: for hw → node id, for monitor → "<sink>.monitor".
        pa_source = (
            src_node.id if src_port.kind == PortKind.SOURCE_OUT
            else f"{src_node.id}.monitor"
        )
        pa_sink = dst_node.id

        try:
            idx = self._pa.load_loopback(pa_source, pa_sink)
        except PABackendError as e:
            self._emit_error(f"create loopback: {e}")
            raise

        cm_id = self._loopback_cfg_id(pa_source, pa_sink)
        self._upsert_config_module(ConfigModule(
            id=cm_id,
            kind="loopback",
            params={"source": pa_source, "sink": pa_sink},
        ))
        self._cfg_module_index[cm_id] = idx
        self._save_config()
        self.rebuild_model()
        return idx

    def request_delete_edge(self, edge_id: str) -> None:
        edge = self._model.find_edge(edge_id)
        if edge is None:
            return
        if edge.kind == "combine-member":
            # combine-member edges are not standalone modules; remove them
            # by reloading the combine-sink without that slave.
            self.request_remove_combine_member(edge.src_node, edge.dst_node)
            return
        if edge.pa_module_index is None:
            return
        try:
            self._pa.unload(edge.pa_module_index)
        except PABackendError as e:
            self._emit_error(f"unload edge: {e}")
            raise
        # drop matching loopback entry from cfg
        self._remove_config_module_by_module_index(edge.pa_module_index)
        self._save_config()
        self.rebuild_model()

    def request_delete_node(self, node_id: str) -> None:
        node = self._model.find_node(node_id)
        if node is None:
            return
        if node.kind in (NodeKind.HW_SOURCE, NodeKind.HW_SINK):
            self._emit_error("cannot delete a hardware device")
            return
        if node.kind == NodeKind.MISSING:
            # placeholder for orphan loopbacks — route through remove-orphan
            self.request_remove_orphan(node_id)
            return
        if node.pa_module_index is None:
            return

        # Collect every PA module that needs to be unloaded:
        #   1. The node's own backing module (null-sink / combine-sink).
        #   2. Every loopback module that has this node as either endpoint —
        #      otherwise it would survive as an orphan referencing a sink/
        #      source that no longer exists.
        # `dict.fromkeys`-style dict preserves insertion order while deduping.
        related: dict[int, None] = {node.pa_module_index: None}
        for edge in self._model.edges():
            if edge.pa_module_index is None:
                continue
            if edge.kind != "loopback":
                # combine-member edges share the combine-sink's module index,
                # which is already accounted for if we're deleting that node.
                continue
            if node_id in (edge.src_node, edge.dst_node):
                related[edge.pa_module_index] = None

        primary_error: PABackendError | None = None
        secondary_failures: list[str] = []
        for idx in related:
            try:
                self._pa.unload(idx)
            except PABackendError as e:
                if idx == node.pa_module_index:
                    primary_error = e
                else:
                    secondary_failures.append(f"module #{idx}: {e}")
                continue
            self._remove_config_module_by_module_index(idx)
        self._save_config()
        self.rebuild_model()
        if secondary_failures:
            self._emit_error(
                "some loopback unloads failed: " + "; ".join(secondary_failures),
            )
        if primary_error is not None:
            self._emit_error(f"unload node: {primary_error}")
            raise primary_error

    def request_remove_orphan(self, node_id: str) -> None:
        """Unload every loopback whose endpoint is this orphan placeholder.

        The placeholder disappears on the next rebuild once no module
        references it any more.
        """
        node = self._model.find_node(node_id)
        if node is None or node.kind != NodeKind.MISSING:
            return
        related = [
            e for e in self._model.edges()
            if e.kind == "loopback"
            and (node_id in (e.src_node, e.dst_node))
            and e.pa_module_index is not None
        ]
        failures: list[str] = []
        for edge in related:
            try:
                self._pa.unload(edge.pa_module_index)
            except PABackendError as e:
                failures.append(str(e))
                continue
            self._remove_config_module_by_module_index(edge.pa_module_index)
        self._save_config()
        self.rebuild_model()
        if failures:
            self._emit_error(
                "some unloads failed: " + "; ".join(failures),
            )

    def request_move_node(self, node_id: str, x: float, y: float) -> None:
        node = self._model.find_node(node_id)
        if node is None:
            return
        self._model.move_node(node_id, x, y)
        self._cfg.layout[node_id] = {"x": float(x), "y": float(y)}
        self._save_config()

    def request_reset_layout(self) -> None:
        """Drop every saved node position so the next rebuild re-applies the
        auto-layout (four columns left-to-right). Useful when a node has been
        dragged off-screen and the user can't find it.
        """
        self._cfg.layout.clear()
        self._save_config()
        self.rebuild_model()

    # ------------------------------------------------------------------
    # PA events

    def _on_pa_event(self, event: PAEvent) -> None:
        log.debug("pa event: %s", event)
        # Only topology changes need a graph rebuild. "change" events fire on
        # routine state mutations (volume tweaks, suspend/resume, port flips)
        # — they don't move boxes/arrows around, and rebuilding on every one
        # of them makes drag-to-position unusable.
        if "new" not in event.type and "remove" not in event.type:
            return
        if self._rebuild_paused:
            self._missed_rebuild = True
            return
        self.rebuild_model()

    def pause_rebuild(self) -> None:
        """Suspend PA-event-driven model rebuilds (used during user drag)."""
        self._rebuild_paused = True

    def resume_rebuild(self, do_rebuild: bool = False) -> None:
        """Resume rebuilds. If events fired while paused, rebuilds once.

        `do_rebuild=True` forces a rebuild regardless of whether events fired.
        """
        self._rebuild_paused = False
        if do_rebuild or self._missed_rebuild:
            self._missed_rebuild = False
            self.rebuild_model()

    # ------------------------------------------------------------------
    # model construction

    def rebuild_model(self) -> None:
        sources = self._pa.list_sources()
        sinks = self._pa.list_sinks()
        modules = self._pa.list_modules()

        sink_by_owner: dict[int, PASink] = {
            s.owner_module: s for s in sinks if s.owner_module is not None
        }
        null_sink_names = {
            sink_by_owner[m.index].name
            for m in modules
            if m.name == "module-null-sink" and m.index in sink_by_owner
        }
        combine_sink_names = {
            sink_by_owner[m.index].name
            for m in modules
            if m.name == "module-combine-sink" and m.index in sink_by_owner
        }
        owner_by_sink_name: dict[str, int] = {
            s.name: s.owner_module for s in sinks if s.owner_module is not None
        }

        # build a fresh layout snapshot so positions are stable across rebuild
        next_row: dict[str, float] = {
            "hw_source": AUTO_LAYOUT_ROW_START,
            "null_sink": AUTO_LAYOUT_ROW_START,
            "combine_sink": AUTO_LAYOUT_ROW_START,
            "hw_sink": AUTO_LAYOUT_ROW_START,
        }
        column_x = {
            "hw_source": AUTO_LAYOUT_COL_HW_SOURCE,
            "null_sink": AUTO_LAYOUT_COL_NULL_SINK,
            "combine_sink": AUTO_LAYOUT_COL_COMBINE_SINK,
            "hw_sink": AUTO_LAYOUT_COL_HW_SINK,
        }

        def assign_layout(node: Node, column: str) -> None:
            saved = self._cfg.layout.get(node.id)
            if saved is not None:
                node.x = float(saved.get("x", 0.0))
                node.y = float(saved.get("y", 0.0))
                return
            node.x = column_x[column]
            node.y = next_row[column]
            next_row[column] += AUTO_LAYOUT_ROW_STEP

        self._model.clear()

        for src in sources:
            if src.is_monitor:
                continue
            node = self._make_source_node(src)
            assign_layout(node, "hw_source")
            self._model.add_node(node)

        for snk in sinks:
            if snk.name in null_sink_names:
                kind = NodeKind.NULL_SINK
                column = "null_sink"
            elif snk.name in combine_sink_names:
                kind = NodeKind.COMBINE_SINK
                column = "combine_sink"
            else:
                kind = NodeKind.HW_SINK
                column = "hw_sink"
            node = self._make_sink_node(snk, kind)
            assign_layout(node, column)
            self._model.add_node(node)

        # Placeholders for endpoints referenced by loaded modules but no
        # longer present in PA (e.g. a sink was unloaded but the loopback
        # that targeted it survived and now plays into the default sink).
        # Rendering them lets the user see the stray module and delete it
        # via right-click → Disconnect.
        for module in modules:
            if module.name != "module-loopback":
                continue
            args = parse_module_args(module.argument)
            for raw_name in (args.get("source"), args.get("sink")):
                if not raw_name:
                    continue
                node_id, _ = self._resolve_source_endpoint(raw_name)
                if self._model.find_node(node_id) is not None:
                    continue
                placeholder = self._make_missing_node(node_id)
                assign_layout(placeholder, "null_sink")
                self._model.add_node(placeholder)

        for module in modules:
            args = parse_module_args(module.argument)
            for edge in self._edges_from_module(
                module, args, owner_by_sink_name,
            ):
                if (
                    self._model.find_node(edge.src_node) is not None
                    and self._model.find_node(edge.dst_node) is not None
                ):
                    self._model.add_edge(edge)

        self.emit("sync-complete")

    @staticmethod
    def _make_source_node(s: PASource) -> Node:
        return Node(
            id=s.name,
            kind=NodeKind.HW_SOURCE,
            label=s.description,
            ports=[Port(id=PORT_SOURCE_OUT, kind=PortKind.SOURCE_OUT, label="out")],
        )

    @staticmethod
    def _make_missing_node(name: str) -> Node:
        """Build a placeholder node for an endpoint that no longer exists in PA.

        The placeholder carries every port kind a loopback might want to dock
        into, so edges render regardless of whether the missing endpoint was
        originally a source or a sink.
        """
        ports = [
            Port(id=PORT_SINK_IN, kind=PortKind.SINK_IN, label="in"),
            Port(id=PORT_SOURCE_OUT, kind=PortKind.SOURCE_OUT, label="out"),
            Port(id=PORT_MONITOR_OUT, kind=PortKind.MONITOR_OUT, label="monitor"),
        ]
        return Node(
            id=name,
            kind=NodeKind.MISSING,
            label=f"{name} (missing)",
            ports=ports,
        )

    @staticmethod
    def _make_sink_node(s: PASink, kind: NodeKind) -> Node:
        # Every sink in PulseAudio has an implicit monitor source — exposing
        # it as a port means loopbacks originating from sink.monitor (whether
        # the user created them in our UI or with `pactl` outside it) render
        # as edges and can be right-clicked → Disconnect.
        ports = [
            Port(id=PORT_SINK_IN, kind=PortKind.SINK_IN, label="in"),
            Port(id=PORT_MONITOR_OUT, kind=PortKind.MONITOR_OUT, label="monitor"),
        ]
        if kind == NodeKind.COMBINE_SINK:
            ports.append(Port(
                id=PORT_COMBINE_MEMBERS,
                kind=PortKind.COMBINE_MEMBERS,
                label="members",
            ))
        return Node(
            id=s.name,
            kind=kind,
            label=s.description,
            ports=ports,
            pa_module_index=s.owner_module,
        )

    def _edges_from_module(
        self,
        module: PAModule,
        args: dict[str, str],
        owner_by_sink_name: dict[str, int],
    ) -> list[Edge]:
        if module.name == "module-loopback":
            pa_source = args.get("source")
            pa_sink = args.get("sink")
            if not pa_source or not pa_sink:
                return []
            src_node_id, src_port_id = self._resolve_source_endpoint(pa_source)
            dst_node_id, dst_port_id = pa_sink, PORT_SINK_IN
            return [Edge(
                id=f"lb-{module.index}",
                src_node=src_node_id,
                src_port=src_port_id,
                dst_node=dst_node_id,
                dst_port=dst_port_id,
                kind="loopback",
                pa_module_index=module.index,
            )]
        if module.name == "module-combine-sink":
            sink_name = args.get("sink_name")
            slaves = [s for s in args.get("slaves", "").split(",") if s]
            if not sink_name:
                return []
            return [
                Edge(
                    id=f"cm-{module.index}-{i}",
                    src_node=sink_name,
                    src_port=PORT_COMBINE_MEMBERS,
                    dst_node=slave,
                    dst_port=PORT_SINK_IN,
                    kind="combine-member",
                    pa_module_index=module.index,
                )
                for i, slave in enumerate(slaves)
            ]
        return []

    @staticmethod
    def _resolve_source_endpoint(pa_source: str) -> tuple[str, str]:
        """A source name like `foo.monitor` resolves to (foo, monitor) port."""
        if pa_source.endswith(".monitor"):
            return pa_source[: -len(".monitor")], PORT_MONITOR_OUT
        return pa_source, PORT_SOURCE_OUT

    # ------------------------------------------------------------------
    # config helpers

    @staticmethod
    def _loopback_cfg_id(pa_source: str, pa_sink: str) -> str:
        return f"loopback::{pa_source}::{pa_sink}"

    def _upsert_config_module(self, cm: ConfigModule) -> None:
        for i, existing in enumerate(self._cfg.modules):
            if existing.id == cm.id:
                self._cfg.modules[i] = cm
                return
        self._cfg.modules.append(cm)

    def _remove_config_module_by_module_index(self, module_index: int) -> None:
        # find which cfg id maps to this index
        cm_id = next(
            (cid for cid, idx in self._cfg_module_index.items() if idx == module_index),
            None,
        )
        if cm_id is None:
            return
        self._cfg_module_index.pop(cm_id, None)
        self._cfg.modules = [m for m in self._cfg.modules if m.id != cm_id]

    def _save_config(self) -> None:
        with self._save_lock:
            try:
                config_module.save(self._cfg, self._config_path)
            except OSError as e:
                self._emit_error(f"save config: {e}")

    # ------------------------------------------------------------------
    # error helpers

    def _emit_error(self, message: str) -> None:
        log.warning(message)
        self.emit("error", message)
