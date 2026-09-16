"""Live contract tests against the upstream part-data services.

**Why this file exists.** Every other test in this suite is offline and
mocked. That is right for CI speed, but it means the whole suite stayed
green for four months while the plugin was completely broken in the field:
upstream retired the data layout the client depended on, the URL began
returning 404, and nothing here noticed (issue #1).

These tests assert the *shape* of live responses — never a specific part,
price, or stock figure, which would make them flap. They are skipped
unless ``KJLC_NETWORK_TESTS=1``, matching the ``KICAD_INSTALLED=1`` pattern
used in test_kicad_cli.py, and run on a weekly schedule in CI
(.github/workflows/contract.yml).

**If these fail, the plugin is broken for every user.** Check whether the
upstream response shape changed before assuming the service is merely down.

Run locally with:

    KJLC_NETWORK_TESTS=1 pytest tests/test_network_contract.py -v
"""

from __future__ import annotations

import os

import httpx
import pytest

from kicad_jlcpcb_mcp import config, lcsc_client, part_library

pytestmark = pytest.mark.skipif(
    os.environ.get("KJLC_NETWORK_TESTS") != "1",
    reason="network contract tests require KJLC_NETWORK_TESTS=1",
)

# A part that has existed for over a decade and is used on a large share of
# all JLCPCB assembly orders: a 10kΩ 0603 1% thick-film chip resistor.
CANARY_LCSC = "C25804"
CANARY_MFR = "0603WAF1002T5E"


@pytest.fixture(autouse=True)
def isolated_cache(tmp_path, monkeypatch):
    """Never let a contract run read or write the developer's real cache."""
    monkeypatch.setattr(config, "CACHE_DIR", tmp_path)


@pytest.fixture
async def client():
    async with httpx.AsyncClient(
        base_url=config.JLCSEARCH_BASE, timeout=30.0, follow_redirects=True
    ) as c:
        yield c


class TestJlcsearchFreeTextRoute:
    async def test_components_list_returns_rows_with_the_expected_keys(self, client):
        rows = await lcsc_client._jlcsearch_components(client, search="resistor", limit=5)
        assert rows, "jlcsearch returned no rows for 'resistor'"
        for row in rows:
            assert lcsc_client._normalize_lcsc(row["lcsc"]).startswith("C")
            for key in ("mfr", "package", "stock", "is_basic"):
                assert key in row, f"missing {key!r}; upstream shape changed"

    async def test_price_field_still_parses(self, client):
        rows = await lcsc_client._jlcsearch_components(client, search="resistor", limit=20)
        priced = [r for r in rows if r.get("price")]
        assert priced, "no row carried a price"
        assert any(lcsc_client._parse_price_breaks(r["price"]) > 0 for r in priced)

    async def test_package_filter_is_still_an_exact_match(self, client):
        rows = await lcsc_client._jlcsearch_components(
            client, search="resistor", package="0603", limit=20
        )
        assert rows
        assert {r["package"] for r in rows} == {"0603"}

    async def test_and_matching_still_holds(self, client):
        """The narrowing logic exists because upstream ANDs every token.

        If upstream switched to OR matching, narrowing would become dead
        weight and ranking would need revisiting.
        """
        nonsense = await lcsc_client._jlcsearch_components(
            client, search="resistor zzzzqqqx", limit=5
        )
        assert nonsense == []


class TestJlcsearchStructuredRoutes:
    async def test_resistor_route_filters_on_ohms(self, client):
        rows = await lcsc_client._jlcsearch_passive(
            client, "resistors", 10_000.0, package="0603", basic_only=True
        )
        assert rows, "no basic 10k 0603 resistor found"
        assert any(lcsc_client._normalize_lcsc(r["lcsc"]) == CANARY_LCSC for r in rows)
        for row in rows:
            assert row["resistance"] == pytest.approx(10_000)
            assert row["is_basic"] is True

    async def test_capacitor_route_filters_on_farads(self, client):
        rows = await lcsc_client._jlcsearch_passive(
            client, "capacitors", 1e-7, package="0402", basic_only=True
        )
        assert rows, "no basic 100nF 0402 capacitor found"
        assert all(r["package"] == "0402" for r in rows)

    async def test_structured_rows_carry_the_attributes_blob(self, client):
        """Descriptions are empty on these rows; attributes is what we
        rebuild them from."""
        rows = await lcsc_client._jlcsearch_passive(
            client, "resistors", 10_000.0, package="0603", basic_only=True
        )
        part = lcsc_client._row_to_part_passive(rows[0], "resistors")
        assert part.description.strip(), "could not synthesise a description"
        assert "0603" in part.description


class TestEasyEdaRoute:
    async def test_component_endpoint_returns_the_jlcpcb_part_class(self):
        """`JLCPCB Part Class` is the only source for the basic/extended
        split on an exact C-number lookup. Without it every part would be
        priced as extended."""
        part_library._limiter.last_request_time = 0.0
        async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as c:
            result = await part_library._fetch_easyeda_raw(CANARY_LCSC, c)
        c_para = result["dataStr"]["head"]["c_para"]
        assert c_para["JLCPCB Part Class"]
        assert c_para["Manufacturer Part"] == CANARY_MFR


class TestEndToEnd:
    async def test_search_finds_a_basic_ldo(self):
        results = await lcsc_client.search("AMS1117-3.3", basic_only=True, limit=5)
        assert results, "no basic AMS1117-3.3 found"
        assert any(p.basic for p in results)

    async def test_passive_search_returns_the_part_actually_asked_for(self):
        """The regression that matters most.

        Free-text search cannot find this part — its upstream description
        is empty — and returns a 510kΩ instead. Silently shipping the wrong
        resistor is worse than an error, so this asserts the exact C-number.
        """
        results = await lcsc_client.search("10k 0603", basic_only=True, limit=5)
        assert results
        assert results[0].lcsc == CANARY_LCSC
        assert results[0].stock > 0

    async def test_decoupling_cap_resolves_across_unit_spellings(self):
        for query in ("0.1uF 0402", "100nF 0402"):
            results = await lcsc_client.search(query, basic_only=True, limit=3)
            assert results, f"{query} found nothing"
            assert results[0].package == "0402"

    async def test_get_part_resolves_an_exact_c_number(self):
        part = await lcsc_client.get_part(CANARY_LCSC)
        assert part.lcsc == CANARY_LCSC
        assert part.mfr_part == CANARY_MFR
        assert part.basic is True

    async def test_the_query_from_issue_1_returns_results(self):
        """github.com/BeckhamLabsLLC/kicad-jlcpcb/issues/1"""
        results = await lcsc_client.search(
            "TVS diode 30V 3000W SMB", basic_only=False, stock_min=100, limit=10
        )
        assert results, "issue #1's query is broken again"
        assert any("SMB" in p.package.upper() for p in results)


class TestEasyEdaGeometryContract:
    """The symbol and footprint geometry the plugin now depends on.

    If EasyEDA reshapes `dataStr.shape` or `packageDetail`, symbols lose
    their pin names and footprints lose their pads — and the fallback is a
    placeholder that silently does not match the real part. These assert
    the shape of the live payload, never specific dimensions.
    """

    CANARY_SOT23_5 = "C82942"  # ME6211C33M5G-N, a 5-pin SOT-23-5 LDO

    async def _fetch(self, lcsc):
        part_library._limiter.last_request_time = 0.0
        async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as c:
            return await part_library._fetch_easyeda_raw(lcsc, c)

    async def test_symbol_pins_still_parse_with_names(self):
        result = await self._fetch(self.CANARY_SOT23_5)
        pins = part_library.parse_symbol_pins(result["dataStr"]["shape"])
        assert len(pins) == 5, f"expected 5 pins, got {len(pins)}"
        names = {p.name for p in pins}
        assert "VOUT" in names and "VIN" in names, names
        assert {p.number for p in pins} == {"1", "2", "3", "4", "5"}

    async def test_footprint_pads_still_parse_with_real_dimensions(self):
        result = await self._fetch(self.CANARY_SOT23_5)
        pads = part_library.parse_footprint_pads(result["packageDetail"]["dataStr"]["shape"])
        assert len(pads) == 5, f"expected 5 pads, got {len(pads)}"
        # SOT-23-5 pitch is 0.95 mm. If the unit scale changes upstream,
        # every footprint silently comes out the wrong size.
        xs = sorted({round(p.x_mm, 3) for p in pads})
        assert abs((xs[1] - xs[0]) - 0.95) < 0.05, f"pitch looks wrong: {xs}"
        assert all(p.is_smd for p in pads)

    async def test_emitted_footprint_is_loadable_by_kicad(self, tmp_path):
        """End to end: live EasyEDA data through our emitter into pcbnew."""
        pytest.importorskip("pcbnew")
        import pcbnew

        result = await self._fetch(self.CANARY_SOT23_5)
        pads = part_library.parse_footprint_pads(result["packageDetail"]["dataStr"]["shape"])
        text = part_library.build_kicad_footprint("C82942", "ME6211", "SOT-23-5", pads)
        pretty = tmp_path / "t.pretty"
        pretty.mkdir()
        # KiCad requires the filename to match the footprint name.
        (pretty / "C82942_SOT-23-5.kicad_mod").write_text(text)
        fp = pcbnew.FootprintLoad(str(pretty), "C82942_SOT-23-5")
        assert fp is not None, "KiCad could not load the generated footprint"
        assert len(list(fp.Pads())) == 5
