from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING

import gi

gi.require_version("Gtk", "3.0")

from gi.repository import Gtk  # noqa: E402

if TYPE_CHECKING:
    from .config import Config
    from .controller import Controller
    from .graph_model import GraphModel
    from .tray import TrayIcon

log = logging.getLogger(__name__)


def sanitize_pa_name(label: str, *, fallback: str = "vdev") -> str:
    """Turn a user-typed label into a valid PulseAudio sink_name.

    PA sink names must be ASCII letters, digits and underscores. Whitespace
    and punctuation collapse to underscores. Empty result falls back.
    """
    cleaned = re.sub(r"[^A-Za-z0-9_]+", "_", label).strip("_")
    return cleaned or fallback


class MainWindow(Gtk.ApplicationWindow):
    """Top-level window: toolbar, central area (placeholder until Stage F),
    statusbar. delete-event either hides (tray mode) or quits.
    """

    def __init__(
        self,
        application: Gtk.Application,
        controller: Controller,
        model: GraphModel,
        cfg: Config,
        *,
        use_tray: bool,
    ) -> None:
        super().__init__(application=application, title="Audio Spider")
        self._controller = controller
        self._model = model
        self._cfg = cfg
        self._use_tray = use_tray

        self.set_default_size(cfg.window.w, cfg.window.h)

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.add(outer)

        outer.pack_start(self._build_toolbar(), False, False, 0)
        outer.pack_start(self._build_graph_view(), True, True, 0)
        self._statusbar = Gtk.Statusbar()
        self._statusbar_ctx = self._statusbar.get_context_id("main")
        outer.pack_start(self._statusbar, False, False, 0)

        self.connect("delete-event", self._on_delete)
        self.connect("configure-event", self._on_configure)

        controller.connect("error", self._on_controller_error)
        controller.connect("sync-complete", self._on_sync_complete)
        # initial counters
        self._refresh_status()

    # --- widget construction -----------------------------------------

    def _build_toolbar(self) -> Gtk.Widget:
        toolbar = Gtk.Toolbar()
        toolbar.set_style(Gtk.ToolbarStyle.BOTH)

        btn_combine = Gtk.ToolButton.new(None, "Speaker Group")
        btn_combine.set_icon_name("audio-card")
        btn_combine.set_tooltip_text(
            "Add a virtual speaker that routes audio to several real speakers "
            "at once. Useful for sending the same sound to e.g. headphones and "
            "external speakers simultaneously.",
        )
        btn_combine.connect("clicked", self._on_add_combine_sink_clicked)
        toolbar.insert(btn_combine, -1)

        toolbar.insert(Gtk.SeparatorToolItem(), -1)

        btn_reload = Gtk.ToolButton.new(None, "Reload config")
        btn_reload.set_icon_name("view-refresh")
        btn_reload.set_tooltip_text("Re-read config + resync with PulseAudio")
        btn_reload.connect("clicked", self._on_reload_clicked)
        toolbar.insert(btn_reload, -1)

        btn_reset = Gtk.ToolButton.new(None, "Reset view")
        btn_reset.set_icon_name("view-restore")
        btn_reset.set_tooltip_text(
            "Re-lay out every node in its default column AND recenter the "
            "viewport AND reset zoom to 100%. Use this if anything's been "
            "dragged or zoomed off-screen.",
        )
        btn_reset.connect("clicked", self._on_reset_view_clicked)
        toolbar.insert(btn_reset, -1)

        btn_center = Gtk.ToolButton.new(None, "Center view")
        btn_center.set_icon_name("zoom-fit-best")
        btn_center.set_tooltip_text(
            "Recenter the viewport on canvas origin without moving nodes "
            "or changing the zoom level.",
        )
        btn_center.connect("clicked", self._on_center_view_clicked)
        toolbar.insert(btn_center, -1)

        btn_zoom_in = Gtk.ToolButton.new(None, "Zoom in")
        btn_zoom_in.set_icon_name("zoom-in")
        btn_zoom_in.set_tooltip_text("Zoom in on the viewport center.")
        btn_zoom_in.connect("clicked", self._on_zoom_in_clicked)
        toolbar.insert(btn_zoom_in, -1)

        btn_zoom_out = Gtk.ToolButton.new(None, "Zoom out")
        btn_zoom_out.set_icon_name("zoom-out")
        btn_zoom_out.set_tooltip_text("Zoom out from the viewport center.")
        btn_zoom_out.connect("clicked", self._on_zoom_out_clicked)
        toolbar.insert(btn_zoom_out, -1)

        btn_zoom_reset = Gtk.ToolButton.new(None, "Reset zoom")
        btn_zoom_reset.set_icon_name("zoom-original")
        btn_zoom_reset.set_tooltip_text("Set zoom back to 100%.")
        btn_zoom_reset.connect("clicked", self._on_reset_zoom_clicked)
        toolbar.insert(btn_zoom_reset, -1)

        # Spacer pushes the Exit button to the right edge of the toolbar.
        spacer = Gtk.SeparatorToolItem()
        spacer.set_draw(False)
        spacer.set_expand(True)
        toolbar.insert(spacer, -1)

        btn_exit = Gtk.ToolButton.new(None, "Exit")
        btn_exit.set_icon_name("application-exit")
        btn_exit.set_tooltip_text(
            "Quit Audio Spider completely (also closes the tray icon). "
            "Loaded PulseAudio modules stay in place.",
        )
        btn_exit.connect("clicked", self._on_exit_clicked)
        toolbar.insert(btn_exit, -1)

        return toolbar

    def _build_graph_view(self) -> Gtk.Widget:
        from .graph_view import GraphView
        self._graph_view = GraphView(self._model, controller=self._controller)
        return self._graph_view

    # --- signal handlers ---------------------------------------------

    def _on_delete(self, _widget, _event) -> bool:
        if self._use_tray:
            self.hide()
            return True  # stop default destroy
        return False

    def _on_configure(self, _widget, event) -> bool:
        # remember window size for next launch
        self._cfg.window.w = event.width
        self._cfg.window.h = event.height
        return False

    def _on_add_combine_sink_clicked(self, _btn) -> None:
        label = _prompt_text(
            self,
            "Add Speaker Group",
            "Name for the new speaker group.\n"
            "Apps will play to it; afterwards right-click the group's "
            "'members' port to choose which real speakers receive its "
            "audio (or drag from the port to a speaker).",
        )
        if not label:
            return
        sink_name = sanitize_pa_name(label, fallback="speaker_group")
        try:
            self._controller.request_create_combine_sink(
                sink_name, members=[], description=label,
            )
            self._set_status(f"Created speaker group '{label}'")
        except Exception as e:
            self._set_status(f"Failed: {e}")

    def _on_exit_clicked(self, _btn) -> None:
        app = self.get_application()
        if app is not None:
            app.quit()

    def _on_reset_view_clicked(self, _btn) -> None:
        try:
            self._controller.request_reset_layout()
            self._graph_view.reset_zoom()
            self._graph_view.reset_viewport()
            self._set_status("View reset to default")
        except Exception as e:
            self._set_status(f"Reset view failed: {e}")

    def _on_center_view_clicked(self, _btn) -> None:
        self._graph_view.reset_viewport()
        self._set_status("Viewport centered")

    def _on_reset_zoom_clicked(self, _btn) -> None:
        self._graph_view.reset_zoom()
        self._set_status("Zoom reset to 100%")

    def _on_zoom_in_clicked(self, _btn) -> None:
        self._graph_view.zoom_in()

    def _on_zoom_out_clicked(self, _btn) -> None:
        self._graph_view.zoom_out()

    def _on_reload_clicked(self, _btn) -> None:
        try:
            report = self._controller.reload_config()
            self._set_status(
                f"Reloaded: {len(report.created)} created, "
                f"{len(report.skipped)} already present, "
                f"{len(report.errors)} failed",
            )
        except Exception as e:
            self._set_status(f"Reload failed: {e}")

    def _on_controller_error(self, _controller, message: str) -> None:
        self._set_status(message)

    def _on_sync_complete(self, _controller) -> None:
        self._refresh_status()

    # --- helpers -----------------------------------------------------

    def _refresh_status(self) -> None:
        n_nodes = len(self._model.nodes())
        n_edges = len(self._model.edges())
        self._set_status(f"{n_nodes} nodes, {n_edges} edges")

    def _set_status(self, message: str) -> None:
        self._statusbar.pop(self._statusbar_ctx)
        self._statusbar.push(self._statusbar_ctx, message)


class AudioSpiderApp(Gtk.Application):
    def __init__(
        self,
        controller: Controller,
        model: GraphModel,
        cfg: Config,
        *,
        use_tray: bool,
        start_minimized: bool,
    ) -> None:
        super().__init__(application_id="com.example.AudioSpider")
        self._controller = controller
        self._model = model
        self._cfg = cfg
        self._use_tray = use_tray
        self._start_minimized = start_minimized
        self._window: MainWindow | None = None
        self._tray: TrayIcon | None = None

    def do_activate(self) -> None:
        if self._window is None:
            self._window = MainWindow(
                self, self._controller, self._model, self._cfg,
                use_tray=self._use_tray,
            )
            if self._use_tray:
                from .tray import TrayIcon
                self._tray = TrayIcon(
                    on_show=self._toggle_window,
                    on_reload=self._reload,
                    on_quit=self.quit,
                )
        if self._start_minimized and self._use_tray:
            self._window.hide()
        else:
            self._window.show_all()

    def _toggle_window(self) -> None:
        if self._window is None:
            return
        if self._window.get_visible():
            self._window.hide()
        else:
            self._window.show_all()
            self._window.present()

    def _reload(self) -> None:
        # Run on main thread; tray menu callbacks already are.
        try:
            self._controller.reload_config()
        except Exception:
            log.exception("reload failed")


def run_gui(
    controller: Controller,
    model: GraphModel,
    cfg: Config,
    *,
    no_tray: bool,
    minimized: bool,
) -> int:
    app = AudioSpiderApp(
        controller, model, cfg,
        use_tray=not no_tray,
        start_minimized=minimized,
    )
    return app.run([])


def _prompt_text(parent: Gtk.Window, title: str, label: str) -> str | None:
    """Modal text-input dialog. Returns None if cancelled."""
    dialog = Gtk.Dialog(title=title, transient_for=parent, flags=0)
    dialog.add_buttons(
        Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL,
        Gtk.STOCK_OK, Gtk.ResponseType.OK,
    )
    dialog.set_default_response(Gtk.ResponseType.OK)
    box = dialog.get_content_area()
    box.set_spacing(8)
    box.set_border_width(12)
    box.pack_start(Gtk.Label(label=label), False, False, 0)
    entry = Gtk.Entry()
    entry.set_activates_default(True)
    box.pack_start(entry, False, False, 0)
    box.show_all()
    response = dialog.run()
    text = entry.get_text().strip() if response == Gtk.ResponseType.OK else None
    dialog.destroy()
    return text or None
