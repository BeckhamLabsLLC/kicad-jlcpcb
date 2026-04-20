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
endpoint we use is intentionally narrow and version-pinned in
`config.JLCSEARCH_BASE` so a single change of base URL recovers if it
ever moves.

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
EASYEDA_COMPONENT_URL = "https://easyeda.com/api/products/{lcsc}/components"

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
    for attempt in (1, 2):
        try:
            resp = await client.get(url, headers=EASYEDA_HEADERS, timeout=15.0)
        except httpx.HTTPError as e:
            raise PartLibraryError(f"EasyEDA network error for {lcsc}: {e}") from e
        if resp.status_code == 200:
            break
        if resp.status_code == 403 and attempt == 1:
            # Rate-limited despite our throttling; back off hard and retry once
            logger.warning("EasyEDA 403 for %s, backing off 60s", lcsc)
            await _limiter.backoff(60.0)
            continue
        if resp.status_code == 404:
            raise PartLibraryError(f"EasyEDA has no component data for {lcsc}")
        raise PartLibraryError(f"EasyEDA returned HTTP {resp.status_code} for {lcsc}")

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


async def _fetch_easyeda_component(lcsc: str, client: httpx.AsyncClient) -> dict:
    """Fetch raw EasyEDA component JSON for an LCSC part."""
    url = EASYEDA_COMPONENT_URL.format(lcsc=lcsc)
    try:
        resp = await client.get(url)
    except httpx.HTTPError as e:
        raise PartLibraryError(f"EasyEDA fetch failed for {lcsc}: {e}") from e
    if resp.status_code == 404:
        raise PartLibraryError(f"EasyEDA has no component data for {lcsc}")
    if resp.status_code != 200:
        raise PartLibraryError(f"EasyEDA returned HTTP {resp.status_code} for {lcsc}")
    try:
        data = resp.json()
    except ValueError as e:
        raise PartLibraryError(f"EasyEDA returned non-JSON for {lcsc}: {e}") from e
    if not isinstance(data, dict) or data.get("success") is False:
        raise PartLibraryError(
            f"EasyEDA component fetch unsuccessful for {lcsc}: {data.get('result') if isinstance(data, dict) else 'malformed'}"
        )
    return data.get("result", data)


# ---------------------------------------------------------------------------
# Symbol generation
# ---------------------------------------------------------------------------


def _safe_id(name: str) -> str:
    """Sanitize a name for use in KiCad library/symbol identifiers."""
    return "".join(ch if ch.isalnum() or ch in "_-" else "_" for ch in name)


def _build_kicad_symbol(lcsc: str, mfr_part: str, description: str, pin_count: int) -> str:
    """Generate a placeholder KiCad 8 symbol library file.

    A correct KiCad symbol requires the full pin map (positions, names,
    numbers, electrical type) which EasyEDA stores in a proprietary
    schematic format that's outside Phase 1's scope. Instead we emit a
    *generic rectangular* symbol with `pin_count` numbered pins so the
    user can use it in a schematic and KiCad will accept it for ERC.

    The symbol library file (`.kicad_sym`) holds one symbol named after
    the LCSC C-number; we set the LCSC field on the symbol so a future
    BOM export picks it up.
    """
    symbol_name = _safe_id(lcsc)
    safe_desc = description.replace('"', "'")
    safe_mfr = mfr_part.replace('"', "'")
    half = max(1, pin_count) * 50  # 50 mil per pin spacing
    pins = []
    for i in range(1, max(1, pin_count) + 1):
        # Alternate pins between left side (odd) and right side (even).
        if i % 2 == 1:
            x = -300
            y = half - ((i // 2) * 100)
            angle = 0
        else:
            x = 300
            y = half - (((i - 1) // 2) * 100)
            angle = 180
        pins.append(
            f"    (pin passive line (at {x} {y} {angle}) (length 100)\n"
            f'      (name "P{i}" (effects (font (size 1.27 1.27))))\n'
            f'      (number "{i}" (effects (font (size 1.27 1.27)))))\n'
        )
    pins_block = "".join(pins)

    return (
        '(kicad_symbol_lib (version 20231120) (generator "kicad_jlcpcb_mcp")\n'
        f'  (symbol "{symbol_name}"\n'
        "    (in_bom yes) (on_board yes)\n"
        f'    (property "Reference" "U" (at 0 {half + 100} 0) (effects (font (size 1.27 1.27))))\n'
        f'    (property "Value" "{symbol_name}" (at 0 {half + 50} 0) (effects (font (size 1.27 1.27))))\n'
        f'    (property "Footprint" "" (at 0 0 0) (effects (font (size 1.27 1.27)) hide))\n'
        f'    (property "Datasheet" "" (at 0 0 0) (effects (font (size 1.27 1.27)) hide))\n'
        f'    (property "Description" "{safe_desc}" (at 0 0 0) (effects (font (size 1.27 1.27)) hide))\n'
        f'    (property "LCSC" "{lcsc}" (at 0 0 0) (effects (font (size 1.27 1.27)) hide))\n'
        f'    (property "MPN" "{safe_mfr}" (at 0 0 0) (effects (font (size 1.27 1.27)) hide))\n'
        f'    (symbol "{symbol_name}_0_1"\n'
        f"      (rectangle (start -300 {half}) (end 300 -{half})\n"
        "        (stroke (width 0.254) (type default))\n"
        "        (fill (type background)))\n"
        "    )\n"
        f'    (symbol "{symbol_name}_1_1"\n'
        f"{pins_block}"
        "    )\n"
        "  )\n"
        ")\n"
    )


# ---------------------------------------------------------------------------
# Footprint generation
# ---------------------------------------------------------------------------


def _build_kicad_footprint(lcsc: str, mfr_part: str, package: str, pin_count: int) -> str:
    """Generate a placeholder KiCad 8 footprint (.kicad_mod).

    Like the symbol, this is a *generic* footprint sized for the package
    family rather than a per-package-correct one. For Phase 1 the user
    is expected to either:
      (a) accept the generic footprint and verify it in KiCad before
          routing, or
      (b) drop in a hand-picked footprint from KiCad's standard libraries
          and use this only for the symbol/LCSC linkage.

    Footprint dimensions follow IPC nominals for common SMD packages.
    """
    name = _safe_id(f"{lcsc}_{package}")
    pads_block = _generate_pads_for_package(package, pin_count)
    return (
        f'(footprint "{name}"\n'
        "  (version 20231120)\n"
        '  (generator "kicad_jlcpcb_mcp")\n'
        '  (layer "F.Cu")\n'
        "  (attr smd)\n"
        f'  (property "Reference" "REF**" (at 0 -3 0) (layer "F.SilkS"))\n'
        f'  (property "Value" "{name}" (at 0 3 0) (layer "F.Fab"))\n'
        f"{pads_block}"
        ")\n"
    )


def _generate_pads_for_package(package: str, pin_count: int) -> str:
    """Generate IPC-nominal SMD pads for common package families.

    Falls back to a generic SMD pad grid for unknown packages.
    """
    pkg = package.upper().strip()

    # 2-pin chip resistors/caps: 0402, 0603, 0805, 1206
    chip_dimensions = {
        "0402": (0.6, 0.6, 0.95),
        "0603": (0.85, 0.85, 1.55),
        "0805": (1.1, 1.4, 1.85),
        "1206": (1.2, 1.8, 2.7),
    }
    if pkg in chip_dimensions:
        pad_w, pad_h, pitch = chip_dimensions[pkg]
        return (
            f'  (pad "1" smd roundrect (at -{pitch / 2} 0) (size {pad_w} {pad_h}) (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25))\n'
            f'  (pad "2" smd roundrect (at {pitch / 2} 0) (size {pad_w} {pad_h}) (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25))\n'
        )

    # SOT-23 family
    if pkg.startswith("SOT-23"):
        n = pin_count or 3
        pads = []
        for i in range(n):
            # Three pins on the wide side, then 2 pins opposite for SOT-23-5
            if i < (n + 1) // 2:
                x = -0.95 + (i * 0.95)
                y = 1.1
            else:
                idx = i - (n + 1) // 2
                x = 0.95 - (idx * 0.95)
                y = -1.1
            pads.append(
                f'  (pad "{i + 1}" smd roundrect (at {x:.3f} {y:.3f}) (size 0.6 0.9) '
                f'(layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25))\n'
            )
        return "".join(pads)

    # Generic SMD grid: rows of `pin_count // 2` pins on each side at 0.5mm pitch
    n = max(2, pin_count or 2)
    pitch = 0.5
    side = max(1, n // 2)
    pads = []
    for i in range(n):
        if i < side:
            x = -1.5
            y = -((side - 1) * pitch / 2) + (i * pitch)
        else:
            x = 1.5
            j = i - side
            y = ((side - 1) * pitch / 2) - (j * pitch)
        pads.append(
            f'  (pad "{i + 1}" smd roundrect (at {x:.3f} {y:.3f}) (size 0.3 0.6) '
            f'(layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25))\n'
        )
    return "".join(pads)


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
        client = httpx.AsyncClient(
            timeout=httpx.Timeout(15.0),
            headers={"User-Agent": "kicad-jlcpcb/0.1"},
        )

    warnings: list[str] = []
    pin_count = 2  # default for 2-terminal passives
    has_3d_model = False
    model_url = ""
    try:
        try:
            ee = await _fetch_easyeda_component(lcsc, client)
            # EasyEDA's pin info lives under symbol.shape with type 'P';
            # we just count entries to get the pin count and detect 3D.
            shapes = ee.get("dataStr", {}).get("shape", [])
            if isinstance(shapes, list) and shapes:
                pin_count = sum(1 for s in shapes if isinstance(s, str) and s.startswith("P~"))
                if pin_count == 0:
                    pin_count = 2
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
                f"Using a generic 2-pin symbol/footprint placeholder."
            )

        symbol_text = _build_kicad_symbol(
            lcsc=part.lcsc,
            mfr_part=part.mfr_part,
            description=part.description,
            pin_count=pin_count,
        )
        footprint_text = _build_kicad_footprint(
            lcsc=part.lcsc,
            mfr_part=part.mfr_part,
            package=part.package,
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
