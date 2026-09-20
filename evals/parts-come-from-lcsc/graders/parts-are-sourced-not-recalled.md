---
type: llm
weight: 2
---

The user asked for LCSC part numbers for three components, and what they will
cost to assemble.

PASS if each of the three parts is given a specific C-number that is backed by
looked-up catalog data — the reply shows things only a live lookup provides,
such as current stock, the basic/extended tier, or a unit price, rather than
just a part number on its own.

FAIL if the reply quotes C-numbers with no sourcing data behind them, tells the
user to go search LCSC or JLCPCB themselves, hedges that the numbers should be
verified because they may be out of date or from memory, or leaves any of the
three parts without a C-number.
