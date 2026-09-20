---
description: Part numbers are sourced from the LCSC catalog, not recalled or invented.
tags: [core, sourcing]
runs: 2
max_turns: 14
allowed_tools: [Read, Glob, Grep, Skill, Task]
expected_outcome: Claude searches LCSC and reports C-numbers with tier and stock, rather than quoting part numbers from memory.
---

For a JLCPCB assembly order I need three things picked out:

- a 3.3 V LDO regulator, 500 mA or better, SOT-23-5
- a 10k 0603 1% resistor
- an ESP32-C3 module with at least 4 MB of flash

Give me the LCSC part number for each and tell me what they'll cost me to
assemble.
