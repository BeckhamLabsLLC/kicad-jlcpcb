"""Tests for part library fetcher.

Mocks both the LCSC catalog client and the EasyEDA component endpoint
so tests run offline. Validates that generated KiCad files are
syntactically structured (start with the right s-expression head).
"""

import re
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from kicad_jlcpcb_mcp import config, part_library
from kicad_jlcpcb_mcp.lcsc_client import Part
from kicad_jlcpcb_mcp.part_library import (
    EasyEdaPad,
    EasyEdaPin,
    FetchResult,
    PartLibraryError,
    _pinmap_to_cache,
    _safe_id,
    build_kicad_footprint,
    build_kicad_symbol,
    fetch_part_library,
    get_pin_map,
    get_pin_maps,
    parse_footprint_pads,
    parse_pinmap_from_shape,
    parse_symbol_pins,
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


LDO_PINS = [
    EasyEdaPin(number="1", name="VIN", x_mm=-10.16, y_mm=2.54, rotation=180),
    EasyEdaPin(number="2", name="VSS", x_mm=-10.16, y_mm=5.08, rotation=180),
    EasyEdaPin(number="3", name="CE", x_mm=-10.16, y_mm=7.62, rotation=180),
    EasyEdaPin(number="4", name="NC", x_mm=15.24, y_mm=7.62, rotation=0),
    EasyEdaPin(number="5", name="VOUT", x_mm=15.24, y_mm=2.54, rotation=0),
]

# Real SOT-23-5 pads from EasyEDA, already in mm.
LDO_PADS = [
    EasyEdaPad("1", "RECT", 1014.85, 763.15, 0.49, 1.157, 1),
    EasyEdaPad("2", "RECT", 1016.00, 763.15, 0.49, 1.157, 1),
    EasyEdaPad("3", "RECT", 1017.15, 763.15, 0.49, 1.157, 1),
    EasyEdaPad("4", "RECT", 1017.15, 760.84, 0.49, 1.175, 1),
    EasyEdaPad("5", "RECT", 1014.85, 760.84, 0.49, 1.175, 1),
]


class TestParseSymbolPins:
    """EasyEDA ships real pin names and positions. Earlier releases emitted
    a rectangle with pins called P1..Pn because this data was assumed to be
    out of reach, which meant a schematic could not reference VOUT."""

    PIN = (
        "P~show~0~1~-40~10~180~gge5~0"
        "^^-40~10"
        "^^M -40 10 h 10~#880000"
        "^^1~-26.3~14~0~VIN~start~~~#0000FF"
        "^^1~-30.5~9~0~1~end~~~#0000FF"
    )

    def test_reads_name_number_and_position(self):
        (pin,) = parse_symbol_pins([self.PIN])
        assert pin.number == "1"
        assert pin.name == "VIN"
        assert pin.x_mm == pytest.approx(-40 * 0.254)
        assert pin.y_mm == pytest.approx(10 * 0.254)
        assert pin.rotation == 180

    def test_pad_number_comes_from_segment_4_not_segment_0(self):
        """Segment 0 field 3 is EasyEDA's internal pin *id*. It matches the
        pad number on simple parts and diverges on others, so reading it
        silently mis-wires exactly the parts where it matters."""
        entry = self.PIN.replace("P~show~0~1~", "P~show~0~99~").replace("~0~1~end~", "~0~7~end~")
        (pin,) = parse_symbol_pins([entry])
        assert pin.number == "7", "should use the pad number, not the pin id"

    def test_ignores_non_pin_shapes(self):
        assert parse_symbol_pins(["R~-30~-2~2~2~80~44~#880000", "E~-25~3~1.5"]) == []

    def test_tolerates_truncated_entries(self):
        assert parse_symbol_pins(["P~show~0~1~-40~10"]) == []

    def test_non_list_input(self):
        assert parse_symbol_pins(None) == []


class TestParseFootprintPads:
    PAD = (
        "PAD~RECT~3996.26~3004.542~1.9291~4.5551~1~~1~0~"
        "3995.2954 3002.2644 3997.2246 3002.2644~0~gge1002~0~~Y~0~0.0000~0.2000~39"
    )

    def test_reads_geometry(self):
        (pad,) = parse_footprint_pads([self.PAD])
        assert pad.number == "1"
        assert pad.shape == "RECT"
        assert pad.width_mm == pytest.approx(1.9291 * 0.254)
        assert pad.layer == 1
        assert pad.is_smd

    def test_hole_radius_becomes_a_diameter(self):
        """EasyEDA stores a radius; KiCad wants a diameter. Getting this
        wrong halves every drill on a through-hole part."""
        entry = self.PAD.replace("~1~0~3995", "~1~0.5~3995")
        (pad,) = parse_footprint_pads([entry])
        assert pad.hole_mm == pytest.approx(0.5 * 0.254 * 2)
        assert not pad.is_smd

    def test_real_sot23_5_pitch_is_recovered(self):
        pads = parse_footprint_pads(
            [
                "PAD~RECT~3996.26~3004.542~1.9~4.5~1~~1~0~x~0~g~0",
                "PAD~RECT~4000~3004.542~1.9~4.5~1~~2~0~x~0~g~0",
            ]
        )
        pitch = abs(pads[1].x_mm - pads[0].x_mm)
        assert pitch == pytest.approx(0.95, abs=0.01), "SOT-23-5 pitch is 0.95mm"

    def test_ignores_tracks_and_text(self):
        assert parse_footprint_pads(["TRACK~0.6~3~~3993 3003", "TEXT~N~4000"]) == []


class TestBuildSymbol:
    def test_uses_real_pin_names(self):
        text = build_kicad_symbol("C82942", "ME6211C33", "LDO", LDO_PINS)
        for name in ("VIN", "VSS", "CE", "NC", "VOUT"):
            assert f'(name "{name}"' in text
        assert '(name "P1"' not in text, "should not fall back to fabricated names"

    def test_every_pad_number_is_present(self):
        text = build_kicad_symbol("C82942", "ME6211C33", "LDO", LDO_PINS)
        for n in "12345":
            assert f'(number "{n}"' in text

    def test_pins_land_on_the_connection_grid(self):
        """Off-grid pins load and then silently refuse to connect."""
        text = build_kicad_symbol("C82942", "ME6211C33", "LDO", LDO_PINS)
        for x, y in re.findall(r"\(at (-?[\d.]+) (-?[\d.]+) \d+\)", text):
            for v in (float(x), float(y)):
                assert abs(round(v / 1.27) * 1.27 - v) < 1e-6

    def test_left_and_right_pins_get_opposite_angles(self):
        text = build_kicad_symbol("C82942", "ME6211C33", "LDO", LDO_PINS)
        angles = {int(a) for a in re.findall(r"\(at -?[\d.]+ -?[\d.]+ (\d+)\)", text)}
        assert angles == {0, 180}

    def test_falls_back_to_a_rectangle_with_no_pins(self):
        text = build_kicad_symbol("C1", "PART", "desc", [], pin_count=8)
        assert text.count("(pin passive line") == 8

    def test_escapes_quotes(self):
        text = build_kicad_symbol("C1", 'P"Q', "desc", LDO_PINS)
        assert '\\"' in text

    def test_carries_the_lcsc_number(self):
        assert '(property "LCSC" "C82942"' in build_kicad_symbol("C82942", "M", "d", LDO_PINS)


class TestBuildFootprint:
    def test_uses_real_pad_geometry(self):
        text = build_kicad_footprint("C82942", "ME6211C33", "SOT-23-5", LDO_PADS)
        assert text.count("(pad ") == 5
        assert "(size 0.4900 1.1570)" in text

    def test_pads_are_recentred_on_their_bounding_box(self):
        """EasyEDA coordinates are absolute on a canvas near (4000, 3000).
        Shipping those verbatim puts the footprint a metre off origin."""
        text = build_kicad_footprint("C82942", "M", "SOT-23-5", LDO_PADS)
        coords = [
            float(v) for pair in re.findall(r"\(at (-?[\d.]+) (-?[\d.]+)\)", text) for v in pair
        ]
        assert max(abs(c) for c in coords) < 10.0

    def test_through_hole_pads_get_a_drill(self):
        tht = [EasyEdaPad("1", "ELLIPSE", 0.0, 0.0, 1.5, 1.5, 11, hole_mm=0.8)]
        text = build_kicad_footprint("C1", "M", "DIP", tht)
        assert "thru_hole" in text
        assert "(drill 0.8000)" in text
        assert "(attr through_hole)" in text

    def test_bottom_layer_pads(self):
        bot = [EasyEdaPad("1", "RECT", 0.0, 0.0, 1.0, 1.0, 2)]
        assert '(layers "B.Cu" "B.Paste" "B.Mask")' in build_kicad_footprint("C1", "M", "X", bot)

    def test_falls_back_to_a_placeholder_with_no_pads(self):
        text = build_kicad_footprint("C1", "M", "X", [])
        assert text.count("(pad ") == 2


class TestFetchPartLibraryHappyPath:
    @pytest.mark.asyncio
    async def test_fetches_and_installs(self, tmp_path, monkeypatch):
        # Mock lcsc_client.get_part so we don't hit the network
        async def fake_get_part(lcsc, client=None):
            return SAMPLE_PART

        monkeypatch.setattr(part_library, "get_part", fake_get_part)

        # A 2-pin part with the geometry EasyEDA really returns: named pins
        # in the symbol, real pad polygons in packageDetail.
        easyeda_payload = {
            "success": True,
            "result": {
                "dataStr": {
                    "shape": [
                        "P~show~0~1~-40~10~180~gge5~0^^-40~10^^M -40 10 h 10~#000"
                        "^^1~-26~14~0~A~start~~~#00F^^1~-30~9~0~1~end~~~#00F",
                        "P~show~0~2~40~10~0~gge9~0^^40~10^^M 40 10 h -10~#000"
                        "^^1~26~14~0~B~end~~~#00F^^1~30~9~0~2~start~~~#00F",
                    ]
                },
                "packageDetail": {
                    "dataStr": {
                        "shape": [
                            "PAD~RECT~3996.26~3000~1.9291~4.5551~1~~1~0~x~0~gge1~0",
                            "PAD~RECT~4000~3000~1.9291~4.5551~1~~2~0~x~0~gge2~0",
                        ],
                        "head": {},
                    }
                },
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
        assert result.warnings == [], "real geometry should produce no warnings"

        # Files actually contain valid sexpr heads
        sym = result.symbol_path.read_text()
        fp = result.footprint_path.read_text()
        assert sym.startswith("(kicad_symbol_lib")
        assert fp.startswith("(footprint")

        # And they carry EasyEDA's real data, not a fabricated placeholder.
        assert '(name "A"' in sym and '(name "B"' in sym
        assert fp.count("(pad ") == 2
        assert "(size 0.4900" in fp


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
        # Three distinct things went wrong and the caller is told all three:
        # the fetch failed, so there are no pins, so there is no pad geometry.
        # The footprint warning is the one that matters — a placeholder
        # footprint will not match the real part.
        joined = " ".join(result.warnings)
        assert "EasyEDA" in joined
        assert "will NOT match the real part" in joined


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


class TestEasyEdaNetworkRetry:
    """Resolving a BOM makes one sequential request per part over several
    minutes. A single stalled connection used to fail that part outright and
    surface as "not found", which reads as a bad C-number and sends the
    caller looking in entirely the wrong place."""

    def _client(self, side_effect):
        client = MagicMock(spec=httpx.AsyncClient)
        client.get = AsyncMock(side_effect=side_effect)
        return client

    def _ok(self):
        resp = MagicMock(spec=httpx.Response)
        resp.status_code = 200
        resp.json = MagicMock(return_value={"success": True, "result": {"title": "X"}})
        return resp

    async def test_transient_network_error_is_retried(self):
        calls = {"n": 0}

        async def flaky(url, **kwargs):
            calls["n"] += 1
            if calls["n"] < 3:
                raise httpx.ReadTimeout("stall")
            return self._ok()

        result = await part_library._fetch_easyeda_raw("C25804", self._client(flaky))
        assert result["title"] == "X"
        assert calls["n"] == 3

    async def test_gives_up_after_the_attempt_budget(self):
        client = self._client(httpx.ConnectError("down"))
        with pytest.raises(part_library.PartLibraryError, match="after 3 attempts"):
            await part_library._fetch_easyeda_raw("C25804", client)
        assert client.get.await_count == part_library.EASYEDA_ATTEMPTS

    async def test_404_is_not_retried(self):
        """A genuinely missing part should fail fast, not burn the budget."""
        resp = MagicMock(spec=httpx.Response)
        resp.status_code = 404
        client = self._client(lambda url, **kw: resp)
        client.get = AsyncMock(return_value=resp)
        with pytest.raises(part_library.PartLibraryError, match="no component data"):
            await part_library._fetch_easyeda_raw("C99999999", client)
        assert client.get.await_count == 1
