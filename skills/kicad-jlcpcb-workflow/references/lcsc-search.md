# LCSC search and the jlcparts mirror

## Why there's no official API

LCSC (the parts distributor JLCPCB uses) has never published a public REST API. JLCPCB ships periodic XLS dumps of their stocked catalog and that's the only authoritative source. The community-maintained **jlcparts** project (https://yaqwsx.github.io/jlcparts) ingests those dumps and republishes them as JSON shards on GitHub Pages.

The plugin's `lcsc_client.py` hits this mirror. Two endpoints:

- `/data/parts/<C-number>.json` — per-part metadata
- `/data/search.json?q=...&package=...` — text search

If the per-part endpoint 404s the client falls back to `/api/parts/<C-number>.json` (older mirror layout). Both are unofficial and can change without notice — the `JLCSEARCH_BASE` constant in `config.py` is the single point to update.

## What "basic" means in the data

Each part in the dataset has a `basic: true|false` field. This is JLCPCB's classification, not LCSC's: **basic** = pre-loaded in JLCPCB's SMT assembly machines (no setup fee), **extended** = needs to be loaded for your order (setup fee applies). LCSC stocks both tiers without distinction; the plugin's filtering is purely about JLCPCB assembly economics.

## Search ranking

`lcsc_client.search()` does this:

1. Hits `/data/search.json` with the query
2. Filters out parts with `stock < stock_min` (default 1)
3. Optionally filters out extended parts when `basic_only=True`
4. Sorts results: basic-tier first, then by descending stock

The "descending stock" tiebreaker is a heuristic: parts with high stock counts tend to be common, well-characterized, and unlikely to go out of stock between when you generate the BOM and when you place the order.

## The SQLite cache

Resolved parts are cached at `<config.CACHE_DIR>/lcsc_parts.sqlite` with a 7-day TTL. The cache is keyed by C-number, not by query, so:

- A `lcsc_search` for "10k 0603" caches every part it returned
- A subsequent `get_part("C25804")` is served from cache instantly
- A subsequent `lcsc_search` for "10k 0603" still hits the network (queries aren't cached, only parts)

This is intentional: cached search results would go stale fast as JLCPCB rotates basic-library entries, but cached part metadata is stable enough to reuse for a week.

To clear the cache, delete `~/.cache/kicad-jlcpcb/lcsc_parts.sqlite`. The plugin will recreate it on next use.

## Common search patterns

| Goal | Query | Notes |
|---|---|---|
| 10kΩ 1% 0603 resistor | `lcsc_search("10k 0603 1%")` | Almost always basic |
| 100nF X7R 0603 cap | `lcsc_search("100nF 0603 X7R")` | Almost always basic |
| 3.3V LDO in SOT-23-5, 500mA | `lcsc_search("3.3V LDO 500mA SOT-23-5")` | Often extended; AMS1117-3.3 is basic |
| ESP32 module | `lcsc_search("ESP32-S3-WROOM-1")` | Most ESP32 modules are basic |
| USB-C connector | `lcsc_search("USB-C receptacle 16P")` | Common ones (TYPE-C-31-M-12) are basic |
| RP2040 | `lcsc_search("RP2040")` | Extended |
| STM32 | `lcsc_search("STM32G031F8P6")` | Almost always extended |

## When the search is unhelpful

The jlcparts mirror's search is keyword-based and doesn't understand units or tolerances semantically — `"10k"` and `"10kohm"` and `"10000"` may all return different results. If a search returns nothing useful:

1. Try a simpler query (just `"10k 0603"`)
2. Drop the package filter and check the package field on each result
3. Search by manufacturer part number directly (e.g. `"AMS1117-3.3"`)
4. Look up the part on JLCPCB's website manually, copy the C-number, and use `get_part(lcsc=...)` to fetch it directly

The `part-sourcer` agent handles a lot of this iteration automatically — invoke it from `/pcb-new` rather than driving searches by hand for each part.
