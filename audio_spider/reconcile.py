from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .config import Config, ConfigModule
from .errors import PABackendError
from .pa_backend import PABackend

ModuleSignature = tuple[Any, ...]


@dataclass
class ReconcileReport:
    created: dict[str, int] = field(default_factory=dict)
    skipped: dict[str, int] = field(default_factory=dict)
    errors: dict[str, str] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.errors

    def id_to_module_index(self) -> dict[str, int]:
        merged: dict[str, int] = {}
        merged.update(self.created)
        merged.update(self.skipped)
        return merged


def parse_module_args(arg_string: str) -> dict[str, str]:
    """Inverse of pa_backend.format_module_args (best-effort).

    PulseAudio echoes module arguments back as a string in the same shape we
    feed in. Parse key=value pairs, supporting "double-quoted" values with
    backslash escapes.
    """
    out: dict[str, str] = {}
    i = 0
    n = len(arg_string)
    while i < n:
        while i < n and arg_string[i].isspace():
            i += 1
        if i >= n:
            break
        key_start = i
        while i < n and arg_string[i] != "=" and not arg_string[i].isspace():
            i += 1
        if i >= n or arg_string[i] != "=":
            break
        key = arg_string[key_start:i]
        i += 1  # consume '='
        if i < n and arg_string[i] == '"':
            i += 1
            chars: list[str] = []
            while i < n:
                ch = arg_string[i]
                if ch == "\\" and i + 1 < n:
                    chars.append(arg_string[i + 1])
                    i += 2
                elif ch == '"':
                    i += 1
                    break
                else:
                    chars.append(ch)
                    i += 1
            value = "".join(chars)
        else:
            val_start = i
            while i < n and not arg_string[i].isspace():
                i += 1
            value = arg_string[val_start:i]
        out[key] = value
    return out


def _signature_from_pa(
    module_name: str, args: dict[str, str]
) -> ModuleSignature | None:
    if module_name == "module-null-sink":
        name = args.get("sink_name")
        return ("null-sink", name) if name else None
    if module_name == "module-combine-sink":
        name = args.get("sink_name")
        if not name:
            return None
        slaves = args.get("slaves", "")
        slave_set = frozenset(s for s in slaves.split(",") if s) if slaves else frozenset()
        return ("combine-sink", name, slave_set)
    if module_name == "module-loopback":
        src = args.get("source")
        snk = args.get("sink")
        return ("loopback", src, snk) if (src and snk) else None
    return None


def _signature_from_config(cm: ConfigModule) -> ModuleSignature | None:
    params = cm.params
    if cm.kind == "null-sink":
        name = params.get("name")
        return ("null-sink", name) if name else None
    if cm.kind == "combine-sink":
        name = params.get("name")
        if not name:
            return None
        members = params.get("members", [])
        return ("combine-sink", name, frozenset(members))
    if cm.kind == "loopback":
        src = params.get("source")
        snk = params.get("sink")
        return ("loopback", src, snk) if (src and snk) else None
    return None


def _load_from_config(pa: PABackend, cm: ConfigModule) -> int:
    params = cm.params
    if cm.kind == "null-sink":
        return pa.load_null_sink(params["name"], params.get("description"))
    if cm.kind == "combine-sink":
        return pa.load_combine_sink(
            params["name"],
            list(params["members"]),
            params.get("description"),
        )
    if cm.kind == "loopback":
        return pa.load_loopback(
            params["source"],
            params["sink"],
            int(params.get("latency_msec", 1)),
        )
    raise PABackendError(f"unknown module kind: {cm.kind}")


def _index_pa_signatures(
    modules: list[Any],
) -> dict[ModuleSignature, int]:
    sig_index: dict[ModuleSignature, int] = {}
    for module in modules:
        args = parse_module_args(module.argument)
        sig = _signature_from_pa(module.name, args)
        if sig is None:
            continue
        sig_index.setdefault(sig, module.index)
    return sig_index


def reconcile(cfg: Config, pa: PABackend) -> ReconcileReport:
    """Bring PulseAudio state into line with the desired config.

    - Loads modules from `cfg` that are not already present (matched by
      signature: kind + identifying params).
    - Records existing-but-matched modules in `skipped`.
    - Per non-destructive policy: never unloads modules. Anything loaded
      outside the config is left alone.
    """
    report = ReconcileReport()
    sig_index = _index_pa_signatures(pa.list_modules())

    for cm in cfg.modules:
        sig = _signature_from_config(cm)
        if sig is None:
            report.errors[cm.id] = "invalid config (missing required params)"
            continue
        if sig in sig_index:
            report.skipped[cm.id] = sig_index[sig]
            continue
        try:
            idx = _load_from_config(pa, cm)
        except (PABackendError, KeyError) as e:
            report.errors[cm.id] = str(e)
            continue
        report.created[cm.id] = idx
        sig_index[sig] = idx
    return report
