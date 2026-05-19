from __future__ import annotations

from typing import TYPE_CHECKING

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("AyatanaAppIndicator3", "0.1")

from gi.repository import AyatanaAppIndicator3 as AppIndicator  # noqa: E402
from gi.repository import Gtk  # noqa: E402

if TYPE_CHECKING:
    from collections.abc import Callable


class TrayIcon:
    """Thin wrapper around AyatanaAppIndicator3 + a Gtk.Menu.

    Keeps the GTK widget code out of the rest of the app: callers supply
    plain callbacks for the three menu actions.
    """

    def __init__(
        self,
        on_show: Callable[[], None],
        on_reload: Callable[[], None],
        on_quit: Callable[[], None],
        icon_name: str = "audio-spider",
        app_id: str = "audio-spider",
    ) -> None:
        self._on_show = on_show
        self._on_reload = on_reload
        self._on_quit = on_quit
        self._indicator = AppIndicator.Indicator.new(
            app_id,
            icon_name,
            AppIndicator.IndicatorCategory.APPLICATION_STATUS,
        )
        self._indicator.set_status(AppIndicator.IndicatorStatus.ACTIVE)
        self._indicator.set_title("Audio Spider")
        self._indicator.set_menu(self._build_menu())

    def _build_menu(self) -> Gtk.Menu:
        menu = Gtk.Menu()
        item_show = Gtk.MenuItem(label="Show / Hide")
        item_show.connect("activate", lambda *_: self._on_show())
        menu.append(item_show)

        item_reload = Gtk.MenuItem(label="Reload config")
        item_reload.connect("activate", lambda *_: self._on_reload())
        menu.append(item_reload)

        menu.append(Gtk.SeparatorMenuItem())

        item_quit = Gtk.MenuItem(label="Quit")
        item_quit.connect("activate", lambda *_: self._on_quit())
        menu.append(item_quit)

        menu.show_all()
        return menu

    def hide(self) -> None:
        self._indicator.set_status(AppIndicator.IndicatorStatus.PASSIVE)
