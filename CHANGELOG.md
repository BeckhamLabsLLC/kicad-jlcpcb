# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.3.0] - 2026-09-16

Fixes three bugs that reported success while producing wrong or unusable
output. All were invisible to the test suite because every check inspected
our own output with our own code.

### Fixed
- **The generated `.kicad_sch` would not open in KiCad.** Any KiCad version
  refused it outright with "Failed to load schematic", for two independent
  reasons: the file carried a `;` comment (KiCad's S-expression grammar has
  no comment syntax) and `(net (name ...) (member ...))` nodes (netlist
  grammar, not schematic grammar). `test_output_is_parseable_sexpr` passed
  throughout, because our own tokenizer accepts `;` as a bare atom.

  Schematics now load, and their netlist matches the spec. Connectivity is
  carried by global labels rather than drawn wires — two global labels
  sharing a name are one net in KiCad, which gives a correct netlist without
  solving schematic routing. Symbols get real pins and an `instances` block
  so reference designators stick, and every coordinate lands on the 1.27 mm
  grid (off-grid pins load fine and then silently refuse to connect).

- **`sch_run_erc` always reported "passed" on KiCad 9 and 10.** Those
  versions write per-violation severity on its own line, which none of the
  parser's patterns matched, so a report with errors parsed as `(0, 0)` and
  came back as `passed: true`. Verified against a real KiCad 10.0.5 report:
  1 error, 3 warnings, previously reported as clean.

- **Pin-map fetch failures were silently swallowed**, so an EasyEDA 403 or
  timeout surfaced as `"U1 has no pad 'GPIO10'"` repeated once per pin.
  Read alone that says the caller's net names are wrong, and the model would
  rewrite a correct netlist chasing it. `pcb_generate` now names the real
  cause in `warnings`, ahead of the errors it produces.

- **`kicad-cli` stderr never reached the model.** `KicadCliError` captured
  `.stdout`/`.stderr` as attributes, but the MCP layer only surfaces
  `str(e)` — so a failure read `"kicad-cli pcb export gerbers failed
  (exit 1)"` with no way to act on it. The captured output is now in the
  message, truncated to 800 characters.

### Added
- `TestKicadActuallyLoadsIt` in `tests/test_schematic.py` — runs the
  generated schematic through `kicad-cli` and asserts the exported netlist
  matches the spec. Gated on `KICAD_INSTALLED=1`. This is the check that was
  missing; everything else validated our output with our own parser.
- ERC report-format tests covering KiCad 9/10, the `Found N errors` summary,
  the legacy `** Errors N ****` form, and `Severity:` lines — with an
  explicit assertion that a report containing violations never parses as
  clean.

### Changed
- `.kicad_sch` output is now format version 20250114 (what KiCad 9/10 write)
  rather than 20231120.
- `/pcb-new` now dispatches `part-sourcer` agents for sourcing, which the
  agent definition and skill reference had claimed all along while the
  command itself called `lcsc_search` directly.
- `part-sourcer` guidance updated for how search actually behaves: short
  queries beat prose, values and chip sizes belong in the query, and a
  `stock_unknown` flag must never be presented as a stock of zero.
- `sexpr.py` documents that it is an inspection utility, is imported by
  nothing in `src/`, and is deliberately more permissive than KiCad's
  parser — a successful `parse()` says nothing about whether KiCad will
  load the file.

## [0.2.1] - 2026-09-16

### Fixed
- **A fresh `pip install` produced a server that would not start.** The `mcp`
  dependency was unbounded (`mcp>=1.0.0`), so a new install resolved mcp 2.x,
  which removed `Server.list_tools` and renamed `Tool.inputSchema`. The server
  died on startup with `'Server' object has no attribute 'list_tools'`. Pinned
  to `mcp>=1.0.0,<2` until the server is ported to the 2.x API.

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

[Unreleased]: https://github.com/BeckhamLabsLLC/kicad-jlcpcb/compare/v0.3.0...HEAD
[0.3.0]: https://github.com/BeckhamLabsLLC/kicad-jlcpcb/releases/tag/v0.3.0
[0.2.1]: https://github.com/BeckhamLabsLLC/kicad-jlcpcb/releases/tag/v0.2.1
[0.2.0]: https://github.com/BeckhamLabsLLC/kicad-jlcpcb/releases/tag/v0.2.0
[0.1.0]: https://github.com/BeckhamLabsLLC/kicad-jlcpcb/releases/tag/v0.1.0
