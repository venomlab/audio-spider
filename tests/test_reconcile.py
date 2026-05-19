from __future__ import annotations

from typing import Any

import pytest

from audio_spider.config import Config, ConfigModule
from audio_spider.errors import PABackendError
from audio_spider.pa_backend import PAModule
from audio_spider.reconcile import (
    parse_module_args,
    reconcile,
)


class FakePA:
    """In-memory stand-in for PABackend with the surface reconcile uses."""

    def __init__(self, modules: list[PAModule] | None = None) -> None:
        self._modules = list(modules or [])
        self._next_idx = max((m.index for m in self._modules), default=-1) + 1
        self.calls: list[tuple[str, tuple, dict]] = []
        self.fail_on: set[str] = set()  # config-module names that should fail

    def list_modules(self) -> list[PAModule]:
        return list(self._modules)

    def load_null_sink(self, name: str, description: str | None = None) -> int:
        self.calls.append(("null-sink", (name,), {"description": description}))
        if name in self.fail_on:
            raise PABackendError(f"simulated failure: {name}")
        return self._record(
            "module-null-sink",
            f"sink_name={name}" + (
                f' sink_properties=device.description={description}'
                if description else ""
            ),
        )

    def load_combine_sink(
        self, name: str, members: list[str], description: str | None = None,
    ) -> int:
        self.calls.append(
            ("combine-sink", (name, tuple(members)), {"description": description})
        )
        if name in self.fail_on:
            raise PABackendError(f"simulated failure: {name}")
        return self._record(
            "module-combine-sink",
            f"sink_name={name} slaves={','.join(members)}",
        )

    def load_loopback(self, source: str, sink: str, latency_msec: int = 1) -> int:
        self.calls.append(
            ("loopback", (source, sink), {"latency_msec": latency_msec})
        )
        if f"loopback:{source}:{sink}" in self.fail_on:
            raise PABackendError("simulated failure")
        return self._record(
            "module-loopback",
            f"source={source} sink={sink} latency_msec={latency_msec}",
        )

    def _record(self, module_name: str, args: str) -> int:
        idx = self._next_idx
        self._next_idx += 1
        self._modules.append(PAModule(index=idx, name=module_name, argument=args))
        return idx


class TestParseModuleArgs:
    def test_bareword_value(self):
        assert parse_module_args("sink_name=vmic") == {"sink_name": "vmic"}

    def test_multiple_pairs(self):
        assert parse_module_args("source=mic sink=vmic") == {
            "source": "mic", "sink": "vmic",
        }

    def test_quoted_value_with_spaces(self):
        assert parse_module_args('description="Audio Spider"') == {
            "description": "Audio Spider",
        }

    def test_quoted_value_with_escaped_quote(self):
        assert parse_module_args(r'desc="has \"q\""') == {"desc": 'has "q"'}

    def test_mixed_quoted_and_bare(self):
        assert parse_module_args('sink_name=v sink_properties="device.description=\'Hi\'"') == {
            "sink_name": "v",
            "sink_properties": "device.description='Hi'",
        }

    def test_empty_input(self):
        assert parse_module_args("") == {}

    def test_extra_whitespace(self):
        assert parse_module_args("  a=1   b=2  ") == {"a": "1", "b": "2"}


class TestReconcile:
    def test_loads_missing_null_sink(self):
        pa = FakePA()
        cfg = Config(modules=[
            ConfigModule(id="vmic1", kind="null-sink",
                         params={"name": "vmic1"}),
        ])
        report = reconcile(cfg, pa)
        assert report.created == {"vmic1": 0}
        assert report.skipped == {}
        assert report.errors == {}
        assert report.ok

    def test_skips_existing_null_sink(self):
        pa = FakePA(modules=[
            PAModule(index=5, name="module-null-sink", argument="sink_name=vmic1"),
        ])
        cfg = Config(modules=[
            ConfigModule(id="vmic1", kind="null-sink",
                         params={"name": "vmic1"}),
        ])
        report = reconcile(cfg, pa)
        assert report.skipped == {"vmic1": 5}
        assert report.created == {}
        assert pa.calls == []  # no load was triggered

    def test_skips_existing_loopback(self):
        pa = FakePA(modules=[
            PAModule(index=7, name="module-loopback",
                     argument="source=mic_a sink=vmic1 latency_msec=5"),
        ])
        cfg = Config(modules=[
            ConfigModule(id="lb1", kind="loopback",
                         params={"source": "mic_a", "sink": "vmic1"}),
        ])
        report = reconcile(cfg, pa)
        assert report.skipped == {"lb1": 7}

    def test_signature_match_for_combine_sink_order_insensitive(self):
        pa = FakePA(modules=[
            PAModule(index=3, name="module-combine-sink",
                     argument="sink_name=split slaves=b,a"),
        ])
        cfg = Config(modules=[
            ConfigModule(id="split", kind="combine-sink",
                         params={"name": "split", "members": ["a", "b"]}),
        ])
        report = reconcile(cfg, pa)
        assert report.skipped == {"split": 3}

    def test_leaves_extras_alone(self):
        # PA has an unrelated module that's not in cfg → reconcile must NOT touch it
        pa = FakePA(modules=[
            PAModule(index=1, name="module-null-sink",
                     argument="sink_name=someone_elses_sink"),
        ])
        cfg = Config(modules=[
            ConfigModule(id="mine", kind="null-sink",
                         params={"name": "my_sink"}),
        ])
        report = reconcile(cfg, pa)
        # extra module 1 still there
        names = {m.argument for m in pa.list_modules()}
        assert "sink_name=someone_elses_sink" in names
        assert report.created == {"mine": 2}

    def test_records_error_on_load_failure(self):
        pa = FakePA()
        pa.fail_on = {"broken"}
        cfg = Config(modules=[
            ConfigModule(id="vmic1", kind="null-sink",
                         params={"name": "broken"}),
            ConfigModule(id="vmic2", kind="null-sink",
                         params={"name": "ok"}),
        ])
        report = reconcile(cfg, pa)
        assert "vmic1" in report.errors
        assert report.created == {"vmic2": 0}
        assert not report.ok

    def test_invalid_config_module_recorded_as_error(self):
        pa = FakePA()
        cfg = Config(modules=[
            ConfigModule(id="bad", kind="null-sink", params={}),  # no `name`
        ])
        report = reconcile(cfg, pa)
        assert "bad" in report.errors
        assert "missing required" in report.errors["bad"]
        assert pa.calls == []

    def test_unknown_kind_is_error_via_signature(self):
        pa = FakePA()
        cfg = Config(modules=[
            ConfigModule(id="x", kind="not-a-real-kind", params={"name": "x"}),
        ])
        report = reconcile(cfg, pa)
        assert "x" in report.errors

    def test_id_to_module_index_merges_created_and_skipped(self):
        pa = FakePA(modules=[
            PAModule(index=2, name="module-null-sink", argument="sink_name=a"),
        ])
        cfg = Config(modules=[
            ConfigModule(id="a", kind="null-sink", params={"name": "a"}),
            ConfigModule(id="b", kind="null-sink", params={"name": "b"}),
        ])
        report = reconcile(cfg, pa)
        mapping = report.id_to_module_index()
        assert mapping["a"] == 2
        assert "b" in mapping
        assert mapping["b"] != 2

    def test_subsequent_entries_see_just_loaded_modules(self):
        """If two cfg entries have the same signature, the second skips."""
        pa = FakePA()
        cfg = Config(modules=[
            ConfigModule(id="first", kind="null-sink", params={"name": "shared"}),
            ConfigModule(id="dup", kind="null-sink", params={"name": "shared"}),
        ])
        report = reconcile(cfg, pa)
        assert "first" in report.created
        assert "dup" in report.skipped
        assert report.created["first"] == report.skipped["dup"]
