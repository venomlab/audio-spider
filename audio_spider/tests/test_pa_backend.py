"""Live PulseAudio integration tests.

Auto-skip when PA isn't reachable (handled by the `live_pa` fixture).
Each mutating test cleans up the modules it loaded; on failure pytest's
`addfinalizer` ensures unload still runs so the system stays clean.
"""
from __future__ import annotations

import time

import pytest
from gi.repository import GLib

from audio_spider.errors import PABackendError
from audio_spider.pa_backend import PABackend


SINK_NAME = "audio_spider_test_vsink"
SINK_DESC = "Audio Spider Test"


def _drain(timeout_s: float = 1.0) -> None:
    """Pump the GLib main context so subscribed events get delivered."""
    ctx = GLib.MainContext.default()
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        ctx.iteration(False)
        time.sleep(0.02)


def _safe_unload(pa: PABackend, idx: int) -> None:
    try:
        pa.unload(idx)
    except PABackendError:
        pass


def test_list_sinks_returns_at_least_one(live_pa: PABackend):
    sinks = live_pa.list_sinks()
    assert sinks, "expected at least one sink on the system"
    assert all(s.name for s in sinks)


def test_list_sources_marks_monitors(live_pa: PABackend):
    sources = live_pa.list_sources()
    assert sources
    monitors = [s for s in sources if s.is_monitor]
    assert monitors, "expected at least one monitor source"
    for m in monitors:
        assert m.monitor_of_sink is not None


def test_list_modules_includes_native_protocol(live_pa: PABackend):
    mods = live_pa.list_modules()
    assert any(m.name.startswith("module-native-protocol") for m in mods)


def test_load_unload_null_sink(live_pa: PABackend, request: pytest.FixtureRequest):
    idx = live_pa.load_null_sink(SINK_NAME, SINK_DESC)
    request.addfinalizer(lambda: _safe_unload(live_pa, idx))
    sinks = {s.name: s for s in live_pa.list_sinks()}
    assert SINK_NAME in sinks
    assert sinks[SINK_NAME].description == SINK_DESC
    live_pa.unload(idx)
    assert SINK_NAME not in {s.name for s in live_pa.list_sinks()}


def test_null_sink_creates_monitor_source(
    live_pa: PABackend, request: pytest.FixtureRequest,
):
    idx = live_pa.load_null_sink(SINK_NAME)
    request.addfinalizer(lambda: _safe_unload(live_pa, idx))
    monitor = f"{SINK_NAME}.monitor"
    assert monitor in {s.name for s in live_pa.list_sources()}


def test_loopback_into_null_sink(
    live_pa: PABackend, request: pytest.FixtureRequest,
):
    sink_idx = live_pa.load_null_sink(SINK_NAME)
    request.addfinalizer(lambda: _safe_unload(live_pa, sink_idx))

    hw_sources = [s for s in live_pa.list_sources() if not s.is_monitor]
    if not hw_sources:
        pytest.skip("no non-monitor source available")

    lb_idx = live_pa.load_loopback(hw_sources[0].name, SINK_NAME, latency_msec=5)
    request.addfinalizer(lambda: _safe_unload(live_pa, lb_idx))

    mods = {m.index: m for m in live_pa.list_modules()}
    assert lb_idx in mods
    assert mods[lb_idx].name == "module-loopback"


def test_combine_sink_loads_without_members(
    live_pa: PABackend, request: pytest.FixtureRequest,
):
    """PA accepts a combine-sink with no slaves; we add them later."""
    idx = live_pa.load_combine_sink("audio_spider_combine_empty", [])
    request.addfinalizer(lambda: _safe_unload(live_pa, idx))
    sinks = {s.name for s in live_pa.list_sinks()}
    assert "audio_spider_combine_empty" in sinks
    mod = next(m for m in live_pa.list_modules() if m.index == idx)
    assert "slaves=" not in mod.argument


def test_combine_sink_with_existing_sink(
    live_pa: PABackend, request: pytest.FixtureRequest,
):
    # use an existing real sink as the single member
    hw_sinks = [
        s for s in live_pa.list_sinks() if s.owner_module is None
    ] or live_pa.list_sinks()
    member = hw_sinks[0].name
    idx = live_pa.load_combine_sink(
        "audio_spider_test_combine", [member], description="Test Combine",
    )
    request.addfinalizer(lambda: _safe_unload(live_pa, idx))
    sinks = {s.name: s for s in live_pa.list_sinks()}
    assert "audio_spider_test_combine" in sinks
    assert sinks["audio_spider_test_combine"].description == "Test Combine"


def test_unload_invalid_index_raises(live_pa: PABackend):
    with pytest.raises(PABackendError, match="unload module"):
        live_pa.unload(999_999)


def test_subscribe_receives_load_and_unload_events(
    live_pa: PABackend, request: pytest.FixtureRequest,
):
    events: list[tuple[str, str, int]] = []
    live_pa.subscribe(
        lambda ev: events.append((ev.facility, ev.type, ev.index))
    )

    idx = live_pa.load_null_sink(SINK_NAME)
    request.addfinalizer(lambda: _safe_unload(live_pa, idx))
    _drain()
    live_pa.unload(idx)
    _drain()

    module_events = [(f, t, i) for f, t, i in events if i == idx]
    assert any("new" in t for _, t, _ in module_events), \
        f"no 'new' event for module {idx}: {events}"
    assert any("remove" in t for _, t, _ in module_events), \
        f"no 'remove' event for module {idx}: {events}"


def test_double_subscribe_raises(live_pa: PABackend):
    live_pa.subscribe(lambda _: None)
    with pytest.raises(PABackendError, match="already subscribed"):
        live_pa.subscribe(lambda _: None)
