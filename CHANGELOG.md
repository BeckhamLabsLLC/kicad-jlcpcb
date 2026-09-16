# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.12.0] - 2026-09-16

### Fixed
- **Components were placed outside the board outline on small boards.**
  Placement advanced Y by fixed amounts — 16 mm after the connector band,
  14 mm after the IC band — and never checked the result against the board
  height. On a 40 x 30 mm board the first passive landed at y=40, below the
  bottom edge; a 30-passive board put **all thirty parts off the board**,
  with Y running to 138 mm. Small boards are exactly what this plugin is for,
  and JLCPCB rejects footprints outside Edge.Cuts.

  Bands are now sized from the board rather than from constants, spacing is
  compressed to fit instead of overflowing, and anything that still does not
  fit is reported rather than silently written out of bounds. Verified
  through `pcbnew`: 20 parts on a 40 x 30 mm board, none outside the outline.

  Compression has a floor per part class. Without one it traded a visible
  problem for an invisible worse one: 100 parts on a 30 x 30 mm board
  compressed to a 0.66 mm pitch, and an 0603 is 1.6 mm long, so the copper
  overlapped. Placement now stops shrinking at a pitch that still fits the
  footprint and reports that the board is too small, which is the true
  answer.

## [0.11.0] - 2026-09-16

### Fixed
- **A split drill export shipped only the non-plated holes.** When KiCad wrote
  `<stem>-PTH.drl` and `<stem>-NPTH.drl` instead of a merged file, the packer
  took the alphabetically-first one — NPTH — packed it alone, and warned that
  it had "used the merged file", which did not exist. The board would come
  back with every via and every plated through-hole missing.

  Both files ship now when only a split pair exists, plated first, with a
  warning that says what actually happened. A merged file is still preferred
  and used alone.

## [0.10.0] - 2026-09-16

### Fixed
- Session writes are atomic. They used a plain `write_text`, so an
  interruption mid-save left unparseable JSON — and `load_session` treats
  unparseable as "no session", silently discarding the whole workflow
  including a BOM the user had already approved.

### Added
- `easyeda_handoff` now returns `before_you_order`: the three things that can
  silently produce a wrong board (a placeholder footprint, uncorrected CPL
  rotation, a missing copper layer). It is the last thing the user reads
  before spending money, so they belong there and not only in a `warnings`
  array that may have scrolled past.
- `.editorconfig` and an optional `.pre-commit-config.yaml` pinned to the same
  ruff version CI uses, so a green commit is a green build.

## [0.9.0] - 2026-09-16

### Fixed
- **4-layer boards shipped with no inner copper at all.** Two independent
  causes, either of which alone was fatal: `package_for_jlcpcb` exported a
  fixed two-layer set and never passed `--layers`, and the gerber filename
  matcher didn't accept KiCad's `.g1`/`.g2` extensions for inner copper. So a
  board generated with `layer_count: 4` produced a zip containing `In1.Cu` and
  `In2.Cu` nowhere — it uploads, the order is accepted, and the board comes
  back with every inner-layer net missing.

  The copper stackup is now read from the board file and the export matches
  it, up to six layers. `package_for_jlcpcb` then verifies every copper layer
  the board has actually reached the zip — `pack_for_jlcpcb` only ever sees a
  directory of files, so it could not catch this on its own.
- **The inner-layer Protel extensions were wrong.** `In1_Cu` mapped to `G2L`
  and `In2_Cu` to `G3L` — a transposition of Altium's `GL2`/`GL3`. JLCPCB
  documents `.G1`/`.G2`, which is also what KiCad writes natively, so the two
  now line up with no renaming guesswork.
- `jlcpcb-rules.md` claimed most ESP32 modules are basic-tier. They are not,
  and this plugin's own example BOM marks its ESP32-C3-WROOM-02 as extended.
  It also credited `package_for_jlcpcb` with writing the JLCPCB design rules;
  `create_project` does, which is why they apply from the start.

## [0.8.0] - 2026-09-16

### Added
- **`session_confirm_bom`.** `bom_confirmed` was a declared session stage with
  its own resume hint that nothing ever set. The BOM checkpoint is the
  plugin's one guard against spending money on a wrong board, and its approval
  lived only in the conversation — so a Claude Code restart between approving
  the BOM and generating the board reported the stage as merely "parts
  sourced" and asked the user to approve the same BOM again. `/pcb-new` now
  records it, with an optional note for whatever the user asked to change.
  That is 14 tools, all reachable from a slash command.
- Coverage reported on every CI run. Deliberately not gated: a threshold on a
  suite whose two most important layers — KiCad integration and live upstream
  — sit behind env vars would measure the wrong thing.

### Fixed
- Platform-specific assumptions in three tests, exposed by the new macOS and
  Windows matrix within minutes of its first run: two asserted the install
  hint names Fedora (platform-specific since 0.4.0), one asserted a
  non-executable file is skipped during discovery (Windows has no execute bit,
  so `os.access(X_OK)` is true for any file that exists), and one asserted a
  session path starts with `/` (a Windows absolute path starts with a drive
  letter). All test bugs; the per-platform code behaves correctly everywhere.
- `pytest-cov` was missing from the dev extra, so the coverage step ran against
  an environment without it. The edit adding it had silently matched nothing
  and it worked locally only because the package was already installed.

### Changed
- README and the workflow skill describe what the plugin actually produces,
  including the two things worth checking before ordering: a placeholder
  footprint does not match the real part, and CPL rotations are passed through
  rather than corrected.
- The example walkthrough is labelled as illustrative rather than a recording,
  and its timings reflect that pin-map fetches are skipped for parts wired by
  pad number — 3 fetches for the 13-part example, not 13.

## [0.7.0] - 2026-09-16

### Fixed
- **The CPL described a different coordinate system than the gerbers.** Only
  the position export passed `--use-drill-file-origin`; gerber export has no
  origin option and is always absolute. On a board with a drill/place origin
  set — routine in KiCad — every component in the CPL was offset by that
  origin. Measured on a board with a 25 × 15 mm aux origin: R1 sits at
  (10, -40) in the gerbers and was written to the CPL as (-15, -55). The zip
  uploads cleanly and the parts go on millimetres off the board.

  Boards this plugin generates never set an aux origin, so the two agreed by
  accident and nothing caught it. All three exports now state absolute
  origin explicitly, and a KiCad-gated test builds a board with an aux origin
  and asserts the CPL is not shifted.

### Added
- **macOS and Windows in CI.** The README has claimed both since v0.1.0, and
  v0.4.0/v0.5.0 added real per-platform code for finding `kicad-cli` and
  KiCad's footprint libraries — none of it exercised anywhere. The matrix now
  runs the offline suite on all three platforms.
- Coverage reporting (`pytest-cov`, configured in `pyproject.toml`). Running
  it first was what surfaced how little of the MCP surface was tested:
  `server.py` sat at 58%, because the tool-routing branches — every
  `args.get()` default the model depends on — had no tests at all. Now 74%,
  with routing tests for every tool, and 87% overall.
- Tests asserting the `kicad-cli` export flags directly. `--no-x2`,
  `--subtract-soldermask`, decimal Excellon zeros and the origin flags are
  what JLCPCB correctness rests on, and a wrong one produces files that
  upload cleanly and fabricate wrong.

## [0.6.0] - 2026-09-16

Three things that looked finished and were not: the libraries were
fabricated, the manufacturing files were in the wrong format, and placement
misread half the reference designators.

### Fixed
- **Symbols and footprints were invented, not fetched.** `fetch_part_library`
  used EasyEDA only to *count* pins, then emitted a rectangle with pins named
  `P1..Pn` and pads from a hardcoded IPC table covering
  0402/0603/0805/1206/SOT-23 — with a generic two-row guess for everything
  else. Any part outside that table got pads that did not match it, and the
  tool description called EasyEDA "the source of truth".

  EasyEDA ships the real geometry and it is now used: pin numbers, names and
  positions from `dataStr.shape`, and pad positions, sizes, shapes, layers and
  drills from `packageDetail`. A generated SOT-23-5 now loads in `pcbnew` with
  0.95 mm pitch matching KiCad's own. When EasyEDA has no geometry, the
  placeholder is still written — but the response now says, in `warnings`,
  that it will not match the real part.

- **The CPL was not in JLCPCB's format.** `kicad-cli pcb export pos` writes
  `Ref,Val,Package,PosX,PosY,Rot,Side`; JLCPCB needs
  `Designator,Mid X,Mid Y,Layer,Rotation` with `Top`/`Bottom` capitalised.
  Every zip this plugin has ever produced carried a placement file JLCPCB
  rejects or misreads.
- **The BOM was not in JLCPCB's format either** — KiCad's
  `Reference,Value,Footprint,LCSC,...` instead of
  `Comment,Designator,Footprint,LCSC Part #`. Both are converted now, and a
  conversion that cannot find the columns it needs fails loudly rather than
  shipping a file that would be silently misread.
- **Placement misclassified common reference designators.** Matching was
  first-prefix-wins, so `XT1` (a crystal) was filed as a connector because it
  starts with `X`, `USB1` as an IC because it starts with `U`, and `SW1` never
  reached the switch rule at all. Matching is longest-prefix-first now and
  covers the IEEE 315 prefixes.

### Added
- `jlcpcb_format.py` — the KiCad-to-JLCPCB column conversions, as pure text
  transforms that are testable without KiCad.
- Live contract tests for EasyEDA's symbol and footprint geometry, including
  one that runs live upstream data through the emitter into `pcbnew`. If
  EasyEDA reshapes its payload, symbols lose their pin names and footprints
  lose their pads, and the fallback silently does not match the real part.

### Known limitation
- CPL rotations are passed through as the board has them. JLCPCB's expected
  orientation differs from KiCad's for some packages, and correcting it needs
  a per-package table maintained by hand. Footprints generated from EasyEDA
  share JLCPCB's convention; footprints taken from KiCad's standard libraries
  may not. `package_for_jlcpcb` says so in `warnings` rather than applying a
  guess — check JLCPCB's assembly preview after upload.

## [0.5.0] - 2026-09-16

### Fixed
- **`pcb_generate` could not place a footprint on macOS or Windows.** The
  footprint library path was the hardcoded string
  `/usr/share/kicad/footprints`, so board generation failed everywhere else
  — including for users whose KiCad v0.4.0 had just taught `detect_kicad` to
  find. The directory is now resolved at call time from `KJLC_FOOTPRINT_DIR`,
  then KiCad's own `KICAD{10,9,8}_FOOTPRINT_DIR` (the variables its
  `fp-lib-table` expands), then the standard locations for Linux, macOS,
  Windows and Flatpak. `pcb_generate` also accepts `lib_dir` for a one-off.
- The `mcp<2` bound was enforced only by a comment. A Dependabot PR widening
  it to `<3` passed CI against mcp 2.2.0 while the server was unstartable,
  because no test constructed the server — every other test calls
  `_tool_definitions()` and `_handle_tool` directly.
  `TestServerActuallyConstructs` now builds the server and asserts the
  low-level `Server` API we depend on is present.
- `Footprint not found` errors now say how many libraries were actually
  present and suggest near matches, instead of only naming the path.

### Changed
- **Pin-map fetches are skipped when they cannot help.** EasyEDA is limited
  to one request per 12 seconds, and every component carrying an LCSC number
  was fetched — including passives wired as `("C1", "1")`, whose pads are
  already numbers. Only refs whose nets reference a pin by *name* are
  fetched now. On the shipped example that is 3 requests instead of 13:
  **156 seconds of waiting down to 36.**
- Docs no longer assume the Linux footprint path or quote a per-IC stall
  that no longer reflects what the plugin does.

## [0.4.0] - 2026-09-16

Reliability and reach: the plugin now finds KiCad where people actually
install it, survives a flaky network, and exposes every tool it ships.

### Fixed
- **KiCad was undetectable on macOS and Flatpak.** Discovery probed `PATH`
  only. The macOS installer never puts `kicad-cli` on `PATH` (it lives inside
  `KiCad.app`), and a Flatpak install exposes only a GUI launcher — so users
  who had KiCad were told to install it. The README promises macOS support
  and this module's own hint recommended Flatpak. Both now work: known
  install locations are probed after `PATH`, and Flatpak is invoked through
  `flatpak run --command=kicad-cli`.
- **Install instructions were Fedora-only**, printed verbatim on macOS and
  Windows. Now platform-appropriate.
- **A single stalled EasyEDA connection failed the part outright** and
  reported it as "not found" — which reads as a bad C-number and sends you
  looking in the wrong place. Resolving a 13-part BOM makes 13 sequential
  requests over several minutes, so this was not rare: 2 of 13 parts failed
  on a real run. Network faults are now retried like 403s already were; a
  genuine 404 still fails fast.
- **An assembly zip could ship without a BOM.** When a project had no
  schematic, `package_for_jlcpcb` set the BOM to `None` while a comment
  claimed it built one from the board — producing a zip you cannot actually
  order assembly with. It now falls back to the component list the session
  recorded, via the `bom_from_components` helper that existed for exactly
  this and was never called.
- **`fetch_part_library` used a second, unthrottled EasyEDA fetcher** with no
  403 retry and a generic User-Agent, making it far more likely to be blocked
  than `part_pin_map`. Removed; both paths now share one throttled client.
- `pcb_generate` accepted `auto_fetch_pinmaps` and `force_refresh` without
  declaring them, so the model had no way to discover either. Now in the
  schema.
- `kicad_cli` pointed callers at a `bom_from_footprints()` helper that does
  not exist.

### Added
- `SECURITY.md`, including what the plugin actually touches: subprocesses,
  files, and two unauthenticated outbound hosts.
- `.github/dependabot.yml` for pip and GitHub Actions.
- `tests/conftest.py` zeroes the EasyEDA throttle for all tests. Routing
  `fetch_part_library` through the rate-limited client had turned
  `test_part_library.py` into a 24-second run; it is back to 0.06s.
- Tests for cross-platform discovery, platform-specific install hints, the
  EasyEDA retry budget, and the BOM fallback.

### Changed
- All 13 MCP tools are now reachable from a slash command. `fetch_part_library`,
  `sch_generate`, `sch_run_erc`, and `package_for_jlcpcb` were exposed over MCP
  and listed in the README while being unreachable from either command.
- `/pcb-new` gained a schematic step (now that schematics load) and an optional
  packaging step, and tells the model to read `warnings` before `errors` —
  a pin-map failure produces one misleading "no pad" error per pin.
- The `net_stats` guidance no longer treats every 1-pad net as a defect; the
  shipped example has a deliberate one, which the example now explains.
- Example docs corrected: the LDO is SOT-223, not SOT-23-5, and the ESP32-C3
  is a module, not a QFN.

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

[Unreleased]: https://github.com/BeckhamLabsLLC/kicad-jlcpcb/compare/v0.12.0...HEAD
[0.12.0]: https://github.com/BeckhamLabsLLC/kicad-jlcpcb/releases/tag/v0.12.0
[0.11.0]: https://github.com/BeckhamLabsLLC/kicad-jlcpcb/releases/tag/v0.11.0
[0.10.0]: https://github.com/BeckhamLabsLLC/kicad-jlcpcb/releases/tag/v0.10.0
[0.9.0]: https://github.com/BeckhamLabsLLC/kicad-jlcpcb/releases/tag/v0.9.0
[0.8.0]: https://github.com/BeckhamLabsLLC/kicad-jlcpcb/releases/tag/v0.8.0
[0.7.0]: https://github.com/BeckhamLabsLLC/kicad-jlcpcb/releases/tag/v0.7.0
[0.6.0]: https://github.com/BeckhamLabsLLC/kicad-jlcpcb/releases/tag/v0.6.0
[0.5.0]: https://github.com/BeckhamLabsLLC/kicad-jlcpcb/releases/tag/v0.5.0
[0.4.0]: https://github.com/BeckhamLabsLLC/kicad-jlcpcb/releases/tag/v0.4.0
[0.3.0]: https://github.com/BeckhamLabsLLC/kicad-jlcpcb/releases/tag/v0.3.0
[0.2.1]: https://github.com/BeckhamLabsLLC/kicad-jlcpcb/releases/tag/v0.2.1
[0.2.0]: https://github.com/BeckhamLabsLLC/kicad-jlcpcb/releases/tag/v0.2.0
[0.1.0]: https://github.com/BeckhamLabsLLC/kicad-jlcpcb/releases/tag/v0.1.0
