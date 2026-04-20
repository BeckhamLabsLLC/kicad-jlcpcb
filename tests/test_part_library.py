"""Tests for part library fetcher.

Mocks both the LCSC catalog client and the EasyEDA component endpoint
so tests run offline. Validates that generated KiCad files are
syntactically structured (start with the right s-expression head).
"""

from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from kicad_jlcpcb_mcp import config, part_library
from kicad_jlcpcb_mcp.lcsc_client import Part
from kicad_jlcpcb_mcp.part_library import (
    FetchResult,
    PartLibraryError,
    _build_kicad_footprint,
    _build_kicad_symbol,
    _generate_pads_for_package,
    _pinmap_to_cache,
    _safe_id,
    fetch_part_library,
    get_pin_map,
    get_pin_maps,
    parse_pinmap_from_shape,
)


@pytest.fixture(autouse=True)
def isolated_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "CACHE_DIR", tmp_path / "cache")


SAMPLE_PART = Part(
    lcsc="C25804",
    mfr_part="RC0603FR-0710KL",
    manufacturer="YAGEO",
    description="10 kOhms 0603",
    package="0603",
    basic=True,
    stock=100000,
    price_usd=0.0012,
)


def make_response(status_code=200, json_data=None, content=b"") -> MagicMock:
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status_code
    if json_data is not None:
        resp.json = MagicMock(return_value=json_data)
    else:
        resp.json = MagicMock(side_effect=ValueError)
    resp.content = content
    return resp


class TestSafeId:
    def test_alnum_passthrough(self):
        assert _safe_id("C25804") == "C25804"

    def test_underscores_special(self):
        assert _safe_id("foo bar.baz") == "foo_bar_baz"

    def test_hyphens_kept(self):
        assert _safe_id("ESP32-S3") == "ESP32-S3"


class TestBuildSymbol:
    def test_returns_valid_sexpr_head(self):
        text = _build_kicad_symbol("C25804", "RC0603", "test resistor", 2)
        assert text.startswith("(kicad_symbol_lib")
        assert "(version 20231120)" in text

    def test_embeds_lcsc_field(self):
        text = _build_kicad_symbol("C25804", "RC0603", "test", 2)
        assert '(property "LCSC" "C25804"' in text

    def test_pin_count_respected(self):
        text = _build_kicad_symbol("C123", "PART", "desc", 8)
        # Each pin generates a "(pin passive ..." entry
        assert text.count("(pin passive") == 8

    def test_escapes_quotes_in_description(self):
        text = _build_kicad_symbol("C1", "P", 'desc with "quotes"', 2)
        # No unescaped quotes inside the description property
        assert "\"desc with 'quotes'\"" in text


class TestBuildFootprint:
    def test_returns_valid_sexpr_head(self):
        text = _build_kicad_footprint("C25804", "RC0603", "0603", 2)
        assert text.startswith('(footprint "C25804_0603"')
        assert "(version 20231120)" in text

    def test_chip_package_emits_two_pads(self):
        text = _build_kicad_footprint("C25804", "RC0603", "0603", 2)
        assert text.count("(pad ") == 2


class TestGeneratePads:
    def test_known_chip_packages(self):
        for pkg in ("0402", "0603", "0805", "1206"):
            pads = _generate_pads_for_package(pkg, 2)
            assert pads.count("(pad ") == 2

    def test_sot23_3pin(self):
        pads = _generate_pads_for_package("SOT-23", 3)
        assert pads.count("(pad ") == 3

    def test_sot23_5pin(self):
        pads = _generate_pads_for_package("SOT-23-5", 5)
        assert pads.count("(pad ") == 5

    def test_unknown_package_falls_back(self):
        pads = _generate_pads_for_package("MYSTERY-PKG-99", 8)
        assert pads.count("(pad ") == 8


class TestFetchPartLibraryHappyPath:
    @pytest.mark.asyncio
    async def test_fetches_and_installs(self, tmp_path, monkeypatch):
        # Mock lcsc_client.get_part so we don't hit the network
        async def fake_get_part(lcsc, client=None):
            return SAMPLE_PART

        monkeypatch.setattr(part_library, "get_part", fake_get_part)

        # Mock the EasyEDA fetch with a 2-pin component
        easyeda_payload = {
            "success": True,
            "result": {
                "dataStr": {"shape": ["P~1~", "P~2~"]},
                "packageDetail": {"dataStr": {"shape": [], "head": {}}},
            },
        }
        client = MagicMock(spec=httpx.AsyncClient)
        client.get = AsyncMock(return_value=make_response(200, easyeda_payload))
        client.aclose = AsyncMock()

        result = await fetch_part_library("C25804", tmp_path, client=client)
        assert isinstance(result, FetchResult)
        assert result.lcsc == "C25804"
        assert result.symbol_path is not None
        assert result.symbol_path.exists()
        assert result.footprint_path is not None
        assert result.footprint_path.exists()
        # 2-pin part on a 0603 package — no 3D model
        assert result.model_3d_path is None
        assert result.warnings == []

        # Files actually contain valid sexpr heads
        assert result.symbol_path.read_text().startswith("(kicad_symbol_lib")
        assert result.footprint_path.read_text().startswith("(footprint")


class TestFetchPartLibraryEasyEdaUnreachable:
    @pytest.mark.asyncio
    async def test_falls_back_with_warning(self, tmp_path, monkeypatch):
        async def fake_get_part(lcsc, client=None):
            return SAMPLE_PART

        monkeypatch.setattr(part_library, "get_part", fake_get_part)

        # EasyEDA returns 404
        client = MagicMock(spec=httpx.AsyncClient)
        client.get = AsyncMock(return_value=make_response(404))
        client.aclose = AsyncMock()

        result = await fetch_part_library("C25804", tmp_path, client=client)
        assert result.symbol_path is not None
        assert result.symbol_path.exists()
        assert len(result.warnings) == 1
        assert "EasyEDA" in result.warnings[0]


class TestFetchPartLibraryHttpError:
    @pytest.mark.asyncio
    async def test_falls_back_on_connection_error(self, tmp_path, monkeypatch):
        async def fake_get_part(lcsc, client=None):
            return SAMPLE_PART

        monkeypatch.setattr(part_library, "get_part", fake_get_part)

        client = MagicMock(spec=httpx.AsyncClient)
        client.get = AsyncMock(side_effect=httpx.ConnectError("boom"))
        client.aclose = AsyncMock()

        result = await fetch_part_library("C25804", tmp_path, client=client)
        # Symbol still produced as a fallback placeholder
        assert result.symbol_path is not None
        assert any("EasyEDA" in w for w in result.warnings)


class TestFetchPartLibraryUnknownPart:
    @pytest.mark.asyncio
    async def test_propagates_lcsc_error(self, tmp_path, monkeypatch):
        from kicad_jlcpcb_mcp.lcsc_client import LcscError

        async def fake_get_part(lcsc, client=None):
            raise LcscError("not found")

        monkeypatch.setattr(part_library, "get_part", fake_get_part)

        client = MagicMock(spec=httpx.AsyncClient)
        client.get = AsyncMock()
        client.aclose = AsyncMock()

        with pytest.raises(LcscError):
            await fetch_part_library("C99999", tmp_path, client=client)


# ---------------------------------------------------------------------------
# Pin map parsing + get_pin_map
# ---------------------------------------------------------------------------


# Real ME6211 LDO shape data from EasyEDA (5 pins, well-known)
ME6211_SHAPE = [
    "P~show~0~1~-40~10~180~gge5~0^^-40~10^^M -40 10 h 10~#880000^^1~-26.3~14~0~VIN~start~~~#0000FF^^1~-30.5~9~0~1~end~~~#0000FF^^0~-33~10^^0~M -30 13 L -27 10 L -30 7",
    "P~show~0~2~-40~20~180~gge13~0^^-40~20^^M -40 20 h 10~#000000^^1~-26.3~24~0~VSS~start~~~#000000^^1~-30.5~19~0~2~end~~~#000000^^0~-33~20^^0~M -30 23 L -27 20 L -30 17",
    "P~show~0~3~-40~30~180~gge21~0^^-40~30^^M -40 30 h 10~#880000^^1~-26.3~34~0~CE~start~~~#0000FF^^1~-30.5~29~0~3~end~~~#0000FF^^0~-33~30^^0~M -30 33 L -27 30 L -30 27",
    "P~show~0~4~60~30~0~gge28~0^^60~30^^M 60 30 h -10~#880000^^1~46.3~34~0~NC~end~~~#0000FF^^1~50.5~29~0~4~start~~~#0000FF^^0~53~30^^0~M 50 27 L 47 30 L 50 33",
    "P~show~0~5~60~10~0~gge35~0^^60~10^^M 60 10 h -10~#880000^^1~46.3~14~0~VOUT~end~~~#0000FF^^1~50.5~9~0~5~start~~~#0000FF^^0~53~10^^0~M 50 7 L 47 10 L 50 13",
    # Non-pin entries that should be ignored
    "R~id~x~y~~~~~rectangle",
    "L~id~start~end~~~line",
]


class TestParsePinmapFromShape:
    def test_me6211_five_pins(self):
        pm = parse_pinmap_from_shape(ME6211_SHAPE)
        assert pm == {"VIN": "1", "VSS": "2", "CE": "3", "NC": "4", "VOUT": "5"}

    def test_empty_shape(self):
        assert parse_pinmap_from_shape([]) == {}

    def test_non_list_input(self):
        assert parse_pinmap_from_shape(None) == {}

    def test_no_pin_entries(self):
        assert parse_pinmap_from_shape(["R~x~y", "L~a~b"]) == {}

    def test_malformed_pin_entry_skipped(self):
        # Pin entry with fewer than 4 segments
        pm = parse_pinmap_from_shape(["P~show~0~1"])
        assert pm == {}

    def test_unnamed_pin_skipped(self):
        # Pin 2 has empty name → should be skipped
        shape = [ME6211_SHAPE[0]]  # VIN on pin 1 (valid)
        shape.append(
            "P~show~0~2~0~0~0~id~0^^0~0^^M 0 0 h 10~#000^^1~0~0~0~~start~~~#000^^1~0~0~0~2~end~~~#000"
        )
        pm = parse_pinmap_from_shape(shape)
        assert pm == {"VIN": "1"}  # pin 2 with empty name dropped


class TestGetPinMap:
    @pytest.fixture(autouse=True)
    def reset_rate_limit(self):
        """Reset the module-level rate-limit timestamp between tests."""
        part_library._limiter.last_request_time = 0.0

    @pytest.mark.asyncio
    async def test_invalid_lcsc_format(self):
        with pytest.raises(PartLibraryError, match="Invalid LCSC"):
            await get_pin_map("not-a-c-number")

    @pytest.mark.asyncio
    async def test_served_from_cache(self, tmp_path, monkeypatch):
        monkeypatch.setattr(config, "CACHE_DIR", tmp_path)
        _pinmap_to_cache(
            "C82942",
            {
                "lcsc": "C82942",
                "title": "ME6211",
                "pin_count": 5,
                "pinmap": {"VIN": "1", "VSS": "2", "CE": "3", "NC": "4", "VOUT": "5"},
                "pad_to_name": {"1": "VIN", "2": "VSS", "3": "CE", "4": "NC", "5": "VOUT"},
            },
        )
        # Pass a client that would fail if called
        client = MagicMock(spec=httpx.AsyncClient)
        client.get = AsyncMock(side_effect=AssertionError("cache should prevent this"))
        client.aclose = AsyncMock()

        result = await get_pin_map("C82942", client=client)
        assert result["source"] == "cache"
        assert result["pinmap"]["VIN"] == "1"
        assert client.get.call_count == 0

    @pytest.mark.asyncio
    async def test_fetches_from_easyeda_on_miss(self, tmp_path, monkeypatch):
        monkeypatch.setattr(config, "CACHE_DIR", tmp_path)
        # Bypass the 12s rate limit for tests
        monkeypatch.setattr(part_library, "EASYEDA_MIN_DELAY_SECONDS", 0.0)

        fake_response = MagicMock(spec=httpx.Response)
        fake_response.status_code = 200
        fake_response.json = MagicMock(
            return_value={
                "success": True,
                "result": {
                    "title": "ME6211C33M5G-N",
                    "dataStr": {"shape": ME6211_SHAPE},
                },
            }
        )

        client = MagicMock(spec=httpx.AsyncClient)
        client.get = AsyncMock(return_value=fake_response)
        client.aclose = AsyncMock()

        result = await get_pin_map("C82942", client=client)
        assert result["source"] == "easyeda"
        assert result["pin_count"] == 5
        assert result["pinmap"]["VOUT"] == "5"
        # Second call should serve from cache
        result2 = await get_pin_map("C82942", client=client)
        assert result2["source"] == "cache"
        assert client.get.call_count == 1

    @pytest.mark.asyncio
    async def test_retries_on_403(self, tmp_path, monkeypatch):
        monkeypatch.setattr(config, "CACHE_DIR", tmp_path)
        monkeypatch.setattr(part_library, "EASYEDA_MIN_DELAY_SECONDS", 0.0)

        # Skip the 60s backoff in the retry path
        async def fake_sleep(seconds):
            return

        monkeypatch.setattr(part_library.asyncio, "sleep", fake_sleep)

        forbidden = MagicMock(spec=httpx.Response)
        forbidden.status_code = 403
        ok = MagicMock(spec=httpx.Response)
        ok.status_code = 200
        ok.json = MagicMock(
            return_value={
                "success": True,
                "result": {"title": "X", "dataStr": {"shape": ME6211_SHAPE}},
            }
        )

        client = MagicMock(spec=httpx.AsyncClient)
        client.get = AsyncMock(side_effect=[forbidden, ok])
        client.aclose = AsyncMock()

        result = await get_pin_map("C82942", client=client)
        assert result["pin_count"] == 5
        assert client.get.call_count == 2  # first 403, second 200

    @pytest.mark.asyncio
    async def test_raises_on_404(self, tmp_path, monkeypatch):
        monkeypatch.setattr(config, "CACHE_DIR", tmp_path)
        monkeypatch.setattr(part_library, "EASYEDA_MIN_DELAY_SECONDS", 0.0)
        resp = MagicMock(spec=httpx.Response)
        resp.status_code = 404
        client = MagicMock(spec=httpx.AsyncClient)
        client.get = AsyncMock(return_value=resp)
        client.aclose = AsyncMock()
        with pytest.raises(PartLibraryError, match="no component data"):
            await get_pin_map("C99999", client=client)


class TestGetPinMaps:
    @pytest.fixture(autouse=True)
    def reset_rate_limit(self):
        part_library._limiter.last_request_time = 0.0

    @pytest.mark.asyncio
    async def test_bulk_fetch_mixed_results(self, tmp_path, monkeypatch):
        monkeypatch.setattr(config, "CACHE_DIR", tmp_path)
        monkeypatch.setattr(part_library, "EASYEDA_MIN_DELAY_SECONDS", 0.0)

        ok = MagicMock(spec=httpx.Response)
        ok.status_code = 200
        ok.json = MagicMock(
            return_value={
                "success": True,
                "result": {"title": "X", "dataStr": {"shape": ME6211_SHAPE}},
            }
        )
        not_found = MagicMock(spec=httpx.Response)
        not_found.status_code = 404

        client = MagicMock(spec=httpx.AsyncClient)
        responses = {"C82942": ok, "C99999": not_found}

        async def fake_get(url, **kwargs):
            for lcsc, r in responses.items():
                if lcsc in url:
                    return r
            return not_found

        client.get = AsyncMock(side_effect=fake_get)
        client.aclose = AsyncMock()

        results = await get_pin_maps(["C82942", "C99999"], client=client)
        assert "C82942" in results and "C99999" in results
        assert results["C82942"]["pinmap"]["VIN"] == "1"
        assert "error" in results["C99999"]
