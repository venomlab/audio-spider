# Audio Spider

GTK3 flow-graph editor for PulseAudio. Visually wire microphones to sinks
and group several speakers into one virtual output.

## Features

- **Speaker Group** — combine several real speakers into a single virtual
  sink (PulseAudio `module-combine-sink`). Apps play to the group; sound
  goes out every member at once.
- **Loopback routing** — drag an arrow from any source (or a sink's
  monitor) to any sink to create a `module-loopback`.
- Live graph view of the current PulseAudio topology — drag nodes to
  rearrange, right-click for per-port actions, middle-button to pan,
  toolbar buttons to zoom.
- Desired state persisted in JSON; reconcile-on-start re-creates missing
  modules without touching anything else (non-destructive — the app never
  unloads modules it didn't load itself in this session).

## Requirements

System packages (Debian/Ubuntu naming):

- `python3-gi`
- `gir1.2-gtk-3.0`
- `gir1.2-goocanvas-2.0`
- `gir1.2-ayatanaappindicator3-0.1` (optional — tray icon)
- `libpulse0` + working PulseAudio (or PipeWire's `pulseaudio` shim)

## Install

### From .deb (Ubuntu 22.04 / 24.04)

```sh
sudo apt install ./audio-spider_0.1.0-1_all.deb
```

APT pulls in `python3-gi`, `gir1.2-gtk-3.0`, `gir1.2-goocanvas-2.0`,
`python3-pulsectl`, `libpulse0` and (recommended)
`gir1.2-ayatanaappindicator3-0.1`. Launches from the application menu;
an XDG autostart entry starts it minimised to tray on next login.

### Build the .deb yourself (no host pollution)

Needs Docker; everything else stays inside a disposable Ubuntu 22.04
container.

```sh
scripts/build-deb.sh    # writes dist/audio-spider_0.1.0-1_all.deb
```

### From source (development)

```sh
uv sync
uv run audio-spider
```

## Config

Lives at `$XDG_CONFIG_HOME/audio_spider/config.json` (defaults to
`~/.config/audio_spider/config.json`). The file is rewritten whenever
state changes; hand-editing is supported — use the "Reload config"
toolbar button afterwards.

## Tests

```sh
uv run pytest
```

GTK-touching tests auto-skip when `$DISPLAY` is unset.
