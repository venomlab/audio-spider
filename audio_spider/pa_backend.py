from __future__ import annotations

import contextlib
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from gi.repository import GLib

# Vendored pulsectl has no type stubs; aliasing through Any keeps the rest
# of this module under mypy --strict without sprinkling type: ignores.
from audio_spider._vendor import pulsectl as _pulsectl
from audio_spider.errors import PABackendError

pulsectl: Any = _pulsectl


@dataclass(frozen=True)
class PASource:
    name: str
    index: int
    description: str
    is_monitor: bool
    monitor_of_sink: str | None


@dataclass(frozen=True)
class PASink:
    name: str
    index: int
    description: str
    owner_module: int | None


@dataclass(frozen=True)
class PAModule:
    index: int
    name: str
    argument: str


@dataclass(frozen=True)
class PAEvent:
    facility: str
    type: str
    index: int


EventCallback = Callable[[PAEvent], None]


def format_module_args(params: dict[str, Any]) -> str:
    parts: list[str] = []
    for key, value in params.items():
        if value is None:
            continue
        text = str(value)
        if any(ch in text for ch in (" ", "\t", '"', "\\", "'")):
            escaped = text.replace("\\", "\\\\").replace('"', '\\"')
            text = f'"{escaped}"'
        parts.append(f"{key}={text}")
    return " ".join(parts)


def format_proplist(props: dict[str, str]) -> str:
    """Format a PulseAudio property list (used for sink_properties argument).

    Values containing spaces, quotes, or proplist separators are wrapped in
    single quotes — that's what pa_proplist_from_string expects.
    """
    parts: list[str] = []
    for key, value in props.items():
        text = str(value)
        if any(ch in text for ch in (" ", "\t", "'", '"', "=", ",")):
            escaped = text.replace("\\", "\\\\").replace("'", "\\'")
            text = f"'{escaped}'"
        parts.append(f"{key}={text}")
    return ",".join(parts)


class PABackend:
    def __init__(self, client_name: str = "audio-spider") -> None:
        self._client_name = client_name
        self._pulse: pulsectl.Pulse | None = None
        self._event_pulse: pulsectl.Pulse | None = None
        self._event_thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._ready_event = threading.Event()
        self._callback: EventCallback | None = None
        self._lock = threading.RLock()

    def connect(self) -> None:
        try:
            self._pulse = pulsectl.Pulse(self._client_name)
        except pulsectl.PulseError as e:
            msg = f"cannot connect to PulseAudio: {e}"
            raise PABackendError(msg) from e

    def close(self) -> None:
        # non-destructive: never unload loaded modules on shutdown
        self._stop_event.set()
        if self._event_pulse is not None:
            with contextlib.suppress(Exception):
                self._event_pulse.event_listen_stop()
        if self._event_thread is not None:
            self._event_thread.join(timeout=2.0)
            self._event_thread = None
        if self._event_pulse is not None:
            with contextlib.suppress(Exception):
                self._event_pulse.close()
            self._event_pulse = None
        if self._pulse is not None:
            with contextlib.suppress(Exception):
                self._pulse.close()
            self._pulse = None

    def _require(self) -> pulsectl.Pulse:
        if self._pulse is None:
            msg = "backend not connected"
            raise PABackendError(msg)
        return self._pulse

    def list_sources(self) -> list[PASource]:
        with self._lock:
            try:
                raws = self._require().source_list()
            except pulsectl.PulseError as e:
                msg = f"source_list failed: {e}"
                raise PABackendError(msg) from e
        result: list[PASource] = []
        for s in raws:
            monitor_name = getattr(s, "monitor_of_sink_name", None)
            result.append(
                PASource(
                    name=s.name,
                    index=s.index,
                    description=s.description or s.name,
                    is_monitor=bool(monitor_name),
                    monitor_of_sink=monitor_name or None,
                )
            )
        return result

    def list_sinks(self) -> list[PASink]:
        with self._lock:
            try:
                raws = self._require().sink_list()
            except pulsectl.PulseError as e:
                msg = f"sink_list failed: {e}"
                raise PABackendError(msg) from e
        result: list[PASink] = []
        for s in raws:
            owner = s.owner_module if s.owner_module >= 0 else None
            result.append(
                PASink(
                    name=s.name,
                    index=s.index,
                    description=s.description or s.name,
                    owner_module=owner,
                )
            )
        return result

    def list_modules(self) -> list[PAModule]:
        with self._lock:
            try:
                raws = self._require().module_list()
            except pulsectl.PulseError as e:
                msg = f"module_list failed: {e}"
                raise PABackendError(msg) from e
        return [PAModule(index=m.index, name=m.name, argument=m.argument or "") for m in raws]

    def load_null_sink(self, name: str, description: str | None = None) -> int:
        params: dict[str, Any] = {"sink_name": name}
        if description:
            params["sink_properties"] = format_proplist(
                {"device.description": description},
            )
        return self._load("module-null-sink", params)

    def load_combine_sink(
        self,
        name: str,
        members: list[str],
        description: str | None = None,
    ) -> int:
        # Empty members are allowed: PA accepts a combine-sink with no
        # slaves, and we add/remove members later by unload+reload (cheap;
        # any loopbacks into the sink survive the brief gap).
        params: dict[str, Any] = {"sink_name": name}
        if members:
            params["slaves"] = ",".join(members)
        if description:
            params["sink_properties"] = format_proplist(
                {"device.description": description},
            )
        return self._load("module-combine-sink", params)

    def load_loopback(
        self,
        source: str,
        sink: str,
        latency_msec: int = 1,
    ) -> int:
        params: dict[str, Any] = {
            "source": source,
            "sink": sink,
            "latency_msec": str(latency_msec),
        }
        return self._load("module-loopback", params)

    def _load(self, module_name: str, params: dict[str, Any]) -> int:
        args = format_module_args(params)
        with self._lock:
            try:
                idx = self._require().module_load(module_name, args)
            except pulsectl.PulseError as e:
                msg = f"failed to load {module_name} ({args}): {e}"
                raise PABackendError(
                    msg,
                ) from e
        if idx is None or idx < 0:
            msg = f"failed to load {module_name} ({args})"
            raise PABackendError(msg)
        return int(idx)

    def unload(self, module_index: int) -> None:
        with self._lock:
            try:
                self._require().module_unload(module_index)
            except pulsectl.PulseError as e:
                msg = f"unload module {module_index} failed: {e}"
                raise PABackendError(
                    msg,
                ) from e

    def subscribe(self, callback: EventCallback, timeout: float = 2.0) -> None:
        if self._event_thread is not None:
            msg = "already subscribed"
            raise PABackendError(msg)
        self._callback = callback
        self._stop_event.clear()
        self._ready_event.clear()
        self._event_thread = threading.Thread(
            target=self._event_loop,
            name="pa-events",
            daemon=True,
        )
        self._event_thread.start()
        if not self._ready_event.wait(timeout):
            msg = "event listener failed to start in time"
            raise PABackendError(msg)

    def _event_loop(self) -> None:
        try:
            self._event_pulse = pulsectl.Pulse(self._client_name + "-events")
            self._event_pulse.event_mask_set("sink", "source", "module")
            self._event_pulse.event_callback_set(self._on_pa_event)
        except Exception:  # noqa: BLE001
            self._ready_event.set()
            return
        self._ready_event.set()
        while not self._stop_event.is_set():
            try:
                self._event_pulse.event_listen(timeout=1.0)
            except pulsectl.PulseError:
                break

    def _on_pa_event(self, event: Any) -> None:
        if self._callback is None or self._stop_event.is_set():
            return
        ev = PAEvent(
            facility=str(event.facility),
            type=str(event.t),
            index=int(event.index),
        )
        cb = self._callback
        GLib.idle_add(self._dispatch, cb, ev)

    @staticmethod
    def _dispatch(cb: EventCallback, ev: PAEvent) -> bool:
        with contextlib.suppress(Exception):
            cb(ev)
        return False
