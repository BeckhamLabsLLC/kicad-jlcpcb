# LCSC search: how part sourcing actually works

## Where the data comes from

LCSC has never published a public REST API, and JLCPCB's own catalog endpoints
are not documented for third-party use. The plugin therefore reads from three
sources, each doing what it is good at:

| Source | Answers | Route |
|---|---|---|
| jlcpcb.com parts search | part-number and descriptive search, live JLCPCB SMT stock, basic/extended tier. Tried first | the endpoint behind jlcpcb.com/parts |
| jlcsearch (`jlcsearch.tscircuit.com`) | fallback free-text search; structured resistor/capacitor lookup | `/components/list`, `/resistors/list`, `/capacitors/list` |
| EasyEDA (`easyeda.com`) | exact C-number lookup, symbols, pin maps | `/api/products/<C-number>/components` |

All base URLs are overridable (`KJLC_JLCPCB_BASE`, `KJLC_JLCSEARCH_BASE`,
`KJLC_EASYEDA_BASE`). jlcpcb.com protects its endpoint against automated
clients. When it refuses a request the plugin stops asking for five minutes
and uses jlcsearch alone, which since October 2026 has been unreliable for
part numbers (issue #9). If results suddenly get worse mid-session, that is
why.

Resolved parts are cached in `~/.cache/kicad-jlcpcb/lcsc_parts.sqlite` for 24
hours. **There is no bulk catalog download** — a cold query is a single HTTP
round trip, and a warm one is local SQL.

## What the tools do

`lcsc_search(query, package?, basic_only=true, stock_min=1, limit=20)` is the
only search tool exposed over MCP. There is no `get_part` tool; exact
C-numbers are resolved through `lcsc_resolve_bom` with a `lcsc` field.

Internally `lcsc_search`:

1. **Searches a part number on its own.** If the query names one (`SS34`,
   `AMS1117-3.3`, `ESP32-C3`), that token goes to jlcpcb.com alone. Only parts
   carrying that part number are accepted.
2. **Otherwise asks jlcpcb.com with the whole query,** with JLCPCB's own
   basic-only filter when `basic_only=true`, dropping a word at most twice
   if nothing usable comes back.
3. **Falls back to jlcsearch:** first the structured resistor/capacitor
   routes, which filter on the parsed value, then free text. Free text drops
   the least-matchable words and retries, stopping at half the original word
   count, because one leftover word matches half the catalog.
4. **Gates every candidate for relevance.** A value in the query (`10k`)
   must be the part's value, so an 8.2k or a 510k sharing the package is
   rejected. A descriptive query must match roughly half its words. An
   upstream regression that returns popular-but-unrelated parts therefore
   produces an empty result, not a wrong part.
5. **Re-ranks locally** against the full query, then basic-first, then stock,
   and applies `stock_min` client-side.

## Writing queries that work

- **Put the package in the query.** `10k 0603` is far better than `10k`. A
  chip size (`0402`, `0603`, `0805`, `1206`, …) becomes an exact filter.
- **Values are normalised.** `0.1uF` and `100nF` find the same part. So do
  `4k7` and `4.7k`.
- **Fewer, more distinctive words beat more words.** `TVS SMB 30V` beats
  `bidirectional TVS diode rated 30 volts in SMB package`.
- **Don't put tolerance or temperature in the first attempt.** `10k 0603`
  first; add `1%` only if you need to disambiguate.
- **Package names other than chip sizes are not filters.** `SOT-23-5` is
  spelled inconsistently upstream, so it is treated as an ordinary search
  word.

## Reading the results

Each result carries `basic` (no setup fee) or extended, `stock`, `price_usd`
at qty 1, and `package`.

**Prefer basic.** JLCPCB charges a one-time ~$3 setup fee per unique extended
part on assembled boards. `lcsc_resolve_bom` counts unique extended parts and
reports the fee in `estimated_setup_fee_usd`.

**Watch for `stock_unknown`.** A part resolved by exact C-number comes from
EasyEDA, which reports LCSC *retail* stock — it reads 0 for parts with
millions in JLCPCB inventory. The plugin backfills real stock from
jlcpcb.com, then jlcsearch, and flags the part when neither confirms it.
When that happens, say so in the BOM checkpoint rather than presenting 0
as fact. An `OUT OF STOCK` warning is different: jlcpcb.com confirmed zero,
so swap the part.

## When a search comes back empty

That is a real answer, not a bug — the narrowing logic has already retried
with fewer words. Try, in order:

1. `basic_only=false` — the part may only exist as extended.
2. A different package, or drop the package entirely.
3. If you know a candidate part number, search for it directly
   (`AMS1117-3.3`, `SS34`). Part numbers are the most reliable query there
   is. Otherwise try a shorter functional description (`3.3V LDO SOT-23-5`).
4. Never fill the gap with a C-number from memory. If the user has one,
   resolve it with `lcsc_resolve_bom` and `{"lcsc": "C..."}`.

If *every* query returns empty, upstream is likely down or has changed shape.
`KJLC_NETWORK_TESTS=1 pytest tests/test_network_contract.py` will say which.

## Sourcing a whole BOM

Use the `part-sourcer` agent for multi-part sourcing rather than driving
`lcsc_search` by hand. It applies the basic-first ladder consistently and
returns structured results ready for the BOM checkpoint.
