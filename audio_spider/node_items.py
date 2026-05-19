from __future__ import annotations

from typing import Callable

import gi

gi.require_version("GooCanvas", "2.0")

from gi.repository import GooCanvas, Pango  # noqa: E402

from .graph_model import Edge, Node, NodeKind, Port, PortKind

NODE_WIDTH = 200.0
NODE_HEADER_HEIGHT = 28.0
NODE_PORT_HEIGHT = 22.0
NODE_PORT_TOP_PADDING = 6.0
NODE_PORT_BOTTOM_PADDING = 8.0
NODE_CORNER_RADIUS = 8.0
PORT_RADIUS = 10.0
PORT_HIT_RADIUS = 18.0  # invisible larger circle for easier dropping
PORT_LABEL_INSET = PORT_RADIUS + 8.0

NODE_FILL: dict[NodeKind, int] = {
    NodeKind.HW_SOURCE: 0xE8F0FFFF,
    NodeKind.HW_SINK: 0xFFF0E8FF,
    NodeKind.NULL_SINK: 0xE8FFE8FF,
    NodeKind.COMBINE_SINK: 0xFFE8FFFF,
    NodeKind.MISSING: 0xE8E8E8FF,
}
NODE_STROKE = 0x4A4A4AFF
HEADER_FILL: dict[NodeKind, int] = {
    NodeKind.HW_SOURCE: 0xB8D0FFFF,
    NodeKind.HW_SINK: 0xFFD0B8FF,
    NodeKind.NULL_SINK: 0xB8FFB8FF,
    NodeKind.COMBINE_SINK: 0xFFB8FFFF,
    NodeKind.MISSING: 0xC0C0C0FF,
}

EDGE_COLOR: dict[str, str] = {
    "loopback": "#3366cc",
    "combine-member": "#cc6633",
}
EDGE_WIDTH = 2.0
# Thicker stroke applied when the cursor hovers an edge so the user knows
# right-click is available without aiming pixel-perfectly at the 2px line.
EDGE_HOVER_WIDTH = 4.0
# An invisible wider stroke is drawn behind every edge so that hover and
# right-click detection has a generous hit margin around the visible line.
EDGE_HIT_WIDTH = 14.0
PORT_FILL_INPUT = 0x5F8DBEFF
PORT_FILL_OUTPUT = 0x70B070FF
PORT_FILL_MONITOR = 0x9F70BEFF
PORT_FILL_MEMBERS = 0xC09030FF

PORT_FILL: dict[PortKind, int] = {
    PortKind.SOURCE_OUT: PORT_FILL_OUTPUT,
    PortKind.SINK_IN: PORT_FILL_INPUT,
    PortKind.MONITOR_OUT: PORT_FILL_MONITOR,
    PortKind.COMBINE_MEMBERS: PORT_FILL_MEMBERS,
}

# Drop-target highlight applied to a port circle when an in-flight edge drag
# hovers a compatible destination.
PORT_HIGHLIGHT_STROKE = 0x00B050FF
PORT_HIGHLIGHT_WIDTH = 3.0
PORT_DEFAULT_STROKE = 0x4A4A4AFF  # same as NODE_STROKE; named for clarity
PORT_DEFAULT_WIDTH = 1.0


def _is_input_port(kind: PortKind) -> bool:
    return kind == PortKind.SINK_IN


class NodeItem(GooCanvas.CanvasGroup):
    """Visual representation of a single node.

    Children: background Rect, header Rect, label Text, and one Ellipse +
    Text per port. The group as a whole is translated to `(node.x, node.y)`;
    children use local coordinates relative to the group origin.
    """

    def __init__(self, node: Node, **kwargs):
        super().__init__(**kwargs)
        self._node = node
        self._port_index: dict[str, tuple[float, float]] = {}  # in group-local coords
        self._port_ellipses: dict[str, GooCanvas.CanvasEllipse] = {}
        self._build()
        self._apply_position()

    # ------------------------------------------------------------------
    # public

    @property
    def node(self) -> Node:
        return self._node

    def update_from(self, node: Node) -> None:
        self._node = node
        for child in list(self._iter_children()):
            child.remove()
        self._build()
        self._apply_position()

    def update_position(self) -> None:
        """Re-apply x/y translation from the underlying Node."""
        self._apply_position()

    def port_position(self, port_id: str) -> tuple[float, float]:
        """Return port center in canvas (absolute) coordinates."""
        local = self._port_index.get(port_id)
        if local is None:
            raise KeyError(f"unknown port: {port_id}")
        lx, ly = local
        return self._node.x + lx, self._node.y + ly

    def set_port_highlight(self, port_id: str, highlighted: bool) -> None:
        """Toggle the drop-target highlight on a port circle.

        Used by GraphView during an edge drag to flag the port currently
        under the cursor as a valid drop target.
        """
        ellipse = self._port_ellipses.get(port_id)
        if ellipse is None:
            return
        if highlighted:
            ellipse.set_property("stroke-color-rgba", PORT_HIGHLIGHT_STROKE)
            ellipse.set_property("line-width", PORT_HIGHLIGHT_WIDTH)
        else:
            ellipse.set_property("stroke-color-rgba", PORT_DEFAULT_STROKE)
            ellipse.set_property("line-width", PORT_DEFAULT_WIDTH)

    def height(self) -> float:
        return self._height

    # ------------------------------------------------------------------
    # construction

    def _iter_children(self):
        for i in range(self.get_n_children()):
            yield self.get_child(i)

    def _build(self) -> None:
        inputs = [p for p in self._node.ports if _is_input_port(p.kind)]
        outputs = [p for p in self._node.ports if not _is_input_port(p.kind)]
        rows = max(len(inputs), len(outputs), 1)
        body_height = NODE_PORT_TOP_PADDING + rows * NODE_PORT_HEIGHT + NODE_PORT_BOTTOM_PADDING
        self._height = NODE_HEADER_HEIGHT + body_height

        body = GooCanvas.CanvasRect(
            parent=self,
            x=0, y=0,
            width=NODE_WIDTH, height=self._height,
            radius_x=NODE_CORNER_RADIUS, radius_y=NODE_CORNER_RADIUS,
            **{"fill-color-rgba": NODE_FILL[self._node.kind],
               "stroke-color-rgba": NODE_STROKE,
               "line-width": 1.0},
        )
        body.node_id = self._node.id

        header = GooCanvas.CanvasRect(
            parent=self,
            x=0, y=0,
            width=NODE_WIDTH, height=NODE_HEADER_HEIGHT,
            radius_x=NODE_CORNER_RADIUS, radius_y=NODE_CORNER_RADIUS,
            **{"fill-color-rgba": HEADER_FILL[self._node.kind],
               "stroke-color-rgba": NODE_STROKE,
               "line-width": 1.0},
        )
        header.node_id = self._node.id

        text = GooCanvas.CanvasText(
            parent=self,
            text=self._node.label,
            x=NODE_WIDTH / 2, y=NODE_HEADER_HEIGHT / 2,
            width=NODE_WIDTH - 16,
            anchor=GooCanvas.CanvasAnchorType.CENTER,
            alignment=Pango.Alignment.CENTER,
            **{"font": "Sans Bold 10"},
        )
        text.node_id = self._node.id

        self._port_index.clear()
        self._port_ellipses.clear()
        for i, port in enumerate(inputs):
            cy = NODE_HEADER_HEIGHT + NODE_PORT_TOP_PADDING + i * NODE_PORT_HEIGHT + NODE_PORT_HEIGHT / 2
            self._port_index[port.id] = (0.0, cy)
            self._add_port_visuals(port, x=0.0, y=cy, on_left=True)
        for i, port in enumerate(outputs):
            cy = NODE_HEADER_HEIGHT + NODE_PORT_TOP_PADDING + i * NODE_PORT_HEIGHT + NODE_PORT_HEIGHT / 2
            self._port_index[port.id] = (NODE_WIDTH, cy)
            self._add_port_visuals(port, x=NODE_WIDTH, y=cy, on_left=False)

    def _add_port_visuals(
        self, port: Port, *, x: float, y: float, on_left: bool,
    ) -> None:
        # Visible port circle
        ellipse = GooCanvas.CanvasEllipse(
            parent=self,
            center_x=x, center_y=y,
            radius_x=PORT_RADIUS, radius_y=PORT_RADIUS,
            **{"fill-color-rgba": PORT_FILL[port.kind],
               "stroke-color-rgba": PORT_DEFAULT_STROKE,
               "line-width": PORT_DEFAULT_WIDTH},
        )
        ellipse.port_id = port.id
        ellipse.node_id = self._node.id
        self._port_ellipses[port.id] = ellipse

        # Larger invisible hit-target so dropping the cursor near a port still
        # registers as a port hit. Default pointer-events is VISIBLE_PAINTED,
        # which excludes alpha=0 areas — we override to FILL so a fully
        # transparent fill still receives clicks.
        hit = GooCanvas.CanvasEllipse(
            parent=self,
            center_x=x, center_y=y,
            radius_x=PORT_HIT_RADIUS, radius_y=PORT_HIT_RADIUS,
            **{"fill-color-rgba": 0x00000000,
               "stroke-color-rgba": 0x00000000,
               "line-width": 0.0,
               "pointer-events": GooCanvas.CanvasPointerEvents.FILL},
        )
        hit.port_id = port.id
        hit.node_id = self._node.id

        label_x = x + (-PORT_LABEL_INSET if on_left else PORT_LABEL_INSET)
        anchor = (
            GooCanvas.CanvasAnchorType.E if on_left
            else GooCanvas.CanvasAnchorType.W
        )
        GooCanvas.CanvasText(
            parent=self,
            text=port.label,
            x=label_x, y=y,
            anchor=anchor,
            **{"font": "Sans 9", "fill-color-rgba": 0x303030FF},
        )

    def _apply_position(self) -> None:
        # GooCanvas.Group has its own transform; reset and translate.
        self.set_simple_transform(self._node.x, self._node.y, 1.0, 0.0)


class EdgeItem(GooCanvas.CanvasGroup):
    """Bezier curve between two port centers (canvas-absolute coords).

    Backed by two stacked paths: an invisible wide-stroke `_hit_path`
    underneath that catches pointer events with a generous margin, and a
    thin `_visible_path` on top that renders the actual line. Splitting the
    two lets us thicken the visible line on hover without touching hit
    geometry, and lets users grab the edge without aiming pixel-precisely.
    """

    def __init__(self, edge: Edge, path_provider: Callable[[Edge], str], **kwargs):
        super().__init__(**kwargs)
        self._edge = edge
        self._path_provider = path_provider
        self.edge_id = edge.id
        data = path_provider(edge)
        self._hit_path = GooCanvas.CanvasPath(
            parent=self,
            data=data,
            **{"stroke-color-rgba": 0x00000000,
               "line-width": EDGE_HIT_WIDTH,
               "pointer-events": GooCanvas.CanvasPointerEvents.STROKE},
        )
        self._hit_path.edge_id = edge.id
        self._visible_path = GooCanvas.CanvasPath(
            parent=self,
            data=data,
            **{"stroke-color": EDGE_COLOR.get(edge.kind, "#888"),
               "line-width": EDGE_WIDTH,
               "antialias": 1,
               # Visible path doesn't take hits — the wide hit path beneath
               # already does, and having both pickable would shadow each
               # other depending on z-order.
               "pointer-events": GooCanvas.CanvasPointerEvents.NONE},
        )
        self._visible_path.edge_id = edge.id

    @property
    def edge(self) -> Edge:
        return self._edge

    def refresh(self) -> None:
        data = self._path_provider(self._edge)
        self._hit_path.set_property("data", data)
        self._visible_path.set_property("data", data)

    def set_hover(self, hovered: bool) -> None:
        self._visible_path.set_property(
            "line-width", EDGE_HOVER_WIDTH if hovered else EDGE_WIDTH,
        )


def bezier_between(
    x1: float, y1: float, x2: float, y2: float,
    *,
    src_facing_right: bool = True,
    dst_facing_left: bool = True,
) -> str:
    """Build an SVG path string for a horizontal-ish cubic bezier.

    Each control point is pushed in the direction the corresponding port
    faces (right-edge ports face +x, left-edge ports face -x), so the curve
    leaves and enters horizontally. Defaults match the common case: a
    producer port on the right of a node connecting to a sink port on the
    left of another node. Pass the flags otherwise (e.g. drag preview that
    starts from a sink_in port).
    """
    handle = max(40.0, abs(x2 - x1) * 0.5)
    c1x = x1 + handle if src_facing_right else x1 - handle
    c2x = x2 - handle if dst_facing_left else x2 + handle
    return f"M {x1},{y1} C {c1x},{y1} {c2x},{y2} {x2},{y2}"
