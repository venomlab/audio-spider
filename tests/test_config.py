from __future__ import annotations

import json
from pathlib import Path

import pytest

from audio_spider import config
from audio_spider.config import Config, ConfigModule, WindowState
from audio_spider.errors import ConfigError


def test_round_trip_preserves_fields() -> None:
    cfg = Config(
        modules=[
            ConfigModule(id="vmic1", kind="null-sink", params={"name": "vmic1", "description": "Virtual"}),
            ConfigModule(id="lb1", kind="loopback", params={"source": "mic", "sink": "vmic1"}),
        ],
        layout={
            "vmic1": {"x": 100.0, "y": 50.0},
            "mic": {"x": 10.0, "y": 20.0},
        },
        window=WindowState(w=1024, h=768, start_minimized=True),
    )
    restored = Config.from_dict(cfg.to_dict())
    assert restored == cfg


def test_load_returns_default_when_missing(tmp_path: Path) -> None:
    cfg = config.load(tmp_path / "absent.json")
    assert cfg == Config()


def test_save_then_load(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    cfg = Config(
        modules=[ConfigModule(id="x", kind="null-sink", params={"name": "x"})],
        layout={"x": {"x": 1.0, "y": 2.0}},
    )
    config.save(cfg, path)
    assert path.exists()
    assert json.loads(path.read_text())["version"] == 1
    restored = config.load(path)
    assert restored == cfg


def test_save_is_atomic(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    config.save(Config(), path)
    # No leftover tempfile in the directory after save
    leftovers = [p for p in tmp_path.iterdir() if p.name != "config.json"]
    assert leftovers == []


def test_load_rejects_future_version(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"version": 999}))
    with pytest.raises(ConfigError, match="unsupported config version"):
        config.load(path)


def test_load_rejects_non_object_root(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    path.write_text("[]")
    with pytest.raises(ConfigError, match="must be object"):
        config.load(path)


def test_load_rejects_malformed_json(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    path.write_text("{not json")
    with pytest.raises(ConfigError, match="malformed JSON"):
        config.load(path)


def test_default_config_path_respects_xdg(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert config.default_config_path() == tmp_path / "audio_spider" / "config.json"


def test_default_config_path_falls_back_to_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    assert config.default_config_path() == tmp_path / ".config" / "audio_spider" / "config.json"


def test_layout_ignores_partial_entries() -> None:
    data = {
        "version": 1,
        "layout": {
            "good": {"x": 1.0, "y": 2.0},
            "no_y": {"x": 1.0},
            "not_a_dict": "broken",
        },
    }
    cfg = Config.from_dict(data)
    assert cfg.layout == {"good": {"x": 1.0, "y": 2.0}}
