# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.2.0] - 2026-09-16

Repairs a total outage of part sourcing, plus the install path and KiCad
compatibility. **Anyone on 0.1.0 should upgrade — it does not work.**

### Fixed
- **Part sourcing was completely broken.** v0.1.0 read the catalog from the jlcparts
  GitHub Pages mirror. Upstream retired that data layout, `/data/index.json` began
  returning HTTP 404, and every `lcsc_search`, `lcsc_resolve_bom`, and
  `pcb_generate` call failed. Part data now comes from
  [jlcsearch](https://github.com/tscircuit/jlcsearch) for search and EasyEDA for
  exact C-number lookups. There is no longer a ~17 MB first-run download.
  ([#1](https://github.com/BeckhamLabsLLC/kicad-jlcpcb/issues/1))
- **`load_project` rejected every real KiCad project.** It refused any
  `.kicad_pro` with `meta.version` above 2; KiCad 9 and 10 both write 3. This was
  also a one-way trap: a project created by the plugin became permanently
  unloadable the first time KiCad saved it. New projects now emit version 3, and
  a newer schema warns instead of refusing.
- **The documented install could not work.** There was no
  `.claude-plugin/marketplace.json`, so `/plugin marketplace add` had nothing to
  resolve. Added, under the marketplace name `beckhamlabs`.
- **`.mcp.json` required the plugin to be on `PATH`** and failed with `ENOENT`
  otherwise. It now launches `python3 -m kicad_jlcpcb_mcp` with `PYTHONPATH` set
  to the plugin root, so no `PATH` entry and no hand-editing of a tracked file.
- **`kicad-jlcpcb --help` hung the terminal.** `main()` ignored `argv` and blocked
  on stdin. `--help` and `--version` now work.
- Chip passives resolved to the wrong part. Many basic passives carry an empty
  description upstream, so a text search for `10k 0603` returned a 510 kΩ. Queries
  containing a resistance or capacitance now use structured value lookups, and
  `0.1uF` / `100nF` are recognised as the same part.

### Added
- `tests/test_network_contract.py` — live contract tests against both upstream
  services, gated on `KJLC_NETWORK_TESTS=1`, running weekly in CI
  (`.github/workflows/contract.yml`). The offline suite stayed green for four
  months while the plugin was broken in the field; this is the tripwire.
- `KJLC_JLCSEARCH_BASE` and `KJLC_EASYEDA_BASE` environment overrides, so a fork
  can repoint at a mirror without a code change.
- A stock-provenance warning: parts resolved via EasyEDA carry LCSC retail stock,
  not JLCPCB SMT inventory, and are now flagged instead of reporting a
  misleading 0.
- Query narrowing: upstream AND-matches every word, so descriptive specs
  returned nothing. The client drops the least-matchable words and retries,
  stopping at half the original word count.

### Changed
- The part cache is now a 24-hour per-row cache rather than a 7-day bulk catalog.
  An existing `lcsc_parts.sqlite` from 0.1.0 is discarded on first run — it holds
  rows from a retired source.
- `pyproject.toml` sets `pythonpath = ["src"]`; `PYTHONPATH=src` is no longer
  needed to run the tests.
- CI runs on all branches, not just `main`, and `ruff` is pinned.

## [0.1.0] - 2026-04-20

Initial public release (Phase 1.6).

### Added
- `kicad-jlcpcb` MCP server exposing 13 tools for KiCad → EasyEDA → JLCPCB workflows
- `/pcb-new` command: start a fresh PCB from a text description
- `/pcb-from-bom` command: start from an existing LCSC BOM CSV
- `part-sourcer` agent: find the best JLCPCB-stocked LCSC part for a generic spec
- `kicad-jlcpcb-workflow` skill: full workflow guidance with decision trees and footprint reference
- LCSC catalog via the [jlcparts](https://yaqwsx.github.io/jlcparts/) mirror, cached locally in SQLite with a hard preference for JLCPCB basic-tier parts
- EasyEDA pin-map auto-fetch (rate-limited to one request per 12 s per IC, cached indefinitely)
- `.kicad_pcb` generation via KiCad's `pcbnew` Python API with three-band auto-placement (connectors / ICs / passives) and pad-to-pad net wiring by pin name
- KiCad 8/9 Gerber export normalization (Protel extensions) and JLCPCB upload-zip packaging
- Schematic (`.kicad_sch`) generator from netlist specs
- `kicad-cli` ERC wrapper with KiCad 8 and 9 report parsing
- Session persistence: `.kicad_jlcpcb_session.json` tracks workflow state between invocations, enabling `/pcb-new` resumption after a Claude Code restart

### Known limitations
- Headless auto-routing is **not** in scope — Freerouting 2.1.0's CLI can't reliably route real RF boards. Routing is delegated to EasyEDA's cloud auto-router.
- First-run pin-map fetches for unique ICs pause ~12 s each (EasyEDA anti-bot). Cached forever after first hit.
- Some LCSC parts lack EasyEDA symbol data; for those, provide an explicit `pinmap` field in the component spec.
- Auto-placement is a three-band grid, not an aesthetic layout. Final placement happens in EasyEDA before routing.

[Unreleased]: https://github.com/BeckhamLabsLLC/kicad-jlcpcb/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/BeckhamLabsLLC/kicad-jlcpcb/releases/tag/v0.2.0
[0.1.0]: https://github.com/BeckhamLabsLLC/kicad-jlcpcb/releases/tag/v0.1.0
