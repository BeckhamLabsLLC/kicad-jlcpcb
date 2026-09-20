---
name: kicad-jlcpcb-workflow
description: |
  Use this skill when the user asks to "design a PCB", "make a board", "build a PCB project", "order from JLCPCB", "create a schematic", "source LCSC parts", "wire up a board", "generate a PCB from a BOM", or any KiCad + JLCPCB workflow. Also use when working with `.kicad_pro` or `.kicad_pcb` files. Handles project setup, LCSC part sourcing, EasyEDA pin-map fetching, fully-wired `.kicad_pcb` generation via pcbnew, and hand-off to EasyEDA for routing + JLCPCB ordering.
---

# KiCad → EasyEDA → JLCPCB Workflow

Help the user take a PCB project from "I want a board that does X" to "I clicked Order in EasyEDA and it's on its way from JLCPCB." The plugin automates parts sourcing, pin-map lookup, footprint placement, and net wiring — which was the slow tedious part. Routing and ordering happen in EasyEDA's web app because its cloud auto-router handles real boards (including RF) far more reliably than anything shippable headlessly.

## The seven stages

1. **Project setup** — `detect_kicad`, `create_project` (or `load_project`)
2. **Component sourcing** — `lcsc_search`, `lcsc_resolve_bom`
3. **Pin maps** — `part_pin_map` (auto-called by `pcb_generate`, but you can inspect individual parts)
4. **PCB generation** — `pcb_generate` produces the wired `.kicad_pcb`
5. **EasyEDA handoff** — `easyeda_handoff` gives the user the import instructions
6. **Routing** — *user, in EasyEDA web app* (Auto Route button)
7. **Ordering** — *user, in EasyEDA* (PCB Order via JLCPCB button)

## Resume-or-restart on an existing project

Every project writes a `.kicad_jlcpcb_session.json` to its root that tracks which stages have completed (`project_created` → `parts_sourced` → `bom_confirmed` → `pcb_generated` → `handoff_rendered`).

When the user points `/pcb-new` or `/pcb-from-bom` at a path that already has a project:

1. Call `load_project` on it. The response includes a `session` summary and `resume_available: true` if the prior workflow got past stage `created`.
2. Show the user the session summary: completed checkpoints, the persisted BOM length, whether a spec has been generated, what's next.
3. Ask whether to **resume** or **start fresh**. Resume means re-using the persisted `bom` / `spec` and skipping the stages that are already done. Starting fresh means deleting `.kicad_jlcpcb_session.json` (or picking a different directory) so the workflow begins at stage `created` again.
4. You can also call `session_resume` directly on any project directory to inspect state without loading the project as the active workspace.

Use this generously — Claude Code restarts are common mid-flow, and the session file makes resuming cheap.

## Why EasyEDA handoff

I tried shipping headless Freerouting in an earlier phase. It doesn't work on RF boards (RF matching networks with picofarad/nanohenry components create maze-search constraints the open-source router can't solve). It also has a known CLI bug where `-mp` is ignored and it won't save partial results. **EasyEDA's cloud auto-router handles the same boards reliably** and it's owned by the same company as JLCPCB, so ordering is a single click. The plugin stops at "wired `.kicad_pcb`" because that's the boundary where automation breaks down in the open-source world but works cleanly in EasyEDA.

## Checkpoint discipline

There is one hard checkpoint: **after BOM resolution, before PCB generation.** Always show the user:
- Every resolved part with its C-number, package, tier (basic/extended), and stock
- **Every extended-tier part with the cost warning**, bold
- The `estimated_setup_fee_usd` total
- Anything in `unresolved`

Ask whether to swap any extended parts for basic alternatives. **Do not call `pcb_generate` until the user confirms.**

## The basic-vs-extended decision tree

JLCPCB charges ~$3 per unique extended-library part as a one-time SMT assembly setup fee. For a small-batch board, 10 extended parts = $30 extra regardless of quantity. The plugin's `lcsc_resolve_bom` tallies this automatically.

Decision tree per component:

```
Need a part →
  lcsc_search(basic_only=True)
  Got results? → pick the top by stock
  No results? →
    lcsc_search(basic_only=False)
    Got results? → warn user with cost impact, ask
    No results? → mark unresolved, ask user for guidance
```

## The PCB spec format

`pcb_generate` consumes a dict with this shape:

```json
{
  "name": "soilnode",
  "board": {"width_mm": 80, "height_mm": 60, "layer_count": 2},
  "components": [
    {
      "ref": "U1",
      "value": "ESP32-C3-WROOM-02",
      "lcsc": "C2934560",
      "lib": "RF_Module",
      "fp": "ESP32-C3-WROOM-02"
    },
    {
      "ref": "R1",
      "value": "10k",
      "lcsc": "C25804",
      "lib": "Resistor_SMD",
      "fp": "R_0603_1608Metric"
    }
  ],
  "nets": {
    "3V3": [["U1", "3V3"], ["R1", "1"]],
    "GND": [["U1", "GND"], ["R1", "2"]],
    "I2C_SCL": [["U1", "GPIO9"], ["U3", "SCL"]]
  }
}
```

**Critical rule: reference IC pins by NAME, not number.** The plugin auto-fetches pin maps from EasyEDA for any component with an `lcsc` field. You write `["U1", "GPIO10"]` and the plugin resolves it to the right pad number. For passives (resistors, caps) use bare pad numbers `"1"` and `"2"`.

If you need to know what pin names a specific IC exposes (e.g. is it `"VCC"` or `"VDD"`, `"GND"` or `"VSS"`?), call `part_pin_map` with the C-number before building the spec:

```
part_pin_map(lcsc="C82942")
→ {
    "title": "ME6211C33M5G-N",
    "pin_count": 5,
    "pinmap": {"VIN": "1", "VSS": "2", "CE": "3", "NC": "4", "VOUT": "5"}
  }
```

Now you know the LDO uses `VIN/VSS/VOUT` not `VCC/GND/VOUT`, and you can write nets that match.

## KiCad stdlib footprint reference

The plugin loads footprints from KiCad's stock libraries, `<footprint-dir>/<lib>.pretty/<fp>.kicad_mod`. It finds that directory itself on Linux, macOS, Windows and Flatpak. Common picks:

| Component | lib | fp |
|---|---|---|
| Resistor 0402/0603/0805 | `Resistor_SMD` | `R_0402_1005Metric` / `R_0603_1608Metric` / `R_0805_2012Metric` |
| MLCC 0402/0603/0805 | `Capacitor_SMD` | `C_0402_1005Metric` / `C_0603_1608Metric` / `C_0805_2012Metric` |
| SOT-23-5 (LDO) | `Package_TO_SOT_SMD` | `SOT-23-5` |
| SOT-23-6 (dual FET, protection IC) | `Package_TO_SOT_SMD` | `SOT-23-6` |
| VSSOP-10 (ADS1115) | `Package_SO` | `VSSOP-10_3x3mm_P0.5mm` |
| QFN-24 w/ EP (SX1262) | `Package_DFN_QFN` | `QFN-24-1EP_4x4mm_P0.5mm_EP2.7x2.7mm` |
| DFN-8 (CN3065) | `Package_DFN_QFN` | `DFN-8-1EP_3x3mm_P0.65mm_EP1.55x2.4mm` |
| SOIC-8 | `Package_SO` | `SOIC-8_3.9x4.9mm_P1.27mm` |
| ESP32-C3 module | `RF_Module` | `ESP32-C3-WROOM-02` |
| USB-C 16P | `Connector_USB` | `USB_C_Receptacle_HRO_TYPE-C-31-M-12` |
| JST-PH 2/3 pin | `Connector_JST` | `JST_PH_B2B-PH-K_1x02_P2.00mm_Vertical` / `JST_PH_B3B-PH-K_1x03_P2.00mm_Vertical` |
| Crystal 3225 | `Crystal` | `Crystal_SMD_3225-4Pin_3.2x2.5mm` |
| 0603 LED | `LED_SMD` | `LED_0603_1608Metric` |
| Tactile switch SMD | `Button_Switch_SMD` | `SW_SPST_TL3342` (4 copper pads, 2 nets: pad "1" + pad "2") |

If the user asks for a part not in this reference, list the footprint directory via Bash and pick the closest match by dimensions (on Linux that is usually `/usr/share/kicad/footprints/`; on macOS `/Applications/KiCad/KiCad.app/Contents/SharedSupport/footprints/`). A wrong `lib`/`fp` produces an error naming near matches, so guessing once is cheap.

## Common IC pinouts the user should double-check

The EasyEDA pin-name extraction is accurate but uses datasheet nomenclature, which doesn't always match what humans call pins. Two examples that bit us during the SoilNode trial:

- **ESP32-C3-WROOM-02 doesn't break out GPIO0.** The 18-pin module exposes GPIO1-10, 18-21 + bootstraps. If the user's design claims "GPIO0 = boot button," remap it to GPIO9 (the real hardware boot-strap pin). Same module doesn't have GPIO11 either.
- **LDOs use VIN/VSS/VOUT** not VCC/GND/VOUT. Some protection ICs use VDD not VCC. Always `part_pin_map` before writing nets if you're uncertain.

## Failure modes to recognize

- **`detect_kicad` returns `meets_min: false`** — pass the `install_hint` through verbatim.
- **`lcsc_resolve_bom` unresolved rows** — show and ask for substitutions.
- **`part_pin_map` raises "no component data"** — that LCSC number isn't in EasyEDA's library. You'll need to hardcode the pinmap in the component spec (provide a `"pinmap": {...}` field).
- **`pcb_generate` errors list non-empty** — check each error:
  - `"X: Footprint not found"` — wrong `lib`/`fp` name. The error lists near matches; if it says no libraries were found at all, KiCad's footprints are somewhere unusual and `KJLC_FOOTPRINT_DIR` needs setting.
  - `"net X: Y has no pad Z"` — pin name doesn't match EasyEDA's data, call `part_pin_map` and fix
- **EasyEDA rate-limited warning in logs** — first run of `pcb_generate` pauses ~12 seconds per part whose nets reference pins by *name*. Parts wired by pad number are skipped entirely, so a board of mostly passives is far quicker than the part count suggests. Subsequent runs are instant.

## Before telling the user the board is ready

Two checks, both of which the tools report and neither of which is optional:

1. **`fetch_part_library` warnings.** If EasyEDA had no geometry for a part,
   the footprint written is a placeholder that does *not* match the real
   component. Say so explicitly and point at KiCad's standard libraries for a
   replacement. Never present a placeholder as a finished footprint.
2. **`package_for_jlcpcb` warnings.** CPL rotations are passed through from
   the board. JLCPCB's expected orientation differs from KiCad's for some
   packages, so tell the user to check the assembly preview after upload.
   EasyEDA-derived footprints are already in JLCPCB's convention;
   KiCad-stdlib ones may not be.

And relay `easyeda_handoff`'s `before_you_order` verbatim. It is the last thing
the user reads before spending money.

## `pcb_generate` overwrites the board file

`pcb_generate` builds the board from scratch every time and saves over the
project's `.kicad_pcb` without asking. There is no merge and no backup.

So if the user may have opened the project in KiCad and placed, routed or
edited anything by hand, **say so before re-running it** and let them decide.
Re-running after only a spec change is fine; re-running after manual work
silently discards that work.

## Reference docs

- `references/jlcpcb-rules.md` — JLCPCB design rules, cost model, manufacturing file requirements
- `references/lcsc-search.md` — Where part data comes from and how to write queries that find parts
- `references/troubleshooting.md` — Quick reference for common runtime failure modes
- Worked sample project: [`examples/soilnode-esp32/`](../../examples/soilnode-esp32/) in the repo root — full spec, BOM, and step-by-step walkthrough
