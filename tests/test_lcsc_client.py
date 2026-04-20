"""Tests for the jlcparts-backed LCSC client.

All HTTP is mocked with a fake httpx.AsyncClient that responds to:
  - /data/index.json
  - /data/<sourcename>.json.gz    (gzipped category dump)
  - /data/<sourcename>.stock.json (stock map)

SQLite is redirected to tmp_path via monkeypatch on config.CACHE_DIR.
"""

import gzip
import json
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from kicad_jlcpcb_mcp import config
from kicad_jlcpcb_mcp.lcsc_client import (
    LcscError,
    _extract_attr,
    _extract_basic_flag,
    _extract_price,
    _row_dict_to_part,
    get_part,
    populate_cache,
    resolve_bom,
    search,
)

# ---------------------------------------------------------------------------
# Fake jlcparts mirror
# ---------------------------------------------------------------------------

# A tiny two-category catalog: one resistor (basic) and one exotic IC (extended).
SAMPLE_INDEX = {
    "created": "2026-04-08T00:00:00Z",
    "categories": {
        "Resistors": {
            "Chip Resistor": {
                "sourcename": "Resistors_Chip",
                "datahash": "a",
                "stockhash": "b",
            },
        },
        "ICs": {
            "Exotic": {
                "sourcename": "ICs_Exotic",
                "datahash": "c",
                "stockhash": "d",
            },
        },
    },
}

SAMPLE_SCHEMA = [
    "lcsc",
    "mfr",
    "joints",
    "description",
    "datasheet",
    "price",
    "img",
    "url",
    "attributes",
]

RESISTOR_ROW = [
    "C25804",
    "RC0603FR-0710KL",
    2,
    "10 kOhms +/-1% 0.1W 0603 Chip Resistor",
    "https://example.com/ds.pdf",
    [{"price": 0.0012, "qFrom": 1, "qTo": 99}],
    None,
    None,
    {
        "Basic/Extended": {
            "format": "${default}",
            "primary": "default",
            "values": {"default": ["Basic", "string"]},
        },
        "Manufacturer": {
            "format": "${default}",
            "primary": "default",
            "values": {"default": ["YAGEO", "string"]},
        },
        "Package": {
            "format": "${default}",
            "primary": "default",
            "values": {"default": ["0603", "string"]},
        },
    },
]

EXOTIC_ROW = [
    "C9000",
    "EXOTIC-CHIP",
    32,
    "Exotic 32-pin mystery chip",
    "",
    [{"price": 5.50, "qFrom": 1, "qTo": 99}],
    None,
    None,
    {
        "Basic/Extended": {
            "format": "${default}",
            "primary": "default",
            "values": {"default": ["Extended", "string"]},
        },
        "Manufacturer": {
            "format": "${default}",
            "primary": "default",
            "values": {"default": ["Acme Corp", "string"]},
        },
        "Package": {
            "format": "${default}",
            "primary": "default",
            "values": {"default": ["QFN-32", "string"]},
        },
    },
]


def _make_gzip_response(obj: dict) -> MagicMock:
    """Build a fake httpx.Response carrying a gzipped JSON payload."""
    body = gzip.compress(json.dumps(obj).encode("utf-8"))
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = 200
    resp.content = body
    resp.json = MagicMock(side_effect=ValueError("gzipped, not JSON"))
    return resp


def _make_json_response(obj, status_code: int = 200) -> MagicMock:
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status_code
    resp.content = json.dumps(obj).encode("utf-8")
    resp.json = MagicMock(return_value=obj)
    return resp


def _make_404() -> MagicMock:
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = 404
    resp.content = b""
    resp.json = MagicMock(side_effect=ValueError)
    return resp


def make_fake_client(
    *,
    index=SAMPLE_INDEX,
    categories=None,
    stock=None,
) -> MagicMock:
    """Build a fake httpx.AsyncClient that serves a full fake mirror.

    `categories` is a dict of sourcename -> category_obj ({schema, components}).
    `stock` is a dict of sourcename -> {lcsc: stock_int}.
    """
    if categories is None:
        categories = {
            "Resistors_Chip": {"schema": SAMPLE_SCHEMA, "components": [RESISTOR_ROW]},
            "ICs_Exotic": {"schema": SAMPLE_SCHEMA, "components": [EXOTIC_ROW]},
        }
    if stock is None:
        stock = {
            "Resistors_Chip": {"C25804": 100000},
            "ICs_Exotic": {"C9000": 50},
        }

    client = MagicMock(spec=httpx.AsyncClient)

    async def fake_get(path, params=None, **kwargs):
        if path == "/data/index.json":
            return _make_json_response(index)
        if path.endswith(".stock.json"):
            sourcename = path.split("/")[-1].removesuffix(".stock.json")
            return _make_json_response(stock.get(sourcename, {}))
        if path.endswith(".json.gz"):
            sourcename = path.split("/")[-1].removesuffix(".json.gz")
            if sourcename in categories:
                return _make_gzip_response(categories[sourcename])
            return _make_404()
        return _make_404()

    client.get = AsyncMock(side_effect=fake_get)
    client.aclose = AsyncMock()
    return client


# ---------------------------------------------------------------------------
# Autouse fixture: fresh cache dir per test
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def isolated_cache(tmp_path, monkeypatch):
    """Each test gets its own SQLite cache directory."""
    monkeypatch.setattr(config, "CACHE_DIR", tmp_path)
    return tmp_path


# ---------------------------------------------------------------------------
# Attribute-extraction helpers
# ---------------------------------------------------------------------------


class TestExtractAttr:
    def test_reads_primary_default(self):
        attrs = {
            "Package": {
                "primary": "default",
                "values": {"default": ["0603", "string"]},
            }
        }
        assert _extract_attr(attrs, "Package") == "0603"

    def test_missing_key_returns_empty(self):
        assert _extract_attr({}, "Package") == ""

    def test_non_dict_safe(self):
        assert _extract_attr("nope", "Package") == ""


class TestExtractBasicFlag:
    def test_basic(self):
        attrs = {
            "Basic/Extended": {"primary": "default", "values": {"default": ["Basic", "string"]}}
        }
        assert _extract_basic_flag(attrs) is True

    def test_extended(self):
        attrs = {
            "Basic/Extended": {"primary": "default", "values": {"default": ["Extended", "string"]}}
        }
        assert _extract_basic_flag(attrs) is False

    def test_missing(self):
        assert _extract_basic_flag({}) is False


class TestExtractPrice:
    def test_list_with_entries(self):
        assert _extract_price([{"price": 0.0012, "qFrom": 1}]) == 0.0012

    def test_empty(self):
        assert _extract_price([]) == 0.0

    def test_none(self):
        assert _extract_price(None) == 0.0


class TestRowDictToPart:
    def test_resistor(self):
        row_dict = dict(zip(SAMPLE_SCHEMA, RESISTOR_ROW))
        part = _row_dict_to_part(
            row_dict, stock=100000, category="Resistors", subcategory="Chip Resistor"
        )
        assert part.lcsc == "C25804"
        assert part.basic is True
        assert part.stock == 100000
        assert part.package == "0603"
        assert part.manufacturer == "YAGEO"
        assert part.price_usd == 0.0012
        assert part.cost_warning() is None

    def test_extended_part(self):
        row_dict = dict(zip(SAMPLE_SCHEMA, EXOTIC_ROW))
        part = _row_dict_to_part(row_dict, stock=50, category="ICs", subcategory="Exotic")
        assert part.basic is False
        assert part.tier == "extended"
        warning = part.cost_warning()
        assert warning is not None and "C9000" in warning


# ---------------------------------------------------------------------------
# populate_cache
# ---------------------------------------------------------------------------


class TestPopulateCache:
    @pytest.mark.asyncio
    async def test_full_populate(self):
        client = make_fake_client()
        result = await populate_cache(client=client, force=True)
        assert result["skipped"] is False
        assert result["categories_total"] == 2
        assert result["categories_failed"] == 0
        assert result["parts_inserted"] == 2

    @pytest.mark.asyncio
    async def test_skips_when_fresh(self):
        client = make_fake_client()
        await populate_cache(client=client, force=True)
        # Second call should skip (cache is fresh)
        result = await populate_cache(client=client, force=False)
        assert result["skipped"] is True

    @pytest.mark.asyncio
    async def test_force_refresh(self):
        client = make_fake_client()
        await populate_cache(client=client, force=True)
        result = await populate_cache(client=client, force=True)
        assert result["skipped"] is False

    @pytest.mark.asyncio
    async def test_index_fetch_error_raises(self):
        client = MagicMock(spec=httpx.AsyncClient)
        client.get = AsyncMock(side_effect=httpx.ConnectError("no internet"))
        client.aclose = AsyncMock()
        with pytest.raises(LcscError, match="index fetch failed"):
            await populate_cache(client=client, force=True)

    @pytest.mark.asyncio
    async def test_category_fetch_failure_tolerated(self):
        # Omit one category from the mock — populate should succeed
        # with one failed category counted.
        client = make_fake_client(
            categories={"Resistors_Chip": {"schema": SAMPLE_SCHEMA, "components": [RESISTOR_ROW]}},
            # ICs_Exotic is intentionally missing → will 404
        )
        result = await populate_cache(client=client, force=True)
        assert result["categories_failed"] == 1
        assert result["parts_inserted"] == 1


# ---------------------------------------------------------------------------
# get_part
# ---------------------------------------------------------------------------


class TestGetPart:
    @pytest.mark.asyncio
    async def test_found(self):
        client = make_fake_client()
        part = await get_part("C25804", client=client)
        assert part.lcsc == "C25804"
        assert part.basic is True

    @pytest.mark.asyncio
    async def test_invalid_format(self):
        with pytest.raises(LcscError, match="Invalid LCSC"):
            await get_part("not-a-c-number")

    @pytest.mark.asyncio
    async def test_not_in_catalog(self):
        client = make_fake_client()
        with pytest.raises(LcscError, match="not found"):
            await get_part("C99999", client=client)

    @pytest.mark.asyncio
    async def test_second_call_reuses_cache(self):
        client1 = make_fake_client()
        await get_part("C25804", client=client1)
        # Second call with a different client that would 404 everything —
        # it should never be invoked because the cache is fresh.
        client2 = MagicMock(spec=httpx.AsyncClient)
        client2.get = AsyncMock(return_value=_make_404())
        client2.aclose = AsyncMock()
        part = await get_part("C25804", client=client2)
        assert part.lcsc == "C25804"
        assert client2.get.call_count == 0


# ---------------------------------------------------------------------------
# search
# ---------------------------------------------------------------------------


class TestSearch:
    @pytest.mark.asyncio
    async def test_finds_basic(self):
        client = make_fake_client()
        results = await search("10k 0603", basic_only=True, client=client)
        assert len(results) == 1
        assert results[0].lcsc == "C25804"

    @pytest.mark.asyncio
    async def test_basic_only_excludes_extended(self):
        client = make_fake_client()
        results = await search("Exotic", basic_only=True, client=client)
        assert results == []

    @pytest.mark.asyncio
    async def test_includes_extended_when_allowed(self):
        client = make_fake_client()
        results = await search("Exotic", basic_only=False, client=client)
        assert len(results) == 1
        assert results[0].lcsc == "C9000"

    @pytest.mark.asyncio
    async def test_package_filter(self):
        client = make_fake_client()
        results = await search("Chip", package="0603", client=client)
        assert all(p.package == "0603" for p in results)

    @pytest.mark.asyncio
    async def test_stock_min(self):
        # Extended chip has stock=50; basic resistor has stock=100000
        client = make_fake_client()
        results = await search("chip", stock_min=100, basic_only=False, client=client)
        assert all(p.stock >= 100 for p in results)

    @pytest.mark.asyncio
    async def test_empty_query_returns_nothing(self):
        client = make_fake_client()
        results = await search("   ", client=client)
        assert results == []


# ---------------------------------------------------------------------------
# resolve_bom
# ---------------------------------------------------------------------------


class TestResolveBom:
    @pytest.mark.asyncio
    async def test_resolves_lcsc_rows(self):
        client = make_fake_client()
        result = await resolve_bom([{"lcsc": "C25804", "qty": 10}], client=client)
        assert len(result["resolved"]) == 1
        assert result["unresolved"] == []
        assert result["extended_part_count"] == 0
        assert result["estimated_setup_fee_usd"] == 0.0

    @pytest.mark.asyncio
    async def test_warns_on_extended_part(self):
        client = make_fake_client()
        result = await resolve_bom([{"lcsc": "C9000", "qty": 1}], client=client)
        assert result["extended_part_count"] == 1
        assert result["estimated_setup_fee_usd"] == config.EXTENDED_PART_SETUP_FEE_USD
        assert len(result["resolved"][0]["warnings"]) == 1

    @pytest.mark.asyncio
    async def test_unresolved_collected(self):
        client = make_fake_client()
        result = await resolve_bom([{"lcsc": "C99999", "qty": 1}], client=client)
        assert result["resolved"] == []
        assert len(result["unresolved"]) == 1
        assert "not found" in result["unresolved"][0]["reason"]

    @pytest.mark.asyncio
    async def test_query_row_falls_through_to_search(self):
        client = make_fake_client()
        result = await resolve_bom([{"query": "Exotic chip", "qty": 1}], client=client)
        # Exotic chip is extended-only, so basic search misses and the
        # fallback picks it up
        assert len(result["resolved"]) == 1
        assert result["resolved"][0]["part"]["lcsc"] == "C9000"
        assert result["extended_part_count"] == 1

    @pytest.mark.asyncio
    async def test_query_with_no_match(self):
        client = make_fake_client()
        result = await resolve_bom([{"query": "nothing at all", "qty": 1}], client=client)
        assert result["resolved"] == []
        assert len(result["unresolved"]) == 1

    @pytest.mark.asyncio
    async def test_duplicate_extended_parts_count_once(self):
        client = make_fake_client()
        result = await resolve_bom(
            [
                {"lcsc": "C9000", "qty": 1},
                {"lcsc": "C9000", "qty": 3},  # same part, different row
            ],
            client=client,
        )
        assert len(result["resolved"]) == 2
        # But the setup fee is per unique part, not per row
        assert result["extended_part_count"] == 1
        assert result["estimated_setup_fee_usd"] == config.EXTENDED_PART_SETUP_FEE_USD
