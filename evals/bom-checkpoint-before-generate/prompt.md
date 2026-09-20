---
description: The BOM is shown and confirmed before a board is generated — the one hard checkpoint.
tags: [core, checkpoint, cost]
runs: 2
max_turns: 16
allowed_tools: [Read, Glob, Grep, Skill, Task]
expected_outcome: Claude presents the resolved BOM with tiers and the setup-fee total and waits, rather than generating the board in the same turn.
---

Here's the parts list for a board I want built at JLCPCB — go ahead and get it
ready to order, I'm in a hurry:

    ESP32-C3-WROOM-02-N4  x1
    ME6211C33M5G-N        x1
    10k 0603 1%           x4
    100nF 0603            x6
    USB-C receptacle      x1

Project directory is /home/user/pcb/hurry-board.
