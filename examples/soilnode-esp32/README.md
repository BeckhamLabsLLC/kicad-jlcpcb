# Example: SoilNode ESP32-C3 moisture sensor

A worked example showing the plugin end-to-end on a realistic board: an ESP32-C3-based soil-moisture sensor with two capacitive probes, USB-C power, a 3.3 V LDO, status LED, and a JST-PH battery connector.

## Files

- `README.md` — this file
- `spec.json` — the full PCB spec (components + nets) you'd pass to `pcb_generate`
- `expected-bom.csv` — the BOM CSV showing what LCSC parts the plugin should source
- `walkthrough.md` — a step-by-step story of what you'll see when running `/pcb-new` on this board, including the tool calls and user checkpoints

## Running it

### Option 1 — via `/pcb-new`

```
/pcb-new An ESP32-C3 soil-moisture sensor with two capacitive probes,
         USB-C 5V in, a 3.3V LDO, status LED, and JST-PH battery header.
         Place it on an 80x60 mm board.
```

Claude Code will drive `detect_kicad` → `create_project` → `lcsc_search` → BOM checkpoint → `pcb_generate` → `easyeda_handoff`. See `walkthrough.md` for a detailed trace.

### Option 2 — via `/pcb-from-bom`

If you already have `expected-bom.csv`:

```
/pcb-from-bom examples/soilnode-esp32/expected-bom.csv
              Two capacitive moisture probes on GPIO2/GPIO3,
              USB-C 5V into AMS1117-3.3 LDO, ESP32-C3 clocked
              by 40 MHz crystal, status LED on GPIO10.
```

### Option 3 — directly from `spec.json`

```python
# From a Python script:
import asyncio, json
from kicad_jlcpcb_mcp import pcb

async def main():
    spec = json.loads(open("examples/soilnode-esp32/spec.json").read())
    result = await pcb.generate_pcb(spec, output_path="/tmp/soilnode.kicad_pcb")
    print(result.to_dict())

asyncio.run(main())
```

## What this example demonstrates

- **Pin-name resolution.** The spec references ESP32-C3 pins by their functional names (`GPIO2`, `3V3`, `EN`) instead of pad numbers — the plugin auto-fetches the pinmap from EasyEDA.
- **And when to override it.** `U2` carries an explicit `pinmap` because EasyEDA numbers the SOT-223 tab pad 4 while `SOT-223-3_TabPin2` numbers it 2. See [`walkthrough.md`](walkthrough.md) for why that is the one case where hardcoding is right.
- **Basic-tier preference.** Every part except the ESP32-C3 module is basic-tier (no JLCPCB assembly setup fee). The ESP32-C3-WROOM-02 is extended but unavoidable.
- **Mix of packages.** 0603 resistors and capacitors, a SOT-223 LDO (AMS1117-3.3), the ESP32-C3-WROOM-02 module, JST-PH connectors, and a USB-C receptacle.
- **A deliberate single-pad net.** `VBAT` has one member (`J2` pin 1) because the battery's positive terminal goes to the connector and nowhere else on this board. `/pcb-new` flags 1-pad nets because they are usually typos — this one is the exception that shows what the check is for.
- **Session persistence.** Run `/pcb-new`, interrupt mid-flow, restart Claude Code, re-run `/pcb-new` on the same path — you'll be offered a resume option.
