# kicad-jlcpcb

[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%20%7C%203.11%20%7C%203.12%20%7C%203.13-blue.svg)](#requirements)
[![KiCad 8+](https://img.shields.io/badge/kicad-8.0%2B-informational.svg)](https://www.kicad.org/)
[![Tests](https://img.shields.io/badge/tests-212%20passing-brightgreen.svg)](#testing)
[![MCP](https://img.shields.io/badge/MCP-server-8A2BE2.svg)](https://modelcontextprotocol.io/)

> From "I want a board that does X" to a wired `.kicad_pcb` EasyEDA can auto-route and JLCPCB can build — in a single Claude Code conversation.

`kicad-jlcpcb` is a Claude Code plugin + MCP server that automates the tedious half of going from idea to fab. It sources LCSC parts with a hard preference for JLCPCB basic-library stock, auto-fetches pin maps from EasyEDA, places KiCad-stdlib footprints, wires every net by pin **name** (not pad number), and hands off a `.kicad_pcb` that EasyEDA can route and order in two clicks.

```text
┌─ you ──────────────────────────────────┐
│  /pcb-new An ESP32-C3 soil-moisture    │
│          sensor, USB-C, 3.3V LDO...    │
└────────────────┬───────────────────────┘
                 │
   ┌─────────────▼─────────────┐
   │  kicad-jlcpcb MCP server  │
   │  • source parts (LCSC)    │
   │  • fetch pin maps         │
   │  • place + wire footprints│
   │  • save .kicad_pcb        │
   └─────────────┬─────────────┘
                 │
       drag into easyeda.com
                 │
             Auto Route
                 │
          Order via JLCPCB
```

---

## Why this plugin

Three recurring friction points in small-batch PCB work, automated:

1. **"Is this part basic or extended on JLCPCB?"** — You stop needing to cross-reference LCSC's UI. The plugin's SQLite-cached jlcparts mirror answers in milliseconds and always prefers basic-tier (no $3/part assembly setup fee).
2. **"What's the right pad number for this IC's `GPIO10`?"** — You stop reading datasheets to build netlists. The plugin queries EasyEDA by LCSC C-number, caches the pin-name → pad-number map, and lets you reference pins by their functional names.
3. **"Why is my auto-router failing?"** — You stop fighting Freerouting on RF boards. The plugin stops at "wired `.kicad_pcb`" and hands off to EasyEDA's cloud auto-router, which works on real designs.

---

## Quick tour

- **Two slash commands**: `/pcb-new` (from a description) and `/pcb-from-bom` (from a CSV).
- **One agent**: `part-sourcer` — finds the best JLCPCB-stocked part for a generic spec.
- **One skill**: `kicad-jlcpcb-workflow` — the full reference the LLM consults while driving the workflow.
- **13 MCP tools** covering setup, sourcing, schematic, PCB generation, EasyEDA handoff, and session resume.
- **Session persistence** — each project writes a `.kicad_jlcpcb_session.json` so `/pcb-new` can resume mid-flow after a Claude Code restart.

---

## Requirements

| Component | Version | Notes |
|---|---|---|
| Python | 3.10 – 3.13 | Tested on all four |
| KiCad | 8.0+ | `kicad-cli` on `PATH`, `pcbnew` Python bindings for `pcb_generate` |
| EasyEDA account | free | Only needed for the final routing + ordering step |

Install KiCad:

- **Fedora 40+:** `sudo dnf install kicad`
- **Ubuntu 22.04+:** `sudo add-apt-repository ppa:kicad/kicad-9.0-releases && sudo apt install kicad`
- **Arch:** `sudo pacman -Syu kicad`
- **macOS:** [kicad.org/download](https://www.kicad.org/download/)

---

## Install

Distributed via GitHub only — no PyPI, no marketplace. Clone and install locally.

### 1. Clone + install (virtualenv recommended)

```bash
git clone https://github.com/BeckhamLabsLLC/kicad-jlcpcb.git
cd kicad-jlcpcb
python -m venv .venv
source .venv/bin/activate     # Windows: .\.venv\Scripts\activate
pip install -e ".[dev]"
```

The editable install puts a `kicad-jlcpcb` entry-point script in the venv's `bin/`. `.mcp.json` calls that script directly; if the venv isn't active when Claude Code launches, point `.mcp.json` at the absolute path:

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

If you'd rather install into your user Python without a venv, substitute `pip install -e .` after `cd kicad-jlcpcb` and skip the venv lines. The entry-point lands in `~/.local/bin` instead.

### 2. Register with Claude Code

```
/plugin marketplace add /abs/path/to/kicad-jlcpcb
/plugin install kicad-jlcpcb@local
```

Restart Claude Code so the MCP server registers.

### 3. Sanity check

```bash
kicad-jlcpcb --help     # should print nothing (MCP servers speak JSON-RPC on stdio), exit 0
```

---

## Five-minute quick-start

Pick a small idea — an ESP32-C3 board with one sensor, a USB-C port, and an LDO works well. Run:

```
/pcb-new An ESP32-C3 soil-moisture sensor with two capacitive probes,
         USB-C 5V in, a 3.3V LDO, status LED, and JST-PH battery header.
         Place it on an 80x60 mm board.
```

Claude walks you through:

1. `detect_kicad` — verify the toolchain (< 1 s)
2. `create_project` — scaffold `.kicad_pro` + `.kicad_sch` + session file
3. Decomposes the description into ~12 generic part specs
4. `lcsc_search` per spec (first run populates the 17 MB jlcparts cache)
5. **BOM checkpoint** — shows every resolved part, flags extended-tier ones with cost warnings, and waits for your confirmation
6. `pcb_generate` — fetches EasyEDA pin maps (~12 s per unique IC, first run only), places footprints, wires nets, saves `.kicad_pcb`
7. `easyeda_handoff` — prints the import instructions

The full trace with real timings and tool outputs: **[`examples/soilnode-esp32/walkthrough.md`](examples/soilnode-esp32/walkthrough.md)**.

First run: ~90 s. Subsequent runs on similar designs: under 10 s.

---

## What the plugin does **not** do

Set expectations honestly before you start:

- ❌ **Auto-route traces.** That's why the `.kicad_pcb` gets handed to EasyEDA. Freerouting 2.1.0's CLI is buggy and can't handle RF matching networks; nothing else works headlessly well enough to ship.
- ❌ **Beautiful placement.** The three-band grid (connectors on top, ICs in the middle, passives below) is functional, not pretty. You rearrange in EasyEDA before routing.
- ❌ **Design review.** There's no DRC integration (yet — see Roadmap). The plugin trusts your spec and relies on KiCad / EasyEDA to catch rule violations.
- ❌ **PyPI distribution.** Install from the clone. No `pip install kicad-jlcpcb`.

---

## MCP tool surface

| Stage | Tool | Purpose |
|---|---|---|
| Setup | `detect_kicad` | Probe `kicad-cli` version, return install hint if missing |
| Setup | `create_project` | Scaffold `.kicad_pro` + subdirs + session file |
| Setup | `load_project` | Validate existing `.kicad_pro`; surfaces resumable session state |
| Resume | `session_resume` | Report where a prior workflow left off for a project dir |
| Sourcing | `lcsc_search` | Free-text part search, basic-only by default |
| Sourcing | `lcsc_resolve_bom` | Batch BOM resolution with cost-impact warnings |
| Sourcing | `fetch_part_library` | Placeholder symbol/footprint fetch into project `libs/` |
| Pin maps | `part_pin_map` | Fetch pin-name → pad-number map from EasyEDA |
| Schematic | `sch_generate` | Emit `.kicad_sch` from a netlist spec |
| Schematic | `sch_run_erc` | Run `kicad-cli sch erc` and parse the report |
| PCB | **`pcb_generate`** | **Main tool.** Auto-fetches pin maps, places footprints, wires every net, saves `.kicad_pcb` |
| Terminal | **`easyeda_handoff`** | **Recommended terminal tool.** Produces EasyEDA import instructions |
| Legacy | `package_for_jlcpcb` | For users routing in KiCad: export Gerbers + package a JLCPCB upload zip |

Full input-schema definitions are in [`src/kicad_jlcpcb_mcp/server.py`](src/kicad_jlcpcb_mcp/server.py) under `_tool_definitions()`.

---

## PCB spec format

`pcb_generate` consumes a JSON-serializable dict:

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
    "3V3":     [["U1", "3V3"], ["C1", "1"]],
    "GND":     [["U1", "GND"], ["C1", "2"]],
    "SPI_SCK": [["U1", "GPIO10"], ["U2", "SCK"]]
  }
}
```

Key rules:

- Reference IC pins by their **functional name** (`3V3`, `GPIO10`, `SCK`). The plugin resolves them via EasyEDA's pinmap.
- For passives (R, C, L, D), use bare pad numbers: `"1"`, `"2"`.
- `lib` and `fp` are KiCad-stdlib library + footprint names. See `/usr/share/kicad/footprints/` for the catalog.

Full worked spec: **[`examples/soilnode-esp32/spec.json`](examples/soilnode-esp32/spec.json)**.

---

## Architecture

```
src/kicad_jlcpcb_mcp/
  server.py         ← MCP server + 13 tool definitions
  session.py        ← per-project state (.kicad_jlcpcb_session.json)
  project.py        ← .kicad_pro create / load / validate
  kicad_cli.py      ← async wrapper for kicad-cli (KiCad 8/9)
  lcsc_client.py    ← jlcparts mirror + SQLite cache + basic-tier filter
  part_library.py   ← EasyEDA client (EasyEdaRateLimiter + pin-map cache)
  pcb.py            ← pcbnew-based .kicad_pcb generator
  schematic.py      ← netlist spec → .kicad_sch
  gerber_pack.py    ← KiCad 8/9 Protel extension normalizer + JLCPCB zip
  sexpr.py          ← s-expression reader/writer
  config.py         ← module-level constants
```

All HTTP goes through `lcsc_client` and `part_library`. All KiCad CLI invocations go through `kicad_cli`. `pcbnew` is lazy-imported inside `pcb.py` so the rest of the plugin runs fine when KiCad isn't installed (most tools don't need it).

---

## Testing

```bash
PYTHONPATH=src pytest tests/           # 212 tests (6 skipped without KiCad)
```

With KiCad's `pcbnew` bindings available:

```bash
KICAD_INSTALLED=1 PYTHONPATH=src pytest tests/ -v   # runs integration suite too
```

Coverage spans subprocess wrapping, HTTP mocking, SQLite cache, s-expression round-trip, schematic emission, Gerber renaming, EasyEDA pin-map parsing, rate-limit / retry, session persistence, MCP tool routing, and real `pcbnew` board generation.

Lint:

```bash
ruff check .
ruff format --check .
```

---

## Troubleshooting

Full guide: **[`TROUBLESHOOTING.md`](TROUBLESHOOTING.md)**. Most common issues:

| Symptom | Fix |
|---|---|
| `kicad-jlcpcb` command not found | `pip install -e .` from the clone, restart Claude Code |
| `ImportError: No module named pcbnew` | Install KiCad; don't try to `pip install pcbnew` (it ships with KiCad) |
| First run stalls ~12 s per IC | Expected — EasyEDA rate limit. Cached forever after first fetch. |
| `Footprint not found` | Check `/usr/share/kicad/footprints/<lib>.pretty/` for the exact name |
| `/pcb-new` offers to resume when you wanted a clean start | Delete `.kicad_jlcpcb_session.json` or pick a new project name |

---

## Design rationale (Phase 1.6)

Earlier releases tried to route the board headlessly with Freerouting and produce a JLCPCB Gerber zip directly. That didn't work for real boards — Freerouting 2.1.0 has CLI bugs, can't route RF matching networks, and won't save partial results.

Phase 1.6 takes the pragmatic win: **the plugin wires everything up, EasyEDA routes and orders.** The tradeoff is opening a browser tab and clicking two buttons; in exchange you get reliability the open-source tooling can't match and a one-click path to a JLCPCB order.

---

## Roadmap

- **Phase 2** — auto-placement that respects functional groupings (power domain, RF block, analog front-end), DRC integration, differential-pair awareness.
- **Phase 3** — vision-based schematic extraction: drop in a photo of a hand-drawn schematic, out comes a wired `.kicad_pcb`.

---

## Contributing

See **[`CONTRIBUTING.md`](CONTRIBUTING.md)** for dev setup, test running, and PR conventions. All contributors follow the [Code of Conduct](CODE_OF_CONDUCT.md). Bug reports: [open an issue](https://github.com/BeckhamLabsLLC/kicad-jlcpcb/issues).

---

## License

MIT — see **[`LICENSE`](LICENSE)**.
