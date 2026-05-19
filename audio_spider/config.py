from __future__ import annotations

import contextlib
import json
import os
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from audio_spider.errors import ConfigError

CONFIG_VERSION = 1
APP_NAME = "audio_spider"


@dataclass
class ConfigModule:
    id: str
    kind: str  # "null-sink" | "combine-sink" | "loopback"
    params: dict[str, Any] = field(default_factory=dict)


@dataclass
class WindowState:
    w: int = 1200
    h: int = 800
    start_minimized: bool = False


@dataclass
class Config:
    version: int = CONFIG_VERSION
    modules: list[ConfigModule] = field(default_factory=list)
    layout: dict[str, dict[str, float]] = field(default_factory=dict)
    window: WindowState = field(default_factory=WindowState)

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "modules": [asdict(m) for m in self.modules],
            "layout": self.layout,
            "window": asdict(self.window),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Config:
        version = data.get("version", 1)
        if not isinstance(version, int) or version > CONFIG_VERSION:
            msg = f"unsupported config version: {version!r} (max {CONFIG_VERSION})"
            raise ConfigError(
                msg,
            )
        try:
            modules = [
                ConfigModule(
                    id=m["id"],
                    kind=m["kind"],
                    params=dict(m.get("params", {})),
                )
                for m in data.get("modules", [])
                if m.get("kind") != "virtual-mic"
            ]
        except (KeyError, TypeError) as e:
            msg = f"malformed module entry: {e}"
            raise ConfigError(msg) from e
        layout = {
            str(k): {"x": float(v["x"]), "y": float(v["y"])}
            for k, v in data.get("layout", {}).items()
            if isinstance(v, dict) and "x" in v and "y" in v
        }
        window = WindowState(**data.get("window", {}))
        return cls(version=version, modules=modules, layout=layout, window=window)


def default_config_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME")
    root = Path(base) if base else Path.home() / ".config"
    return root / APP_NAME / "config.json"


def load(path: Path | None = None) -> Config:
    path = path or default_config_path()
    if not path.exists():
        return Config()
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as e:
        msg = f"cannot read {path}: {e}"
        raise ConfigError(msg) from e
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        msg = f"malformed JSON in {path}: {e}"
        raise ConfigError(msg) from e
    if not isinstance(data, dict):
        msg = f"config root must be object in {path}, got {type(data).__name__}"
        raise ConfigError(
            msg,
        )
    return Config.from_dict(data)


def save(cfg: Config, path: Path | None = None) -> None:
    path = path or default_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(cfg.to_dict(), indent=2, ensure_ascii=False)
    fd, tmp_name = tempfile.mkstemp(
        prefix=".config-",
        suffix=".json",
        dir=path.parent,
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(payload)
        os.replace(tmp_name, path)
    except Exception:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp_name)
        raise
