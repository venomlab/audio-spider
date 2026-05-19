from __future__ import annotations

import contextlib
import json
from typing import TYPE_CHECKING, Any

import pytest
from click.testing import CliRunner

from audio_spider.cli import main

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def _write_config(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload))


def test_cli_help(runner: CliRunner) -> None:
    result = runner.invoke(main, ["--help"])
    assert result.exit_code == 0
    assert "--headless" in result.output
    assert "--no-tray" in result.output
    assert "--minimized" in result.output


def test_cli_rejects_bad_config(tmp_path: Path, runner: CliRunner) -> None:
    bad = tmp_path / "config.json"
    bad.write_text("{not json")
    result = runner.invoke(main, ["--config", str(bad), "--headless"])
    assert result.exit_code == 2
    assert "config error" in result.output


def test_cli_rejects_future_version(tmp_path: Path, runner: CliRunner) -> None:
    bad = tmp_path / "config.json"
    _write_config(bad, {"version": 999})
    result = runner.invoke(main, ["--config", str(bad), "--headless"])
    assert result.exit_code == 2


def test_cli_headless_empty_config(tmp_path: Path, runner: CliRunner) -> None:
    """Smoke: headless with an empty config completes without touching PA modules."""
    pytest.importorskip("pulsectl")
    from audio_spider.errors import PABackendError
    from audio_spider.pa_backend import PABackend

    probe = PABackend(client_name="audio-spider-probe")
    try:
        probe.connect()
        probe.close()
    except PABackendError:
        pytest.skip("PulseAudio not reachable")

    cfg_path = tmp_path / "config.json"
    _write_config(cfg_path, {"version": 1, "modules": []})

    result = runner.invoke(main, ["--config", str(cfg_path), "--headless"])
    assert result.exit_code == 0


def test_cli_headless_loads_null_sink(tmp_path: Path, runner: CliRunner) -> None:
    """End-to-end headless: configure a null-sink, ensure it's loaded after run."""
    from audio_spider.errors import PABackendError
    from audio_spider.pa_backend import PABackend

    probe = PABackend(client_name="audio-spider-probe")
    try:
        probe.connect()
    except PABackendError:
        pytest.skip("PulseAudio not reachable")

    sink_name = "audio_spider_cli_test"
    cfg_path = tmp_path / "config.json"
    _write_config(
        cfg_path,
        {
            "version": 1,
            "modules": [
                {"id": sink_name, "kind": "null-sink", "params": {"name": sink_name, "description": "CLI test"}},
            ],
        },
    )

    # find any pre-existing module with this sink_name (unlikely) and ignore
    try:
        result = runner.invoke(main, ["--config", str(cfg_path), "--headless", "-v"])
        assert result.exit_code == 0, result.output

        sinks = {s.name: s for s in probe.list_sinks()}
        assert sink_name in sinks, f"sink not loaded; sinks={list(sinks)}"
        assert sinks[sink_name].description == "CLI test"

        # idempotency: a second run should skip (not duplicate)
        result2 = runner.invoke(main, ["--config", str(cfg_path), "--headless", "-v"])
        assert result2.exit_code == 0
        sinks2 = [s for s in probe.list_sinks() if s.name == sink_name]
        assert len(sinks2) == 1
    finally:
        # cleanup: unload anything we created
        for m in probe.list_modules():
            if m.name == "module-null-sink" and f"sink_name={sink_name}" in m.argument:
                with contextlib.suppress(PABackendError):
                    probe.unload(m.index)
        probe.close()
