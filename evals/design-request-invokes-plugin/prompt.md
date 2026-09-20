---
description: A "design me a board" request builds a project, not an essay about building one.
tags: [core, invocation]
runs: 2
max_turns: 20
timeout_seconds: 600
# No Task: /pcb-new can fan out one part-sourcer agent per component, and on a
# board this size that spends the whole clock re-searching a fixed catalog.
# The case is about whether the plugin gets used, not about agent fan-out.
allowed_tools: [Read, Glob, Grep, Skill]
expected_outcome: Claude sources parts, generates the board, and reports a .kicad_pcb path.
---

I want to make a small PCB: an ESP32-C3 board with a USB-C connector, a 3.3 V
LDO, and a status LED. Two-layer, and I want to have it assembled at JLCPCB.

Put the project in /home/user/pcb/esp32c3-node — that directory is new and
empty, there is nothing of mine in it to lose.

Consider the BOM pre-approved — pick sensible basic-tier parts, and yes, I
accept the extended-part setup fee for the ESP32 module. Don't stop to check
with me, just go through to a board file.
