# Troubleshooting

Common failure modes and how to resolve them. If your issue isn't here, please [open an issue](https://github.com/BeckhamLabsLLC/kicad-jlcpcb/issues) with the output of `kicad-cli --version`, your Python version, and the exact prompt that triggered the failure.

---

## Install & startup

### `kicad-jlcpcb` command not found

You installed the plugin but the MCP server won't start, or Claude Code reports the MCP server is missing.

**Fix:** `pip install -e .` from the plugin directory. This installs the `kicad-jlcpcb` entry-point script onto your `PATH`. If you used a virtualenv, the `.venv/bin/kicad-jlcpcb` is what `.mcp.json` needs to find — either activate the venv before launching Claude Code or edit `.mcp.json` to point `"command"` at `/abs/path/to/.venv/bin/kicad-jlcpcb`.

### `kicad-jlcpcb failed to start: missing Python dependencies (mcp, httpx)`

The preflight check fired. `pip install -e .` from the plugin directory, in the same Python environment that `which python3` resolves to.

### `ImportError: No module named pcbnew`

KiCad's `pcbnew` is shipped as part of KiCad itself, not pip. If `pcb_generate` fails with this error:

- **Fedora:** `sudo dnf install kicad` (the main package includes the Python bindings).
- **Ubuntu:** `sudo apt install kicad python3-pcbnew` (the bindings may be a separate package).
- **macOS:** Install KiCad from the official installer. KiCad.app embeds a Python, so you may need to run Claude Code under that same interpreter.
- **KiCad 11+ removed SWIG bindings.** Stay on KiCad 9 or 10 until the plugin is updated to the new API.

---

## Detection & versioning

### `detect_kicad` returns `meets_min: false`

The plugin requires KiCad 8.0 or newer. Upgrade:

- **Fedora 40+:** `sudo dnf install kicad` ships 9.x.
- **Ubuntu 22.04+:** `sudo add-apt-repository ppa:kicad/kicad-9.0-releases && sudo apt install kicad`
- **Arch:** `sudo pacman -Syu kicad`
- **macOS:** Install the latest stable from [kicad.org/download](https://www.kicad.org/download/).

---

## Part sourcing (LCSC / JLCPCB)

### Where part data comes from

Two live services, neither official, both overridable:

| Service | Used for | Override |
|---|---|---|
| `jlcsearch.tscircuit.com` | Catalog search, JLCPCB stock, basic/extended tier | `KJLC_JLCSEARCH_BASE` |
| `easyeda.com` | Exact C-number lookup, symbols, pin maps | `KJLC_EASYEDA_BASE` |

Results are cached per-part in `~/.cache/kicad-jlcpcb/lcsc_parts.sqlite` for
24 hours. There is no bulk catalog download — the first query costs one HTTP
round trip.

### `lcsc_search` returns nothing

1. Check the services are up:
   ```bash
   KJLC_NETWORK_TESTS=1 pytest tests/test_network_contract.py -v
   ```
   If these fail, part sourcing is broken for everyone, not just you —
   please [open an issue](https://github.com/BeckhamLabsLLC/kicad-jlcpcb/issues).
2. Loosen the query. Upstream AND-matches every word, so
   `basic_only=false` and fewer terms both help. The client already drops
   the least-matchable words and retries, but it will not narrow a
   five-word query below three words — one leftover word matches half the
   catalog, and a wrong part is worse than no part.
3. For a resistor or capacitor, put the value and the chip size in the
   query (`10k 0603`, `0.1uF 0402`). Those route to a structured lookup
   that filters on the parsed value, which is far more accurate than text
   search. `0.1uF` and `100nF` are treated as the same part.

### Stock shows as 0 with a warning

A part resolved by exact C-number comes from EasyEDA, which reports LCSC
*retail* stock rather than JLCPCB SMT inventory — it reads 0 for parts with
millions in stock. The plugin flags these rather than printing a misleading
number. It tries to backfill real stock from jlcsearch, which works when the
part is text-searchable; some passives with empty upstream descriptions
aren't. Confirm on jlcpcb.com before ordering.

### Stale prices or stock

Delete the cache to force a refetch:

```bash
rm ~/.cache/kicad-jlcpcb/lcsc_parts.sqlite
```

Or pass `force_refresh: true` to `lcsc_search` / `lcsc_resolve_bom`.

### `lcsc_resolve_bom` says a C-number is not found

Exact C-numbers resolve through EasyEDA. If it 404s for a part that exists:

- Confirm the C-number on LCSC's site — a transposed digit is the usual cause.
- EasyEDA rate-limits aggressively. The plugin throttles to one request per
  12 seconds and backs off for 60 s on a 403, so a large BOM takes a few
  minutes to warm on first run. That is expected, not a hang.
- Use `lcsc_search` to find an equivalent part instead.

### Every result is extended-tier

Your query is too specific. Try:

- Loosen tolerance: `10k 0603` instead of `10k 0603 0.1%`.
- Drop temperature rating: `C0G NP0` → just cap value + package.
- Widen package options: `4.7uF 0603 OR 0805`.

If extended-tier is unavoidable, note the cost impact: JLCPCB charges ~$3 USD per unique extended part for SMT assembly setup.

---

## EasyEDA pin-map fetch (`part_pin_map`, `pcb_generate`)

### Pausing for 12 seconds per IC on first run

This is the rate-limit throttle (EasyEDA serves ~1 request per 12 s per IP before returning 403). It only runs once per IC; the result is cached indefinitely in `~/.cache/kicad-jlcpcb/easyeda_pinmaps.sqlite`. A 10-IC project takes ~2 minutes to warm the cache, then is instant forever.

Reduce the delay for testing only:

```python
# NOT for production — violates EasyEDA's anti-bot
export KJLC_TEST_FAST=1  # not currently honored; edit part_library.EASYEDA_MIN_DELAY_SECONDS
```

### `EasyEDA has no component data for CXXXXXX`

Some LCSC parts — especially older or very exotic ones — don't have symbol/footprint data in EasyEDA. Workarounds:

- Provide an explicit `pinmap` field in the component spec: `{"ref": "U1", "lcsc": "C...", "pinmap": {"VIN": "1", "GND": "3"}}`.
- Pick an alternative LCSC part that shares the pinout.
- Fall back to a generic footprint (2-pin for passives, numbered pads for everything else).

### EasyEDA 403 / "backing off 60s"

The anti-bot system triggered despite our throttling. The plugin waits 60 s and retries once. If the second attempt also fails, you'll get `PartLibraryError`. Cause is almost always:

- You ran a big `pcb_generate` batch right after a previous failed run (state might be out of sync).
- Your IP is on a shared exit node that hit the quota (VPN, corporate egress).
- Restart Claude Code, wait 5 min, and retry. If it keeps failing, hardcode pinmaps in the spec.

---

## PCB generation (`pcb_generate`)

### `Footprint library not found` / `Footprint not found`

The `lib` / `fp` fields in your component spec don't match KiCad's stock
libraries. The error says how many libraries were found and suggests near
matches; look up the exact name with:

```bash
# Linux
ls /usr/share/kicad/footprints/Resistor_SMD.pretty/ | grep 0603
# macOS
ls "/Applications/KiCad/KiCad.app/Contents/SharedSupport/footprints/Resistor_SMD.pretty/" | grep 0603
```

### `KiCad's footprint libraries could not be found`

The plugin probes `KJLC_FOOTPRINT_DIR`, then KiCad's own
`KICAD{10,9,8}_FOOTPRINT_DIR`, then the standard install locations for
Linux, macOS, Windows and Flatpak. If none hit, point it at the directory
containing the `.pretty` folders:

```bash
export KJLC_FOOTPRINT_DIR=/path/to/kicad/footprints
```

Or pass `lib_dir` to `pcb_generate` for a one-off.

Common correct names:

| Type | lib | fp |
|---|---|---|
| 0603 resistor | `Resistor_SMD` | `R_0603_1608Metric` |
| 0603 capacitor | `Capacitor_SMD` | `C_0603_1608Metric` |
| SOT-23-5 IC | `Package_TO_SOT_SMD` | `SOT-23-5` |
| ESP32-C3 | `RF_Module` | `ESP32-C3-WROOM-02` |
| USB-C 16-pin | `Connector_USB` | `USB_C_Receptacle_HRO_TYPE-C-31-M-12` |

### `Pin name 'X' not found on U1`

The pin name you referenced in the nets dict isn't in the pinmap for that component. Either:

- Call `part_pin_map` with the C-number to see what names are available.
- Use pad numbers instead of names (for passives: `"1"`, `"2"`).
- Fetch a fresh pinmap with `force_refresh=true`.

### `nets_created == 0` or nets with fewer than 2 pads

A net with 0-1 pads means you listed a single terminal with no partner, or all the referenced components are missing. Inspect `result.warnings` and `result.errors`.

---

## Session persistence

### `/pcb-new` offers to "resume" when I want a fresh board

The plugin found a `.kicad_jlcpcb_session.json` in the target directory. Either:

- Pick a different project name so a new directory is created.
- Delete `.kicad_jlcpcb_session.json` in the existing directory.
- Resume — most mid-flow progress is safe to pick up.

### `session_resume` says "no session found"

The session file was never created (the project was made outside this plugin) or was deleted. Start over with `/pcb-new` or manually construct a spec and call `pcb_generate`.

---

## JLCPCB upload (`package_for_jlcpcb`)

### "No PCB file" error

Most users do not need this legacy tool — the EasyEDA handoff flow is preferred. If you do want the zip path, first route your board in KiCad or open it in EasyEDA and export, then re-run.

### JLCPCB web upload rejects the zip

Most common causes:

- **Missing Edge.Cuts layer.** KiCad won't export an Edge.Cuts Gerber if your board has no outline. Draw a rectangle on the Edge.Cuts layer first.
- **Non-X2 Gerber format.** KiCad 8/9 default to X2, which JLCPCB accepts. Check your project settings if you explicitly changed formats.
- **Unmerged drill files.** The plugin prefers the merged `.drl`. If you see split PTH/NPTH files in the zip, KiCad was configured for separate files — change in `File → Plot → Drill` preferences.

---

## Server won't start

### `'Server' object has no attribute 'list_tools'`

You have `mcp` 2.x installed. The server is built on the low-level
`mcp.server.Server` API, which 2.x reorganised. `pyproject.toml` pins
`mcp>=1.0.0,<2`; if you installed before that pin landed, or forced a newer
version:

```bash
pip install -e . --upgrade
python -c "import importlib.metadata as m; print(m.version('mcp'))"   # expect 1.x
```

### `ENOENT: Executable not found in $PATH: kicad-jlcpcb`

You are on an old `.mcp.json` that invoked a bare `kicad-jlcpcb` command.
Pull the latest — it now launches `python3 -m kicad_jlcpcb_mcp` with
`PYTHONPATH` set to the plugin root, so nothing needs to be on `PATH`.

---

### `pcb_generate` seems to hang

It is fetching EasyEDA pin maps, which are rate-limited to one request per
12 seconds. Only parts whose nets reference pins by *name* need one —
anything wired by pad number is skipped, so a board of mostly passives
finishes far quicker than its part count suggests. The shipped example
needs 3 fetches for 13 parts.

Results are cached indefinitely, so the second run on the same parts is
instant. To avoid the wait entirely, pass `auto_fetch_pinmaps: false` and
reference pads by number.

---

## Tests (contributors)

### `ModuleNotFoundError: No module named 'kicad_jlcpcb_mcp'`

Run pytest from the repo root — `pyproject.toml` sets `pythonpath = ["src"]`:

```bash
pytest tests/
```

If you invoke it from elsewhere, install the package first with `pip install -e .`.

### Integration tests skip

The `pcbnew`-dependent tests are gated by `KICAD_INSTALLED=1`:

```bash
KICAD_INSTALLED=1 pytest tests/test_pcb.py -v
```

The live upstream contract tests are gated separately, because they hit the
network:

```bash
KJLC_NETWORK_TESTS=1 pytest tests/test_network_contract.py -v
```

Run those if part sourcing is misbehaving — they check the two upstream APIs
directly and will tell you whether the problem is upstream or in the plugin.

---

Still stuck? [File an issue](https://github.com/BeckhamLabsLLC/kicad-jlcpcb/issues) with a minimal reproduction.
