"""LCSC / JLCPCB part query client.

Part data comes from two live sources, because no single public endpoint
covers both free-text search and per-C-number lookup:

  - **Search** — ``jlcsearch`` (https://jlcsearch.tscircuit.com), a public
    JSON mirror of JLCPCB's assembly catalog. ``/components/list`` answers
    free-text queries with real JLCPCB SMT stock, the basic/extended tier,
    and quantity price breaks. It has no per-C-number lookup: querying an
    exact C-number returns nothing, and the category routes only *sort* by
    ``lcsc``, they don't filter.

  - **Exact C-number** — EasyEDA's component endpoint
    (``https://easyeda.com/api/products/<C-number>/components``). EasyEDA,
    LCSC, and JLCPCB share a parent company, so this is authoritative for
    part identity. Its ``dataStr.head.c_para`` block carries the
    ``JLCPCB Part Class`` field we need for the basic/extended split.
    It does *not* carry JLCPCB SMT stock — ``szlcsc.stock`` is LCSC's
    retail stock, which reads 0 for parts with millions in JLCPCB
    inventory. See ``_enrich_stock_from_jlcsearch``.

Both base URLs are overridable via environment variables (see
``config.JLCSEARCH_BASE`` / ``config.EASYEDA_BASE``) so a fork can point
at a self-hosted mirror. This module previously depended on a single
hardcoded third-party URL that was retired upstream, which broke every
part lookup silently for months; the override exists so that is
recoverable without a code change.

Results are cached row-by-row in SQLite under
``config.CACHE_DIR / config.LCSC_CACHE_DB`` with a short TTL (stock moves
daily). There is no bulk download: the first query costs one HTTP round
trip, and repeats inside the TTL are pure local SQL.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterable

import httpx

from . import config

logger = logging.getLogger(__name__)

# How long a cached row stays fresh. Unlike the old bulk-catalog cache
# (7 days), these rows carry live stock figures, so a day is the most we
# can claim without misleading a BOM decision.
CACHE_TTL_SECONDS = 24 * 3600

# Bumped whenever the `parts` table layout changes. An older cache is
# dropped rather than migrated — it holds rows from a retired data
# source, so there is nothing worth preserving.
_CACHE_SCHEMA_VERSION = 2

# jlcsearch caps `/components/list` at 100 rows regardless of the limit
# we ask for. Clamp locally so the MCP tool's declared limit isn't a lie.
JLCSEARCH_MAX_LIMIT = 100

# jlcsearch is a free community mirror and stalls under load often enough
# that a single read timeout must not fail a BOM.
SEARCH_TIMEOUT = 30.0
SEARCH_RETRIES = 3


class LcscError(RuntimeError):
    """An LCSC query failed (network, parse, or unknown C-number)."""


@dataclass
class Part:
    """Normalized representation of a JLCPCB-stocked LCSC part."""

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

    def stock_warning(self) -> str | None:
        """Return a warning if this part's stock figure is not authoritative.

        Parts resolved by exact C-number come from EasyEDA, which reports
        LCSC retail stock rather than JLCPCB SMT inventory. Rather than
        print a misleading 0, we flag it.
        """
        if not self.extra.get("stock_unknown"):
            return None
        return (
            f"{self.lcsc} ({self.mfr_part}) was resolved via EasyEDA; JLCPCB SMT "
            "stock is not available from that source. Verify availability on "
            "jlcpcb.com before ordering."
        )


# ---------------------------------------------------------------------------
# SQLite row cache
# ---------------------------------------------------------------------------


def _cache_path() -> Path:
    """Resolve the cache db path lazily so monkeypatched config takes effect."""
    return Path(config.CACHE_DIR) / config.LCSC_CACHE_DB


@contextmanager
def _open_cache():
    """Yield a sqlite3 connection, creating (or resetting) the schema."""
    path = _cache_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    try:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version != _CACHE_SCHEMA_VERSION:
            # Stale layout (or a catalog from the retired bulk mirror).
            # Nothing here is worth migrating.
            conn.executescript("DROP TABLE IF EXISTS parts;")
            conn.execute(f"PRAGMA user_version = {_CACHE_SCHEMA_VERSION}")
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
                subcategory TEXT,
                stock_unknown INTEGER NOT NULL DEFAULT 0,
                fetched_at INTEGER NOT NULL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS parts_basic ON parts(basic);
            CREATE INDEX IF NOT EXISTS parts_package ON parts(package);
            CREATE INDEX IF NOT EXISTS parts_mfr ON parts(mfr_part);
            """
        )
        conn.commit()
        yield conn
    finally:
        conn.close()


_PARTS_COLUMNS = (
    "lcsc, mfr_part, manufacturer, description, package, basic, stock, "
    "price_usd, datasheet_url, category, subcategory, stock_unknown"
)


def _row_to_part(row: tuple) -> Part:
    """Reconstruct a Part from a SQLite row in `_PARTS_COLUMNS` order."""
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
        stock_unknown,
    ) = row
    extra: dict = {"category": category or "", "subcategory": subcategory or ""}
    if stock_unknown:
        extra["stock_unknown"] = True
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
        extra=extra,
    )


def _cache_put(parts: Iterable[Part]) -> None:
    """Write parts through to the local cache. Never raises on a cache fault."""
    rows = [
        (
            p.lcsc,
            p.mfr_part,
            p.manufacturer,
            p.description,
            p.package,
            1 if p.basic else 0,
            p.stock,
            p.price_usd,
            p.datasheet_url,
            p.extra.get("category", ""),
            p.extra.get("subcategory", ""),
            1 if p.extra.get("stock_unknown") else 0,
            int(time.time()),
        )
        for p in parts
    ]
    if not rows:
        return
    try:
        with _open_cache() as conn:
            conn.executemany(
                f"INSERT OR REPLACE INTO parts ({_PARTS_COLUMNS}, fetched_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                rows,
            )
            conn.commit()
    except sqlite3.Error as e:
        # A broken cache must never fail a query that already succeeded.
        logger.warning("LCSC cache write failed: %s", e)


def _cache_get(lcsc: str) -> Part | None:
    """Return a cached Part if present and still inside the TTL."""
    cutoff = int(time.time()) - CACHE_TTL_SECONDS
    try:
        with _open_cache() as conn:
            row = conn.execute(
                f"SELECT {_PARTS_COLUMNS} FROM parts WHERE lcsc = ? AND fetched_at >= ?",
                (lcsc, cutoff),
            ).fetchone()
    except sqlite3.Error as e:
        logger.warning("LCSC cache read failed: %s", e)
        return None
    return _row_to_part(row) if row else None


# ---------------------------------------------------------------------------
# Response parsing
# ---------------------------------------------------------------------------


def _parse_price_breaks(price) -> float:
    """Return the qty-1 unit price from jlcsearch's price field.

    jlcsearch reports price two ways depending on the route: a float on
    ``/api/search``, and a comma-separated break-point string on
    ``/components/list``::

        "1-999:0.0027,1000-2999:0.0023,3000-9999:0.0021"

    Returns the price of the lowest-quantity tier, or 0.0 if unparseable.
    """
    if isinstance(price, (int, float)):
        return float(price)
    if not isinstance(price, str) or not price.strip():
        return 0.0
    best_qty: int | None = None
    best_price = 0.0
    for chunk in price.split(","):
        _, _, value = chunk.rpartition(":")
        qty_range = chunk[: len(chunk) - len(value) - 1]
        low, _, _high = qty_range.partition("-")
        try:
            qty = int(low)
            unit = float(value)
        except ValueError:
            continue
        if best_qty is None or qty < best_qty:
            best_qty, best_price = qty, unit
    return best_price


def _normalize_lcsc(value) -> str:
    """jlcsearch returns `lcsc` as a bare integer; restore the C prefix."""
    if value is None:
        return ""
    s = str(value).strip()
    if not s:
        return ""
    return s if s.upper().startswith("C") else f"C{s}"


def _row_to_part_jlcsearch(row: dict) -> Part:
    """Convert one `/components/list` row into a Part."""
    return Part(
        lcsc=_normalize_lcsc(row.get("lcsc")),
        mfr_part=str(row.get("mfr") or ""),
        manufacturer=str(row.get("manufacturer") or ""),
        description=str(row.get("description") or ""),
        package=str(row.get("package") or ""),
        basic=bool(row.get("is_basic")),
        stock=int(row.get("stock") or 0),
        price_usd=_parse_price_breaks(row.get("price")),
        datasheet_url=str(row.get("datasheet") or ""),
        extra={
            "category": str(row.get("category") or ""),
            "subcategory": str(row.get("subcategory") or ""),
            "preferred": bool(row.get("is_preferred")),
        },
    )


def _part_from_easyeda(lcsc: str, result: dict) -> Part:
    """Convert EasyEDA's component payload into a Part.

    The fields we need live in two places: top-level ``title`` /
    ``description`` / ``szlcsc``, and the ``dataStr.head.c_para`` block
    that carries manufacturer and the JLCPCB tier.
    """
    data_str = result.get("dataStr")
    head = data_str.get("head", {}) if isinstance(data_str, dict) else {}
    c_para = head.get("c_para") if isinstance(head, dict) else None
    if not isinstance(c_para, dict):
        c_para = {}
    szlcsc = result.get("szlcsc")
    if not isinstance(szlcsc, dict):
        szlcsc = {}

    tier = str(c_para.get("JLCPCB Part Class") or "")
    mfr_part = str(c_para.get("Manufacturer Part") or result.get("title") or "")

    return Part(
        lcsc=lcsc,
        mfr_part=mfr_part,
        manufacturer=str(c_para.get("Manufacturer") or ""),
        description=str(result.get("description") or ""),
        package=str(c_para.get("package") or ""),
        basic=tier.strip().lower().startswith("basic"),
        # EasyEDA reports LCSC retail stock, not JLCPCB SMT inventory.
        # Leave it at 0 and flag it rather than publish a wrong number.
        stock=0,
        price_usd=float(szlcsc.get("price") or 0),
        datasheet_url="",
        extra={"category": "", "subcategory": "", "stock_unknown": True},
    )


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------


def _make_client() -> httpx.AsyncClient:
    """Build the httpx client. Factored out so tests can patch."""
    return httpx.AsyncClient(
        base_url=config.JLCSEARCH_BASE,
        timeout=httpx.Timeout(SEARCH_TIMEOUT),
        headers={
            "User-Agent": ("kicad-jlcpcb/0.2 (+https://github.com/BeckhamLabsLLC/kicad-jlcpcb)"),
            "Accept": "application/json",
        },
        follow_redirects=True,
    )


async def _jlcsearch_get(path: str, params: dict[str, str], client: httpx.AsyncClient) -> dict:
    """GET a jlcsearch JSON route, retrying on transient network faults."""
    last_exc: Exception | None = None
    for attempt in range(1, SEARCH_RETRIES + 1):
        try:
            resp = await client.get(path, params=params, timeout=SEARCH_TIMEOUT)
        except httpx.HTTPError as e:
            last_exc = e
            if attempt < SEARCH_RETRIES:
                backoff = 2.0 ** (attempt - 1)
                logger.debug(
                    "jlcsearch %s: %r — retry %d/%d in %.0fs",
                    path,
                    e,
                    attempt,
                    SEARCH_RETRIES,
                    backoff,
                )
                await asyncio.sleep(backoff)
                continue
            raise LcscError(
                f"jlcsearch request failed after {SEARCH_RETRIES} attempts: {e!r}. "
                "The service may be down; set KJLC_JLCSEARCH_BASE to point at a "
                "mirror."
            ) from last_exc
        break

    if resp.status_code != 200:
        raise LcscError(
            f"jlcsearch returned HTTP {resp.status_code} for {path}. If this "
            "persists the upstream API may have changed — please open an issue "
            "at https://github.com/BeckhamLabsLLC/kicad-jlcpcb/issues"
        )
    try:
        payload = resp.json()
    except ValueError as e:
        raise LcscError(f"jlcsearch returned non-JSON from {path}: {e}") from e
    if not isinstance(payload, dict):
        raise LcscError(f"jlcsearch returned an unexpected shape from {path}")
    return payload


async def _jlcsearch_components(
    client: httpx.AsyncClient,
    *,
    search: str | None = None,
    package: str | None = None,
    basic_only: bool = False,
    limit: int = JLCSEARCH_MAX_LIMIT,
) -> list[dict]:
    """Query jlcsearch's free-text `/components/list` route. Returns raw rows."""
    params: dict[str, str] = {
        "json": "true",
        "limit": str(min(int(limit), JLCSEARCH_MAX_LIMIT)),
    }
    if search:
        params["search"] = search
    if package:
        params["package"] = package
    if basic_only:
        params["is_basic"] = "true"

    payload = await _jlcsearch_get("/components/list", params, client)
    rows = payload.get("components")
    if not isinstance(rows, list):
        raise LcscError(
            "jlcsearch response has no 'components' list — the upstream API "
            "shape changed. Please open an issue."
        )
    return [r for r in rows if isinstance(r, dict)]


# ---------------------------------------------------------------------------
# Query understanding
# ---------------------------------------------------------------------------

# Two-digit imperial chip sizes. These are the only package strings that
# jlcsearch stores verbatim, so they're the only ones safe to pass to its
# exact-match `package` filter. Anything else (SOT-23-5, DO-214AA) is
# spelled inconsistently upstream and would filter every row away.
_CHIP_PACKAGE_RE = re.compile(
    r"^(0075|0100|0201|0402|0603|0805|1206|1210|1218|1806|1812|2010|2512|2725)$"
)

# Capacitance written one way in a spec and another in the catalog:
# "0.1uF" and "100nF" are the same part, and JLCPCB's catalog uses both.
# Without this, a search for the single most common decoupling capacitor
# in electronics returns a 1uF in the wrong package.
_CAP_UNITS = {"pf": 1e-12, "nf": 1e-9, "uf": 1e-6, "\u00b5f": 1e-6, "mf": 1e-3}
_VALUE_RE = re.compile(r"^(\d+(?:\.\d+)?)(pf|nf|uf|\u00b5f|mf)$")

# Resistance written the way a schematic writes it: 10k, 4.7k, 100R, 1M, 4k7.
_RES_SUFFIX = {"r": 1.0, "\u03a9": 1.0, "ohm": 1.0, "ohms": 1.0, "k": 1e3, "m": 1e6}
_RES_RE = re.compile(r"^(\d+(?:\.\d+)?)(r|\u03a9|ohms?|k|m)$", re.I)
_RES_INFIX_RE = re.compile(r"^(\d+)(r|k|m)(\d+)$", re.I)

# Queries where a bare "10k" does not mean a plain chip resistor.
_NOT_A_CHIP_PASSIVE = {
    "potentiometer",
    "pot",
    "trimmer",
    "array",
    "network",
    "networks",
    "thermistor",
    "varistor",
    "photoresistor",
}


def _format_value(farads: float, unit: str) -> str:
    """Render a capacitance in `unit`, trimming trailing zeros."""
    scaled = farads / _CAP_UNITS[unit]
    text = f"{scaled:.6f}".rstrip("0").rstrip(".")
    return f"{text}{unit}"


def _value_aliases(token: str) -> set[str]:
    """Return equivalent spellings of a capacitance token, including itself."""
    m = _VALUE_RE.match(token)
    if not m:
        return {token}
    try:
        farads = float(m.group(1)) * _CAP_UNITS[m.group(2)]
    except (ValueError, KeyError):
        return {token}
    out = {token}
    for unit in ("pf", "nf", "uf"):
        scaled = farads / _CAP_UNITS[unit]
        # Only offer spellings a human would actually write.
        if 0.1 <= scaled < 10000:
            out.add(_format_value(farads, unit))
    return out


def _extract_package(tokens: list[str]) -> str | None:
    """Pull a chip-size package code out of a free-text query, if present."""
    for t in tokens:
        if _CHIP_PACKAGE_RE.match(t):
            return t
    return None


def _parse_resistance(token: str) -> float | None:
    """Parse a schematic-style resistance into ohms, or None."""
    m = _RES_RE.match(token)
    if m:
        # Uppercase M is mega; lowercase m would be milliohms, which no one
        # writes for a chip resistor, so treat both as mega.
        return float(m.group(1)) * _RES_SUFFIX[m.group(2).lower()]
    m = _RES_INFIX_RE.match(token)
    if m:
        # "4k7" == 4.7k — the suffix letter stands in for the decimal point.
        whole, suffix, frac = m.groups()
        return float(f"{whole}.{frac}") * _RES_SUFFIX[suffix.lower()]
    return None


def _parse_capacitance(token: str) -> float | None:
    """Parse a capacitance token into farads, or None."""
    m = _VALUE_RE.match(token)
    if not m:
        return None
    try:
        return float(m.group(1)) * _CAP_UNITS[m.group(2)]
    except (ValueError, KeyError):
        return None


def _detect_passive(tokens: list[str]) -> tuple[str, float] | None:
    """Classify a query as a chip resistor or capacitor with a value.

    Returns ("resistors", ohms) or ("capacitors", farads), else None.

    This matters more than it looks. Many basic passives carry an *empty*
    description in the upstream catalog, so free-text search cannot find
    them at all — a text query for "10k 0603" returns a 510k resistor. The
    structured routes filter on the parsed value instead, and return the
    part you actually asked for.
    """
    if any(t in _NOT_A_CHIP_PASSIVE for t in tokens):
        return None
    for t in tokens:
        farads = _parse_capacitance(t)
        if farads is not None:
            return ("capacitors", farads)
    for t in tokens:
        # Only trust a resistance reading when the query also looks like a
        # discrete passive, so "1M gate driver" isn't routed to resistors.
        ohms = _parse_resistance(t)
        if ohms is not None and (
            _extract_package(tokens) or any(x.startswith("resistor") for x in tokens)
        ):
            return ("resistors", ohms)
    return None


async def _jlcsearch_passive(
    client: httpx.AsyncClient,
    kind: str,
    value: float,
    *,
    package: str | None,
    basic_only: bool,
) -> list[dict]:
    """Query the structured /resistors/list or /capacitors/list route.

    Unlike the free-text route, `is_basic` is safe to pass here: these
    routes filter on an indexed value first, so the basic filter narrows a
    precise result set rather than gutting a text-relevance ranking.
    """
    params: dict[str, str] = {"json": "true", "limit": str(JLCSEARCH_MAX_LIMIT)}
    if kind == "resistors":
        params["resistance"] = repr(value)
    else:
        params["capacitance"] = repr(value)
    if package:
        params["package"] = package
    if basic_only:
        params["is_basic"] = "true"

    payload = await _jlcsearch_get(f"/{kind}/list", params, client)
    rows = payload.get(kind) if isinstance(payload, dict) else None
    return [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []


def _row_to_part_passive(row: dict, kind: str) -> Part:
    """Convert a structured resistor/capacitor row into a Part.

    These rows carry an empty `description`, so we rebuild one from the
    `attributes` blob. Without it the model gets a BOM line it cannot
    sanity-check, and the part is indistinguishable from any other.
    """
    attrs: dict = {}
    raw = row.get("attributes")
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                attrs = parsed
        except ValueError:
            pass

    package = str(row.get("package") or "")
    if kind == "resistors":
        ordered = ("Resistance", "Tolerance", "Power(Watts)", "Temperature Coefficient")
        noun = "Chip Resistor"
    else:
        ordered = ("Capacitance", "Voltage Rated", "Tolerance", "Temperature Coefficient")
        noun = "Multilayer Ceramic Capacitor"
    bits = [str(attrs[k]) for k in ordered if attrs.get(k)]
    description = str(row.get("description") or "").strip()
    if not description:
        description = " ".join([*bits, package, noun]).strip()

    return Part(
        lcsc=_normalize_lcsc(row.get("lcsc")),
        mfr_part=str(row.get("mfr") or ""),
        manufacturer="",
        description=description,
        package=package,
        basic=bool(row.get("is_basic")),
        stock=int(row.get("stock") or 0),
        price_usd=_parse_price_breaks(row.get("price1", row.get("price"))),
        datasheet_url="",
        extra={
            "category": "Resistors" if kind == "resistors" else "Capacitors",
            "subcategory": "",
            "preferred": bool(row.get("is_preferred")),
        },
    )


def _score(part: Part, tokens: list[str]) -> int:
    """Score how well a part matches the query tokens.

    jlcsearch ranks loosely — a query of "10k 0603" returns a 510k
    resistor and an 0603x4 resistor *array* above the plain 10k 0603. We
    re-rank locally so the first hit is the one a human would have picked.

    A token scores 2 when it appears on a value boundary and 1 when it is
    only an embedded substring. The distinction is what separates "10kΩ"
    (boundary, wanted) from "510kΩ" (substring, not wanted) and "0603 Chip
    Resistor" (boundary) from "0603x4" (substring, a resistor array).
    """
    haystack = f"{part.description} {part.mfr_part} {part.package}".lower()
    total = 0
    for t in tokens:
        if not t:
            continue
        aliases = _value_aliases(t)
        if any(re.search(rf"(?<![0-9a-z]){re.escape(a)}(?![0-9a-z])", haystack) for a in aliases):
            total += 2
        elif any(a in haystack for a in aliases):
            total += 1
    return total


# Ordered most- to least-likely to appear verbatim in an upstream
# description. Measured against jlcsearch, not guessed: alphabetic terms
# ("TVS", "diode", "SMB") match far more reliably than formatted values
# ("30V", "3000W"), which upstream may render as "30.0V" or omit.
def _token_keep_rank(token: str) -> tuple:
    has_digit = any(ch.isdigit() for ch in token)
    has_alpha = any(ch.isalpha() for ch in token)
    if has_alpha and not has_digit:
        tier = 0  # TVS, diode, SMB, LDO
    elif has_alpha and has_digit:
        tier = 1  # 0603, SOT-23, AMS1117
    else:
        tier = 2  # bare numbers
    return (tier, -len(token))


async def _search_narrowing(
    client: httpx.AsyncClient,
    tokens: list[str],
    *,
    package: str | None,
    accept,
    max_attempts: int = 5,
) -> list[Part]:
    """Query jlcsearch, dropping the least-matchable tokens until it bites.

    jlcsearch AND-matches every token, so a natural-language spec like
    "TVS diode 30V 3000W SMB" returns exactly zero rows while "TVS diode
    SMB" returns a hundred. Without this, every descriptive query the
    plugin generates would come back empty.

    `accept` filters a batch of candidates (basic-only, stock floor). We
    keep narrowing while it returns nothing, so a request for basic parts
    doesn't stop at the first query that merely returned *something*.

    Narrowing floors at half the original tokens. Dropping further reliably
    produces nonsense — one token of "TVS diode 30V 3000W SMB" matches every
    LED in the catalog, because LED descriptions contain the word "Diode".
    Returning nothing is the honest answer there.
    """
    ordered = sorted(tokens, key=_token_keep_rank)
    min_k = max(1, (len(ordered) + 1) // 2)
    seen: set[tuple[str, str | None]] = set()
    attempts = 0

    # `package` is an exact-match filter upstream. Try it first because it
    # is what rescues passives ("10k 0603" is unusable without it), then
    # drop it, because a package string spelled differently upstream would
    # otherwise filter every row away.
    for pkg in (package, None) if package else (None,):
        for k in range(len(ordered), min_k - 1, -1):
            if attempts >= max_attempts:
                break
            # Preserve the user's original word order for readability.
            keep = set(ordered[:k])
            query = " ".join(t for t in tokens if t in keep)
            if not query or (query, pkg) in seen:
                continue
            seen.add((query, pkg))
            attempts += 1

            # Deliberately *not* passing is_basic to the API: it filters
            # before applying the row limit, which collapses a 100-row
            # candidate pool to two or three poor matches. Fetch the full
            # pool and filter locally instead.
            rows = await _jlcsearch_components(
                client, search=query, package=pkg, limit=JLCSEARCH_MAX_LIMIT
            )
            parts = [p for p in (_row_to_part_jlcsearch(r) for r in rows) if p.lcsc]
            if parts:
                # Everything we saw is worth caching, even rows this query
                # rejects — a later get_part may want them.
                _cache_put(parts)
            usable = accept(parts)
            if usable:
                if k < len(ordered) or pkg != package:
                    logger.debug(
                        "jlcsearch: narrowed %r -> %r (package=%s, %d usable)",
                        " ".join(tokens),
                        query,
                        pkg,
                        len(usable),
                    )
                return usable
    return []


# ---------------------------------------------------------------------------
# Public query API
# ---------------------------------------------------------------------------


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
    """Search the JLCPCB catalog by free-text query.

    jlcsearch AND-matches every token, so a long descriptive query is
    progressively narrowed (see `_search_narrowing`) until it returns
    candidates. Those are then re-ranked locally against the *full* query,
    then by basic-first and descending stock. `stock_min` is applied
    client-side so a part with unknown stock is never silently dropped.

    `force_refresh` is accepted for API compatibility; search always hits
    the network (results are written through to the cache for later
    `get_part` calls).
    """
    raw_tokens = [t for t in query.strip().split() if t]
    tokens = [t.lower() for t in raw_tokens]
    if not tokens:
        return []

    # A chip size embedded in the query ("10k 0603") is worth far more as
    # an exact package filter than as another search token: without it the
    # candidate pool for passives comes back full of wrong-size parts.
    effective_package = package or _extract_package(tokens)

    def accept(parts: list[Part]) -> list[Part]:
        keep = parts
        if basic_only:
            keep = [p for p in keep if p.basic]
        keep = [p for p in keep if p.stock >= int(stock_min) or p.extra.get("stock_unknown")]
        return sorted(keep, key=lambda p: (-_score(p, tokens), not p.basic, -p.stock))

    own_client = client is None
    if own_client:
        client = _make_client()
    try:
        ranked: list[Part] = []

        # Chip passives go to the structured routes first: free-text search
        # cannot find the ones with an empty upstream description, which
        # includes most of the basic-tier jellybeans a board is made of.
        passive = _detect_passive(tokens)
        if passive:
            kind, value = passive
            try:
                rows = await _jlcsearch_passive(
                    client,
                    kind,
                    value,
                    package=effective_package,
                    basic_only=basic_only,
                )
            except LcscError as e:
                logger.debug("structured %s lookup failed, falling back: %s", kind, e)
                rows = []
            parts = [p for p in (_row_to_part_passive(r, kind) for r in rows) if p.lcsc]
            if parts:
                _cache_put(parts)
            ranked = accept(parts)

        if not ranked:
            ranked = await _search_narrowing(
                client, raw_tokens, package=effective_package, accept=accept
            )
    finally:
        if own_client:
            await client.aclose()

    return ranked[: int(limit)]


async def _enrich_stock_from_jlcsearch(part: Part, client: httpx.AsyncClient) -> Part:
    """Best-effort: replace EasyEDA's placeholder stock with real JLCPCB stock.

    EasyEDA knows the part but not JLCPCB's SMT inventory. jlcsearch knows
    the inventory but cannot be queried by C-number, so we search for the
    manufacturer part number and accept the row only if its C-number
    matches exactly. That works for most ICs and discretes; some passives
    carry an empty description upstream and aren't indexed, in which case
    the part keeps its `stock_unknown` flag.
    """
    if not part.mfr_part:
        return part
    try:
        rows = await _jlcsearch_components(client, search=part.mfr_part, limit=20)
    except LcscError as e:
        logger.debug("Stock enrichment for %s failed: %s", part.lcsc, e)
        return part

    for row in rows:
        if _normalize_lcsc(row.get("lcsc")) != part.lcsc:
            continue
        enriched = _row_to_part_jlcsearch(row)
        # Prefer jlcsearch for anything it is authoritative about, but keep
        # EasyEDA's identity fields when jlcsearch's are blank.
        enriched.mfr_part = enriched.mfr_part or part.mfr_part
        enriched.manufacturer = enriched.manufacturer or part.manufacturer
        enriched.description = enriched.description or part.description
        enriched.package = enriched.package or part.package
        enriched.price_usd = enriched.price_usd or part.price_usd
        return enriched
    return part


async def get_part(
    lcsc: str,
    *,
    client: httpx.AsyncClient | None = None,
    force_refresh: bool = False,
) -> Part:
    """Fetch a single part by LCSC C-number.

    Served from the local cache when fresh. On a miss, resolves via
    EasyEDA (the only source that supports exact C-number lookup) and
    then tries to attach real JLCPCB stock from jlcsearch.

    Raises LcscError for a malformed C-number or an unresolvable part.
    """
    if not lcsc.startswith("C") or not lcsc[1:].isdigit():
        raise LcscError(f"Invalid LCSC part number: {lcsc!r}")

    if not force_refresh:
        cached = _cache_get(lcsc)
        if cached is not None:
            return cached

    # Imported lazily: part_library imports get_part from this module at
    # import time, so a module-level import here would be circular.
    from . import part_library

    own_client = client is None
    if own_client:
        client = _make_client()
    try:
        try:
            result = await part_library._fetch_easyeda_raw(lcsc, client)
        except part_library.PartLibraryError as e:
            raise LcscError(f"LCSC part not found: {lcsc} ({e})") from e

        part = _part_from_easyeda(lcsc, result)
        if not part.mfr_part:
            raise LcscError(f"LCSC part not found: {lcsc}")
        part = await _enrich_stock_from_jlcsearch(part, client)
    finally:
        if own_client:
            await client.aclose()

    _cache_put([part])
    return part


# ---------------------------------------------------------------------------
# BOM resolution
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
    stock_unverified = 0
    try:
        for row in rows:
            try:
                part = await _resolve_one(row, client, force_refresh=force_refresh)
            except LcscError as e:
                unresolved.append({"row": row, "reason": str(e)})
                continue
            warnings = []
            cw = part.cost_warning()
            if cw:
                warnings.append(cw)
                seen_extended_lcsc.add(part.lcsc)
            sw = part.stock_warning()
            if sw:
                warnings.append(sw)
                stock_unverified += 1
            resolved.append({"row": row, "part": asdict(part), "warnings": warnings})
    finally:
        if own_client:
            await client.aclose()

    setup_fee = len(seen_extended_lcsc) * config.EXTENDED_PART_SETUP_FEE_USD
    summary = (
        f"Resolved {len(resolved)} of {len(resolved) + len(unresolved)} BOM rows. "
        f"{len(seen_extended_lcsc)} unique extended-tier parts; "
        f"estimated assembly setup fee impact: ${setup_fee:.2f}."
    )
    if stock_unverified:
        summary += f" {stock_unverified} part(s) have unverified JLCPCB stock."
    return {
        "resolved": resolved,
        "unresolved": unresolved,
        "extended_part_count": len(seen_extended_lcsc),
        "estimated_setup_fee_usd": round(setup_fee, 2),
        "summary": summary,
    }


async def _resolve_one(row: dict, client: httpx.AsyncClient, *, force_refresh: bool) -> Part:
    """Resolve a single BOM row to a Part."""
    if row.get("lcsc"):
        return await get_part(row["lcsc"], client=client, force_refresh=force_refresh)

    query = row.get("query") or row.get("description")
    if not query:
        raise LcscError(f"BOM row has neither 'lcsc' nor 'query': {row}")

    package = row.get("package")
    # Ask for a spread of candidates so local re-ranking has something to
    # work with, then take the best. Basic-first, falling back to extended.
    for basic_only in (True, False):
        matches = await search(
            query,
            package=package,
            basic_only=basic_only,
            limit=20,
            client=client,
        )
        if matches:
            return matches[0]
    raise LcscError(f"No LCSC match for query={query!r} package={package!r}")
