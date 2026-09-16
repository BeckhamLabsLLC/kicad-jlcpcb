"""Fetch KiCad library files (symbol, footprint, 3D model) for an LCSC part.

JLC2KiCad_lib (the historical tool for this) is unmaintained, so we roll
a minimal fetcher that:

  1. Looks up the LCSC component on EasyEDA's component endpoint (the
     same backend EasyEDA's web app uses to load preview symbols).
  2. Converts the EasyEDA symbol → a KiCad 8 .kicad_sym S-expression.
  3. Converts the EasyEDA footprint → a KiCad 8 .kicad_mod S-expression.
  4. Downloads the OBJ/STEP 3D model when present.
  5. Installs everything into <project>/libs/ with predictable filenames.

EasyEDA's component data is the canonical source for JLCPCB-stocked
parts because EasyEDA, LCSC, and JLCPCB share a parent company. The
endpoint we use is intentionally narrow and its base URL lives in
`config.EASYEDA_BASE` (override with `KJLC_EASYEDA_BASE`) so a single
change recovers if it ever moves.

This module deliberately produces *correct enough* KiCad files rather
than perfect ones. KiCad will open them, the user can review/edit, and
the schematic + footprint will route through ERC and DRC. Anything more
ambitious (full pin metadata fidelity, parametric footprints) is Phase 2.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from . import config
from .lcsc_client import get_part

logger = logging.getLogger(__name__)

# EasyEDA's component info endpoint. Returns the raw schematic symbol +
# footprint data that the EasyEDA web app uses to render previews. We use
# it for two things:
#   1. Pin-number → pin-name maps (via `part_pin_map`), so the LLM can
#      write nets like `("U1", "GPIO10")` instead of guessing pin numbers.
#   2. Placeholder symbol/footprint generation inside `fetch_part_library`.
#
# Rate-limited to roughly 1 request/minute per IP after the first burst.
# We enforce client-side throttling via a last-hit timestamp and cache
# responses in SQLite so each unique C-number is fetched exactly once.
EASYEDA_COMPONENT_URL = config.EASYEDA_BASE + "/api/products/{lcsc}/components"

EASYEDA_HEADERS = {
    "Accept-Encoding": "gzip, deflate",
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Referer": "https://easyeda.com/",
    "X-Requested-With": "XMLHttpRequest",
}

# Minimum delay between back-to-back EasyEDA requests. Empirical: the
# service tolerates roughly 1 req/minute per IP after an initial burst.
# We target ~12s between requests so a 10-part BOM takes ~2 min to warm
# the cache on first run, then instant forever after.
EASYEDA_MIN_DELAY_SECONDS = 12.0

# Per-request timeout and attempt count. A BOM resolve makes one sequential
# request per unique part across several minutes, so a single stalled
# connection should not fail the part — reported as "not found", it reads
# as a bad C-number and sends the caller looking in the wrong place.
EASYEDA_TIMEOUT_SECONDS = 20.0
EASYEDA_ATTEMPTS = 3
EASYEDA_RETRY_BACKOFF_SECONDS = 2.0


class EasyEdaRateLimiter:
    """Throttle EasyEDA component fetches to 1 request per ``EASYEDA_MIN_DELAY_SECONDS``.

    State is process-local (no cross-worker coordination). That is fine
    because the MCP server runs in a single process, and EasyEDA's
    anti-bot quota is applied per IP, not per connection.

    The delay is read from the module-level constant each time so tests
    can set it to 0 via ``monkeypatch.setattr(part_library,
    "EASYEDA_MIN_DELAY_SECONDS", 0.0)`` without replacing the limiter.
    """

    def __init__(self) -> None:
        self.last_request_time: float = 0.0
        self.lock: asyncio.Lock = asyncio.Lock()

    async def acquire(self) -> None:
        """Block until the min-delay window has elapsed since the last acquire."""
        async with self.lock:
            now = time.monotonic()
            elapsed = now - self.last_request_time
            if elapsed < EASYEDA_MIN_DELAY_SECONDS:
                wait = EASYEDA_MIN_DELAY_SECONDS - elapsed
                logger.info("EasyEDA rate limit: sleeping %.1fs before next request", wait)
                await asyncio.sleep(wait)
            self.last_request_time = time.monotonic()

    async def backoff(self, seconds: float) -> None:
        """Hard backoff (used after a 403); resets the timing window on resume."""
        async with self.lock:
            await asyncio.sleep(seconds)
            self.last_request_time = time.monotonic()


# Process-local singleton. Tests that need the limiter reset between cases
# should set ``_limiter.last_request_time = 0.0`` in a fixture.
_limiter = EasyEdaRateLimiter()


@dataclass
class FetchResult:
    """What got installed for one LCSC part."""

    lcsc: str
    symbol_path: Path | None = None
    footprint_path: Path | None = None
    model_3d_path: Path | None = None
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "lcsc": self.lcsc,
            "symbol_path": str(self.symbol_path) if self.symbol_path else None,
            "footprint_path": str(self.footprint_path) if self.footprint_path else None,
            "model_3d_path": str(self.model_3d_path) if self.model_3d_path else None,
            "warnings": self.warnings,
        }


class PartLibraryError(RuntimeError):
    """Fetch / convert / install failed."""


# ---------------------------------------------------------------------------
# Pin-map cache (SQLite alongside the LCSC parts cache)
# ---------------------------------------------------------------------------


def _pinmap_cache_path() -> Path:
    """Resolve the cache db path for pin maps. Lazy so monkeypatch works."""
    return Path(config.CACHE_DIR) / "easyeda_pinmaps.sqlite"


@contextmanager
def _open_pinmap_cache():
    path = _pinmap_cache_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS pinmaps (
                lcsc TEXT PRIMARY KEY,
                payload TEXT NOT NULL,
                fetched_at INTEGER NOT NULL
            )
            """
        )
        conn.commit()
        yield conn
    finally:
        conn.close()


def _pinmap_from_cache(lcsc: str) -> dict | None:
    with _open_pinmap_cache() as conn:
        row = conn.execute("SELECT payload FROM pinmaps WHERE lcsc = ?", (lcsc,)).fetchone()
    if row is None:
        return None
    return json.loads(row[0])


def _pinmap_to_cache(lcsc: str, payload: dict) -> None:
    with _open_pinmap_cache() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO pinmaps (lcsc, payload, fetched_at) VALUES (?, ?, ?)",
            (lcsc, json.dumps(payload), int(time.time())),
        )
        conn.commit()


# ---------------------------------------------------------------------------
# Pin map extraction from EasyEDA's shape array
# ---------------------------------------------------------------------------


# EasyEDA works in 10-mil units for both symbols and footprints, so one
# unit is exactly 0.254 mm. Verified against a SOT-23-5: its pads sit
# 3.74 units apart, and 3.74 * 0.254 = 0.95 mm, the package's real pitch.
EASYEDA_UNIT_MM = 0.254

# File-format versions for what we emit. These are what KiCad 9/10 write.
KICAD_SYM_VERSION = "20241209"
KICAD_MOD_VERSION = "20241229"


@dataclass
class EasyEdaPin:
    """One symbol pin, in millimetres relative to the symbol origin."""

    number: str
    name: str
    x_mm: float
    y_mm: float
    rotation: int  # degrees, EasyEDA convention (direction the pin points)
    electrical: str = "passive"


@dataclass
class EasyEdaPad:
    """One footprint pad, in millimetres relative to the footprint origin."""

    number: str
    shape: str  # RECT | ELLIPSE | OVAL | POLYGON
    x_mm: float
    y_mm: float
    width_mm: float
    height_mm: float
    layer: int  # EasyEDA layer id: 1 = top copper, 2 = bottom
    hole_mm: float = 0.0
    rotation: float = 0.0

    @property
    def is_smd(self) -> bool:
        return self.hole_mm <= 0.0


def _f(value: str, default: float = 0.0) -> float:
    """Parse a float from EasyEDA's data, tolerating blanks and junk."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def parse_symbol_pins(shape_list: list) -> list[EasyEdaPin]:
    """Extract full pin geometry from an EasyEDA symbol `dataStr.shape`.

    A pin entry is `^^`-joined segments::

        P~<show>~<electric>~<id>~<x>~<y>~<rot>~<gge>~<locked>
         ^^<dot_x>~<dot_y>
         ^^<path>~<color>
         ^^<show>~<nx>~<ny>~<nrot>~<NAME>~<anchor>~...
         ^^<show>~<px>~<py>~<prot>~<NUMBER>~<anchor>~...

    Note the pad number comes from segment 4, not segment 0. Segment 0
    field 3 is EasyEDA's internal pin *id*, which coincides with the pad
    number on simple parts and diverges on others — reading it as the pad
    number silently mis-wires exactly the parts where it matters.
    """
    pins: list[EasyEdaPin] = []
    if not isinstance(shape_list, list):
        return pins
    for entry in shape_list:
        if not isinstance(entry, str) or not entry.startswith("P~"):
            continue
        segs = entry.split("^^")
        if len(segs) < 5:
            continue
        head = segs[0].split("~")
        name_seg = segs[3].split("~")
        num_seg = segs[4].split("~")
        if len(head) < 7 or len(name_seg) < 5 or len(num_seg) < 5:
            continue

        number = num_seg[4].strip() or head[3].strip()
        if not number:
            continue
        pins.append(
            EasyEdaPin(
                number=number,
                name=name_seg[4].strip() or number,
                x_mm=_f(head[4]) * EASYEDA_UNIT_MM,
                y_mm=_f(head[5]) * EASYEDA_UNIT_MM,
                rotation=int(_f(head[6])) % 360,
            )
        )
    return pins


def parse_footprint_pads(package_shape: list) -> list[EasyEdaPad]:
    """Extract pad geometry from an EasyEDA `packageDetail.dataStr.shape`.

    A pad entry is::

        PAD~<shape>~<x>~<y>~<w>~<h>~<layer>~<net>~<number>~<hole_r>
            ~<points>~<rot>~<gge>~...

    Coordinates are absolute on EasyEDA's canvas (origin near 4000,3000);
    callers re-centre them with `pads_origin`.
    """
    pads: list[EasyEdaPad] = []
    if not isinstance(package_shape, list):
        return pads
    for entry in package_shape:
        if not isinstance(entry, str) or not entry.startswith("PAD~"):
            continue
        f = entry.split("~")
        if len(f) < 10:
            continue
        number = f[8].strip()
        if not number:
            continue
        pads.append(
            EasyEdaPad(
                number=number,
                shape=(f[1] or "RECT").strip().upper(),
                x_mm=_f(f[2]) * EASYEDA_UNIT_MM,
                y_mm=_f(f[3]) * EASYEDA_UNIT_MM,
                width_mm=_f(f[4]) * EASYEDA_UNIT_MM,
                height_mm=_f(f[5]) * EASYEDA_UNIT_MM,
                layer=int(_f(f[6], 1)),
                # EasyEDA stores a hole *radius*; KiCad wants a diameter.
                hole_mm=_f(f[9]) * EASYEDA_UNIT_MM * 2.0,
                rotation=_f(f[11]) if len(f) > 11 else 0.0,
            )
        )
    return pads


def pads_origin(pads: list[EasyEdaPad]) -> tuple[float, float]:
    """Centre of the pad bounding box, used to re-zero absolute coordinates."""
    if not pads:
        return (0.0, 0.0)
    xs = [p.x_mm for p in pads]
    ys = [p.y_mm for p in pads]
    return ((min(xs) + max(xs)) / 2.0, (min(ys) + max(ys)) / 2.0)


def parse_pinmap_from_shape(shape_list: list) -> dict[str, str]:
    """Extract {pin_name: pin_num} from an EasyEDA dataStr.shape array.

    EasyEDA's shape entries for pins have the format:

        P~<show>~<electric>~<pinNum>~<x>~<y>~<rot>~<id>~<locked>
         ^^<pathStart>
         ^^<pathDrawing>~<color>
         ^^<nameShow>~<nx>~<ny>~<nr>~<pinName>~<anchor>~<?>~<?>~<nameColor>
         ^^<numShow>~<numx>~<numy>~<numr>~<pinNum>~<anchor>~<?>~<?>~<numColor>
         ...

    Segments are joined by `^^`. Within each segment, fields are `~`-separated.
    We extract pin_num from segment 0 position 3 and pin_name from segment 3
    position 4. Entries with empty names are skipped.

    Returns an empty dict if no pins are found (common for passives and
    connectors where EasyEDA doesn't name the pins).
    """
    pinmap: dict[str, str] = {}
    if not isinstance(shape_list, list):
        return pinmap
    for s in shape_list:
        if not isinstance(s, str) or not s.startswith("P~"):
            continue
        segs = s.split("^^")
        if len(segs) < 4:
            continue
        seg0 = segs[0].split("~")
        seg3 = segs[3].split("~")
        if len(seg0) < 4 or len(seg3) < 5:
            continue
        pin_num = seg0[3].strip()
        pin_name = seg3[4].strip()
        if pin_num and pin_name:
            # Later entries with same name overwrite earlier — shouldn't
            # happen in practice since EasyEDA assigns unique names, but
            # keep the pinmap keyed by name so nets can reference it.
            pinmap[pin_name] = pin_num
    return pinmap


# ---------------------------------------------------------------------------
# Rate-limited EasyEDA fetch
# ---------------------------------------------------------------------------


async def _fetch_easyeda_raw(lcsc: str, client: httpx.AsyncClient) -> dict:
    """Low-level EasyEDA fetch with rate limiting and one-shot retry on 403.

    Returns the parsed ``result`` dict. Raises :class:`PartLibraryError`
    on unrecoverable failure.
    """
    await _limiter.acquire()

    url = EASYEDA_COMPONENT_URL.format(lcsc=lcsc)
    resp = None
    for attempt in range(1, EASYEDA_ATTEMPTS + 1):
        try:
            resp = await client.get(url, headers=EASYEDA_HEADERS, timeout=EASYEDA_TIMEOUT_SECONDS)
        except httpx.HTTPError as e:
            # Retry transient network faults, not just 403s. Resolving a
            # 13-part BOM makes 13 sequential requests over several minutes;
            # a single stalled connection used to fail the part outright and
            # report it as "not found", which reads as a bad C-number.
            if attempt < EASYEDA_ATTEMPTS:
                logger.debug(
                    "EasyEDA network error for %s (attempt %d/%d): %r — retrying",
                    lcsc,
                    attempt,
                    EASYEDA_ATTEMPTS,
                    e,
                )
                await asyncio.sleep(EASYEDA_RETRY_BACKOFF_SECONDS * attempt)
                continue
            raise PartLibraryError(
                f"EasyEDA network error for {lcsc} after {EASYEDA_ATTEMPTS} attempts: {e!r}"
            ) from e
        if resp.status_code == 200:
            break
        if resp.status_code == 403 and attempt < EASYEDA_ATTEMPTS:
            # Rate-limited despite our throttling; back off hard and retry.
            logger.warning("EasyEDA 403 for %s, backing off 60s", lcsc)
            await _limiter.backoff(60.0)
            continue
        if resp.status_code == 404:
            raise PartLibraryError(f"EasyEDA has no component data for {lcsc}")
        raise PartLibraryError(f"EasyEDA returned HTTP {resp.status_code} for {lcsc}")

    if resp is None or resp.status_code != 200:
        raise PartLibraryError(f"EasyEDA fetch for {lcsc} exhausted {EASYEDA_ATTEMPTS} attempts")

    try:
        data = resp.json()
    except ValueError as e:
        raise PartLibraryError(f"EasyEDA returned non-JSON for {lcsc}: {e}") from e
    if not isinstance(data, dict) or data.get("success") is False:
        raise PartLibraryError(
            f"EasyEDA component fetch unsuccessful for {lcsc}: "
            f"{data.get('result') if isinstance(data, dict) else 'malformed'}"
        )
    return data.get("result", data)


# ---------------------------------------------------------------------------
# Public API for pin maps
# ---------------------------------------------------------------------------


async def get_pin_map(
    lcsc: str,
    *,
    client: httpx.AsyncClient | None = None,
    force_refresh: bool = False,
) -> dict:
    """Return the pin-name → pad-number map for an LCSC C-number.

    Hits the SQLite cache first. On cache miss, fetches from EasyEDA with
    client-side rate limiting (minimum 12s between requests) and writes
    the result to cache indefinitely.

    Returns:
      {
        "lcsc": "C82942",
        "title": "ME6211C33M5G-N",
        "pin_count": 5,
        "pinmap": {"VIN": "1", "VSS": "2", "CE": "3", "NC": "4", "VOUT": "5"},
        "pad_to_name": {"1": "VIN", "2": "VSS", ...},   # reverse lookup
        "source": "cache" | "easyeda"
      }

    Raises PartLibraryError on unrecoverable fetch failure. An empty
    pinmap (e.g. passives without named pins) is a valid successful
    result — the caller can use bare pad numbers instead.
    """
    if not lcsc.startswith("C") or not lcsc[1:].isdigit():
        raise PartLibraryError(f"Invalid LCSC part number: {lcsc!r}")

    if not force_refresh:
        cached = _pinmap_from_cache(lcsc)
        if cached is not None:
            cached["source"] = "cache"
            return cached

    own_client = client is None
    if own_client:
        client = httpx.AsyncClient(timeout=httpx.Timeout(15.0), follow_redirects=True)
    try:
        result = await _fetch_easyeda_raw(lcsc, client)
        title = result.get("title", "")
        shape = result.get("dataStr", {}).get("shape", [])
        pinmap = parse_pinmap_from_shape(shape)
        pad_to_name = {v: k for k, v in pinmap.items()}

        payload = {
            "lcsc": lcsc,
            "title": title,
            "pin_count": len(pinmap),
            "pinmap": pinmap,
            "pad_to_name": pad_to_name,
        }
        _pinmap_to_cache(lcsc, payload)
        payload["source"] = "easyeda"
        return payload
    finally:
        if own_client:
            await client.aclose()


async def get_pin_maps(
    lcscs: list[str],
    *,
    client: httpx.AsyncClient | None = None,
    force_refresh: bool = False,
) -> dict[str, dict]:
    """Bulk variant. Fetches in order (respecting rate limits) and returns
    a dict keyed by C-number. Individual failures are captured as
    `{"error": "..."}` entries so one bad part doesn't blow up the whole
    BOM resolution."""
    own_client = client is None
    if own_client:
        client = httpx.AsyncClient(timeout=httpx.Timeout(15.0), follow_redirects=True)
    try:
        results: dict[str, dict] = {}
        for lcsc in lcscs:
            try:
                results[lcsc] = await get_pin_map(lcsc, client=client, force_refresh=force_refresh)
            except PartLibraryError as e:
                results[lcsc] = {"lcsc": lcsc, "error": str(e), "pinmap": {}}
        return results
    finally:
        if own_client:
            await client.aclose()


# ---------------------------------------------------------------------------
# EasyEDA fetch
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Symbol generation
# ---------------------------------------------------------------------------


def _safe_id(name: str) -> str:
    """Sanitize a name for use in KiCad library/symbol identifiers."""
    return "".join(ch if ch.isalnum() or ch in "_-" else "_" for ch in name)


def _kicad_pin_angle(easyeda_rotation: int) -> int:
    """Convert EasyEDA's pin direction to KiCad's.

    EasyEDA's angle is the direction the pin points away from the body; a
    left-edge pin reads 180. KiCad's angle is the direction the pin body
    extends from its connection point, so the same pin reads 0.
    """
    return (180 - int(easyeda_rotation)) % 360


def build_kicad_symbol(
    lcsc: str,
    mfr_part: str,
    description: str,
    pins: list[EasyEdaPin],
    *,
    pin_count: int = 2,
) -> str:
    """Emit a `.kicad_sym` from real EasyEDA pin geometry.

    Earlier releases emitted a rectangle with pins named `P1..Pn`, because
    the pin data was assumed to be out of reach. It is not: EasyEDA's
    `dataStr.shape` carries each pin's number, name and position, and this
    now uses them. That means a schematic can reference `VOUT` and get the
    right pad.

    Falls back to the old numbered-rectangle behaviour only when EasyEDA
    returned no pins at all, which happens for some passives.
    """
    symbol_name = _safe_id(lcsc)
    if not pins:
        pins = [
            EasyEdaPin(number=str(i + 1), name=str(i + 1), x_mm=0.0, y_mm=0.0, rotation=180)
            for i in range(max(1, pin_count))
        ]
        # Lay the fallback out as a plain two-column rectangle.
        left = (len(pins) + 1) // 2
        for i, pin in enumerate(pins):
            row = i if i < left else i - left
            pin.x_mm = -8.89 if i < left else 8.89
            pin.y_mm = -(row * 2.54)
            pin.rotation = 180 if i < left else 0

    # EasyEDA's symbol Y grows downward; KiCad's grows upward.
    xs = [p.x_mm for p in pins]
    ys = [-p.y_mm for p in pins]
    cx = (min(xs) + max(xs)) / 2.0
    cy = (min(ys) + max(ys)) / 2.0

    def snap(v: float) -> float:
        """KiCad refuses to connect pins that are off the 1.27 mm grid."""
        return round(v / 1.27) * 1.27

    placed = [(p, snap(p.x_mm - cx), snap(-p.y_mm - cy)) for p in pins]
    half_w = max((abs(x) for _, x, _ in placed), default=5.08) - 2.54
    half_w = max(half_w, 2.54)
    half_h = max((abs(y) for _, _, y in placed), default=2.54) + 2.54

    pin_lines = []
    for pin, x, y in placed:
        pin_lines.append(
            f"      (pin passive line\n"
            f"        (at {x:g} {y:g} {_kicad_pin_angle(pin.rotation)})\n"
            f"        (length 2.54)\n"
            f'        (name "{_escape_sexpr(pin.name)}" (effects (font (size 1.27 1.27))))\n'
            f'        (number "{_escape_sexpr(pin.number)}" (effects (font (size 1.27 1.27))))\n'
            f"      )"
        )

    return (
        "(kicad_symbol_lib\n"
        f"  (version {KICAD_SYM_VERSION})\n"
        '  (generator "kicad_jlcpcb_mcp")\n'
        f'  (symbol "{symbol_name}"\n'
        "    (pin_names (offset 0.762))\n"
        "    (exclude_from_sim no)\n"
        "    (in_bom yes)\n"
        "    (on_board yes)\n"
        f'    (property "Reference" "U" (at 0 {half_h + 1.27:g} 0)'
        " (effects (font (size 1.27 1.27))))\n"
        f'    (property "Value" "{_escape_sexpr(mfr_part or lcsc)}"'
        f" (at 0 {-(half_h + 1.27):g} 0) (effects (font (size 1.27 1.27))))\n"
        f'    (property "Description" "{_escape_sexpr(description)}" (at 0 0 0)'
        " (effects (font (size 1.27 1.27)) (hide yes)))\n"
        f'    (property "LCSC" "{_escape_sexpr(lcsc)}" (at 0 0 0)'
        " (effects (font (size 1.27 1.27)) (hide yes)))\n"
        f'    (symbol "{symbol_name}_0_1"\n'
        f"      (rectangle (start {-half_w:g} {half_h:g}) (end {half_w:g} {-half_h:g})\n"
        "        (stroke (width 0.254) (type default))\n"
        "        (fill (type background))\n"
        "      )\n"
        "    )\n"
        f'    (symbol "{symbol_name}_1_1"\n' + "\n".join(pin_lines) + "\n    )\n"
        "  )\n"
        ")\n"
    )


def _escape_sexpr(value: str) -> str:
    return str(value).replace("\\", "\\\\").replace('"', '\\"')


# ---------------------------------------------------------------------------
# Footprint generation
# ---------------------------------------------------------------------------

# EasyEDA layer ids we care about. 1 is top copper, 2 is bottom.
_EE_LAYER_TOP = 1
_EE_LAYER_BOTTOM = 2

_KICAD_PAD_SHAPES = {
    "RECT": "rect",
    "ELLIPSE": "circle",
    "OVAL": "oval",
    "POLYGON": "rect",
}


def build_kicad_footprint(
    lcsc: str,
    mfr_part: str,
    package: str,
    pads: list[EasyEdaPad],
    *,
    pin_count: int = 2,
) -> str:
    """Emit a `.kicad_mod` from real EasyEDA pad geometry.

    Earlier releases emitted IPC nominals from a hardcoded table covering
    0402/0603/0805/1206/SOT-23, and a generic two-row guess for anything
    else — so any part outside that table got pads that did not match it.
    EasyEDA ships the actual pad polygons in `packageDetail`, so this uses
    them: position, size, shape, layer, and drill.

    Falls back to a minimal two-pad placeholder only when EasyEDA returned
    no pads, and marks the result so callers can warn.
    """
    name = _safe_id(f"{lcsc}_{package}" if package else lcsc)

    if not pads:
        pads = [
            EasyEdaPad(
                number=str(i + 1),
                shape="RECT",
                x_mm=(-1.0 if i == 0 else 1.0),
                y_mm=0.0,
                width_mm=1.0,
                height_mm=1.0,
                layer=_EE_LAYER_TOP,
            )
            for i in range(max(2, min(pin_count, 2)))
        ]

    ox, oy = pads_origin(pads)
    has_tht = any(not p.is_smd for p in pads)

    lines = []
    for pad in pads:
        shape = _KICAD_PAD_SHAPES.get(pad.shape, "rect")
        x = pad.x_mm - ox
        y = pad.y_mm - oy
        w = max(pad.width_mm, 0.05)
        h = max(pad.height_mm, 0.05)
        at = f"(at {x:.4f} {y:.4f}{f' {pad.rotation:g}' if pad.rotation else ''})"
        if pad.is_smd:
            layer = "F" if pad.layer == _EE_LAYER_TOP else "B"
            layers = f'(layers "{layer}.Cu" "{layer}.Paste" "{layer}.Mask")'
            lines.append(
                f'  (pad "{_escape_sexpr(pad.number)}" smd {shape} {at} '
                f"(size {w:.4f} {h:.4f}) {layers})"
            )
        else:
            drill = max(pad.hole_mm, 0.2)
            lines.append(
                f'  (pad "{_escape_sexpr(pad.number)}" thru_hole {shape} {at} '
                f'(size {w:.4f} {h:.4f}) (drill {drill:.4f}) (layers "*.Cu" "*.Mask"))'
            )

    xs = [p.x_mm - ox for p in pads]
    ys = [p.y_mm - oy for p in pads]
    hw = (max(xs) - min(xs)) / 2.0 + 0.6
    hh = (max(ys) - min(ys)) / 2.0 + 0.6

    return (
        f'(footprint "{name}"\n'
        f"  (version {KICAD_MOD_VERSION})\n"
        '  (generator "kicad_jlcpcb_mcp")\n'
        '  (layer "F.Cu")\n'
        f"  (attr {'through_hole' if has_tht else 'smd'})\n"
        f'  (property "Reference" "REF**" (at 0 {-hh - 0.8:.3f} 0) (layer "F.SilkS")\n'
        "    (effects (font (size 1 1) (thickness 0.15)))\n  )\n"
        f'  (property "Value" "{_escape_sexpr(mfr_part or name)}" (at 0 {hh + 0.8:.3f} 0)'
        ' (layer "F.Fab")\n'
        "    (effects (font (size 1 1) (thickness 0.15)))\n  )\n"
        f"  (fp_rect (start {-hw:.3f} {-hh:.3f}) (end {hw:.3f} {hh:.3f})\n"
        '    (stroke (width 0.1) (type default)) (fill none) (layer "F.CrtYd"))\n'
        + "\n".join(lines)
        + "\n)\n"
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def fetch_part_library(
    lcsc: str,
    libs_dir: str | Path,
    *,
    client: httpx.AsyncClient | None = None,
) -> FetchResult:
    """Install symbol + footprint (+ 3D model when available) for `lcsc`
    into `libs_dir`.

    The Part metadata comes from `lcsc_client.get_part`. The structural
    information (pin count, package) comes from EasyEDA when reachable;
    when EasyEDA is unreachable we fall back to a 2-pin generic part with
    a warning so the rest of the pipeline still proceeds.
    """
    libs = Path(libs_dir).expanduser().resolve()
    libs.mkdir(parents=True, exist_ok=True)

    # Always fetch the canonical Part metadata first; this also primes
    # the LCSC cache and validates the C-number.
    part = await get_part(lcsc, client=client)

    own_client = client is None
    if own_client:
        # Headers are set per-request by _fetch_easyeda_raw (EasyEDA
        # rejects a generic UA), but redirects must be followed here.
        client = httpx.AsyncClient(timeout=httpx.Timeout(15.0), follow_redirects=True)

    warnings: list[str] = []
    pin_count = 2  # default for 2-terminal passives
    ee_pins: list[EasyEdaPin] = []
    ee_pads: list[EasyEdaPad] = []
    has_3d_model = False
    model_url = ""
    try:
        try:
            ee = await _fetch_easyeda_raw(lcsc, client)
            # EasyEDA's pin info lives under symbol.shape with type 'P';
            # we just count entries to get the pin count and detect 3D.
            shapes = ee.get("dataStr", {}).get("shape", [])
            if isinstance(shapes, list) and shapes:
                pin_count = sum(1 for s in shapes if isinstance(s, str) and s.startswith("P~"))
                if pin_count == 0:
                    pin_count = 2
                ee_pins = parse_symbol_pins(shapes)
                ee_pads = parse_footprint_pads(
                    ee.get("packageDetail", {}).get("dataStr", {}).get("shape", [])
                )
            model_block = ee.get("packageDetail", {}).get("dataStr", {}).get("shape", [])
            for block in model_block if isinstance(model_block, list) else []:
                if isinstance(block, str) and block.startswith("SVGNODE"):
                    has_3d_model = True
                    break
            # Best-effort 3D model URL
            uuid = ee.get("packageDetail", {}).get("dataStr", {}).get("head", {}).get("uuid", "")
            if has_3d_model and uuid:
                model_url = f"https://modules.easyeda.com/3dmodel/{uuid}"
        except PartLibraryError as e:
            warnings.append(
                f"Could not fetch EasyEDA component data: {e}. "
                f"Falling back to a generic 2-pin symbol/footprint placeholder — "
                f"verify both in KiCad before routing."
            )

        if not ee_pins:
            warnings.append(
                "EasyEDA returned no symbol pins; the symbol is a numbered "
                "rectangle rather than the real part. Pin names will not resolve."
            )
        if not ee_pads:
            warnings.append(
                "EasyEDA returned no pad geometry; the footprint is a placeholder "
                "and will NOT match the real part. Substitute a footprint from "
                "KiCad's standard libraries before ordering."
            )

        symbol_text = build_kicad_symbol(
            lcsc=part.lcsc,
            mfr_part=part.mfr_part,
            description=part.description,
            pins=ee_pins,
            pin_count=pin_count,
        )
        footprint_text = build_kicad_footprint(
            lcsc=part.lcsc,
            mfr_part=part.mfr_part,
            package=part.package,
            pads=ee_pads,
            pin_count=pin_count,
        )

        sym_path = libs / f"{_safe_id(part.lcsc)}.kicad_sym"
        sym_path.write_text(symbol_text)

        # Footprints in KiCad live in .pretty directories.
        pretty = libs / "kicad_jlcpcb.pretty"
        pretty.mkdir(exist_ok=True)
        fp_path = pretty / f"{_safe_id(part.lcsc)}_{_safe_id(part.package or 'GEN')}.kicad_mod"
        fp_path.write_text(footprint_text)

        model_path: Path | None = None
        if has_3d_model and model_url:
            try:
                m = await client.get(model_url)
                if m.status_code == 200:
                    models_dir = libs / "3d"
                    models_dir.mkdir(exist_ok=True)
                    model_path = models_dir / f"{_safe_id(part.lcsc)}.obj"
                    model_path.write_bytes(m.content)
                else:
                    warnings.append(f"3D model fetch returned HTTP {m.status_code}")
            except httpx.HTTPError as e:
                warnings.append(f"3D model fetch failed: {e}")

        return FetchResult(
            lcsc=part.lcsc,
            symbol_path=sym_path,
            footprint_path=fp_path,
            model_3d_path=model_path,
            warnings=warnings,
        )
    finally:
        if own_client:
            await client.aclose()
