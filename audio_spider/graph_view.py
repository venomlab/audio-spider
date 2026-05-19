from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING, Any

import gi

from audio_spider.drag_state import (
    ConnectIntent,
    DragMachine,
    EdgeHit,
    Hit,
    MoveNodeIntent,
    NodeHit,
    PortHit,
    can_connect,
)
from audio_spider.graph_model import Edge, GraphModel, Node, NodeKind, Port, PortKind
from audio_spider.node_items import (
    EDGE_WIDTH,
    EdgeItem,
    NodeItem,
    bezier_between,
)

gi.require_version("Gtk", "3.0")
gi.require_version("GooCanvas", "2.0")

from gi.repository import Gdk, GooCanvas, Gtk  # noqa: E402

if TYPE_CHECKING:
    from collections.abc import Callable

    from audio_spider.controller import Controller

# Effectively unlimited workspace: canvas bounds are huge and symmetric so
# users can drag nodes — and pan the viewport — anywhere without bumping
# into an invisible wall. Scrollbars are hidden; navigation is exclusively
# middle-click pan + "Reset view" to recenter.
CANVAS_BOUNDS_HALF = 50000.0

# Zoom is anchored at the cursor: scrolling the wheel scales by ZOOM_STEP
# per tick, clamped to [ZOOM_MIN, ZOOM_MAX]. Tune these in one place when
# the desired range or speed changes.
ZOOM_MIN = 0.2
ZOOM_MAX = 2.0
ZOOM_STEP = 1.1


class GraphView(Gtk.EventBox):
    """Container holding a GooCanvas mirror of a GraphModel.

    Renders nodes/edges and routes pointer events to a DragMachine so users
    can drag nodes, drag-to-connect ports, and right-click → delete.

    A plain EventBox (not ScrolledWindow) is used because the workspace is
    conceptually infinite — there are no scrollbars; navigation is purely
    middle-button pan. GooCanvas has its own scroll_to API which we drive
    directly instead of going through Gtk.Adjustments.
    """

    def __init__(
        self,
        model: GraphModel,
        controller: Controller | None = None,
    ) -> None:
        super().__init__()
        self._model = model
        self._controller = controller
        self._drag = DragMachine(model)
        self._canvas = GooCanvas.Canvas()
        self._canvas.set_size_request(800, 600)
        self._canvas.set_bounds(
            -CANVAS_BOUNDS_HALF,
            -CANVAS_BOUNDS_HALF,
            CANVAS_BOUNDS_HALF,
            CANVAS_BOUNDS_HALF,
        )
        self._canvas.props.background_color_rgb = 0xFAFAFA
        # View transform lives on the root group rather than on the canvas
        # itself: `canvas.set_scale` + `canvas.scroll_to` interact with
        # bounds and adjustments in subtle ways that broke cursor-anchored
        # zoom for us. Driving everything through a simple 2D transform
        # (translate + scale) on the root item is unambiguous.
        self._view_tx: float = 0.0
        self._view_ty: float = 0.0
        self._view_scale: float = 1.0
        # Motion/release are wired to the canvas widget rather than the root
        # item so they fire regardless of which item is under the cursor.
        # Without this, a fast drag leaves the cursor outside the moving node
        # → item-level motion events stop → node visibly trails the pointer.
        self._canvas.add_events(
            Gdk.EventMask.BUTTON_PRESS_MASK
            | Gdk.EventMask.BUTTON_RELEASE_MASK
            | Gdk.EventMask.POINTER_MOTION_MASK
            | Gdk.EventMask.LEAVE_NOTIFY_MASK,
        )
        self.add(self._canvas)

        self._root = self._canvas.get_root_item()
        # Intermediate group that holds every node / edge / preview path.
        # We apply the view transform (pan + zoom) to this group rather than
        # to the root, since the root item in GooCanvas is special and may
        # not respect manual simple-transforms.
        self._view_group = GooCanvas.CanvasGroup(parent=self._root)
        self._node_items: dict[str, NodeItem] = {}
        self._edge_items: dict[str, EdgeItem] = {}
        self._pending_edge: GooCanvas.CanvasPath | None = None
        # During an edge drag, the port currently under the cursor that's a
        # valid drop target (i.e. release here would create a connection).
        self._hover_port: PortHit | None = None
        # When idle (no drag in flight), the edge under the cursor is thick-
        # ened so the user sees that right-click → Disconnect is available.
        self._hover_edge_id: str | None = None
        # Middle-button viewport pan: (start_widget_x, start_widget_y,
        # start_hadj_value, start_vadj_value). Set on middle press, cleared
        # on middle release.
        self._pan: tuple[float, float, float, float] | None = None

        for node in model.nodes():
            self._on_node_added(model, node.id)
        for edge in model.edges():
            self._on_edge_added(model, edge.id)

        model.connect("node-added", self._on_node_added)
        model.connect("node-removed", self._on_node_removed)
        model.connect("node-moved", self._on_node_moved)
        model.connect("node-changed", self._on_node_changed)
        model.connect("edge-added", self._on_edge_added)
        model.connect("edge-removed", self._on_edge_removed)
        model.connect("cleared", self._on_cleared)

        # All three pointer events on the canvas widget so they share one
        # coordinate path (widget pixels → convert_from_pixels → canvas user
        # units). Mixing item-level press with widget-level motion produced
        # a constant offset because the two paths handle anchor/scroll
        # differently for `event.x/y`.
        self._canvas.connect("button-press-event", self._on_press)
        self._canvas.connect("motion-notify-event", self._on_motion)
        self._canvas.connect("button-release-event", self._on_release)
        self._canvas.connect("leave-notify-event", self._on_leave)

        # Pin the canvas viewport to user (0, 0) once: GooCanvas with the
        # symmetric negative bounds we use defaults to the lower bound
        # (-50K, -50K), which would render the visible area far off in
        # empty space. We never touch canvas scroll/scale again — all view
        # manipulation goes through the view_group transform below.
        self._canvas.scroll_to(0.0, 0.0)
        self._apply_view_transform()

    def reset_viewport(self) -> None:
        """Reset the view translation so canvas user (0, 0) sits at the
        widget's top-left, without changing zoom.
        """
        self._view_tx = 0.0
        self._view_ty = 0.0
        self._apply_view_transform()

    def reset_zoom(self) -> None:
        """Set zoom back to 100%, anchored at the viewport center so the
        content currently in the middle of the view stays put.
        """
        alloc = self._canvas.get_allocation()
        cx = alloc.width / 2.0 if alloc.width > 0 else 0.0
        cy = alloc.height / 2.0 if alloc.height > 0 else 0.0
        self._zoom_at(cx, cy, 1.0)

    def _apply_view_transform(self) -> None:
        """Push the current (translate, scale) tuple to the view group.
        Everything underneath inherits the transform.
        """
        self._view_group.set_simple_transform(
            self._view_tx,
            self._view_ty,
            self._view_scale,
            0.0,
        )

    def _widget_to_local(self, widget_x: float, widget_y: float) -> tuple[float, float]:
        """Map a widget pixel to view_group local coords given the current
        view transform.

        First go through GooCanvas's pixel→user conversion (since canvas
        may carry an internal offset for negative bounds), then strip the
        view group's transform.
        """
        user_x, user_y = self._canvas.convert_from_pixels(widget_x, widget_y)
        return (
            (user_x - self._view_tx) / self._view_scale,
            (user_y - self._view_ty) / self._view_scale,
        )

    def _widget_to_user(self, widget_x: float, widget_y: float) -> tuple[float, float]:
        """Map widget pixel to canvas user coords. Used for `get_item_at`,
        which expects canvas user coords (NOT view-group local).
        """
        ux, uy = self._canvas.convert_from_pixels(widget_x, widget_y)
        return float(ux), float(uy)

    # ------------------------------------------------------------------
    # introspection helpers (used by tests, future drag logic)

    def node_item(self, node_id: str) -> NodeItem | None:
        return self._node_items.get(node_id)

    def edge_item(self, edge_id: str) -> EdgeItem | None:
        return self._edge_items.get(edge_id)

    @property
    def canvas(self) -> GooCanvas.Canvas:
        return self._canvas

    # ------------------------------------------------------------------
    # model handlers

    def _on_node_added(self, model: GraphModel, node_id: str) -> None:
        node = model.find_node(node_id)
        if node is None or node_id in self._node_items:
            return
        item = NodeItem(node, parent=self._view_group)
        self._node_items[node_id] = item

    def _on_node_removed(self, _model: GraphModel, node_id: str) -> None:
        item = self._node_items.pop(node_id, None)
        if item is not None:
            item.remove()

    def _on_node_moved(self, _model: GraphModel, node_id: str) -> None:
        item = self._node_items.get(node_id)
        if item is None:
            return
        item.update_position()
        self._refresh_incident_edges(node_id)

    def _on_node_changed(self, model: GraphModel, node_id: str) -> None:
        item = self._node_items.get(node_id)
        node = model.find_node(node_id)
        if item is None or node is None:
            return
        item.update_from(node)
        self._refresh_incident_edges(node_id)

    def _on_edge_added(self, model: GraphModel, edge_id: str) -> None:
        edge = model.find_edge(edge_id)
        if edge is None or edge_id in self._edge_items:
            return
        if edge.src_node not in self._node_items or edge.dst_node not in self._node_items:
            return
        item = EdgeItem(edge, self._compute_path, parent=self._view_group)
        # render edges below nodes so node fills stay readable
        item.lower(None)
        self._edge_items[edge_id] = item

    def _on_edge_removed(self, _model: GraphModel, edge_id: str) -> None:
        if self._hover_edge_id == edge_id:
            self._hover_edge_id = None
        item = self._edge_items.pop(edge_id, None)
        if item is not None:
            item.remove()

    def _on_cleared(self, _model: GraphModel) -> None:
        for item in self._node_items.values():
            item.remove()
        for item in self._edge_items.values():
            item.remove()
        self._node_items.clear()
        self._edge_items.clear()
        self._hover_edge_id = None
        self._hover_port = None

    # ------------------------------------------------------------------
    # geometry

    def _compute_path(self, edge: Edge) -> str:
        src_item = self._node_items.get(edge.src_node)
        dst_item = self._node_items.get(edge.dst_node)
        if src_item is None or dst_item is None:
            return ""
        try:
            sx, sy = src_item.port_position(edge.src_port)
            dx, dy = dst_item.port_position(edge.dst_port)
        except KeyError:
            return ""
        return bezier_between(sx, sy, dx, dy)

    def _refresh_incident_edges(self, node_id: str) -> None:
        for _edge_id, item in list(self._edge_items.items()):
            edge = item.edge
            if node_id in (edge.src_node, edge.dst_node):
                item.refresh()

    # ------------------------------------------------------------------
    # event handling

    def _hit_for(self, target: Any) -> Hit:
        """Identify what got clicked: port, node body, edge, or blank."""
        if target is None:
            return None
        if hasattr(target, "port_id") and hasattr(target, "node_id"):
            return PortHit(node_id=target.node_id, port_id=target.port_id)
        if hasattr(target, "edge_id"):
            return EdgeHit(edge_id=target.edge_id)
        if hasattr(target, "node_id"):
            return NodeHit(node_id=target.node_id)
        return None

    def _on_press(self, _widget: Any, event: Any) -> bool:
        if event.button == Gdk.BUTTON_MIDDLE:
            self._begin_pan(event.x_root, event.y_root)
            return True
        # get_item_at takes canvas user coords; the canvas may carry an
        # internal offset (especially with negative bounds), so route the
        # widget pixel through convert_from_pixels first.
        ux, uy = self._widget_to_user(event.x, event.y)
        target = self._canvas.get_item_at(ux, uy, True)
        if event.button == Gdk.BUTTON_SECONDARY:
            hit = self._hit_for(target)
            self._show_context_menu(hit, event)
            return True
        if event.button != Gdk.BUTTON_PRIMARY:
            return False
        hit = self._hit_for(target)
        if hit is None:
            return False
        self._set_hover_edge(None)
        # Drag works in root-local (canvas user) coords because that's where
        # node.x/y and port_position live.
        x, y = self._widget_to_local(event.x, event.y)
        if isinstance(hit, NodeHit):
            node = self._model.find_node(hit.node_id)
            if node is None:
                return False
            self._drag.begin(hit, x, y, node_origin=(node.x, node.y))
            if self._controller is not None:
                self._controller.pause_rebuild()
            return True
        if isinstance(hit, PortHit):
            self._drag.begin(hit, x, y)
            self._begin_pending_edge(x, y)
            if self._controller is not None:
                self._controller.pause_rebuild()
            return True
        return False

    def _on_motion(self, _widget: Any, event: Any) -> bool:
        from audio_spider.drag_state import EdgeDrag, NodeDrag

        if self._pan is not None:
            self._update_pan(event.x_root, event.y_root)
            return True
        ux, uy = self._widget_to_user(event.x, event.y)
        if self._drag.state is None:
            self._update_edge_hover(ux, uy)
            return False
        x, y = self._widget_to_local(event.x, event.y)
        self._drag.update(x, y)
        state = self._drag.state
        if isinstance(state, NodeDrag):
            self._model.move_node(state.node_id, state.current_x, state.current_y)
        elif isinstance(state, EdgeDrag):
            self._update_pending_edge(x, y)
            self._update_hover_highlight(state, ux, uy)
        return True

    def _on_leave(self, _widget: Any, _event: Any) -> bool:
        self._set_hover_edge(None)
        # Keep pan running so the user can drag the canvas past the viewport
        # edge without it "letting go" mid-gesture; pan only ends on release.
        return False

    def _on_release(self, _widget: Any, event: Any) -> bool:
        if self._pan is not None and event.button == Gdk.BUTTON_MIDDLE:
            self._end_pan()
            return True
        if self._drag.state is None:
            return False
        ux, uy = self._widget_to_user(event.x, event.y)
        target = self._canvas.get_item_at(ux, uy, True)
        drop_hit = self._hit_for(target)
        intent = self._drag.end(drop_hit)
        self._clear_pending_edge()
        self._clear_hover_highlight()
        try:
            if intent is None or self._controller is None:
                return True
            if isinstance(intent, MoveNodeIntent):
                self._controller.request_move_node(intent.node_id, intent.x, intent.y)
            elif isinstance(intent, ConnectIntent):
                # controller already emits 'error' signal; statusbar shows it
                with contextlib.suppress(Exception):
                    self._controller.request_connect(
                        intent.src_node_id,
                        intent.src_port_id,
                        intent.dst_node_id,
                        intent.dst_port_id,
                    )
            return True
        finally:
            if self._controller is not None:
                self._controller.resume_rebuild(do_rebuild=False)

    # ------------------------------------------------------------------
    # pending-edge preview

    def _begin_pending_edge(self, x: float, y: float) -> None:
        if self._pending_edge is not None:
            self._pending_edge.remove()
        self._pending_edge = GooCanvas.CanvasPath(
            parent=self._view_group,
            data=bezier_between(x, y, x, y),
            **{
                "stroke-color": "#888888",
                "line-width": EDGE_WIDTH,
                "line-dash": GooCanvas.CanvasLineDash.newv([6.0, 4.0]),
                # The drag preview line ends at the cursor — if it stayed
                # hit-testable it would intercept get_item_at and shadow the
                # port the user is hovering, breaking drop detection.
                "pointer-events": GooCanvas.CanvasPointerEvents.NONE,
            },
        )

    def _update_pending_edge(self, x: float, y: float) -> None:
        from .drag_state import EdgeDrag

        state = self._drag.state
        if not isinstance(state, EdgeDrag) or self._pending_edge is None:
            return
        src_item = self._node_items.get(state.src_node_id)
        if src_item is None:
            return
        try:
            sx, sy = src_item.port_position(state.src_port_id)
        except KeyError:
            return
        # Source side faces left when the drag started from a sink_in port
        # (reverse-direction drag); otherwise it faces right. The cursor end
        # is treated as facing the opposite way, since the user is dragging
        # toward a port of the complementary kind.
        src_node = self._model.find_node(state.src_node_id)
        src_facing_right = True
        if src_node is not None:
            src_port = next(
                (p for p in src_node.ports if p.id == state.src_port_id),
                None,
            )
            if src_port is not None and src_port.kind == PortKind.SINK_IN:
                src_facing_right = False
        self._pending_edge.set_property(
            "data",
            bezier_between(
                sx,
                sy,
                x,
                y,
                src_facing_right=src_facing_right,
                dst_facing_left=src_facing_right,
            ),
        )

    def _clear_pending_edge(self) -> None:
        if self._pending_edge is not None:
            self._pending_edge.remove()
            self._pending_edge = None

    # ------------------------------------------------------------------
    # drop-target hover highlight

    def _update_hover_highlight(self, state: Any, x: float, y: float) -> None:
        """Mark the port under the cursor (if compatible) as the active drop
        target so the user sees where the edge will land.
        """
        from audio_spider.drag_state import EdgeDrag

        if not isinstance(state, EdgeDrag):
            return
        target = self._canvas.get_item_at(x, y, True)
        new_hover: PortHit | None = None
        hit = self._hit_for(target)
        if isinstance(hit, PortHit):
            src = PortHit(state.src_node_id, state.src_port_id)
            if can_connect(self._model, src, hit):
                new_hover = hit
        if new_hover == self._hover_port:
            return
        if self._hover_port is not None:
            item = self._node_items.get(self._hover_port.node_id)
            if item is not None:
                item.set_port_highlight(self._hover_port.port_id, False)
        if new_hover is not None:
            item = self._node_items.get(new_hover.node_id)
            if item is not None:
                item.set_port_highlight(new_hover.port_id, True)
        self._hover_port = new_hover

    def _clear_hover_highlight(self) -> None:
        if self._hover_port is None:
            return
        item = self._node_items.get(self._hover_port.node_id)
        if item is not None:
            item.set_port_highlight(self._hover_port.port_id, False)
        self._hover_port = None

    # ------------------------------------------------------------------
    # idle edge-hover affordance

    def _update_edge_hover(self, x: float, y: float) -> None:
        """Fire on every idle motion event: if cursor is over an edge's
        hit-zone, thicken it so the user knows right-click works there.
        """
        target = self._canvas.get_item_at(x, y, True)
        edge_id: str | None = None
        if target is not None and hasattr(target, "edge_id"):
            edge_id = target.edge_id
        self._set_hover_edge(edge_id)

    def _set_hover_edge(self, edge_id: str | None) -> None:
        if edge_id == self._hover_edge_id:
            return
        if self._hover_edge_id is not None:
            prev = self._edge_items.get(self._hover_edge_id)
            if prev is not None:
                prev.set_hover(False)
        if edge_id is not None:
            new = self._edge_items.get(edge_id)
            if new is not None:
                new.set_hover(True)
        self._hover_edge_id = edge_id

    # ------------------------------------------------------------------
    # middle-button viewport pan

    def _begin_pan(self, start_root_x: float, start_root_y: float) -> None:
        # Root (screen) coords stay stable regardless of the view changes we
        # apply mid-gesture, so deltas computed from them aren't subject to
        # any feedback loop.
        self._pan = (start_root_x, start_root_y, self._view_tx, self._view_ty)
        # Drop any in-flight idle hover so the user doesn't see a stale
        # thickened edge while panning.
        self._set_hover_edge(None)
        window = self._canvas.get_window()
        if window is not None:
            display = self._canvas.get_display()
            cursor = Gdk.Cursor.new_for_display(display, Gdk.CursorType.FLEUR)
            window.set_cursor(cursor)

    def _update_pan(self, root_x: float, root_y: float) -> None:
        if self._pan is None:
            return
        start_root_x, start_root_y, start_tx, start_ty = self._pan
        # Pan in widget pixels: translation lives in widget pixels, so we
        # add the pixel delta directly. Drag-the-scene feel: content moves
        # WITH the cursor (tx grows when cursor moves right).
        self._view_tx = start_tx + (root_x - start_root_x)
        self._view_ty = start_ty + (root_y - start_root_y)
        self._apply_view_transform()

    def _end_pan(self) -> None:
        self._pan = None
        window = self._canvas.get_window()
        if window is not None:
            window.set_cursor(None)

    # ------------------------------------------------------------------
    # button-driven zoom (always anchored at the viewport center)

    def zoom_in(self) -> None:
        self._zoom_by(ZOOM_STEP)

    def zoom_out(self) -> None:
        self._zoom_by(1.0 / ZOOM_STEP)

    def _zoom_by(self, factor: float) -> None:
        new_scale = max(ZOOM_MIN, min(ZOOM_MAX, self._view_scale * factor))
        if abs(new_scale - self._view_scale) < 1e-6:
            return
        alloc = self._canvas.get_allocation()
        cx = alloc.width / 2.0 if alloc.width > 0 else 0.0
        cy = alloc.height / 2.0 if alloc.height > 0 else 0.0
        self._zoom_at(cx, cy, new_scale)

    def _zoom_at(self, widget_x: float, widget_y: float, new_scale: float) -> None:
        """Apply a new scale while keeping the canvas user coord under the
        cursor anchored to the same widget pixel — so the user's reference
        point on the scene stays put while everything grows/shrinks
        around it.

        Visible widget pixel of an item at root-local (lx, ly):
            widget_x = tx + lx * scale
        Cursor is at widget (cx, cy). Its current root-local coord:
            lx = (cx - tx) / scale
        After scale changes to s', we want the same (lx, ly) under the
        cursor, so tx' = cx - lx * s'.
        """
        local_x = (widget_x - self._view_tx) / self._view_scale
        local_y = (widget_y - self._view_ty) / self._view_scale
        self._view_tx = widget_x - local_x * new_scale
        self._view_ty = widget_y - local_y * new_scale
        self._view_scale = new_scale
        self._apply_view_transform()

    # ------------------------------------------------------------------
    # context menu

    def _show_context_menu(self, hit: Hit, event: Any) -> None:
        if self._controller is None:
            return
        menu = Gtk.Menu()
        if isinstance(hit, PortHit):
            if not self._build_port_menu(menu, hit):
                return
        elif isinstance(hit, NodeHit):
            node = self._model.find_node(hit.node_id)
            if node is None:
                return
            if node.kind == NodeKind.MISSING:
                item = Gtk.MenuItem(label="Remove all orphan connections")
                item.set_tooltip_text(
                    "Unload every loopback that targets this missing endpoint",
                )
                item.connect(
                    "activate",
                    lambda *_: self._safe_call(
                        self._controller.request_remove_orphan,
                        hit.node_id,
                    ),
                )
                menu.append(item)
            else:
                item = Gtk.MenuItem(label="Delete node")
                if node.kind in (NodeKind.HW_SOURCE, NodeKind.HW_SINK):
                    item.set_sensitive(False)
                    item.set_tooltip_text("Cannot delete a hardware device")
                else:
                    item.connect(
                        "activate",
                        lambda *_: self._safe_call(
                            self._controller.request_delete_node,
                            hit.node_id,
                        ),
                    )
                menu.append(item)
        elif isinstance(hit, EdgeHit):
            item = Gtk.MenuItem(label="Delete edge")
            item.connect(
                "activate",
                lambda *_: self._safe_call(
                    self._controller.request_delete_edge,
                    hit.edge_id,
                ),
            )
            menu.append(item)
        else:
            return
        menu.show_all()
        menu.popup_at_pointer(None)

    # ------------------------------------------------------------------
    # port context menu

    def _build_port_menu(self, menu: Gtk.Menu, port_hit: PortHit) -> bool:
        """Populate `menu` with the actions for a right-clicked port.

        Returns True if anything was appended (caller decides whether to pop
        the empty menu open or skip).
        """
        node = self._model.find_node(port_hit.node_id)
        if node is None:
            return False
        port = next((p for p in node.ports if p.id == port_hit.port_id), None)
        if port is None:
            return False

        header = Gtk.MenuItem(label=f"{node.label} :: {port.label}")
        header.set_sensitive(False)
        menu.append(header)
        menu.append(Gtk.SeparatorMenuItem())

        appended = False
        if port.kind == PortKind.COMBINE_MEMBERS:
            appended |= self._add_combine_member_actions(menu, node, port)
        elif port.kind in (PortKind.SOURCE_OUT, PortKind.MONITOR_OUT):
            appended |= self._add_outgoing_loopback_actions(menu, node, port)
        elif port.kind == PortKind.SINK_IN:
            appended |= self._add_incoming_loopback_actions(menu, node, port)

        if not appended:
            empty = Gtk.MenuItem(label="No actions available")
            empty.set_sensitive(False)
            menu.append(empty)
        return True

    def _add_combine_member_actions(
        self,
        menu: Gtk.Menu,
        node: Node,
        port: Port,
    ) -> bool:
        """Build the menu for a Speaker Group's `members` port: existing
        members get a Remove entry; a submenu lists the remaining hw sinks
        that can still be added.
        """
        controller = self._controller
        assert controller is not None  # menu only opens when controller is set  # noqa: S101
        existing = [e for e in self._model.edges() if e.kind == "combine-member" and e.src_node == node.id]
        appended = False
        for edge in existing:
            dst_node = self._model.find_node(edge.dst_node)
            if dst_node is None:
                continue
            item = Gtk.MenuItem(label=f"Remove member: {dst_node.label}")
            combine_id = node.id
            member_id = edge.dst_node
            item.connect(
                "activate",
                lambda *_, c=combine_id, m=member_id: self._safe_call(
                    controller.request_remove_combine_member,
                    c,
                    m,
                ),
            )
            menu.append(item)
            appended = True

        existing_member_ids = {e.dst_node for e in existing}
        candidates = [
            n
            for n in self._model.nodes()
            if n.kind == NodeKind.HW_SINK and n.id != node.id and n.id not in existing_member_ids
        ]
        if candidates:
            if appended:
                menu.append(Gtk.SeparatorMenuItem())
            add_root = Gtk.MenuItem(label="Add member")
            submenu = Gtk.Menu()
            for cand in candidates:
                sub_item = Gtk.MenuItem(label=cand.label)
                combine_id = node.id
                sink_id = cand.id
                sub_item.connect(
                    "activate",
                    lambda *_, c=combine_id, s=sink_id: self._safe_call(
                        controller.request_add_combine_member,
                        c,
                        s,
                    ),
                )
                submenu.append(sub_item)
            add_root.set_submenu(submenu)
            menu.append(add_root)
            appended = True
        return appended

    def _add_outgoing_loopback_actions(
        self,
        menu: Gtk.Menu,
        node: Node,
        port: Port,
    ) -> bool:
        controller = self._controller
        assert controller is not None  # menu only opens when controller is set  # noqa: S101
        existing = [
            e for e in self._model.edges() if e.kind == "loopback" and e.src_node == node.id and e.src_port == port.id
        ]
        already_connected = {(e.dst_node, e.dst_port) for e in existing}
        appended = False

        for edge in existing:
            dst_node = self._model.find_node(edge.dst_node)
            if dst_node is None:
                continue
            dst_port = next(
                (p for p in dst_node.ports if p.id == edge.dst_port),
                None,
            )
            dst_label = f"{dst_node.label} :: {dst_port.label}" if dst_port is not None else dst_node.label
            item = Gtk.MenuItem(label=f"Disconnect from {dst_label}")
            edge_id = edge.id  # capture by name
            item.connect(
                "activate",
                lambda *_, eid=edge_id: self._safe_call(
                    controller.request_delete_edge,
                    eid,
                ),
            )
            menu.append(item)
            appended = True

        # build "Connect to" submenu of compatible SINK_IN ports
        targets = self._compatible_sink_targets(
            self_node_id=node.id,
            exclude=already_connected,
        )
        if targets:
            if appended:
                menu.append(Gtk.SeparatorMenuItem())
            connect_root = Gtk.MenuItem(label="Connect to")
            submenu = Gtk.Menu()
            for target_node, target_port in targets:
                label = f"{target_node.label} :: {target_port.label}"
                sub_item = Gtk.MenuItem(label=label)
                sub_item.connect(
                    "activate",
                    lambda *_, src_n=node.id, src_p=port.id, dst_n=target_node.id, dst_p=target_port.id: (
                        self._safe_call(
                            controller.request_connect,
                            src_n,
                            src_p,
                            dst_n,
                            dst_p,
                        )
                    ),
                )
                submenu.append(sub_item)
            connect_root.set_submenu(submenu)
            menu.append(connect_root)
            appended = True
        return appended

    def _add_incoming_loopback_actions(
        self,
        menu: Gtk.Menu,
        node: Node,
        port: Port,
    ) -> bool:
        controller = self._controller
        assert controller is not None  # menu only opens when controller is set  # noqa: S101
        existing = [
            e for e in self._model.edges() if e.kind == "loopback" and e.dst_node == node.id and e.dst_port == port.id
        ]
        appended = False
        for edge in existing:
            src_node = self._model.find_node(edge.src_node)
            if src_node is None:
                continue
            src_port = next(
                (p for p in src_node.ports if p.id == edge.src_port),
                None,
            )
            src_label = f"{src_node.label} :: {src_port.label}" if src_port is not None else src_node.label
            item = Gtk.MenuItem(label=f"Disconnect from {src_label}")
            edge_id = edge.id
            item.connect(
                "activate",
                lambda *_, eid=edge_id: self._safe_call(
                    controller.request_delete_edge,
                    eid,
                ),
            )
            menu.append(item)
            appended = True
        return appended

    def _compatible_sink_targets(
        self,
        *,
        self_node_id: str,
        exclude: set[tuple[str, str]],
    ) -> list[tuple[Node, Port]]:
        out: list[tuple[Node, Port]] = []
        for node in self._model.nodes():
            if node.id == self_node_id:
                continue
            for port in node.ports:
                if port.kind != PortKind.SINK_IN:
                    continue
                if (node.id, port.id) in exclude:
                    continue
                out.append((node, port))
        return out

    @staticmethod
    def _safe_call(fn: Callable[..., Any], *args: Any) -> None:
        # controller already surfaces via 'error' signal
        with contextlib.suppress(Exception):
            fn(*args)
