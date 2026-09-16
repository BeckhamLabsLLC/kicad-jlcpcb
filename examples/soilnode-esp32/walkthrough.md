# SoilNode end-to-end walkthrough

This is a **shape-of-the-flow** document: the order the tools are called in, the
checkpoint you'll be asked to confirm, and roughly what each response looks
like. It is illustrative, not a recording — the JSON is trimmed for
readability, and stock figures and prices move daily, so treat any number here
as an example rather than a current quote.

Timings are order-of-magnitude on a wired connection. The one that dominates is
EasyEDA's rate limit, and it applies only to parts whose nets reference pins by
*name*; anything wired by pad number is skipped.

## User prompt

```
/pcb-new An ESP32-C3 soil-moisture sensor with two capacitive probes,
         USB-C 5V in, a 3.3V LDO, status LED, and JST-PH battery header.
         Place it on an 80x60 mm board.
```

## Step 1 — KiCad detection (<1 s)

Claude calls `detect_kicad`:

```json
{"found": true, "version": "9.0.8", "meets_min": true, "path": "/usr/bin/kicad-cli"}
```

## Step 2 — Project scaffold (<1 s)

Claude asks where to create the project. You reply `~/pcbs` and name it `soilnode`. Claude calls `create_project`:

```json
{
  "active_project": {
    "name": "soilnode",
    "root": "/home/alex/pcbs/soilnode",
    "pro_path": "/home/alex/pcbs/soilnode/soilnode.kicad_pro",
    "sch_path": "/home/alex/pcbs/soilnode/soilnode.kicad_sch",
    "pcb_path": "/home/alex/pcbs/soilnode/soilnode.kicad_pcb",
    "libs_dir": "/home/alex/pcbs/soilnode/libs",
    "manufacturing_dir": "/home/alex/pcbs/soilnode/manufacturing"
  },
  "session": {
    "stage": "created",
    "next_step": "Source parts (lcsc_search or lcsc_resolve_bom)..."
  }
}
```

The `.kicad_jlcpcb_session.json` is now written to the project root.

## Step 3 — Decompose into generic specs (instant)

Claude works out the bill of materials from the description:

- 1 × ESP32-C3-WROOM-02 module
- 1 × 3.3 V LDO, ≥500 mA, SOT-223
- 1 × USB-C receptacle, 16-pin
- 3 × JST-PH 2-pin header (battery + 2 probes)
- 1 × green status LED, 0603
- 1 × 1 k resistor, 0603, 1 % (LED current limit)
- 1 × 10 k resistor, 0603, 1 % (EN pull-up)
- 2 × 100 nF decoupling, 0603, X7R
- 1 × 10 µF bulk, 0603 (LDO input)
- 1 × 22 µF bulk, 0603 (LDO output)

## Step 4 — Source parts (~5 s, cold cache ~60 s)

Claude runs parallel `lcsc_search` calls, one per generic spec. Each returns the top 3 picks. On first run each unique part costs one HTTP round trip; results are cached locally for 24 hours.

## Step 5 — BOM checkpoint (requires your confirmation)

Claude assembles the picks into a BOM, calls `lcsc_resolve_bom`, and presents:

```
Resolved BOM (13 parts, 1 unique extended):

  Ref  Part                LCSC     Tier      Stock     Price
  U1   ESP32-C3-WROOM-02   C2934560 EXTENDED  15,230    $1.10  ⚠ +$3 setup fee
  U2   AMS1117-3.3         C6186    basic     850,000   $0.05
  J1   USB-C Receptacle    C2765186 basic     42,000    $0.18
  J2-4 JST-PH 2P × 3       C144394  basic     120,000   $0.04
  D1   LED 0603 green      C72043   basic     90,000    $0.01
  R1   1k 0603 1%          C21190   basic     500,000   $0.001
  R2   10k 0603 1%         C25804   basic     850,000   $0.001
  C1   10uF 0603           C15850   basic     220,000   $0.02
  C2   22uF 0603           C45783   basic     180,000   $0.03
  C3-4 100nF 0603 X7R × 2  C14663   basic     1,200,000 $0.002

  Estimated JLCPCB assembly setup fee: $3.00 (1 extended part × $3)
  Part cost per board (qty 10): ~$1.82

Proceed, or swap the ESP32-C3-WROOM-02 for a basic-tier alternative?
```

The session file is now at `stage: parts_sourced`. If you restart Claude Code here, `/pcb-new` will offer to resume from the BOM checkpoint.

You reply "proceed". Claude moves to Step 6.

## Step 6 — Build the PCB spec (instant)

Claude builds the dict shown in `spec.json`. Notice:

- Every IC pin is referenced by **functional name** (`3V3`, `GND`, `EN`, `GPIO10`, etc.), not pad number. The plugin resolves these automatically.
- Passives (R, C, D) use pad numbers `"1"` and `"2"`.
- The USB-C pins use EasyEDA's pad-name conventions (`A1`, `A4`, `B9`, etc.).

## Step 7 — `pcb_generate` (~30-60 s, first run for unique ICs)

Claude calls `pcb_generate` with the spec. On first run:

- EasyEDA pin-map fetch, ~12 s each, for the three parts whose nets use pin
  names: `U1` (ESP32-C3), `U2` (the LDO) and `J1` (USB-C)
- The other ten parts — resistors, caps, LED, JST-PH headers — are wired by
  pad number, so no lookup happens at all
- pcbnew footprint load + placement + net creation: ~5 s

That is 3 fetches for a 13-part board rather than 13. Cached after the first
run, so a second `pcb_generate` on the same parts is immediate.

Output:

```json
{
  "pcb_path": "/home/alex/pcbs/soilnode/soilnode.kicad_pcb",
  "footprints_placed": 13,
  "nets_created": 10,
  "errors": [],
  "warnings": [],
  "net_stats": {
    "VBUS": 4, "3V3": 6, "GND": 13, "EN": 2,
    "STATUS_LED": 2, "LED_A": 2, "PROBE1": 2, "PROBE2": 2,
    "VBAT": 1
  }
}
```

Note that the `VBAT` net has only 1 pad — nothing is drawing battery power in this design. You can either leave it (many projects use VBAT as a spare pin) or add a connection before routing.

Session is now at `stage: pcb_generated` with the spec persisted for resume.

## Step 8 — EasyEDA handoff

Claude calls `easyeda_handoff`:

```
Your board file is at /home/alex/pcbs/soilnode/soilnode.kicad_pcb (48 KB).
Size: 13 components, 10 nets, 80x60 mm, 2-layer.

Next steps:
  1. Open https://easyeda.com/editor in your browser.
  2. File → Import → EasyEDA Source / Specctra / KiCad / Altium.
  3. Pick 'KiCad' and select /home/alex/pcbs/soilnode/soilnode.kicad_pcb.
  4. Drag components around if you want a nicer layout.
  5. Route → Auto Route → Start.
  6. Fabrication → PCB Order via JLCPCB. One click.
```

Session is now at `stage: handoff_rendered` — the full workflow is complete.

## What's next

You open EasyEDA, auto-route the board (takes 10-30 s on a board this size), review it, and click "PCB Order via JLCPCB". You'll land on JLCPCB's quote page with the right Gerbers, drill, CPL, and BOM pre-loaded. Typical cost for 5 boards with basic assembly: ~$15-20 USD shipped.

## Total wall-clock time, first run

- Cache populate: 60 s
- Pin-map fetches (2 unique ICs): 24 s
- Everything else: <10 s
- **Total: ~90 s to a fabricable board file**

## On subsequent runs

Cache and pin maps are both warm: `/pcb-new` on a similar design takes ~5 s total (the longest step is assembling the BOM checkpoint for the LLM to review with you).
