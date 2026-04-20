# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- Session persistence: `.kicad_jlcpcb_session.json` tracks workflow state between invocations, enabling `/pcb-new` resumption after a Claude Code restart

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

### Known limitations
- Headless auto-routing is **not** in scope — Freerouting 2.1.0's CLI can't reliably route real RF boards. Routing is delegated to EasyEDA's cloud auto-router.
- First-run pin-map fetches for unique ICs pause ~12 s each (EasyEDA anti-bot). Cached forever after first hit.
- Some LCSC parts lack EasyEDA symbol data; for those, provide an explicit `pinmap` field in the component spec.
- Auto-placement is a three-band grid, not an aesthetic layout. Final placement happens in EasyEDA before routing.

[Unreleased]: https://github.com/BeckhamLabsLLC/kicad-jlcpcb/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/BeckhamLabsLLC/kicad-jlcpcb/releases/tag/v0.1.0
