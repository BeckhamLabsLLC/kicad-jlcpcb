# LCSC search: how part sourcing actually works

## Where the data comes from

LCSC has never published a public REST API, and JLCPCB's own catalog endpoints
are not documented for third-party use. The plugin therefore reads from two
unofficial services, each doing the one thing it is good at:

| Source | Answers | Route |
|---|---|---|
| jlcsearch (`jlcsearch.tscircuit.com`) | free-text search, JLCPCB SMT stock, basic/extended tier, price breaks | `/components/list`, `/resistors/list`, `/capacitors/list` |
| EasyEDA (`easyeda.com`) | exact C-number lookup, symbols, pin maps | `/api/products/<C-number>/components` |

Both base URLs are overridable (`KJLC_JLCSEARCH_BASE`, `KJLC_EASYEDA_BASE`).

Resolved parts are cached in `~/.cache/kicad-jlcpcb/lcsc_parts.sqlite` for 24
hours. **There is no bulk catalog download** — a cold query is a single HTTP
round trip, and a warm one is local SQL.

## What the tools do

`lcsc_search(query, package?, basic_only=true, stock_min=1, limit=20)` is the
only search tool exposed over MCP. There is no `get_part` tool; exact
C-numbers are resolved through `lcsc_resolve_bom` with a `lcsc` field.

Internally `lcsc_search`:

1. **Routes chip passives to a structured lookup.** If the query contains a
   resistance (`10k`, `4k7`, `100R`) or a capacitance (`0.1uF`, `100nF`) it
   queries by parsed value rather than by text. This matters: many basic
   passives have an *empty* description upstream and cannot be found by text
   search at all — a text query for "10k 0603" returns a 510kΩ.
2. **Otherwise searches free text, narrowing as needed.** Upstream AND-matches
   every word, so a five-word spec usually returns nothing. The client drops
   the least-matchable words and retries, stopping at half the original word
   count — one leftover word matches half the catalog.
3. **Re-ranks locally** against the full query, then basic-first, then stock.
4. **Filters `basic_only` and `stock_min` client-side.** Upstream's basic
   filter is applied before its row limit and guts the result set, so the
   plugin never sends it on the free-text route.

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
millions in JLCPCB inventory. The plugin flags these rather than printing a
misleading number, and tries to backfill real stock from jlcsearch. When it
can't, say so in the BOM checkpoint rather than presenting 0 as fact.

## When a search comes back empty

That is a real answer, not a bug — the narrowing logic has already retried
with fewer words. Try, in order:

1. `basic_only=false` — the part may only exist as extended.
2. A different package, or drop the package entirely.
3. A functional description instead of a part number (`3.3V LDO 500mA`
   rather than an exact MPN; upstream's index does not reliably cover MPNs).

If *every* query returns empty, upstream is likely down or has changed shape.
`KJLC_NETWORK_TESTS=1 pytest tests/test_network_contract.py` will say which.

## Sourcing a whole BOM

Use the `part-sourcer` agent for multi-part sourcing rather than driving
`lcsc_search` by hand. It applies the basic-first ladder consistently and
returns structured results ready for the BOM checkpoint.
