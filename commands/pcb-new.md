---
name: pcb-new
description: Start a new PCB project from a text description. Sources parts, generates a wired .kicad_pcb, and hands off to EasyEDA for routing + JLCPCB ordering.
argument-hint: "[short description of the board you want to design]"
allowed-tools:
  - Read
  - Write
  - Glob
  - Grep
  - Task
  - mcp__kicad-jlcpcb__detect_kicad
  - mcp__kicad-jlcpcb__create_project
  - mcp__kicad-jlcpcb__load_project
  - mcp__kicad-jlcpcb__session_resume
  - mcp__kicad-jlcpcb__session_confirm_bom
  - mcp__kicad-jlcpcb__lcsc_search
  - mcp__kicad-jlcpcb__lcsc_resolve_bom
  - mcp__kicad-jlcpcb__part_pin_map
  - mcp__kicad-jlcpcb__pcb_generate
  - mcp__kicad-jlcpcb__fetch_part_library
  - mcp__kicad-jlcpcb__sch_generate
  - mcp__kicad-jlcpcb__sch_run_erc
  - mcp__kicad-jlcpcb__package_for_jlcpcb
  - mcp__kicad-jlcpcb__easyeda_handoff
---

# /pcb-new

Take a text description of a PCB and deliver a ready-to-import KiCad board file the user drags into EasyEDA for routing and ordering.

## Instructions

### 1. Detect KiCad

Call `detect_kicad`. If `meets_min` is false, stop and pass the `install_hint` to the user. KiCad's `pcbnew` Python module is required by `pcb_generate`.

### 2. Check for a resumable session, then create or load the project

Ask the user where the project should live (default: current working directory) and what to name it.

If a `<parent_dir>/<name>/` directory already exists:
- Call `load_project` on it.
- If the response has `resume_available: true`, show the user the session summary (stage, completed checkpoints, next step) and ask whether to **resume** or start a **fresh** project in a different directory.
- If resuming, skip any step the session has already checkpointed (e.g. skip part sourcing if `parts_sourced` is already complete — just reuse the persisted `bom` and go to the BOM checkpoint).

Otherwise, call `create_project` with `parent_dir` and `name`. Each subsequent tool call automatically updates the session file, so mid-flow restarts are safe.

### 3. Decompose the description into a parts list

Read the user's description carefully. List the components the board will need: microcontroller, regulator, decoupling caps, connectors, indicator LEDs, etc. For each, write a one-line generic spec like `"3.3V LDO, SOT-23-5, 500mA, basic-tier preferred"`.

### 4. Source parts

Dispatch one `part-sourcer` agent per generic spec, in parallel — a single message with one Task call per spec. Each agent applies the basic-first ladder and returns a structured pick. For parts the user already has LCSC C-numbers for, skip to step 5.

For one or two specs, or when an agent comes back with no match and you want to retry by hand, call `lcsc_search` directly instead: `basic_only=true` first, then `basic_only=false`.

When writing a spec, put the **value and package in the query** (`10k 0603`, `0.1uF 0402`). Values route to an exact structured lookup; `0.1uF` and `100nF` are the same part. Keep queries short — the catalog AND-matches every word, so `TVS SMB 30V` finds parts that `bidirectional TVS diode rated 30 volts in SMB package` does not.

Collect picks into a BOM array of `{lcsc, qty}` rows and call `lcsc_resolve_bom` to validate and tally the extended-part setup fee.

Watch for a `stock_unknown` flag on any resolved part — its JLCPCB stock could not be verified, and you must say so at the checkpoint rather than presenting the number as fact.

### 5. **CHECKPOINT — present the BOM**

Show the user:
- Each resolved part (C-number, mfr part, package, tier, stock)
- **Every extended-tier part with its cost warning**
- The `estimated_setup_fee_usd` total
- Anything in `unresolved`

Ask whether to swap any extended parts for basic alternatives or proceed. **Do not call `pcb_generate` until the user confirms.**

When they confirm, call `session_confirm_bom` before moving on — pass any change they asked for as `notes`. Without it the approval exists only in this conversation, and a Claude Code restart will ask them to approve the same BOM again. Spending money on a board is the one decision here worth making durable.

### 6. Build the PCB spec

Construct a spec dict for `pcb_generate`:

```json
{
  "name": "<project name>",
  "board": {"width_mm": 80, "height_mm": 60, "layer_count": 2},
  "components": [
    {
      "ref": "U1",
      "value": "ESP32-C3-WROOM-02",
      "lcsc": "C2934560",
      "lib": "RF_Module",
      "fp": "ESP32-C3-WROOM-02"
    },
    ...
  ],
  "nets": {
    "3V3": [["U1", "3V3"], ["C1", "1"], ...],
    "GND": [["U1", "GND"], ["C1", "2"], ...],
    "SPI_SCK": [["U1", "GPIO10"], ["U2", "SCK"]],
    ...
  }
}
```

**Do not hardcode pin maps.** The plugin auto-fetches them from EasyEDA. Reference pins by their **functional names** (GND, VCC, 3V3, GPIO10, SDA, MOSI, SCK, NSS, etc.) and the plugin resolves them to pad numbers at generation time. If you're unsure what pin names a specific IC exposes, call `part_pin_map` for that C-number first.

For the KiCad stdlib footprint fields (`lib`, `fp`), use standard KiCad footprint library names. Common picks:
- Resistors: `Resistor_SMD` / `R_0402_1005Metric`, `R_0603_1608Metric`, `R_0805_2012Metric`
- Capacitors: `Capacitor_SMD` / `C_0402_1005Metric`, `C_0603_1608Metric`, `C_0805_2012Metric`
- SOT-23-5/6: `Package_TO_SOT_SMD` / `SOT-23-5`, `SOT-23-6`
- ESP32-C3: `RF_Module` / `ESP32-C3-WROOM-02`
- SX1262 QFN-24: `Package_DFN_QFN` / `QFN-24-1EP_4x4mm_P0.5mm_EP2.7x2.7mm`
- USB-C 16P: `Connector_USB` / `USB_C_Receptacle_HRO_TYPE-C-31-M-12`
- JST-PH: `Connector_JST` / `JST_PH_B2B-PH-K_1x02_P2.00mm_Vertical` (or `B3B` for 3-pin)
- Crystal 3225: `Crystal` / `Crystal_SMD_3225-4Pin_3.2x2.5mm`

### 7. Generate the PCB

Call `pcb_generate` with the spec. First run may pause ~12 seconds per unique IC while the plugin fetches pin maps from EasyEDA. Subsequent runs on the same parts are instant (SQLite cache).

Inspect the result:
- `footprints_placed` should equal the number of components
- `nets_created` should equal the number of nets in your spec
- `errors` should be empty (any errors here indicate a wrong footprint name or pin name)
- Every entry in `net_stats` should have ≥2 pads — a 1-pad net is usually a typo, though a deliberate test point or mounting pad is a legitimate exception. Call it out either way rather than silently accepting it.

**Read `warnings` before you read `errors`.** A pin-map fetch failure is reported as a warning and then produces one "no pad <name>" error per pin of that part. Those errors are a symptom; rewriting the netlist to chase them makes things worse. Fix the warning first.

If there are genuine errors, fix the spec and re-run.

### 8. Generate the schematic

Call `sch_generate` with a netlist spec derived from the same components and nets, then `sch_run_erc`.

The schematic is not required for the board — `pcb_generate` already produced it — but it gives the user something to open and check, and ERC catches connectivity mistakes the PCB stage cannot. Expect `lib_symbol_issues` warnings: the symbols are embedded in the file rather than installed as a library, which is normal and harmless.

Report the ERC result honestly. An unconnected pin is a real finding worth surfacing, not noise to skip past.

### 9. Package for JLCPCB (optional, ask first)

If the user wants manufacturing files without routing in EasyEDA first, call `package_for_jlcpcb`. Be explicit that the board is **unrouted** — the zip will be fabricable but the copper won't connect anything. Most users should route in EasyEDA first (next step) and package afterwards.

### 10. **TERMINAL STEP — hand off to EasyEDA**

Call `easyeda_handoff`. Relay the response verbatim to the user — specifically:

- The path to the generated `.kicad_pcb`
- The step-by-step EasyEDA import instructions
- The `why_easyeda` explanation
- The `alternative` KiCad-based flow if they'd rather not use EasyEDA

Phase 1.6 stops here. The plugin has done everything it can automate: parts, pin maps, footprints, placement, connectivity. Routing and ordering are reliably automated by EasyEDA's web app, so the plugin hands off rather than trying to ship its own broken headless auto-router.
