"""GTK-shell smoke tests.

These need a working display (or Xvfb). The fixture auto-skips when GTK
init fails, so a CI box without one just won't run them. On the dev box
we can also force-skip by setting AUDIO_SPIDER_SKIP_GUI=1.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any, cast

import pytest

if TYPE_CHECKING:
    from pathlib import Path

    from audio_spider.config import Config
    from audio_spider.controller import Controller
    from audio_spider.graph_model import GraphModel
    from audio_spider.pa_backend import PABackend

HeadlessCtrl = tuple["Controller", "GraphModel", "Config"]

pytestmark = [pytest.mark.gtk, pytest.mark.usefixtures("_gtk")]


@pytest.fixture(scope="module")
def _gtk() -> Any:
    if os.environ.get("AUDIO_SPIDER_SKIP_GUI"):
        pytest.skip("AUDIO_SPIDER_SKIP_GUI set")
    if not os.environ.get("DISPLAY") and not os.environ.get("WAYLAND_DISPLAY"):
        pytest.skip("no display available")
    import gi

    gi.require_version("Gtk", "3.0")
    from gi.repository import Gtk

    if not Gtk.init_check():
        pytest.skip("Gtk.init_check() failed")
    return Gtk


@pytest.fixture
def headless_ctrl(tmp_path: Path) -> HeadlessCtrl:
    """Set up Controller backed by FakePA for GUI tests."""
    from audio_spider.config import Config
    from audio_spider.controller import Controller
    from audio_spider.graph_model import GraphModel
    from tests.test_controller import FakePA

    pa = FakePA()
    pa.add_hw_source("mic_a", "Microphone A")
    pa.add_hw_sink("speakers", "Speakers")
    cfg = Config()
    model = GraphModel()
    c = Controller(
        cast("PABackend", pa),
        cfg,
        model,
        config_path=tmp_path / "config.json",
    )
    c.initial_sync()
    return c, model, cfg


def test_main_window_constructs(headless_ctrl: HeadlessCtrl) -> None:
    from audio_spider.app import AudioSpiderApp, MainWindow

    controller, model, cfg = headless_ctrl
    app = AudioSpiderApp(controller, model, cfg, use_tray=False, start_minimized=False)
    win = MainWindow(app, controller, model, cfg, use_tray=False)
    assert win.get_title() == "Audio Spider"
    assert win.get_default_size() == (cfg.window.w, cfg.window.h)
    win.destroy()


def test_main_window_toolbar_has_three_actions(headless_ctrl: HeadlessCtrl) -> None:
    from gi.repository import Gtk

    from audio_spider.app import AudioSpiderApp, MainWindow

    controller, model, cfg = headless_ctrl
    app = AudioSpiderApp(controller, model, cfg, use_tray=False, start_minimized=False)
    win = MainWindow(app, controller, model, cfg, use_tray=False)

    # crawl children to find the toolbar
    def _find_toolbar(widget: Any) -> Gtk.Toolbar | None:
        if isinstance(widget, Gtk.Toolbar):
            return widget
        if isinstance(widget, Gtk.Container):
            for child in widget.get_children():
                found = _find_toolbar(child)
                if found is not None:
                    return found
        return None

    toolbar = _find_toolbar(win)
    assert toolbar is not None
    tool_buttons = [
        toolbar.get_nth_item(i)
        for i in range(toolbar.get_n_items())
        if isinstance(toolbar.get_nth_item(i), Gtk.ToolButton)
    ]
    labels = {b.get_label() for b in tool_buttons}
    assert {
        "Speaker Group",
        "Reload config",
        "Reset view",
        "Center view",
        "Zoom in",
        "Zoom out",
        "Reset zoom",
        "Exit",
    } <= labels
    win.destroy()


def test_controller_error_signal_updates_statusbar(headless_ctrl: HeadlessCtrl) -> None:
    from gi.repository import Gtk

    from audio_spider.app import AudioSpiderApp, MainWindow

    controller, model, cfg = headless_ctrl
    app = AudioSpiderApp(controller, model, cfg, use_tray=False, start_minimized=False)
    win = MainWindow(app, controller, model, cfg, use_tray=False)

    def _find_statusbar(w: Any) -> Any:
        if isinstance(w, Gtk.Statusbar):
            return w
        if isinstance(w, Gtk.Container):
            for child in w.get_children():
                found = _find_statusbar(child)
                if found is not None:
                    return found
        return None

    sb = _find_statusbar(win)
    assert sb is not None
    controller.emit("error", "boom — test error")
    # pump events
    while Gtk.events_pending():
        Gtk.main_iteration_do(False)
    # statusbar text via messages stack
    msg = sb.get_message_area().get_children()[0].get_text()
    assert "boom" in msg
    win.destroy()


def test_delete_event_hides_window_in_tray_mode(headless_ctrl: HeadlessCtrl) -> None:
    from gi.repository import Gdk, Gtk

    from audio_spider.app import AudioSpiderApp, MainWindow

    controller, model, cfg = headless_ctrl
    app = AudioSpiderApp(controller, model, cfg, use_tray=True, start_minimized=False)
    win = MainWindow(app, controller, model, cfg, use_tray=True)
    win.show_all()
    while Gtk.events_pending():
        Gtk.main_iteration_do(False)
    handled = win.emit("delete-event", Gdk.Event.new(Gdk.EventType.DELETE))
    while Gtk.events_pending():
        Gtk.main_iteration_do(False)
    assert handled is True
    assert not win.get_visible()
    win.destroy()


def test_delete_event_quits_when_no_tray(headless_ctrl: HeadlessCtrl) -> None:
    from gi.repository import Gdk

    from audio_spider.app import AudioSpiderApp, MainWindow

    controller, model, cfg = headless_ctrl
    app = AudioSpiderApp(controller, model, cfg, use_tray=False, start_minimized=False)
    win = MainWindow(app, controller, model, cfg, use_tray=False)
    handled = win.emit("delete-event", Gdk.Event.new(Gdk.EventType.DELETE))
    assert handled is False
    win.destroy()
