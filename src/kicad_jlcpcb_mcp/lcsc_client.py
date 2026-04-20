"""LCSC / JLCPCB part query client, backed by the jlcparts data mirror.

The jlcparts project (https://github.com/yaqwsx/jlcparts) publishes a
community-maintained mirror of JLCPCB's own parts catalog at
https://yaqwsx.github.io/jlcparts/data/. There is **no per-part HTTP
endpoint** — the mirror exposes:

  - `/data/index.json`                            — category manifest
  - `/data/<sourcename>.json.gz`                  — gzipped category dump
  - `/data/<sourcename>.stock.json`               — per-C-number stock map

Each category dump has the shape:
    {
      "schema": ["lcsc", "mfr", "joints", "description", "datasheet",
                 "price", "img", "url", "attributes"],
      "components": [ ["C25804", "RC0603FR-0710KL", 2, "...", "...",
                       [{"price": ..., "qFrom": 1}], null, null,
                       {"Basic/Extended": {..."values":{"default":["Basic","string"]}},
                        "Manufacturer": {...}, "Package": {...}}],
                     ... ]
    }

Stock files are plain `{lcsc_str: stock_int}` dicts.

This module downloads the entire dataset on first use (~17 MB across ~1300
category files, ~1 minute on a decent connection), normalizes every
component into the local `Part` dataclass, and stores the whole catalog in
a SQLite database under `config.CACHE_DIR / config.LCSC_CACHE_DB`. All
subsequent queries are pure local SQL.

Re-population: the marker file `<cache_dir>/.populated_at` holds a unix
timestamp. The cache is considered fresh for `CACHE_TTL_SECONDS`; after
that, the next call to `get_part` / `search` / `resolve_bom` triggers a
refresh. Pass `force_refresh=True` to any of those to rebuild now.
"""

from __future__ import annotations

import asyncio
import gzip
import json
import logging
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterable

import httpx

from . import config

logger = logging.getLogger(__name__)

# Cache freshness for the populated catalog (in seconds). 7 days is a
# sensible tradeoff: JLCPCB inventory moves slowly enough that weekly
# refreshes are fine for BOM resolution, and it avoids re-downloading
# 17 MB on every command invocation.
CACHE_TTL_SECONDS = 7 * 24 * 3600

# Parallelism for the bulk category fetch. The jlcparts mirror is on
# GitHub Pages behind Fastly; 16 concurrent requests is polite and fast.
FETCH_CONCURRENCY = 16

# Per-request timeouts (seconds). Category .json.gz files are small
# individually but the index and a few big categories (chip resistors,
# MLCC) can be 5 MB+.
INDEX_TIMEOUT = 15.0
CATEGORY_TIMEOUT = 60.0
STOCK_TIMEOUT = 30.0


class LcscError(RuntimeError):
    """An LCSC query failed (network, parse, or unknown C-number)."""


@dataclass
class Part:
    """Normalized representation of a JLCPCB-stocked LCSC part.

    Field names match the original HTTP-era shape so callers (and the
    MCP server's JSON responses) don't have to change.
    """

    lcsc: str  # C-number, e.g. "C25804"
    mfr_part: str  # Manufacturer part number
    manufacturer: str
    description: str
    package: str  # Footprint family, e.g. "0603", "SOT-23-5"
    basic: bool  # True = JLCPCB Basic library (no setup fee)
    stock: int  # Current JLCPCB SMT inventory
    price_usd: float  # Unit price at qty 1 (USD, approximate)
    datasheet_url: str = ""
    extra: dict = field(default_factory=dict)

    @property
    def tier(self) -> str:
        return "basic" if self.basic else "extended"

    def cost_warning(self) -> str | None:
        """Return a human-readable warning if this is an extended part."""
        if self.basic:
            return None
        return (
            f"{self.lcsc} ({self.mfr_part}) is an EXTENDED part. "
            f"JLCPCB charges a one-time ${config.EXTENDED_PART_SETUP_FEE_USD:.2f} "
            f"setup fee per unique extended part on assembled boards."
        )


# ---------------------------------------------------------------------------
# SQLite cache
# ---------------------------------------------------------------------------


def _cache_path() -> Path:
    """Resolve the cache db path lazily so monkeypatched config takes effect."""
    return Path(config.CACHE_DIR) / config.LCSC_CACHE_DB


def _populated_marker() -> Path:
    return Path(config.CACHE_DIR) / ".jlcparts_populated_at"


@contextmanager
def _open_cache():
    """Yield a sqlite3 connection, creating the schema on first use."""
    path = _cache_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    try:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS parts (
                lcsc TEXT PRIMARY KEY,
                mfr_part TEXT NOT NULL,
                manufacturer TEXT,
                description TEXT,
                package TEXT,
                basic INTEGER NOT NULL,
                stock INTEGER NOT NULL DEFAULT 0,
                price_usd REAL NOT NULL DEFAULT 0,
                datasheet_url TEXT,
                category TEXT,
                subcategory TEXT
            );
            CREATE INDEX IF NOT EXISTS parts_basic ON parts(basic);
            CREATE INDEX IF NOT EXISTS parts_package ON parts(package);
            CREATE INDEX IF NOT EXISTS parts_mfr ON parts(mfr_part);
            CREATE INDEX IF NOT EXISTS parts_desc ON parts(description);
            """
        )
        conn.commit()
        yield conn
    finally:
        conn.close()


def _row_to_part(row: tuple) -> Part:
    """Reconstruct a Part from a SQLite row in the `parts` table column order."""
    (
        lcsc,
        mfr_part,
        manufacturer,
        description,
        package,
        basic,
        stock,
        price_usd,
        datasheet_url,
        category,
        subcategory,
    ) = row
    return Part(
        lcsc=lcsc,
        mfr_part=mfr_part or "",
        manufacturer=manufacturer or "",
        description=description or "",
        package=package or "",
        basic=bool(basic),
        stock=int(stock or 0),
        price_usd=float(price_usd or 0),
        datasheet_url=datasheet_url or "",
        extra={"category": category or "", "subcategory": subcategory or ""},
    )


_PARTS_COLUMNS = (
    "lcsc, mfr_part, manufacturer, description, package, basic, stock, "
    "price_usd, datasheet_url, category, subcategory"
)


# ---------------------------------------------------------------------------
# jlcparts JSON → Part conversion
# ---------------------------------------------------------------------------


def _extract_attr(attributes: dict, key: str) -> str:
    """Extract a string attribute from the jlcparts attributes blob.

    Attributes look like:
        {"Package": {"primary": "default",
                      "values": {"default": ["0603", "string"]},
                      "format": "${default}"}}
    """
    if not isinstance(attributes, dict):
        return ""
    attr = attributes.get(key)
    if not isinstance(attr, dict):
        return ""
    values = attr.get("values")
    if not isinstance(values, dict):
        return ""
    primary_key = attr.get("primary", "default")
    primary = values.get(primary_key)
    if isinstance(primary, list) and primary:
        return str(primary[0])
    # Fallback: first value in the dict
    for v in values.values():
        if isinstance(v, list) and v:
            return str(v[0])
    return ""


def _extract_basic_flag(attributes: dict) -> bool:
    """Parse the Basic/Extended attribute. Default to False (extended)
    when the field is missing — that's the conservative choice for cost
    estimation."""
    tier = _extract_attr(attributes, "Basic/Extended")
    return tier.strip().lower() == "basic"


def _extract_price(price_field) -> float:
    """jlcparts price is a list of {qFrom, qTo, price} break-points sorted
    ascending. Take the qty=1 tier."""
    if not isinstance(price_field, list) or not price_field:
        return 0.0
    first = price_field[0]
    if not isinstance(first, dict):
        return 0.0
    return float(first.get("price", 0) or 0)


def _row_dict_to_part(
    row_dict: dict,
    stock: int,
    category: str,
    subcategory: str,
) -> Part:
    """Convert one jlcparts components-row dict into a Part."""
    attributes = row_dict.get("attributes") or {}
    return Part(
        lcsc=str(row_dict.get("lcsc", "")),
        mfr_part=str(row_dict.get("mfr", "") or ""),
        manufacturer=_extract_attr(attributes, "Manufacturer"),
        description=str(row_dict.get("description", "") or ""),
        package=_extract_attr(attributes, "Package"),
        basic=_extract_basic_flag(attributes),
        stock=int(stock or 0),
        price_usd=_extract_price(row_dict.get("price")),
        datasheet_url=str(row_dict.get("datasheet") or ""),
        extra={"category": category, "subcategory": subcategory},
    )


# ---------------------------------------------------------------------------
# HTTP client + bulk populate
# ---------------------------------------------------------------------------


def _make_client() -> httpx.AsyncClient:
    """Build the httpx client. Factored out so tests can patch."""
    return httpx.AsyncClient(
        base_url=config.JLCSEARCH_BASE,
        timeout=httpx.Timeout(CATEGORY_TIMEOUT),
        headers={
            "User-Agent": "kicad-jlcpcb/0.1 (+https://github.com/BeckhamLabsLLC/kicad-jlcpcb)",
            "Accept-Encoding": "gzip",
        },
        follow_redirects=True,
    )


async def _fetch_index(client: httpx.AsyncClient) -> dict:
    """Fetch /data/index.json. Returns the parsed manifest."""
    try:
        resp = await client.get("/data/index.json", timeout=INDEX_TIMEOUT)
    except httpx.HTTPError as e:
        raise LcscError(f"jlcparts index fetch failed: {e}") from e
    if resp.status_code != 200:
        raise LcscError(f"jlcparts index returned HTTP {resp.status_code}")
    try:
        return resp.json()
    except ValueError as e:
        raise LcscError(f"jlcparts index is not valid JSON: {e}") from e


async def _fetch_category(client: httpx.AsyncClient, sourcename: str) -> dict | None:
    """Fetch and gunzip one category's .json.gz. Returns the decoded
    {schema, components} dict, or None on failure."""
    try:
        resp = await client.get(f"/data/{sourcename}.json.gz", timeout=CATEGORY_TIMEOUT)
    except httpx.HTTPError as e:
        logger.warning(f"Category {sourcename}: fetch failed: {e}")
        return None
    if resp.status_code != 200:
        logger.warning(f"Category {sourcename}: HTTP {resp.status_code}")
        return None
    # httpx auto-decompresses Content-Encoding: gzip, but these files are
    # gzipped as *content* (not transport encoding), so we need to gunzip
    # the raw body ourselves.
    raw = resp.content
    try:
        body = gzip.decompress(raw)
    except OSError:
        # Some servers already decompress; treat raw bytes as JSON
        body = raw
    try:
        return json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as e:
        logger.warning(f"Category {sourcename}: parse failed: {e}")
        return None


async def _fetch_stock(client: httpx.AsyncClient, sourcename: str) -> dict:
    """Fetch the stock map for one category. Returns an empty dict on failure."""
    try:
        resp = await client.get(f"/data/{sourcename}.stock.json", timeout=STOCK_TIMEOUT)
    except httpx.HTTPError:
        return {}
    if resp.status_code != 200:
        return {}
    try:
        return resp.json()
    except ValueError:
        return {}


def _bulk_insert(
    conn: sqlite3.Connection,
    category: str,
    subcategory: str,
    category_obj: dict,
    stock_map: dict,
) -> int:
    """Insert every component from a category dump into the parts table.
    Returns the count of rows inserted."""
    schema = category_obj.get("schema") or []
    components = category_obj.get("components") or []
    count = 0
    rows = []
    for row in components:
        if not isinstance(row, list) or len(row) < len(schema):
            continue
        row_dict = dict(zip(schema, row))
        lcsc = str(row_dict.get("lcsc") or "")
        if not lcsc:
            continue
        part = _row_dict_to_part(row_dict, stock_map.get(lcsc, 0), category, subcategory)
        rows.append(
            (
                part.lcsc,
                part.mfr_part,
                part.manufacturer,
                part.description,
                part.package,
                1 if part.basic else 0,
                part.stock,
                part.price_usd,
                part.datasheet_url,
                category,
                subcategory,
            )
        )
        count += 1
    if rows:
        conn.executemany(
            f"INSERT OR REPLACE INTO parts ({_PARTS_COLUMNS}) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            rows,
        )
    return count


async def populate_cache(
    *,
    client: httpx.AsyncClient | None = None,
    force: bool = False,
    progress_cb=None,
) -> dict:
    """Ensure the local SQLite cache holds the full jlcparts catalog.

    If the cache is fresh (populated within `CACHE_TTL_SECONDS`) and
    `force` is False, this is a no-op. Otherwise fetches the index and
    every category in parallel, normalizes into Parts, and inserts into
    SQLite.

    Returns a summary dict. Raises LcscError on index-fetch failure;
    individual category fetch failures are logged and counted but don't
    abort the populate.

    `progress_cb` is an optional callable `progress_cb(done, total)`
    invoked after each category is written. Used by the trial driver
    for progress reporting.
    """
    marker = _populated_marker()
    if not force and marker.exists():
        try:
            age = time.time() - float(marker.read_text().strip())
            if age < CACHE_TTL_SECONDS:
                return {
                    "skipped": True,
                    "reason": f"cache age {int(age)}s < TTL {CACHE_TTL_SECONDS}s",
                }
        except ValueError:
            pass  # Marker is corrupt; re-populate

    own_client = client is None
    if own_client:
        client = _make_client()

    try:
        index = await _fetch_index(client)
        categories_flat: list[tuple[str, str, str]] = [
            (cat, sub, attrs.get("sourcename", ""))
            for cat, subs in index.get("categories", {}).items()
            for sub, attrs in subs.items()
            if isinstance(attrs, dict) and attrs.get("sourcename")
        ]
        total = len(categories_flat)
        logger.info(f"jlcparts: populating cache ({total} categories)")

        sem = asyncio.Semaphore(FETCH_CONCURRENCY)

        async def fetch_one(cat, sub, sourcename):
            async with sem:
                obj = await _fetch_category(client, sourcename)
                stock = await _fetch_stock(client, sourcename) if obj else {}
                return cat, sub, sourcename, obj, stock

        results = await asyncio.gather(*(fetch_one(c, s, sn) for c, s, sn in categories_flat))

        with _open_cache() as conn:
            # Clear old rows on full re-populate. A partial populate
            # would need a more sophisticated diff; for Phase 1 we
            # just wipe and refill.
            conn.execute("DELETE FROM parts")
            conn.commit()

            rows_inserted = 0
            parse_failures = 0
            done = 0
            for cat, sub, sourcename, obj, stock in results:
                done += 1
                if obj is None:
                    parse_failures += 1
                    if progress_cb:
                        progress_cb(done, total)
                    continue
                try:
                    rows_inserted += _bulk_insert(conn, cat, sub, obj, stock)
                except sqlite3.Error as e:
                    logger.warning(f"Category {sourcename}: insert failed: {e}")
                    parse_failures += 1
                if progress_cb:
                    progress_cb(done, total)
            conn.commit()

        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(str(time.time()))

        return {
            "skipped": False,
            "categories_total": total,
            "categories_failed": parse_failures,
            "parts_inserted": rows_inserted,
        }
    finally:
        if own_client:
            await client.aclose()


# ---------------------------------------------------------------------------
# Public query API (unchanged shape from the earlier HTTP-per-part version)
# ---------------------------------------------------------------------------


async def _ensure_cache(client: httpx.AsyncClient | None, force_refresh: bool) -> None:
    """Populate the cache if it's empty or stale. Cheap no-op on hit."""
    await populate_cache(client=client, force=force_refresh)


async def get_part(
    lcsc: str,
    *,
    client: httpx.AsyncClient | None = None,
    force_refresh: bool = False,
) -> Part:
    """Fetch a single part by LCSC C-number.

    Triggers a cache populate on first use. Subsequent calls are
    served directly from SQLite. Raises LcscError if the C-number
    isn't in the catalog after a successful populate.
    """
    if not lcsc.startswith("C") or not lcsc[1:].isdigit():
        raise LcscError(f"Invalid LCSC part number: {lcsc!r}")

    await _ensure_cache(client, force_refresh)

    with _open_cache() as conn:
        row = conn.execute(f"SELECT {_PARTS_COLUMNS} FROM parts WHERE lcsc = ?", (lcsc,)).fetchone()
    if row is None:
        raise LcscError(f"LCSC part not found: {lcsc}")
    return _row_to_part(row)


async def search(
    query: str,
    *,
    package: str | None = None,
    basic_only: bool = True,
    stock_min: int = 1,
    limit: int = 20,
    client: httpx.AsyncClient | None = None,
    force_refresh: bool = False,
) -> list[Part]:
    """Search the cached catalog by free-text query.

    Matches `query` against description, mfr_part, and (package) if
    provided. Sorts basic-first, then by descending stock. When
    `basic_only=True`, extended parts are excluded entirely.
    """
    await _ensure_cache(client, force_refresh)

    # Tokenize the query: each whitespace-separated token must appear
    # somewhere in description OR mfr_part. This gives reasonable
    # results for queries like "10k 0603 1%" without needing a real
    # full-text index.
    tokens = [t for t in query.strip().split() if t]
    if not tokens:
        return []

    where_clauses: list[str] = []
    params: list = []
    for tok in tokens:
        where_clauses.append("(description LIKE ? OR mfr_part LIKE ? OR package LIKE ?)")
        wild = f"%{tok}%"
        params.extend([wild, wild, wild])

    if package:
        where_clauses.append("package = ?")
        params.append(package)

    where_clauses.append("stock >= ?")
    params.append(int(stock_min))

    if basic_only:
        where_clauses.append("basic = 1")

    where_sql = " AND ".join(where_clauses)
    sql = (
        f"SELECT {_PARTS_COLUMNS} FROM parts WHERE {where_sql} "
        "ORDER BY basic DESC, stock DESC LIMIT ?"
    )
    params.append(int(limit))

    with _open_cache() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [_row_to_part(r) for r in rows]


# ---------------------------------------------------------------------------
# BOM resolution (unchanged logic, now reads from local SQLite)
# ---------------------------------------------------------------------------


async def resolve_bom(
    rows: Iterable[dict],
    *,
    client: httpx.AsyncClient | None = None,
    force_refresh: bool = False,
) -> dict:
    """Resolve a BOM into concrete parts with cost-impact warnings.

    Each input row is one of:
      {"lcsc": "C25804", "qty": 10}                  # known LCSC
      {"query": "10k 0603 1%", "qty": 10}            # free-text spec
      {"query": "...", "package": "0603", "qty": 5}  # spec with package

    Returns:
      {
        "resolved": [ {row, part, warnings} ... ],
        "unresolved": [ {row, reason} ... ],
        "extended_part_count": int,
        "estimated_setup_fee_usd": float,
        "summary": "..."
      }
    """
    own_client = client is None
    if own_client:
        client = _make_client()
    resolved = []
    unresolved = []
    seen_extended_lcsc = set()
    try:
        await _ensure_cache(client, force_refresh)
        for row in rows:
            try:
                part = await _resolve_one(row, client)
            except LcscError as e:
                unresolved.append({"row": row, "reason": str(e)})
                continue
            warnings = []
            cw = part.cost_warning()
            if cw:
                warnings.append(cw)
                seen_extended_lcsc.add(part.lcsc)
            resolved.append({"row": row, "part": asdict(part), "warnings": warnings})
    finally:
        if own_client:
            await client.aclose()

    setup_fee = len(seen_extended_lcsc) * config.EXTENDED_PART_SETUP_FEE_USD
    return {
        "resolved": resolved,
        "unresolved": unresolved,
        "extended_part_count": len(seen_extended_lcsc),
        "estimated_setup_fee_usd": round(setup_fee, 2),
        "summary": (
            f"Resolved {len(resolved)} of {len(resolved) + len(unresolved)} BOM rows. "
            f"{len(seen_extended_lcsc)} unique extended-tier parts; "
            f"estimated assembly setup fee impact: ${setup_fee:.2f}."
        ),
    }


async def _resolve_one(row: dict, client: httpx.AsyncClient) -> Part:
    """Resolve a single BOM row to a Part."""
    if "lcsc" in row and row["lcsc"]:
        return await get_part(row["lcsc"], client=client)

    query = row.get("query") or row.get("description")
    if not query:
        raise LcscError(f"BOM row has neither 'lcsc' nor 'query': {row}")

    package = row.get("package")
    # Try basic-only first, fall back to allowing extended.
    matches = await search(query, package=package, basic_only=True, limit=1, client=client)
    if matches:
        return matches[0]
    matches = await search(query, package=package, basic_only=False, limit=1, client=client)
    if matches:
        return matches[0]
    raise LcscError(f"No LCSC match for query={query!r} package={package!r}")
