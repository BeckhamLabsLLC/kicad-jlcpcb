---
name: pcb-from-bom
description: Start a PCB project from an existing LCSC BOM file (CSV with C-numbers) and a design-intent description.
argument-hint: "[path to BOM CSV] [optional: design intent notes]"
allowed-tools:
  - Read
  - Write
  - Glob
  - Grep
  - mcp__kicad-jlcpcb__detect_kicad
  - mcp__kicad-jlcpcb__create_project
  - mcp__kicad-jlcpcb__load_project
  - mcp__kicad-jlcpcb__session_resume
  - mcp__kicad-jlcpcb__lcsc_resolve_bom
  - mcp__kicad-jlcpcb__part_pin_map
  - mcp__kicad-jlcpcb__pcb_generate
  - mcp__kicad-jlcpcb__fetch_part_library
  - mcp__kicad-jlcpcb__sch_generate
  - mcp__kicad-jlcpcb__sch_run_erc
  - mcp__kicad-jlcpcb__package_for_jlcpcb
  - mcp__kicad-jlcpcb__easyeda_handoff
---

# /pcb-from-bom

Start a KiCad project from an existing BOM CSV. Skips part sourcing and goes straight to pin-map fetch + wired `.kicad_pcb` + EasyEDA handoff.

## Instructions

### 1. Detect KiCad, create or resume the project

Call `detect_kicad`. If the target project directory already exists, call `load_project` on it and check the returned `session` summary — if `resume_available` is true, offer the user a resume-or-restart choice (same pattern as `/pcb-new` step 2). Otherwise call `create_project`.

### 2. Read the BOM

Use `Read` to load the CSV. Auto-detect the column layout — common forms:
- `Reference, Value, LCSC, Quantity`
- `Designator, Value, LCSC Part #, Qty`
- `Comment, Designator, Footprint, LCSC`

Convert each row into a `{lcsc, qty}` dict.

### 3. Resolve the BOM

Call `lcsc_resolve_bom` with the rows. Validates every C-number against the live catalog and catches any out-of-stock or discontinued parts.

### 4. **CHECKPOINT — present the BOM**

Same as `/pcb-new` step 5. Show the user resolved/unresolved/cost and get confirmation.

### 5. Interpret design intent

If the BOM file has a design-intent section (GPIO table, functional descriptions, schematic notes) or the user provided notes alongside the BOM:

a. **Read it carefully.** The design intent tells you how parts connect.

b. **Map every IC to a reasonable footprint.** KiCad stdlib names are listed in `/pcb-new` step 6. For ICs not in KiCad stdlib, pick the closest package-family footprint (SOT-23-6, QFN-24, SOIC-8, etc.) — the user can swap it in EasyEDA later.

c. **Call `part_pin_map`** for each IC whose pinout you're unsure about. It returns `{pin_name: pad_num}` so you know what the nets need to reference.

d. **Build the nets dict.** Map power rails, buses (I2C, SPI, UART, USB), and signal lines based on the design intent. Reference pins by name, not number — the plugin resolves them.

If the BOM has no design intent and the user provided no notes: warn them that you'll generate a components-only spec (no nets), and they'll have to wire things up manually in KiCad or EasyEDA.

### 6. Generate + hand off

Call `pcb_generate` with the spec, then `easyeda_handoff` on the result. Relay the handoff instructions to the user verbatim.
