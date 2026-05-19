from __future__ import annotations

import logging
import sys
from pathlib import Path

import click

from audio_spider import config as config_module
from audio_spider.controller import Controller
from audio_spider.errors import AudioSpiderError
from audio_spider.graph_model import GraphModel
from audio_spider.pa_backend import PABackend
from audio_spider.reconcile import ReconcileReport, reconcile

log = logging.getLogger("audio_spider")


@click.command()
@click.option("--headless", is_flag=True, help="Apply config and exit (for autostart/systemd-user).")
@click.option("--no-tray", is_flag=True, help="Quit on window close instead of hiding to tray.")
@click.option("--minimized", is_flag=True, help="Start minimized to tray.")
@click.option(
    "--config",
    "config_path",
    type=click.Path(path_type=Path, dir_okay=False),  # type: ignore[type-var]
    default=None,
    help="Override config path (default: $XDG_CONFIG_HOME/audio_spider/config.json).",
)
@click.option("-v", "--verbose", count=True, help="Increase log verbosity (-v info, -vv debug).")
def main(
    headless: bool,
    no_tray: bool,
    minimized: bool,
    config_path: Path | None,
    verbose: int,
) -> None:
    """audio_spider — manipulate PulseAudio virtual devices via a flow-graph."""
    _setup_logging(verbose)

    try:
        cfg = config_module.load(config_path)
    except AudioSpiderError as e:
        click.echo(f"config error: {e}", err=True)
        sys.exit(2)

    pa = PABackend()
    try:
        pa.connect()
    except AudioSpiderError as e:
        click.echo(f"pulseaudio error: {e}", err=True)
        sys.exit(3)

    try:
        if headless:
            report = reconcile(cfg, pa)
            _log_report(report)
            sys.exit(0 if report.ok else 1)
        # GUI path: controller drives reconcile + sync + event subscription
        model = GraphModel()
        controller = Controller(pa, cfg, model, config_path=config_path)
        report = controller.initial_sync()
        _log_report(report)
        from audio_spider.app import run_gui

        rc = run_gui(controller, model, cfg, no_tray=no_tray, minimized=minimized)
        # persist final window size on clean exit
        try:
            config_module.save(cfg, config_path)
        except OSError as e:
            log.warning("failed to save config on exit: %s", e)
        sys.exit(rc)
    finally:
        pa.close()  # non-destructive: leaves modules loaded


def _setup_logging(verbose: int) -> None:
    if verbose >= 2:
        level = logging.DEBUG
    elif verbose == 1:
        level = logging.INFO
    else:
        level = logging.WARNING
    logging.basicConfig(
        level=level,
        format="%(levelname)s %(name)s: %(message)s",
    )


def _log_report(report: ReconcileReport) -> None:
    for cm_id, idx in report.created.items():
        log.info("created %s -> module #%d", cm_id, idx)
    for cm_id, idx in report.skipped.items():
        log.info("already present: %s (module #%d)", cm_id, idx)
    for cm_id, err in report.errors.items():
        log.error("failed %s: %s", cm_id, err)
