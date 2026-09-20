---
type: llm
weight: 2
---

The user asked for a small ESP32-C3 board to be built for JLCPCB assembly, said
where to put it, and pre-approved the parts so no confirmation was needed.

PASS if the reply is the result of actually doing the work: it names the
specific parts chosen with their LCSC C-numbers and tier, and names the project
or board file it worked on. The user should be able to see that a board was
built for them, or — if something genuinely blocked that — exactly what and
where it stopped.

FAIL if the reply is a design essay the user would have to execute themselves:
a schematic in prose or ASCII, KiCad menu instructions, a shopping list with no
C-numbers, or a plan it never started. Also FAIL if it asks the user to choose
parts or confirm a BOM, which they already pre-approved.
