"""Tests for pcb.py — spec validation, placement, BOM helper, and (when
KiCad is installed) real pcbnew board generation.

The pcbnew-dependent tests skip automatically unless KICAD_INSTALLED=1
is set in the environment, matching the pattern used by test_kicad_cli.
"""

import os
from pathlib import Path

import pytest

from kicad_jlcpcb_mcp import config, part_library, pcb
from kicad_jlcpcb_mcp.pcb import (
    MIN_PITCH_MM,
    PcbGenerationError,
    _classify,
    _normalize_pin,
    _place,
    _refs_needing_pin_names,
    _resolve_pad,
    _resolve_pinmaps,
    _validate_spec,
    bom_from_components,
    generate_pcb,
    resolve_footprint_dir,
)

# ---------------------------------------------------------------------------
# Sample spec fixtures
# ---------------------------------------------------------------------------


def minimal_spec() -> dict:
    """Two resistors wired into a single net — the smallest interesting spec."""
    return {
        "name": "test",
        "board": {"width_mm": 40.0, "height_mm": 30.0, "layer_count": 2},
        "components": [
            {
                "ref": "R1",
                "value": "10k",
                "lcsc": "C25804",
                "lib": "Resistor_SMD",
                "fp": "R_0603_1608Metric",
            },
            {
                "ref": "R2",
                "value": "10k",
                "lcsc": "C25804",
                "lib": "Resistor_SMD",
                "fp": "R_0603_1608Metric",
            },
        ],
        "nets": {
            "VCC": [["R1", "1"], ["R2", "1"]],
            "GND": [["R1", "2"], ["R2", "2"]],
        },
    }


def spec_with_ic() -> dict:
    """Includes an IC with a pin-name map so we exercise pinmap resolution."""
    s = minimal_spec()
    s["components"].insert(
        0,
        {
            "ref": "U1",
            "value": "ESP32",
            "lib": "RF_Module",
            "fp": "ESP32-C3-WROOM-02",
            "pinmap": {"GND": "1", "3V3": "2", "GPIO10": "15"},
        },
    )
    s["nets"]["VCC"].append(["U1", "3V3"])
    s["nets"]["GND"].append(["U1", "GND"])
    return s


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------


class TestClassify:
    def test_ic_prefixes(self):
        assert _classify("U1") == "ic"
        assert _classify("U12") == "ic"
        assert _classify("Q1") == "ic"
        assert _classify("Y1") == "ic"

    def test_connector(self):
        assert _classify("J1") == "conn"
        assert _classify("J10") == "conn"

    def test_passive(self):
        assert _classify("R1") == "passive"
        assert _classify("C5") == "passive"
        assert _classify("L2") == "passive"
        assert _classify("D1") == "passive"
        assert _classify("SW1") == "passive"


# ---------------------------------------------------------------------------
# Spec validation
# ---------------------------------------------------------------------------


class TestValidateSpec:
    def test_valid_minimal(self):
        board, comps, nets = _validate_spec(minimal_spec())
        assert board["width_mm"] == 40.0
        assert len(comps) == 2
        assert len(nets) == 2

    def test_rejects_non_dict(self):
        with pytest.raises(PcbGenerationError, match="must be a dict"):
            _validate_spec("nope")

    def test_rejects_missing_components(self):
        with pytest.raises(PcbGenerationError, match="components"):
            _validate_spec({"board": {}})

    def test_rejects_empty_components(self):
        with pytest.raises(PcbGenerationError, match="non-empty"):
            _validate_spec({"components": []})

    def test_rejects_duplicate_refs(self):
        spec = minimal_spec()
        spec["components"][1]["ref"] = "R1"  # duplicate
        with pytest.raises(PcbGenerationError, match="Duplicate"):
            _validate_spec(spec)

    def test_rejects_missing_required_field(self):
        spec = minimal_spec()
        del spec["components"][0]["lib"]
        with pytest.raises(PcbGenerationError, match="lib"):
            _validate_spec(spec)

    def test_rejects_unknown_ref_in_net(self):
        spec = minimal_spec()
        spec["nets"]["VCC"].append(["GHOST", "1"])
        with pytest.raises(PcbGenerationError, match="unknown component"):
            _validate_spec(spec)

    def test_rejects_malformed_net_member(self):
        spec = minimal_spec()
        spec["nets"]["VCC"] = [["R1"]]  # only one element
        with pytest.raises(PcbGenerationError, match=r"\[ref, pin\]"):
            _validate_spec(spec)

    def test_rejects_unsupported_layer_count(self):
        spec = minimal_spec()
        spec["board"]["layer_count"] = 6
        with pytest.raises(PcbGenerationError, match="2 or 4 layer"):
            _validate_spec(spec)

    def test_defaults_layer_count_to_2(self):
        spec = minimal_spec()
        del spec["board"]["layer_count"]
        board, _, _ = _validate_spec(spec)
        assert board["layer_count"] == 2


# ---------------------------------------------------------------------------
# Placement
# ---------------------------------------------------------------------------


class TestPlace:
    def test_three_band_layout(self):
        spec = spec_with_ic()
        spec["components"].append(
            {
                "ref": "J1",
                "value": "USB",
                "lib": "Connector_USB",
                "fp": "USB_C_Receptacle_HRO_TYPE-C-31-M-12",
            }
        )
        positions, _ = _place(
            spec["components"],
            board_width_mm=100.0,
            board_height_mm=80.0,
        )
        assert set(positions.keys()) == {"J1", "U1", "R1", "R2"}
        # Connector is in the top band
        assert positions["J1"][1] < positions["U1"][1]
        # IC is above passives
        assert positions["U1"][1] < positions["R1"][1]

    def test_every_component_gets_position(self):
        positions, _ = _place(
            minimal_spec()["components"],
            board_width_mm=40.0,
            board_height_mm=30.0,
        )
        assert set(positions.keys()) == {"R1", "R2"}


# ---------------------------------------------------------------------------
# BOM helper
# ---------------------------------------------------------------------------


class TestBomFromComponents:
    def test_writes_csv(self, tmp_path):
        components = minimal_spec()["components"]
        out = tmp_path / "bom.csv"
        result = bom_from_components(components, out)
        assert result["parts"] == 2
        assert result["rows"] == 1  # R1 and R2 grouped
        text = out.read_text()
        assert "Qty,Value,LCSC" in text
        assert '"R1,R2"' in text
        assert '2,"10k","C25804"' in text

    def test_creates_parent_dir(self, tmp_path):
        out = tmp_path / "nested" / "bom.csv"
        bom_from_components(minimal_spec()["components"], out)
        assert out.is_file()

    def test_groups_by_value_lcsc_footprint(self, tmp_path):
        components = [
            {"ref": "R1", "value": "10k", "lcsc": "C1", "lib": "L", "fp": "F"},
            {"ref": "R2", "value": "10k", "lcsc": "C1", "lib": "L", "fp": "F"},
            {"ref": "R3", "value": "10k", "lcsc": "C2", "lib": "L", "fp": "F"},  # different LCSC
        ]
        out = tmp_path / "bom.csv"
        result = bom_from_components(components, out)
        assert result["rows"] == 2  # C1 group + C2 group
        lines = out.read_text().strip().split("\n")
        assert len(lines) == 3  # header + 2 data rows


# ---------------------------------------------------------------------------
# generate_pcb — integration tests (need pcbnew)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    os.environ.get("KICAD_INSTALLED") != "1",
    reason="Set KICAD_INSTALLED=1 to run pcbnew integration tests",
)
class TestGenerateRealPcb:
    @pytest.mark.asyncio
    async def test_minimal_board(self, tmp_path):
        out = tmp_path / "minimal.kicad_pcb"
        result = await generate_pcb(minimal_spec(), out, auto_fetch_pinmaps=False)
        assert result.footprints_placed == 2
        assert result.nets_created == 2
        assert out.is_file()
        assert result.net_stats["VCC"] == 2
        assert result.net_stats["GND"] == 2
        assert result.errors == []

    @pytest.mark.asyncio
    async def test_ic_with_pinmap(self, tmp_path):
        out = tmp_path / "ic.kicad_pcb"
        result = await generate_pcb(spec_with_ic(), out, auto_fetch_pinmaps=False)
        assert result.footprints_placed == 3
        assert out.is_file()
        assert result.net_stats["VCC"] == 3
        assert result.errors == []

    @pytest.mark.asyncio
    async def test_missing_footprint_reports_error(self, tmp_path):
        spec = minimal_spec()
        spec["components"][0]["fp"] = "NOT_A_REAL_FOOTPRINT_XYZ"
        out = tmp_path / "bad.kicad_pcb"
        result = await generate_pcb(spec, out, auto_fetch_pinmaps=False)
        assert out.is_file()
        assert any("NOT_A_REAL_FOOTPRINT_XYZ" in e for e in result.errors)
        assert result.footprints_placed == 1

    @pytest.mark.asyncio
    async def test_missing_pad_reports_error(self, tmp_path):
        spec = minimal_spec()
        spec["nets"]["VCC"] = [["R1", "99"], ["R2", "1"]]
        out = tmp_path / "badpad.kicad_pcb"
        result = await generate_pcb(spec, out, auto_fetch_pinmaps=False)
        # The error names the pin AND what the footprint actually has —
        # "no pad '99'" alone gives the reader nothing to correct towards.
        assert any("pin '99'" in e for e in result.errors), result.errors
        assert any("Footprint pads:" in e for e in result.errors), result.errors
        assert result.net_stats["VCC"] == 1

    @pytest.mark.asyncio
    async def test_auto_fetch_pinmap_uses_cache(self, tmp_path, monkeypatch):
        """With auto_fetch_pinmaps=True and a pre-populated pinmap cache,
        pcb_generate should resolve pin names without hitting the network."""
        from kicad_jlcpcb_mcp import config, part_library

        monkeypatch.setattr(config, "CACHE_DIR", tmp_path / "cache")

        # Seed the pin-map cache with a fake entry for C12345
        part_library._pinmap_to_cache(
            "C12345",
            {
                "lcsc": "C12345",
                "title": "TEST_PART",
                "pin_count": 2,
                "pinmap": {"VCC_PIN": "1", "GND_PIN": "2"},
                "pad_to_name": {"1": "VCC_PIN", "2": "GND_PIN"},
            },
        )

        spec = {
            "name": "cache_test",
            "board": {"width_mm": 40, "height_mm": 30, "layer_count": 2},
            "components": [
                {
                    "ref": "R1",
                    "value": "10k",
                    "lcsc": "C12345",
                    "lib": "Resistor_SMD",
                    "fp": "R_0603_1608Metric",
                },
            ],
            # Reference pins by name — should resolve via cached pinmap
            "nets": {
                "VCC": [["R1", "VCC_PIN"]],
                "GND": [["R1", "GND_PIN"]],
            },
        }
        out = tmp_path / "cached.kicad_pcb"
        result = await generate_pcb(spec, out, auto_fetch_pinmaps=True)
        assert result.errors == []
        # Both nets should have R1 connected
        assert result.net_stats["VCC"] == 1
        assert result.net_stats["GND"] == 1


# ---------------------------------------------------------------------------
# generate_pcb — no-pcbnew path
# ---------------------------------------------------------------------------


class TestGeneratePcbWithoutPcbnew:
    @pytest.mark.asyncio
    async def test_raises_when_pcbnew_unavailable(self, tmp_path, monkeypatch):
        # Force the lazy import path to fail
        monkeypatch.setattr(pcb, "pcbnew", None)
        import sys

        saved = sys.modules.get("pcbnew")
        sys.modules["pcbnew"] = None
        try:
            with pytest.raises(PcbGenerationError, match="pcbnew"):
                await generate_pcb(
                    minimal_spec(), tmp_path / "x.kicad_pcb", auto_fetch_pinmaps=False
                )
        finally:
            if saved is not None:
                sys.modules["pcbnew"] = saved
            else:
                sys.modules.pop("pcbnew", None)


class TestFootprintDirResolution:
    """`pcb_generate` used to hardcode "/usr/share/kicad/footprints", so it
    could not place a single footprint on macOS or Windows — including for
    users whose KiCad `detect_kicad` had just found successfully."""

    def test_explicit_path_wins(self, tmp_path):
        assert resolve_footprint_dir(str(tmp_path)) == str(tmp_path)

    def test_explicit_path_that_does_not_exist_is_rejected(self):
        assert resolve_footprint_dir("/definitely/not/here") is None

    def test_plugin_env_override(self, tmp_path, monkeypatch):
        monkeypatch.setenv("KJLC_FOOTPRINT_DIR", str(tmp_path))
        assert resolve_footprint_dir() == str(tmp_path)

    def test_honours_kicads_own_env_convention(self, tmp_path, monkeypatch):
        """These are the same variables KiCad's fp-lib-table entries expand."""
        monkeypatch.delenv("KJLC_FOOTPRINT_DIR", raising=False)
        monkeypatch.setenv("KICAD9_FOOTPRINT_DIR", str(tmp_path))
        assert resolve_footprint_dir() == str(tmp_path)

    def test_falls_back_to_platform_candidates(self, tmp_path, monkeypatch):
        for var in config.FOOTPRINT_DIR_ENV_VARS:
            monkeypatch.delenv(var, raising=False)
        monkeypatch.setattr(config, "FOOTPRINT_DIR_CANDIDATES", ("/nope", str(tmp_path)))
        assert resolve_footprint_dir() == str(tmp_path)

    def test_returns_none_when_nothing_is_found(self, monkeypatch):
        for var in config.FOOTPRINT_DIR_ENV_VARS:
            monkeypatch.delenv(var, raising=False)
        monkeypatch.setattr(config, "FOOTPRINT_DIR_CANDIDATES", ("/nope", "/also/nope"))
        assert resolve_footprint_dir() is None

    def test_candidates_cover_every_platform_the_readme_claims(self):
        joined = " ".join(config.FOOTPRINT_DIR_CANDIDATES)
        assert "/usr/share/kicad" in joined, "Linux"
        assert "KiCad.app" in joined, "macOS"
        assert "Program Files" in joined, "Windows"
        assert "flatpak" in joined, "Flatpak"

    @pytest.mark.asyncio
    async def test_generate_pcb_explains_itself_when_no_library_exists(self, tmp_path, monkeypatch):
        """The failure must name the override, not just say 'not found'."""
        for var in config.FOOTPRINT_DIR_ENV_VARS:
            monkeypatch.delenv(var, raising=False)
        monkeypatch.setattr(config, "FOOTPRINT_DIR_CANDIDATES", ("/nope",))
        monkeypatch.setattr(pcb, "_ensure_pcbnew", lambda: object())
        with pytest.raises(PcbGenerationError, match="KJLC_FOOTPRINT_DIR"):
            await generate_pcb(minimal_spec(), tmp_path / "b.kicad_pcb")


class TestPinMapFetchIsSkippedWhenUseless:
    """EasyEDA is rate-limited to one request per 12 seconds, and the old
    code fetched a pin map for every component carrying an LCSC number.
    A decoupling cap wired as ("C1", "1") needs nothing fetched — its pads
    are already numbers. On the shipped example that was 13 requests where
    3 would do: 156 seconds of waiting reduced to 36."""

    def test_named_pins_require_a_fetch(self):
        nets = {"VCC": [("U1", "VIN"), ("C1", "1")]}
        assert _refs_needing_pin_names(nets) == {"U1"}

    def test_numeric_pads_require_nothing(self):
        nets = {"GND": [("C1", "2"), ("R1", "2")]}
        assert _refs_needing_pin_names(nets) == set()

    def test_a_ref_is_flagged_if_any_net_names_a_pin(self):
        nets = {
            "GND": [("U1", "2")],
            "VCC": [("U1", "VIN")],
        }
        assert _refs_needing_pin_names(nets) == {"U1"}

    def test_empty_nets(self):
        assert _refs_needing_pin_names({}) == set()

    @pytest.mark.asyncio
    async def test_resolver_does_not_call_easyeda_for_skipped_refs(self, monkeypatch):
        calls: list[str] = []

        async def spy(lcsc, **kwargs):
            calls.append(lcsc)
            return {"pinmap": {"VIN": "1"}}

        monkeypatch.setattr(part_library, "get_pin_map", spy)
        components = [
            {"ref": "U1", "lcsc": "C82942"},
            {"ref": "C1", "lcsc": "C1525"},
        ]
        maps, warnings = await _resolve_pinmaps(
            components, auto_fetch=True, force_refresh=False, needed_refs={"U1"}
        )
        assert calls == ["C82942"], "should not have fetched for the numeric-pad part"
        assert maps["C1"] == {}
        assert maps["U1"] == {"VIN": "1"}

    @pytest.mark.asyncio
    async def test_an_explicit_pinmap_still_wins(self, monkeypatch):
        async def boom(lcsc, **kwargs):
            raise AssertionError("must not fetch when a pinmap was supplied")

        monkeypatch.setattr(part_library, "get_pin_map", boom)
        maps, _ = await _resolve_pinmaps(
            [{"ref": "U1", "lcsc": "C1", "pinmap": {"A": "1"}}],
            auto_fetch=True,
            force_refresh=False,
            needed_refs={"U1"},
        )
        assert maps["U1"] == {"A": "1"}

    def test_the_shipped_example_benefits(self):
        """Guard the real-world win, not just the unit behaviour."""
        import json

        spec = json.loads(Path("examples/soilnode-esp32/spec.json").read_text())
        nets = {n: [(m[0], m[1]) for m in ms] for n, ms in spec["nets"].items()}
        needed = _refs_needing_pin_names(nets)
        with_lcsc = [c for c in spec["components"] if c.get("lcsc") and not c.get("pinmap")]
        assert len(needed) < len(with_lcsc) / 2, "most parts should not need a fetch"


class TestRefdesClassification:
    """Placement buckets parts by where they want to sit on a board.
    Prefix matching is longest-first, which is load-bearing: "SW1" starts
    with "S", and "XT1" (a crystal) starts with "X", so a naive
    first-match rule files a crystal as a connector."""

    @pytest.mark.parametrize("ref", ["U1", "U12", "IC3", "Q1", "Y1", "XT1", "K1", "T1"])
    def test_active_parts(self, ref):
        assert _classify(ref) == "ic"

    @pytest.mark.parametrize("ref", ["J1", "J10", "CN2", "USB1", "P3", "X1"])
    def test_board_edge_parts(self, ref):
        assert _classify(ref) == "conn"

    @pytest.mark.parametrize("ref", ["R1", "C5", "L2", "D4", "FB1", "SW1", "TP7", "MH1"])
    def test_two_terminal_and_mechanical(self, ref):
        assert _classify(ref) == "passive"

    def test_crystal_is_not_a_connector(self):
        """XT1 starts with X, the auxiliary-connector prefix."""
        assert _classify("XT1") == "ic"
        assert _classify("X1") == "conn"

    def test_usb_connector_is_not_a_passive(self):
        """USB1 starts with U, which alone would read as an IC."""
        assert _classify("USB1") == "conn"

    def test_unknown_and_empty_refs_are_safe(self):
        assert _classify("Z9") == "passive"
        assert _classify("") == "passive"
        assert _classify("123") == "passive"

    def test_case_insensitive(self):
        assert _classify("u1") == "ic"
        assert _classify("usb1") == "conn"


class TestPlacementStaysOnTheBoard:
    """Placement used fixed band offsets — +16 mm after connectors, +14 mm
    after ICs — and never checked the result against the board height. On a
    40 x 30 mm board the first passive landed at y=40, below the bottom
    edge, and all 30 parts of a 30-passive board ended up outside the
    outline. That is exactly the size of board this plugin is for, and
    JLCPCB rejects footprints outside Edge.Cuts.
    """

    def _inside(self, positions, w, h):
        return [ref for ref, (x, y) in positions.items() if x < 0 or y < 0 or x > w or y > h]

    @pytest.mark.parametrize(
        "count,w,h",
        [(30, 40, 30), (2, 20, 20), (13, 80, 60), (1, 10, 10)],
    )
    def test_everything_lands_inside_the_outline(self, count, w, h):
        comps = [{"ref": f"R{i}"} for i in range(count)]
        positions, warnings = _place(comps, board_width_mm=w, board_height_mm=h)
        assert self._inside(positions, w, h) == []
        assert warnings == []

    def test_the_small_board_regression(self):
        """30 passives on 40x30 used to put every single part off the board."""
        comps = [{"ref": f"R{i}"} for i in range(30)]
        positions, _ = _place(comps, board_width_mm=40, board_height_mm=30)
        assert max(y for _, y in positions.values()) <= 30

    def test_bands_keep_their_order(self):
        comps = [{"ref": "J1"}, {"ref": "U1"}, {"ref": "R1"}]
        positions, _ = _place(comps, board_width_mm=80, board_height_mm=60)
        assert positions["J1"][1] < positions["U1"][1] < positions["R1"][1]

    def test_a_board_of_only_passives_uses_the_whole_height(self):
        """An absent band should not reserve space for itself."""
        comps = [{"ref": f"C{i}"} for i in range(12)]
        positions, _ = _place(comps, board_width_mm=40, board_height_mm=40)
        assert min(y for _, y in positions.values()) < 10

    def test_every_component_is_placed(self):
        comps = [{"ref": f"R{i}"} for i in range(50)]
        positions, _ = _place(comps, board_width_mm=50, board_height_mm=50)
        assert len(positions) == 50

    def test_no_two_components_share_a_position(self):
        comps = [{"ref": f"R{i}"} for i in range(20)]
        positions, _ = _place(comps, board_width_mm=60, board_height_mm=40)
        assert len(set(positions.values())) == len(positions)


class TestPlacementNeverOverlaps:
    """Compressing to fit solved parts landing off the board and created a
    second problem: at 100 parts on 30x30 the pitch fell to 0.66 mm, and an
    0603 is 1.6 mm long. Overlapping copper fails DRC and gets rejected —
    and unlike a part that doesn't fit, nothing reports it.
    """

    def _min_pitch(self, positions):
        import math

        pts = sorted(positions.values())
        return min((math.dist(a, b) for a, b in zip(pts, pts[1:])), default=float("inf"))

    @pytest.mark.parametrize("count,w,h", [(20, 40, 30), (30, 40, 30), (100, 30, 30)])
    def test_pitch_never_drops_below_an_0603_footprint(self, count, w, h):
        comps = [{"ref": f"R{i}"} for i in range(count)]
        positions, _ = _place(comps, board_width_mm=w, board_height_mm=h)
        assert self._min_pitch(positions) >= MIN_PITCH_MM["passive"] - 1e-9

    def test_an_overfull_board_reports_rather_than_stacking_parts(self):
        comps = [{"ref": f"R{i}"} for i in range(100)]
        positions, warnings = _place(comps, board_width_mm=30, board_height_mm=30)
        assert warnings, "an overfull board must say so"
        joined = " ".join(warnings)
        assert "do not fit" in joined
        assert "genuinely too small" in joined
        # And it still refuses to overlap what it did place.
        assert self._min_pitch(positions) >= MIN_PITCH_MM["passive"] - 1e-9

    def test_ics_get_more_room_than_passives(self):
        ics, _ = _place([{"ref": f"U{i}"} for i in range(6)], board_width_mm=30, board_height_mm=30)
        passives, _ = _place(
            [{"ref": f"R{i}"} for i in range(6)], board_width_mm=30, board_height_mm=30
        )
        assert self._min_pitch(ics) > self._min_pitch(passives)


class TestUnresolvablePinNamesAreExplained:
    """Net assignment emits one "no pad 'VOUT'" error per pin when a map is
    missing. Read alone those say the caller's net names are wrong, and a
    model will rewrite a correct netlist chasing them. The cause has to be
    stated wherever it arises, not only when a fetch fails."""

    @pytest.mark.asyncio
    async def test_auto_fetch_disabled_is_named_as_the_cause(self):
        maps, warnings = await _resolve_pinmaps(
            [{"ref": "U1", "lcsc": "C82942"}],
            auto_fetch=False,
            force_refresh=False,
            needed_refs={"U1"},
        )
        assert maps["U1"] == {}
        assert any("auto_fetch_pinmaps is off" in w for w in warnings)
        assert any("no pad" in w for w in warnings), "must connect cause to effect"

    @pytest.mark.asyncio
    async def test_a_missing_lcsc_field_is_named_as_the_cause(self):
        _, warnings = await _resolve_pinmaps(
            [{"ref": "U2"}],
            auto_fetch=True,
            force_refresh=False,
            needed_refs={"U2"},
        )
        assert any("no 'lcsc' field" in w for w in warnings)

    @pytest.mark.asyncio
    async def test_parts_wired_by_pad_number_are_not_warned_about(self):
        """Only refs that actually need a name map are a problem."""
        _, warnings = await _resolve_pinmaps(
            [{"ref": "C1", "lcsc": "C1525"}, {"ref": "R1"}],
            auto_fetch=False,
            force_refresh=False,
            needed_refs=set(),
        )
        assert warnings == []

    @pytest.mark.asyncio
    async def test_an_explicit_pinmap_silences_it(self):
        _, warnings = await _resolve_pinmaps(
            [{"ref": "U1", "pinmap": {"VOUT": "5"}}],
            auto_fetch=False,
            force_refresh=False,
            needed_refs={"U1"},
        )
        assert warnings == []


class TestPinNameAliases:
    """A datasheet says GPIO10 and EasyEDA says IO10. A spec written from
    the datasheet — which is how anyone writes one — failed on every GPIO
    on the part. These are naming conventions, not typos, so matching folds
    them together rather than demanding one spelling."""

    PINMAP = {"IO10": "10", "IO2": "2", "VIN": "1", "VOUT": "2", "GND": "3", "3V3": "5"}

    @pytest.mark.parametrize(
        "written,expected",
        [
            ("GPIO10", "10"),
            ("IO10", "10"),
            ("gpio2", "2"),
            ("VI", "1"),
            ("VO", "2"),
            ("vin", "1"),
            ("GND", "3"),
            ("3V3", "5"),
        ],
    )
    def test_common_spellings_all_reach_the_right_pad(self, written, expected):
        assert _resolve_pad(self.PINMAP, written) == expected

    def test_a_bare_pad_number_passes_through(self):
        """Nets that already use pad numbers must not be rewritten."""
        assert _resolve_pad(self.PINMAP, "7") == "7"

    def test_an_unknown_name_passes_through_unchanged(self):
        assert _resolve_pad(self.PINMAP, "NOSUCHPIN") == "NOSUCHPIN"

    def test_separators_are_ignored(self):
        assert _resolve_pad({"IO_10": "10"}, "GPIO10") == "10"

    def test_an_exact_match_always_wins(self):
        """Aliasing must never override a name the part actually has."""
        pinmap = {"VIN": "1", "VI": "9"}
        assert _resolve_pad(pinmap, "VI") == "9"

    def test_gpio_prefix_only_folds_for_numeric_pins(self):
        assert _normalize_pin("GPIOCLK") == "gpioclk"
        assert _normalize_pin("GPIO10") == "io10"
