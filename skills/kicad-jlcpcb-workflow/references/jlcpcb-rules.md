# JLCPCB design rules and cost model

## Default fab process (PCB only)

JLCPCB's standard 2-layer process is what most projects use. The plugin's `JLCPCB_DEFAULT_RULES` in `config.py` matches it:

| Rule | Value | Notes |
|---|---|---|
| Layers | 2 | 4-layer is +cost but unlocks dense routing |
| Thickness | 1.6 mm | Default; 0.8/1.0/1.2/2.0 also available |
| Min track / clearance | 0.127 mm (5 mil) | JLCPCB economy tier |
| Min via diameter | 0.45 mm | |
| Min via drill | 0.30 mm | |
| Min hole size | 0.30 mm | |
| Solder mask | Green (default) | Black/red/blue/yellow/white +cost |
| Silkscreen | White | Always |
| Surface finish | HASL with lead | ENIG +significant cost |

Most boards never need to deviate from these. `create_project` writes them into the project's `.kicad_pro` design rules, so they apply from the moment the project exists — not at packaging time.

## SMT assembly cost model (the basic-vs-extended thing)

JLCPCB's **SMT assembly service** (where they place parts onto your board for you) has two cost dimensions worth understanding:

### 1. Per-part setup fee

Every **unique** extended-library part incurs a one-time setup fee. As of 2026 this is **~$3.00 USD per unique extended part**. The plugin's `EXTENDED_PART_SETUP_FEE_USD` constant tracks this — bump it if JLCPCB adjusts pricing.

This is per-unique-part, not per-instance. A board with 100 instances of one extended 0603 cap pays the setup fee once.

### 2. No setup fee for basic parts

JLCPCB's **basic library** is the subset of common parts they keep loaded in the assembly machines all the time. Switching to a basic-tier alternative completely eliminates the setup fee for that part.

### Why the plugin hard-prefers basic

For a small-batch hobby board with, say, 8 unique passives + 3 unique ICs:
- **All basic:** $0 setup fees → assembly cost driven only by per-board labor
- **3 extended ICs:** $9 in setup fees on top of labor
- **All extended:** $33 in setup fees → can be 2-3× the per-board cost on a 5-board run

This is why `lcsc_resolve_bom` always tries the basic library first and surfaces the cost-delta loud and clear when it has to fall back.

## What the basic library actually contains (rules of thumb)

- **Passives:** virtually all common 0402/0603/0805/1206 R/C/L values are basic
- **Discrete semis:** common BJTs, MOSFETs, schottky diodes in SOT-23 are mostly basic
- **Voltage regulators:** AMS1117 family, common LDOs in SOT-89/SOT-223 are basic
- **Logic:** 74HC family in SOIC-14, basic op-amps in SOIC-8 are basic
- **Connectors:** USB-C 16-pin, JST PH/XH/SH, common pin headers are basic
- **Microcontrollers:** almost always extended, including ESP32 modules. Don't assume otherwise — the ESP32-C3-WROOM-02 in this plugin's own example is extended. Check `lcsc_search` rather than guessing; on a board with one microcontroller the $3 setup fee is usually unavoidable and worth stating plainly at the checkpoint.
- **Specialty ICs (RF, ADC, motor drivers, sensors):** almost always extended

The boundary moves over time as JLCPCB rotates their assembly inventory. Trust the live `lcsc_search` output, not memorized assumptions.

## Manufacturing file requirements

JLCPCB's web upload accepts a single zip file containing:

1. **Gerber files** (RS-274X, **not** X2) named with Protel extensions:
   - `<board>.GTL` — top copper
   - `<board>.GBL` — bottom copper
   - `<board>.GTO` — top silkscreen (overlay)
   - `<board>.GBO` — bottom silkscreen
   - `<board>.GTS` — top soldermask (stop)
   - `<board>.GBS` — bottom soldermask
   - `<board>.GTP` — top paste (assembly only, optional for bare PCB)
   - `<board>.GBP` — bottom paste
   - `<board>.GM1` — board outline (mechanical layer 1)
   - For 4-layer: `<board>.G2L`, `<board>.G3L` for inner layers

2. **Drill file** — single merged Excellon, named `<board>.XLN` (or `.TXT`)

3. **Optional for assembly:**
   - **CPL** — pick-and-place CSV with reference, value, package, X, Y, rotation, side
   - **BOM** — comma-separated reference, value, **LCSC C-number** (critical), description

The plugin's `gerber_pack.pack_for_jlcpcb` produces all of this in the right naming scheme. The KiCad export commands inside `kicad_cli.py` use the right flags (`--no-x2`, merged drill, both sides for CPL).

## Common rejection reasons from JLCPCB review

When a board is rejected at upload, it's almost always one of:

- **Missing edge cuts (.GM1)** — board outline must be a closed polygon on the Edge.Cuts layer
- **Soldermask covering exposed pads** — unlikely if you use KiCad's defaults
- **Gerber X2 instead of X1** — KiCad 8 defaults to X2; the plugin forces `--no-x2`
- **Drill file in inches when board is metric** — the plugin forces `--excellon-units mm`
- **Missing one of the four solder/paste layers** — the plugin's "Missing typical JLCPCB layers" warning catches this
- **0.15mm clearance violations** — JLCPCB economy is 0.127mm minimum; cheaper "ultra economy" tiers have stricter rules

The `package_for_jlcpcb` warnings array surfaces all of these before you upload.
