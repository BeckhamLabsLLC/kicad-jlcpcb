# kicad-jlcpcb

[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%20%7C%203.11%20%7C%203.12%20%7C%203.13-blue.svg)](#requirements)
[![KiCad 8+](https://img.shields.io/badge/kicad-8.0%2B-informational.svg)](https://www.kicad.org/)
[![Tests](https://img.shields.io/badge/tests-212%20passing-brightgreen.svg)](#testing)

A Claude Code plugin that takes a PCB project from **"I want a board that does X"** to a ready-to-import KiCad file that EasyEDA auto-routes and JLCPCB manufactures with one click.

> **Phase 1.6.** LCSC part sourcing (basic-tier preferred), EasyEDA pin-map auto-fetch, schematic generation, and fully-wired `.kicad_pcb` generation via KiCad's `pcbnew` Python API. Ends with a handoff to EasyEDA's web app for the parts this plugin can't automate reliably: routing and ordering.

## What changed from earlier phases

Earlier releases tried to route the board headlessly with Freerouting and produce a JLCPCB Gerber zip directly. That didn't work for real boards — Freerouting 2.1.0 has CLI bugs, it can't route RF matching networks, and it won't save partial results. Phase 1.6 takes the pragmatic win: **the plugin wires everything up, EasyEDA routes and orders.**

The tradeoff: you need to open the `.kicad_pcb` in EasyEDA's web editor and click two buttons (Auto Route, Order). In exchange you get reliability the open-source tooling can't match, and a one-click path to a JLCPCB order instead of downloading a Gerber zip and uploading it.

## What it does

- **LCSC catalog.** Downloads the full jlcparts mirror (~17 MB) into a local SQLite cache on first use. ~569k parts, queryable in milliseconds with a hard preference for JLCPCB basic-library parts.
- **Part sourcing.** Free-text search, BOM row resolution with cost-impact warnings for extended-tier parts ($3 each for SMT assembly setup).
- **Pin-map auto-fetch.** Queries EasyEDA's component endpoint for any LCSC C-number and returns the pin-name → pad-number map. Rate-limited (~12 s between unique parts) and cached indefinitely in SQLite.
- **PCB generation.** Uses KiCad's `pcbnew` Python API to place real KiCad-stdlib footprints on a three-band grid (connectors / ICs / passives), wire every net pad-to-pad by resolved pin names, and save a valid `.kicad_pcb`.
- **EasyEDA handoff.** Produces the import instructions so the user drags the file into easyeda.com for routing and ordering.
- **Session persistence.** A `.kicad_jlcpcb_session.json` in each project tracks where you left off so `/pcb-new` can resume mid-flow after a Claude Code restart.

## Requirements

- **Python 3.10+** (3.10, 3.11, 3.12, and 3.13 are tested)
- **KiCad 8.0 or newer** — `kicad-cli` on `PATH`, `pcbnew` Python module installable. Verified with KiCad 9.0.8 on Fedora 43.
- **A free EasyEDA account** (for the routing + ordering step at the end)

On Fedora 43: `sudo dnf install kicad`. On Ubuntu 22.04+: `sudo add-apt-repository ppa:kicad/kicad-9.0-releases && sudo apt install kicad`. On macOS: install KiCad from [kicad.org/download](https://www.kicad.org/download/).

## Install

This plugin is distributed via GitHub (not PyPI, not the Claude Code marketplace). Clone it and install locally.

### Option A — system/user Python

```bash
git clone https://github.com/BeckhamLabsLLC/kicad-jlcpcb.git
cd kicad-jlcpcb
pip install -e .
```

This installs the `kicad-jlcpcb` entry-point script onto your `PATH`. `.mcp.json` calls that script directly.

### Option B — virtualenv (preferred for sandboxing)

```bash
git clone https://github.com/BeckhamLabsLLC/kicad-jlcpcb.git
cd kicad-jlcpcb
python -m venv .venv
source .venv/bin/activate       # Windows: .\.venv\Scripts\activate
pip install -e ".[dev]"
```

Then edit `.mcp.json` in the plugin root so `"command"` points at the venv's entry-point:

```json
{
  "mcpServers": {
    "kicad-jlcpcb": {
      "command": "/abs/path/to/kicad-jlcpcb/.venv/bin/kicad-jlcpcb",
      "args": []
    }
  }
}
```

### Register with Claude Code

Use a local marketplace that points at the cloned directory:

```
/plugin marketplace add /abs/path/to/kicad-jlcpcb
/plugin install kicad-jlcpcb@local
```

Restart Claude Code afterward so the MCP server registers.

## Quick-start: build your first PCB in five minutes

Pick a room-temperature idea. Something small — an ESP32-C3 board with one sensor, a USB-C port, and an LDO is ideal:

```
/pcb-new An ESP32-C3 soil-moisture sensor with two capacitive probes,
         USB-C 5V in, a 3.3V LDO, status LED, and JST-PH battery header.
         Place it on an 80x60 mm board.
```

Claude drives the workflow: `detect_kicad` → `create_project` → parts sourcing → **BOM checkpoint** (you confirm or swap extended parts) → `pcb_generate` → `easyeda_handoff`.

The full trace for this example — including every tool call, expected timing, and the JSON the plugin returns — is in [`examples/soilnode-esp32/walkthrough.md`](examples/soilnode-esp32/walkthrough.md).

First run takes ~90 s (17 MB catalog download + 12 s EasyEDA pin-map fetch per unique IC). After that, runs on similar designs are under 10 s.

## Commands

- **`/pcb-new <description>`** — start a fresh project from a text description. Drives detection → project create → part sourcing → BOM checkpoint → pin-map fetch → `.kicad_pcb` generation → EasyEDA handoff. Offers to resume if a `.kicad_jlcpcb_session.json` already exists at the target path.
- **`/pcb-from-bom <path-to-csv>`** — start from an LCSC BOM CSV and optional design-intent notes. Skips part sourcing and goes straight to pin-map fetch + generation.

## MCP tool surface

| Stage | Tool | Purpose |
|---|---|---|
| Setup | `detect_kicad` | Probe `kicad-cli` version |
| Setup | `create_project` | Scaffold `.kicad_pro` + subdirs + session file |
| Setup | `load_project` | Validate an existing `.kicad_pro`; surfaces resumable session state |
| Resume | `session_resume` | Report where a prior workflow left off for a given project dir |
| Sourcing | `lcsc_search` | Free-text part search, basic-only by default |
| Sourcing | `lcsc_resolve_bom` | Batch BOM resolution with cost-impact warnings |
| Sourcing | `fetch_part_library` | Placeholder symbol/footprint fetch into project `libs/` |
| Pin maps | `part_pin_map` | Fetch pin-name → pad-number map from EasyEDA |
| Schematic | `sch_generate` | Emit `.kicad_sch` from a netlist spec |
| Schematic | `sch_run_erc` | Run `kicad-cli sch erc` and parse the report |
| PCB | **`pcb_generate`** | **Main tool.** Auto-fetches pin maps, places footprints, wires every net, saves `.kicad_pcb` |
| Terminal | **`easyeda_handoff`** | **Recommended terminal tool.** Produces EasyEDA import instructions |
| Legacy | `package_for_jlcpcb` | For users routing in KiCad: export Gerbers + package a JLCPCB upload zip |

## The PCB spec format

`pcb_generate` consumes a dict with this shape:

```json
{
  "name": "demo",
  "board": {"width_mm": 80, "height_mm": 60, "layer_count": 2},
  "components": [
    {
      "ref": "U1",
      "value": "ESP32-C3-WROOM-02",
      "lcsc": "C2934560",
      "lib": "RF_Module",
      "fp": "ESP32-C3-WROOM-02"
    }
  ],
  "nets": {
    "3V3": [["U1", "3V3"], ["C1", "1"]],
    "GND": [["U1", "GND"], ["C1", "2"]],
    "SPI_SCK": [["U1", "GPIO10"], ["U2", "SCK"]]
  }
}
```

Reference IC pins by **name**, not number. The plugin auto-fetches pin maps from EasyEDA for any component with an `lcsc` field, so `["U1", "GPIO10"]` resolves to the real datasheet pad number at generation time. For passives (R, C, L, D) use bare pad numbers `"1"` and `"2"`.

Full worked example in [`examples/soilnode-esp32/spec.json`](examples/soilnode-esp32/spec.json).

## Architecture

```
src/kicad_jlcpcb_mcp/
  server.py            # MCP server + 13 tool definitions
  config.py            # module-level constants
  project.py           # .kicad_pro create/load/validate
  session.py           # per-project session persistence
  kicad_cli.py         # async wrapper for kicad-cli (KiCad 9 compatible)
  sexpr.py             # s-expression reader/writer
  schematic.py         # netlist spec → .kicad_sch
  lcsc_client.py       # jlcparts mirror + SQLite cache + basic-tier filter
  part_library.py      # EasyEDA client: rate-limited pin maps + symbol/footprint gen
                       #   - EasyEdaRateLimiter class enforces the 12s cooldown
                       #   - SQLite cache indefinitely persists pin maps
  pcb.py               # pcbnew-based .kicad_pcb generator
  gerber_pack.py       # KiCad 8/9 Protel extension normalizer + JLCPCB zip
```

All HTTP goes through `lcsc_client` and `part_library`. All KiCad CLI calls go through `kicad_cli`. PCB construction via `pcbnew.BOARD()` and friends — no `import pcbnew` at module level (lazy-imported in `pcb.py` so the rest of the plugin runs when KiCad isn't installed).

## Testing

```bash
PYTHONPATH=src pytest tests/
```

With KiCad installed:

```bash
KICAD_INSTALLED=1 PYTHONPATH=src pytest tests/ -v
```

**212 tests** covering subprocess wrapping, HTTP client, SQLite cache, s-expression round-trip, schematic generation, Gerber renaming, EasyEDA pin-map parsing, rate-limit + retry path, session persistence, MCP server routing, and real `pcbnew` board generation (gated by `KICAD_INSTALLED=1`).

## Troubleshooting

Common problems and resolutions: [`TROUBLESHOOTING.md`](TROUBLESHOOTING.md).

Highlights:

- **`kicad-jlcpcb` command not found** → `pip install -e .` from the cloned directory.
- **First run stalls 12 s** → EasyEDA rate limit; expected, caches forever.
- **`Footprint not found`** → look up the exact name under `/usr/share/kicad/footprints/`.
- **Session offers to resume when you want a fresh board** → delete `.kicad_jlcpcb_session.json` or pick a new project name.

## Known limitations

- **Headless auto-routing is not in scope.** Freerouting 2.1.0 can't route real RF boards via its CLI reliably. The plugin delegates routing to EasyEDA's cloud auto-router, which works.
- **Pin-map fetches are rate-limited.** First run of `pcb_generate` on fresh ICs pauses ~12 s per unique IC (EasyEDA anti-bot). Cached forever after first fetch.
- **EasyEDA library coverage isn't universal.** Some LCSC parts don't have symbol data in EasyEDA. Provide an explicit `"pinmap"` field in the component spec for those.
- **KiCad stdlib footprint naming varies.** If an IC's footprint isn't in the skill's reference table, `ls /usr/share/kicad/footprints/<Library>.pretty/` and pick the closest match.
- **Auto-placement is a three-band grid.** Connectors on top, ICs in the middle, passives below. You'll rearrange things in EasyEDA before routing. The plugin's job is connectivity, not aesthetics.

## Roadmap

- **Phase 2** — auto-placement that respects functional groupings (power domain, RF block, analog front-end), DRC integration, differential-pair awareness.
- **Phase 3** — vision-based schematic extraction (drop in a photo of a hand-drawn schematic, out comes a wired `.kicad_pcb`).

## Contributing

See [`CONTRIBUTING.md`](CONTRIBUTING.md) for dev setup, test running, and PR conventions. All contributors follow the [Code of Conduct](CODE_OF_CONDUCT.md).

## License

MIT — see [`LICENSE`](LICENSE).
