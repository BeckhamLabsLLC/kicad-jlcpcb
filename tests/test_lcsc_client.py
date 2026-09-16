"""Tests for the LCSC part client.

All HTTP is mocked with a fake httpx.AsyncClient that responds to the two
live routes the client actually uses:

  - jlcsearch  /components/list   (free-text search)
  - jlcsearch  /resistors/list, /capacitors/list  (structured passives)

Per-C-number lookup goes through ``part_library._fetch_easyeda_raw``,
which is monkeypatched directly rather than faked at the HTTP layer.

SQLite is redirected to tmp_path via an autouse monkeypatch on
config.CACHE_DIR, so nothing here touches the developer's real cache.

The fixture shapes below are copied from live responses. If upstream
changes them, these tests keep passing while the plugin breaks — that is
what tests/test_network_contract.py exists to catch. Keep the two in sync.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from kicad_jlcpcb_mcp import config, lcsc_client, part_library

# ---------------------------------------------------------------------------
# Fixtures modelled on real jlcsearch responses
# ---------------------------------------------------------------------------

# /components/list row. `price` is a break-point string, `lcsc` a bare int.
RESISTOR_ROW = {
    "lcsc": 23192,
    "mfr": "0603WAF5103T5E",
    "package": "0603",
    "description": "100mW 510kΩ 75V Thick Film Resistor ±1% 0603 Chip Resistor",
    "stock": 419842,
    "price": "1-999:0.0027,1000-2999:0.0023,3000-:0.0021",
    "category": "Resistors",
    "subcategory": "Chip Resistor - Surface Mount",
    "is_basic": True,
    "is_preferred": False,
}

EXTENDED_ROW = {
    "lcsc": 113998,
    "mfr": "SMBJ30A",
    "package": "DO-214AA(SMB)",
    "description": "12.4A@10/1000us 1uA 30V 36.8V 600W Unidirectional TVS Diode",
    "stock": 89437,
    "price": "1-99:0.0742,100-499:0.0609",
    "category": "Circuit Protection",
    "subcategory": "ESD and Surge Protection",
    "is_basic": False,
    "is_preferred": False,
}

LOW_STOCK_ROW = {
    "lcsc": 999001,
    "mfr": "OBSCURE-PART-1",
    "package": "0603",
    "description": "510kΩ Thick Film Resistor 0603 Chip Resistor",
    "stock": 3,
    "price": "1-10:1.5",
    "category": "Resistors",
    "subcategory": "Chip Resistor - Surface Mount",
    "is_basic": True,
    "is_preferred": False,
}

# /resistors/list row: empty description, structured value, `price1` float.
STRUCTURED_RESISTOR_ROW = {
    "lcsc": 25804,
    "mfr": "0603WAF1002T5E",
    "description": "",
    "stock": 37165617,
    "price1": 0.000842857,
    "resistance": 10000,
    "package": "0603",
    "is_basic": True,
    "is_preferred": False,
    "attributes": json.dumps(
        {
            "Resistance": "10kΩ",
            "Power(Watts)": "100mW",
            "Tolerance": "±1%",
            "Temperature Coefficient": "±100ppm/℃",
        }
    ),
}

STRUCTURED_CAPACITOR_ROW = {
    "lcsc": 1525,
    "mfr": "CL05B104KO5NNNC",
    "description": "",
    "stock": 54323629,
    "price1": 0.001085714,
    "capacitance_farads": 1e-07,
    "package": "0402",
    "is_basic": True,
    "is_preferred": False,
    "attributes": json.dumps(
        {
            "Voltage Rated": "16V",
            "Tolerance": "±10%",
            "Capacitance": "100nF",
            "Temperature Coefficient": "X7R",
        }
    ),
}

# EasyEDA `result` payload, trimmed to the keys the client reads.
EASYEDA_RESULT = {
    "title": "0603WAF1002T5E",
    "description": "10KΩ (1002) ±1%",
    "szlcsc": {"number": "C25804", "price": 0.004864, "stock": 0},
    "dataStr": {
        "head": {
            "c_para": {
                "Manufacturer": "UNI-ROYAL(厚声)",
                "Manufacturer Part": "0603WAF1002T5E",
                "package": "R0603",
                "JLCPCB Part Class": "Basic Part",
            }
        }
    },
}


def make_fake_client(routes: dict | None = None) -> MagicMock:
    """Build a fake AsyncClient that serves jlcsearch JSON routes.

    `routes` maps a path ("/components/list") to either a payload dict or
    a callable taking the query params and returning a payload.
    """
    routes = routes or {}
    client = MagicMock(spec=httpx.AsyncClient)

    async def fake_get(path, params=None, **kwargs):
        resp = MagicMock(spec=httpx.Response)
        handler = routes.get(path)
        if handler is None:
            resp.status_code = 404
            resp.json = MagicMock(return_value={})
            return resp
        payload = handler(params or {}) if callable(handler) else handler
        resp.status_code = 200
        resp.json = MagicMock(return_value=payload)
        return resp

    client.get = AsyncMock(side_effect=fake_get)
    client.aclose = AsyncMock()
    return client


def components(*rows) -> dict:
    return {"components": list(rows)}


@pytest.fixture(autouse=True)
def isolated_cache(tmp_path, monkeypatch):
    """Point the SQLite cache at tmp_path so tests never touch ~/.cache."""
    monkeypatch.setattr(config, "CACHE_DIR", tmp_path)


# ---------------------------------------------------------------------------
# Value / query parsing
# ---------------------------------------------------------------------------


class TestParsePriceBreaks:
    def test_break_string_takes_qty_one_tier(self):
        assert lcsc_client._parse_price_breaks("1-999:0.0027,1000-:0.0023") == 0.0027

    def test_unordered_break_string_still_picks_lowest_qty(self):
        assert lcsc_client._parse_price_breaks("1000-:0.0023,1-999:0.0027") == 0.0027

    def test_plain_float_passes_through(self):
        assert lcsc_client._parse_price_breaks(0.05) == 0.05

    def test_empty_and_garbage(self):
        assert lcsc_client._parse_price_breaks("") == 0.0
        assert lcsc_client._parse_price_breaks(None) == 0.0
        assert lcsc_client._parse_price_breaks("not-a-price") == 0.0


class TestNormalizeLcsc:
    def test_bare_int_gets_c_prefix(self):
        assert lcsc_client._normalize_lcsc(25804) == "C25804"

    def test_already_prefixed_is_untouched(self):
        assert lcsc_client._normalize_lcsc("C25804") == "C25804"

    def test_none_is_empty(self):
        assert lcsc_client._normalize_lcsc(None) == ""


class TestParseResistance:
    @pytest.mark.parametrize(
        "token,ohms",
        [("10k", 10_000), ("4.7k", 4700), ("100r", 100), ("1m", 1_000_000), ("4k7", 4700)],
    )
    def test_schematic_spellings(self, token, ohms):
        assert lcsc_client._parse_resistance(token) == pytest.approx(ohms)

    def test_non_resistance_returns_none(self):
        assert lcsc_client._parse_resistance("esp32") is None
        assert lcsc_client._parse_resistance("30v") is None


class TestParseCapacitance:
    @pytest.mark.parametrize(
        "token,farads", [("0.1uf", 1e-7), ("100nf", 1e-7), ("10pf", 1e-11), ("4.7uf", 4.7e-6)]
    )
    def test_units(self, token, farads):
        assert lcsc_client._parse_capacitance(token) == pytest.approx(farads)

    def test_non_capacitance_returns_none(self):
        assert lcsc_client._parse_capacitance("0603") is None


class TestValueAliases:
    def test_uf_and_nf_are_the_same_part(self):
        assert "100nf" in lcsc_client._value_aliases("0.1uf")
        assert "0.1uf" in lcsc_client._value_aliases("100nf")

    def test_non_value_token_is_its_own_alias(self):
        assert lcsc_client._value_aliases("0603") == {"0603"}


class TestExtractPackage:
    def test_finds_chip_size(self):
        assert lcsc_client._extract_package(["10k", "0603", "1%"]) == "0603"

    def test_ignores_non_chip_packages(self):
        # SOT-23-5 is spelled inconsistently upstream, so it must not be
        # passed to the exact-match package filter.
        assert lcsc_client._extract_package(["ldo", "sot-23-5"]) is None


class TestDetectPassive:
    def test_resistor_with_package(self):
        assert lcsc_client._detect_passive(["10k", "0603"]) == ("resistors", 10_000)

    def test_capacitor(self):
        kind, value = lcsc_client._detect_passive(["0.1uf", "0402"])
        assert kind == "capacitors"
        assert value == pytest.approx(1e-7)

    def test_ic_query_is_not_a_passive(self):
        assert lcsc_client._detect_passive(["esp32-c3", "module"]) is None

    def test_bare_resistance_without_package_is_not_routed(self):
        # "1M gate driver" must not be treated as a 1 megohm chip resistor.
        assert lcsc_client._detect_passive(["1m", "gate", "driver"]) is None

    def test_potentiometer_is_excluded(self):
        assert lcsc_client._detect_passive(["10k", "0603", "potentiometer"]) is None


class TestScore:
    def _part(self, description, package=""):
        return lcsc_client.Part(
            lcsc="C1",
            mfr_part="",
            manufacturer="",
            description=description,
            package=package,
            basic=True,
            stock=1,
            price_usd=0.0,
        )

    def test_boundary_match_beats_substring(self):
        # "510kΩ" contains "10k" as a substring but is not a 10k resistor.
        wanted = self._part("10kΩ 0603 Chip Resistor")
        wrong = self._part("510kΩ 0603 Chip Resistor")
        assert lcsc_client._score(wanted, ["10k"]) > lcsc_client._score(wrong, ["10k"])

    def test_resistor_array_package_scores_below_plain_package(self):
        plain = self._part("0603 Chip Resistor", package="0603")
        array = self._part("0603x4 Resistor Network", package="0603x4")
        assert lcsc_client._score(plain, ["0603"]) > lcsc_client._score(array, ["0603"])

    def test_capacitance_alias_scores(self):
        part = self._part("100nF 16V X7R 0402")
        assert lcsc_client._score(part, ["0.1uf"]) == 2


# ---------------------------------------------------------------------------
# search()
# ---------------------------------------------------------------------------


class TestSearch:
    async def test_finds_basic(self):
        client = make_fake_client({"/components/list": components(RESISTOR_ROW)})
        results = await lcsc_client.search("thick film resistor", client=client)
        assert [p.lcsc for p in results] == ["C23192"]
        assert results[0].basic is True
        assert results[0].price_usd == 0.0027
        assert results[0].extra["category"] == "Resistors"

    async def test_basic_only_excludes_extended(self):
        client = make_fake_client({"/components/list": components(RESISTOR_ROW, EXTENDED_ROW)})
        results = await lcsc_client.search("resistor", basic_only=True, client=client)
        assert [p.lcsc for p in results] == ["C23192"]

    async def test_includes_extended_when_allowed(self):
        client = make_fake_client({"/components/list": components(RESISTOR_ROW, EXTENDED_ROW)})
        results = await lcsc_client.search("resistor", basic_only=False, client=client)
        assert {p.lcsc for p in results} == {"C23192", "C113998"}

    async def test_basic_filter_is_applied_locally_not_by_the_api(self):
        # Passing is_basic upstream filters before the row limit and guts
        # the candidate pool, so the client must never send it on the
        # free-text route.
        client = make_fake_client({"/components/list": components(RESISTOR_ROW)})
        await lcsc_client.search("resistor", basic_only=True, client=client)
        for call in client.get.call_args_list:
            assert "is_basic" not in (call.kwargs.get("params") or {})

    async def test_stock_min(self):
        client = make_fake_client({"/components/list": components(RESISTOR_ROW, LOW_STOCK_ROW)})
        results = await lcsc_client.search("resistor", stock_min=1000, client=client)
        assert [p.lcsc for p in results] == ["C23192"]

    async def test_limit_is_honoured(self):
        client = make_fake_client({"/components/list": components(RESISTOR_ROW, LOW_STOCK_ROW)})
        results = await lcsc_client.search("resistor", limit=1, client=client)
        assert len(results) == 1

    async def test_empty_query_returns_nothing(self):
        client = make_fake_client({"/components/list": components(RESISTOR_ROW)})
        assert await lcsc_client.search("   ", client=client) == []
        client.get.assert_not_called()

    async def test_package_is_passed_as_a_filter(self):
        client = make_fake_client({"/components/list": components(RESISTOR_ROW)})
        await lcsc_client.search("resistor", package="0603", client=client)
        assert client.get.call_args_list[0].kwargs["params"]["package"] == "0603"

    async def test_long_query_is_narrowed_until_it_matches(self):
        """jlcsearch AND-matches, so a 5-token spec returns nothing.

        The client must drop the least-matchable tokens and retry rather
        than reporting that the part doesn't exist.
        """
        seen: list[str] = []

        def handler(params):
            query = params.get("search", "")
            seen.append(query)
            # Mimic upstream: only a short query matches anything.
            if len(query.split()) > 3:
                return {"components": []}
            return components(EXTENDED_ROW)

        client = make_fake_client({"/components/list": handler})
        results = await lcsc_client.search(
            "TVS diode 30V 3000W SMB", basic_only=False, client=client
        )
        assert [p.lcsc for p in results] == ["C113998"]
        assert len(seen) > 1, "should have retried with fewer tokens"
        assert len(seen[-1].split()) < len(seen[0].split())

    async def test_narrowing_stops_before_nonsense(self):
        """A single leftover token matches half the catalog; prefer nothing.

        One token of "TVS diode 30V 3000W SMB" matches every LED in the
        catalog because LED descriptions contain the word "Diode".
        """
        client = make_fake_client({"/components/list": lambda p: {"components": []}})
        results = await lcsc_client.search(
            "TVS diode 30V 3000W SMB", basic_only=False, client=client
        )
        assert results == []
        narrowest = min(
            len((c.kwargs["params"]["search"]).split()) for c in client.get.call_args_list
        )
        assert narrowest >= 3, "must not narrow a 5-token query below half"

    async def test_narrowing_keeps_searching_when_basic_filter_empties_results(self):
        """Getting rows back is not the same as getting usable rows.

        A query that returns only extended parts must keep narrowing when
        the caller asked for basic-only.
        """

        def handler(params):
            # Wider queries return rows, but only extended ones.
            if len(params.get("search", "").split()) > 2:
                return components(EXTENDED_ROW)
            return components(RESISTOR_ROW)

        client = make_fake_client({"/components/list": handler})
        results = await lcsc_client.search("alpha beta gamma delta", basic_only=True, client=client)
        assert [p.lcsc for p in results] == ["C23192"]
        assert client.get.call_count > 1


class TestSearchPassives:
    async def test_resistor_uses_the_structured_route(self):
        client = make_fake_client({"/resistors/list": {"resistors": [STRUCTURED_RESISTOR_ROW]}})
        results = await lcsc_client.search("10k 0603", client=client)
        assert [p.lcsc for p in results] == ["C25804"]
        params = client.get.call_args_list[0].kwargs["params"]
        assert params["resistance"] == repr(10000.0)
        assert params["package"] == "0603"

    async def test_structured_row_gets_a_synthesised_description(self):
        # Upstream leaves `description` empty on these rows; a BOM line the
        # model cannot sanity-check is worse than useless.
        client = make_fake_client({"/resistors/list": {"resistors": [STRUCTURED_RESISTOR_ROW]}})
        part = (await lcsc_client.search("10k 0603", client=client))[0]
        assert "10kΩ" in part.description
        assert "0603" in part.description
        assert part.price_usd == pytest.approx(0.000842857)

    async def test_capacitor_uses_the_structured_route_in_farads(self):
        client = make_fake_client({"/capacitors/list": {"capacitors": [STRUCTURED_CAPACITOR_ROW]}})
        results = await lcsc_client.search("0.1uF 0402 X7R", client=client)
        assert [p.lcsc for p in results] == ["C1525"]
        params = client.get.call_args_list[0].kwargs["params"]
        assert float(params["capacitance"]) == pytest.approx(1e-7)

    async def test_structured_route_may_send_is_basic(self):
        # Safe here: these routes filter on an indexed value first, so the
        # basic filter narrows a precise set rather than gutting a ranking.
        client = make_fake_client({"/resistors/list": {"resistors": [STRUCTURED_RESISTOR_ROW]}})
        await lcsc_client.search("10k 0603", basic_only=True, client=client)
        assert client.get.call_args_list[0].kwargs["params"]["is_basic"] == "true"

    async def test_falls_back_to_free_text_when_structured_finds_nothing(self):
        client = make_fake_client(
            {
                "/resistors/list": {"resistors": []},
                "/components/list": components(RESISTOR_ROW),
            }
        )
        results = await lcsc_client.search("10k 0603", client=client)
        assert [p.lcsc for p in results] == ["C23192"]


# ---------------------------------------------------------------------------
# get_part() — EasyEDA lookup + cache
# ---------------------------------------------------------------------------


@pytest.fixture
def easyeda(monkeypatch):
    """Patch the EasyEDA fetcher and report how many times it was called."""
    calls: list[str] = []

    async def fake_fetch(lcsc, client):
        calls.append(lcsc)
        if lcsc != "C25804":
            raise part_library.PartLibraryError(f"EasyEDA has no component data for {lcsc}")
        return EASYEDA_RESULT

    monkeypatch.setattr(part_library, "_fetch_easyeda_raw", fake_fetch)
    return calls


class TestGetPart:
    async def test_found(self, easyeda):
        client = make_fake_client({"/components/list": {"components": []}})
        part = await lcsc_client.get_part("C25804", client=client)
        assert part.lcsc == "C25804"
        assert part.mfr_part == "0603WAF1002T5E"
        assert part.manufacturer.startswith("UNI-ROYAL")
        assert part.package == "R0603"
        assert part.basic is True
        assert part.price_usd == 0.004864

    async def test_invalid_format(self, easyeda):
        for bad in ("25804", "CABC", "", "X1"):
            with pytest.raises(lcsc_client.LcscError, match="Invalid LCSC part number"):
                await lcsc_client.get_part(bad)
        assert easyeda == []

    async def test_unknown_part_raises(self, easyeda):
        client = make_fake_client({"/components/list": {"components": []}})
        with pytest.raises(lcsc_client.LcscError, match="not found"):
            await lcsc_client.get_part("C99999999", client=client)

    async def test_stock_is_flagged_unknown_not_reported_as_zero(self, easyeda):
        """EasyEDA reports LCSC retail stock, which reads 0 for parts with
        millions in JLCPCB inventory. Publishing that as fact would sink a
        BOM decision, so it must be flagged."""
        client = make_fake_client({"/components/list": {"components": []}})
        part = await lcsc_client.get_part("C25804", client=client)
        assert part.extra["stock_unknown"] is True
        assert "not available" in part.stock_warning()

    async def test_stock_is_enriched_from_jlcsearch_when_the_c_number_matches(self, easyeda):
        row = dict(RESISTOR_ROW, lcsc=25804, mfr="0603WAF1002T5E", stock=37165617)
        client = make_fake_client({"/components/list": components(row)})
        part = await lcsc_client.get_part("C25804", client=client)
        assert part.stock == 37165617
        assert part.extra.get("stock_unknown") is not True
        assert part.stock_warning() is None

    async def test_enrichment_ignores_a_different_c_number(self, easyeda):
        # A same-MPN row for a different C-number must not be adopted.
        client = make_fake_client({"/components/list": components(RESISTOR_ROW)})
        part = await lcsc_client.get_part("C25804", client=client)
        assert part.lcsc == "C25804"
        assert part.extra["stock_unknown"] is True

    async def test_enrichment_failure_is_not_fatal(self, easyeda):
        client = make_fake_client({})  # every jlcsearch route 404s
        part = await lcsc_client.get_part("C25804", client=client)
        assert part.mfr_part == "0603WAF1002T5E"

    async def test_second_call_reuses_cache(self, easyeda):
        client = make_fake_client({"/components/list": {"components": []}})
        await lcsc_client.get_part("C25804", client=client)
        assert len(easyeda) == 1
        client2 = make_fake_client({"/components/list": {"components": []}})
        part = await lcsc_client.get_part("C25804", client=client2)
        assert part.mfr_part == "0603WAF1002T5E"
        assert len(easyeda) == 1, "cache hit must not refetch"
        client2.get.assert_not_called()

    async def test_force_refresh_bypasses_cache(self, easyeda):
        client = make_fake_client({"/components/list": {"components": []}})
        await lcsc_client.get_part("C25804", client=client)
        await lcsc_client.get_part("C25804", client=client, force_refresh=True)
        assert len(easyeda) == 2

    async def test_expired_cache_row_is_refetched(self, easyeda, monkeypatch):
        client = make_fake_client({"/components/list": {"components": []}})
        await lcsc_client.get_part("C25804", client=client)
        monkeypatch.setattr(lcsc_client, "CACHE_TTL_SECONDS", -1)
        await lcsc_client.get_part("C25804", client=client)
        assert len(easyeda) == 2

    async def test_search_results_warm_the_cache_for_get_part(self, easyeda):
        row = dict(RESISTOR_ROW, lcsc=25804, mfr="0603WAF1002T5E")
        client = make_fake_client({"/components/list": components(row)})
        await lcsc_client.search("thick film resistor", client=client)
        part = await lcsc_client.get_part("C25804", client=client)
        assert part.mfr_part == "0603WAF1002T5E"
        assert easyeda == [], "a warm cache must not hit EasyEDA at all"


class TestCacheSchemaMigration:
    def test_a_stale_cache_layout_is_discarded_not_migrated(self, tmp_path, monkeypatch):
        """The previous release cached a retired data source. Reusing those
        rows would serve stale parts forever, so the table is dropped."""
        import sqlite3

        monkeypatch.setattr(config, "CACHE_DIR", tmp_path)
        db = tmp_path / config.LCSC_CACHE_DB
        conn = sqlite3.connect(db)
        conn.execute("CREATE TABLE parts (lcsc TEXT PRIMARY KEY, junk TEXT)")
        conn.execute("INSERT INTO parts VALUES ('C1', 'from the old world')")
        conn.execute("PRAGMA user_version = 1")
        conn.commit()
        conn.close()

        with lcsc_client._open_cache() as conn:
            assert conn.execute("SELECT COUNT(*) FROM parts").fetchone()[0] == 0
            cols = {r[1] for r in conn.execute("PRAGMA table_info(parts)")}
            assert "fetched_at" in cols
            assert conn.execute("PRAGMA user_version").fetchone()[0] == 2


# ---------------------------------------------------------------------------
# resolve_bom()
# ---------------------------------------------------------------------------


class TestResolveBom:
    async def test_resolves_lcsc_rows(self, easyeda):
        row = dict(RESISTOR_ROW, lcsc=25804, mfr="0603WAF1002T5E")
        client = make_fake_client({"/components/list": components(row)})
        result = await lcsc_client.resolve_bom([{"lcsc": "C25804", "qty": 10}], client=client)
        assert len(result["resolved"]) == 1
        assert result["resolved"][0]["part"]["lcsc"] == "C25804"
        assert result["unresolved"] == []

    async def test_query_row_falls_through_to_search(self, easyeda):
        client = make_fake_client({"/components/list": components(RESISTOR_ROW)})
        result = await lcsc_client.resolve_bom(
            [{"query": "thick film resistor", "qty": 5}], client=client
        )
        assert result["resolved"][0]["part"]["lcsc"] == "C23192"

    async def test_query_falls_back_to_extended_when_no_basic_match(self, easyeda):
        client = make_fake_client({"/components/list": components(EXTENDED_ROW)})
        result = await lcsc_client.resolve_bom(
            [{"query": "unidirectional TVS diode", "qty": 1}], client=client
        )
        assert result["resolved"][0]["part"]["lcsc"] == "C113998"
        assert result["extended_part_count"] == 1

    async def test_warns_on_extended_part(self, easyeda):
        client = make_fake_client({"/components/list": components(EXTENDED_ROW)})
        result = await lcsc_client.resolve_bom(
            [{"query": "unidirectional TVS diode", "qty": 1}], client=client
        )
        warnings = result["resolved"][0]["warnings"]
        assert any("EXTENDED" in w for w in warnings)
        assert result["estimated_setup_fee_usd"] == config.EXTENDED_PART_SETUP_FEE_USD

    async def test_duplicate_extended_parts_count_once(self, easyeda):
        client = make_fake_client({"/components/list": components(EXTENDED_ROW)})
        rows = [{"query": "unidirectional TVS diode", "qty": 1}] * 3
        result = await lcsc_client.resolve_bom(rows, client=client)
        assert result["extended_part_count"] == 1
        assert result["estimated_setup_fee_usd"] == config.EXTENDED_PART_SETUP_FEE_USD

    async def test_unresolved_collected(self, easyeda):
        client = make_fake_client({"/components/list": {"components": []}})
        result = await lcsc_client.resolve_bom(
            [{"query": "a part that does not exist", "qty": 1}], client=client
        )
        assert result["resolved"] == []
        assert len(result["unresolved"]) == 1
        assert "No LCSC match" in result["unresolved"][0]["reason"]

    async def test_row_without_lcsc_or_query_is_reported(self, easyeda):
        client = make_fake_client({})
        result = await lcsc_client.resolve_bom([{"qty": 1}], client=client)
        assert "neither" in result["unresolved"][0]["reason"]

    async def test_unverified_stock_is_surfaced_in_the_summary(self, easyeda):
        client = make_fake_client({"/components/list": {"components": []}})
        result = await lcsc_client.resolve_bom([{"lcsc": "C25804", "qty": 1}], client=client)
        assert "unverified JLCPCB stock" in result["summary"]
        assert any("not available" in w for w in result["resolved"][0]["warnings"])


class TestUpstreamFailureMessages:
    async def test_http_error_names_the_override(self, monkeypatch):
        """A forker hitting a dead upstream should be told how to redirect it."""
        client = MagicMock(spec=httpx.AsyncClient)
        client.get = AsyncMock(side_effect=httpx.ReadTimeout("boom"))
        monkeypatch.setattr(lcsc_client, "SEARCH_RETRIES", 1)
        with pytest.raises(lcsc_client.LcscError, match="KJLC_JLCSEARCH_BASE"):
            await lcsc_client.search("resistor", client=client)

    async def test_shape_change_says_so(self):
        client = make_fake_client({"/components/list": {"unexpected": []}})
        with pytest.raises(lcsc_client.LcscError, match="shape changed"):
            await lcsc_client.search("resistor", client=client)

    async def test_transient_error_is_retried(self, monkeypatch):
        calls = {"n": 0}

        async def flaky(path, params=None, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                raise httpx.ReadTimeout("stall")
            resp = MagicMock(spec=httpx.Response)
            resp.status_code = 200
            resp.json = MagicMock(return_value=components(RESISTOR_ROW))
            return resp

        monkeypatch.setattr(lcsc_client.asyncio, "sleep", AsyncMock())
        client = MagicMock(spec=httpx.AsyncClient)
        client.get = AsyncMock(side_effect=flaky)
        results = await lcsc_client.search("thick film resistor", client=client)
        assert [p.lcsc for p in results] == ["C23192"]
        assert calls["n"] == 2
