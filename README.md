# kicad-jlcpcb

[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%20%7C%203.11%20%7C%203.12%20%7C%203.13-blue.svg)](#requirements)
[![KiCad 8+](https://img.shields.io/badge/kicad-8.0%2B-informational.svg)](https://www.kicad.org/)
[![tests](https://github.com/BeckhamLabsLLC/kicad-jlcpcb/actions/workflows/test.yml/badge.svg)](https://github.com/BeckhamLabsLLC/kicad-jlcpcb/actions/workflows/test.yml)
[![upstream contract](https://github.com/BeckhamLabsLLC/kicad-jlcpcb/actions/workflows/contract.yml/badge.svg)](https://github.com/BeckhamLabsLLC/kicad-jlcpcb/actions/workflows/contract.yml)
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

1. **"Is this part basic or extended on JLCPCB?"** — You stop needing to cross-reference LCSC's UI. The plugin queries the JLCPCB catalog directly, caches the answer, and always prefers basic-tier (no $3/part assembly setup fee).
2. **"What's the right pad number for this IC's `GPIO10`?"** — You stop reading datasheets to build netlists. The plugin queries EasyEDA by LCSC C-number, caches the pin-name → pad-number map, and lets you reference pins by their functional names.
3. **"Why is my auto-router failing?"** — You stop fighting Freerouting on RF boards. The plugin stops at "wired `.kicad_pcb`" and hands off to EasyEDA's cloud auto-router, which works on real designs.

---

## Quick tour

- **Two slash commands**: `/pcb-new` (from a description) and `/pcb-from-bom` (from a CSV).
- **One agent**: `part-sourcer` — finds the best JLCPCB-stocked part for a generic spec.
- **One skill**: `kicad-jlcpcb-workflow` — the full reference the LLM consults while driving the workflow.
- **14 MCP tools** covering setup, sourcing, schematic, PCB generation, EasyEDA handoff, and session resume.
- **Session persistence** — each project writes a `.kicad_jlcpcb_session.json` so `/pcb-new` can resume mid-flow after a Claude Code restart.

---

## Requirements

| Component | Version | Notes |
|---|---|---|
| Python | 3.10 – 3.13 | Tested on all four in CI |
| KiCad | 8.0 – 10.x | `kicad-cli` on `PATH` or in a standard install location; `pcbnew` Python bindings for `pcb_generate`. Only the 8.0 floor is enforced — KiCad 11 removed the SWIG bindings and will pass detection, then fail at `pcb_generate`. See [TROUBLESHOOTING](TROUBLESHOOTING.md) |
| EasyEDA account | free | Only needed for the final routing + ordering step |
| Network | required | Part data is fetched live — see below |

### Data dependencies

Part sourcing depends on two **third-party, unofficial** services. Neither is
run by JLCPCB, and neither is run by us. Both are overridable, so a fork can
point at a mirror without touching code:

| Service | Used for | Override |
|---|---|---|
| [`jlcsearch.tscircuit.com`](https://github.com/tscircuit/jlcsearch) | Catalog search, JLCPCB stock, basic/extended tier | `KJLC_JLCSEARCH_BASE` |
| `easyeda.com` | Exact C-number lookup, symbols, pin maps | `KJLC_EASYEDA_BASE` |

Resolved parts are cached in `~/.cache/kicad-jlcpcb/lcsc_parts.sqlite` for 24
hours. There is no bulk catalog download.

> **Why the overrides exist.** v0.1.0 hardcoded a single third-party URL. Upstream
> retired that data layout, the URL started returning 404, and part sourcing
> broke silently for months before anyone noticed
> ([#1](https://github.com/BeckhamLabsLLC/kicad-jlcpcb/issues/1)). A weekly CI job
> now tests both services directly, and you can repoint either one yourself.

Install KiCad:

- **Fedora 40+:** `sudo dnf install kicad`
- **Ubuntu 22.04+:** `sudo add-apt-repository ppa:kicad/kicad-9.0-releases && sudo apt install kicad`
- **Arch:** `sudo pacman -Syu kicad`
- **macOS:** [kicad.org/download](https://www.kicad.org/download/)

---

## Install

```
/plugin marketplace add BeckhamLabsLLC/claude-plugins
/plugin install kicad-jlcpcb@beckhamlabs-plugins
```

Restart Claude Code, then run `/mcp` and look for `kicad-jlcpcb`.

That is the whole install. You do not need to clone the repo, and you do not
need to install anything with `pip` — the plugin resolves its own two
dependencies (`mcp`, `httpx`) on first launch via [uv](https://docs.astral.sh/uv/),
which most Python toolchains already have:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh    # if you do not have it
```

If you would rather not use uv, install the dependencies into whichever Python
`python3` resolves to and the plugin will use them directly:

```bash
python3 -m pip install mcp httpx
```

KiCad itself is a separate install — see [Requirements](#requirements).

<details>
<summary>Installing from a clone instead (contributors)</summary>

```bash
git clone https://github.com/BeckhamLabsLLC/kicad-jlcpcb.git
cd kicad-jlcpcb
pip install -e ".[dev]"
```

Then register the clone as a local marketplace, which is what makes Claude Code
expand `${CLAUDE_PLUGIN_ROOT}` for the MCP server:

```
/plugin marketplace add /abs/path/to/kicad-jlcpcb
/plugin install kicad-jlcpcb@beckhamlabs
```

Note the marketplace name differs from the published one: the repo's own
`.claude-plugin/marketplace.json` declares `beckhamlabs`, while the aggregator
repo declares `beckhamlabs-plugins`.

Do **not** register the checkout by enabling its `.mcp.json` as a project
server. `${CLAUDE_PLUGIN_ROOT}` is only substituted for plugin-provided MCP
configs; in project scope it stays a literal string and the server cannot be
found. See [TROUBLESHOOTING](TROUBLESHOOTING.md).

</details>

### Sanity check

`/mcp` in Claude Code should list `kicad-jlcpcb` with its 14 tools. If it
reports the server failed, see [TROUBLESHOOTING](TROUBLESHOOTING.md) — the
first step there prints the actual reason, which Claude Code does not show you.

From a clone, you can also check the whole path end to end — that the server
starts, registers its handlers, and answers a real tool call:

```bash
python3 bin/launch.py --version     # prints the version, exits 0
python3 scripts/check_protocol.py   # initialize -> tools/list -> tools/call
```

Run the launcher with no arguments and it will appear to hang — that is
correct. An MCP server speaks JSON-RPC on stdio and is waiting for a client.

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
4. `lcsc_search` per spec (live catalog query, cached locally for 24 h)
5. **BOM checkpoint** — shows every resolved part, flags extended-tier ones with cost warnings, and waits for your confirmation
6. `pcb_generate` — fetches EasyEDA pin maps (~12 s each, only for parts whose nets reference pins by *name*, first run only), places footprints, wires nets, saves `.kicad_pcb`
7. `easyeda_handoff` — prints the import instructions

The full trace with real timings and tool outputs: **[`examples/soilnode-esp32/walkthrough.md`](examples/soilnode-esp32/walkthrough.md)**.

First run: ~90 s. Subsequent runs on similar designs: under 10 s.

---

## What the plugin does **not** do

Set expectations honestly before you start:

- ❌ **Auto-route traces.** That's why the `.kicad_pcb` gets handed to EasyEDA. Freerouting 2.1.0's CLI is buggy and can't handle RF matching networks; nothing else works headlessly well enough to ship.
- ❌ **Beautiful placement.** The three-band grid (connectors on top, ICs in the middle, passives below) is functional, not pretty. You rearrange in EasyEDA before routing.
- ❌ **Design review.** There's no DRC integration (yet — see Roadmap). The plugin trusts your spec and relies on KiCad / EasyEDA to catch rule violations.
- ❌ **PyPI distribution.** Install through the Claude Code marketplace. There is no `pip install kicad-jlcpcb`.

---

## MCP tool surface

| Stage | Tool | Purpose |
|---|---|---|
| Setup | `detect_kicad` | Probe `kicad-cli` version, return install hint if missing |
| Setup | `create_project` | Scaffold `.kicad_pro` + subdirs + session file |
| Setup | `load_project` | Validate existing `.kicad_pro`; surfaces resumable session state |
| Resume | `session_resume` | Report where a prior workflow left off for a project dir |
| Resume | `session_confirm_bom` | Record the user's BOM approval so a restart doesn't re-ask |
| Sourcing | `lcsc_search` | Free-text part search, basic-only by default |
| Sourcing | `lcsc_resolve_bom` | Batch BOM resolution with cost-impact warnings |
| Sourcing | `fetch_part_library` | Symbol + footprint from EasyEDA's real geometry into project `libs/` |
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
- `lib` and `fp` are KiCad-stdlib library + footprint names. The plugin finds KiCad's footprint directory automatically on Linux, macOS, Windows and Flatpak; override with `KJLC_FOOTPRINT_DIR` or `pcb_generate`'s `lib_dir`.

Full worked spec: **[`examples/soilnode-esp32/spec.json`](examples/soilnode-esp32/spec.json)**.

---

## Architecture

```
src/kicad_jlcpcb_mcp/
  server.py         ← MCP server + 14 tool definitions
  session.py        ← per-project state (.kicad_jlcpcb_session.json)
  project.py        ← .kicad_pro create / load / validate
  kicad_cli.py      ← async wrapper for kicad-cli (KiCad 8–10)
  lcsc_client.py    ← jlcsearch + EasyEDA lookup, SQLite cache, basic-tier filter
  part_library.py   ← EasyEDA client (EasyEdaRateLimiter + pin-map cache)
  pcb.py            ← pcbnew-based .kicad_pcb generator
  schematic.py      ← netlist spec → .kicad_sch
  gerber_pack.py    ← Protel extension normalizer + JLCPCB zip
  sexpr.py          ← s-expression reader/writer
  config.py         ← module-level constants
```

All HTTP goes through `lcsc_client` and `part_library`. All KiCad CLI invocations go through `kicad_cli`. `pcbnew` is lazy-imported inside `pcb.py` so the rest of the plugin runs fine when KiCad isn't installed (most tools don't need it).

---

## Testing

```bash
pytest tests/       # offline suite; nothing here touches the network
```

Two suites are gated behind environment variables because they need something
the default run can't assume:

```bash
KICAD_INSTALLED=1 pytest tests/ -v                          # needs pcbnew
KJLC_NETWORK_TESTS=1 pytest tests/test_network_contract.py  # hits live APIs
```

The offline suite covers subprocess wrapping, HTTP mocking, the SQLite cache,
s-expression round-trip, schematic emission, Gerber renaming, EasyEDA pin-map
parsing, rate-limit / retry, session persistence, MCP tool routing, and real
`pcbnew` board generation.

**`test_network_contract.py` earns its own paragraph.** Every other test is
mocked, which is why the whole suite stayed green for four months while part
sourcing was completely broken in the field. The contract tests assert the
*shape* of live upstream responses — never a specific price or stock figure —
and run weekly in CI. If that badge goes red, part sourcing is broken for
everyone; please open an issue.

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
| MCP server shows as failed / `CONNECTION_CLOSED` | Run `python3 bin/launch.py --version` by hand — it prints what is missing. Usually: install `uv`, or `python3 -m pip install mcp httpx`. |
| `ImportError: No module named pcbnew` | Install KiCad; don't try to `pip install pcbnew` (it ships with KiCad) |
| First run stalls ~12 s per part | Expected — EasyEDA rate limit, and only for parts whose nets use pin *names*. Cached forever after the first fetch. |
| `Footprint not found` | The error names how many libraries were found and suggests near matches. If none were found, set `KJLC_FOOTPRINT_DIR`. |
| `/pcb-new` offers to resume when you wanted a clean start | Delete `.kicad_jlcpcb_session.json` or pick a new project name |

---

## What you get, and what to check

The plugin produces a `.kicad_pcb` with real footprints and every net wired,
plus a JLCPCB upload zip. Two things are worth checking before you order:

**Part geometry comes from EasyEDA.** Symbols carry the real pin names and
numbers, and footprints the real pad positions, sizes and drills — EasyEDA,
LCSC and JLCPCB share a parent company, so this is the same data JLCPCB
assembles against. When EasyEDA has no geometry for a part, a placeholder is
written instead and `fetch_part_library` says so in `warnings`. **A
placeholder footprint will not match the real part** — replace it from
KiCad's standard libraries before ordering.

**CPL rotations are passed through, not corrected.** JLCPCB's expected
orientation differs from KiCad's for some packages. Footprints generated from
EasyEDA already share JLCPCB's convention; footprints you take from KiCad's
standard libraries may not. The plugin says so rather than applying a guess
that silently rotates parts — check JLCPCB's assembly preview after upload and
fix anything that looks wrong there.

---

## Design rationale: why the handoff

Earlier releases tried to route the board headlessly with Freerouting and produce a JLCPCB Gerber zip directly. That didn't work for real boards — Freerouting 2.1.0 has CLI bugs, can't route RF matching networks, and won't save partial results.

So the plugin takes the pragmatic win: **it wires everything up, EasyEDA routes and orders.** The tradeoff is opening a browser tab and clicking two buttons; in exchange you get reliability the open-source tooling can't match and a one-click path to a JLCPCB order.

---

## Roadmap

- **Next** — auto-placement that respects functional groupings (power domain, RF block, analog front-end), DRC integration, differential-pair awareness, and a per-package CPL rotation table so JLCPCB orientations need no manual correction.
- **Later** — vision-based schematic extraction: drop in a photo of a hand-drawn schematic, out comes a wired `.kicad_pcb`.

---

## Contributing

See **[`CONTRIBUTING.md`](CONTRIBUTING.md)** for dev setup, test running, and PR conventions. All contributors follow the [Code of Conduct](CODE_OF_CONDUCT.md). Bug reports: [open an issue](https://github.com/BeckhamLabsLLC/kicad-jlcpcb/issues).

---

## License

MIT — see **[`LICENSE`](LICENSE)**.
