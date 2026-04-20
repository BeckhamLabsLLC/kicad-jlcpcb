"""Tests for pcb.py — spec validation, placement, BOM helper, and (when
KiCad is installed) real pcbnew board generation.

The pcbnew-dependent tests skip automatically unless KICAD_INSTALLED=1
is set in the environment, matching the pattern used by test_kicad_cli.
"""

import os

import pytest

from kicad_jlcpcb_mcp import pcb
from kicad_jlcpcb_mcp.pcb import (
    PcbGenerationError,
    _classify,
    _place,
    _validate_spec,
    bom_from_components,
    generate_pcb,
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
        positions = _place(
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
        positions = _place(
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
        assert any("has no pad" in e for e in result.errors)
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
