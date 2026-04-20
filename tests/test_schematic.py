"""Tests for the schematic generator."""

import pytest

from kicad_jlcpcb_mcp.schematic import (
    SchematicError,
    _validate,
    sch_generate,
)
from kicad_jlcpcb_mcp.sexpr import parse


def make_simple_spec() -> dict:
    """Two-component RC: a 10k resistor and a 100nF cap, both connected
    between VCC and GND. Enough to exercise validation, layout, and net
    emission without being so large the goldens are unmanageable."""
    return {
        "components": [
            {
                "ref": "R1",
                "value": "10k",
                "lcsc": "C25804",
                "footprint": "kicad_jlcpcb:C25804_0603",
                "pin_count": 2,
            },
            {
                "ref": "C1",
                "value": "100nF",
                "lcsc": "C49678",
                "footprint": "kicad_jlcpcb:C49678_0603",
                "pin_count": 2,
            },
        ],
        "nets": [
            {"name": "VCC", "connects": [["R1", "1"], ["C1", "1"]]},
            {"name": "GND", "connects": [["R1", "2"], ["C1", "2"]]},
        ],
    }


class TestValidate:
    def test_valid_spec(self):
        comps, nets = _validate(make_simple_spec())
        assert len(comps) == 2
        assert len(nets) == 2
        assert comps[0].ref == "R1"

    def test_rejects_non_dict(self):
        with pytest.raises(SchematicError, match="must be a dict"):
            _validate("nope")

    def test_rejects_missing_components(self):
        with pytest.raises(SchematicError, match="components"):
            _validate({"nets": []})

    def test_rejects_empty_components(self):
        with pytest.raises(SchematicError, match="components"):
            _validate({"components": []})

    def test_rejects_duplicate_refs(self):
        spec = {
            "components": [
                {"ref": "R1", "value": "10k"},
                {"ref": "R1", "value": "1k"},
            ]
        }
        with pytest.raises(SchematicError, match="Duplicate"):
            _validate(spec)

    def test_rejects_net_with_unknown_ref(self):
        spec = {
            "components": [{"ref": "R1", "value": "10k"}],
            "nets": [{"name": "VCC", "connects": [["R1", "1"], ["UNKNOWN", "1"]]}],
        }
        with pytest.raises(SchematicError, match="unknown component"):
            _validate(spec)

    def test_rejects_malformed_net_connect(self):
        spec = {
            "components": [{"ref": "R1", "value": "10k"}],
            "nets": [{"name": "VCC", "connects": [["R1"]]}],
        }
        with pytest.raises(SchematicError, match=r"\[ref, pin\]"):
            _validate(spec)

    def test_rejects_zero_pin_count(self):
        spec = {"components": [{"ref": "R1", "value": "10k", "pin_count": 0}]}
        with pytest.raises(SchematicError, match="pin_count"):
            _validate(spec)


class TestSchGenerate:
    def test_writes_file(self, tmp_path):
        out = tmp_path / "demo.kicad_sch"
        result = sch_generate(out, "demo", make_simple_spec())
        assert out.is_file()
        assert result["sch_path"] == str(out)
        assert result["component_count"] == 2
        assert result["net_count"] == 2
        assert result["warnings"] == []

    def test_output_starts_with_kicad_sch(self, tmp_path):
        out = tmp_path / "demo.kicad_sch"
        sch_generate(out, "demo", make_simple_spec())
        text = out.read_text()
        assert text.startswith("(kicad_sch")
        assert "(version 20231120)" in text

    def test_output_is_parseable_sexpr(self, tmp_path):
        out = tmp_path / "demo.kicad_sch"
        sch_generate(out, "demo", make_simple_spec())
        # Strip the comment line our net block emits before parsing
        text = "\n".join(
            line for line in out.read_text().splitlines() if not line.lstrip().startswith(";")
        )
        node = parse(text)
        assert node[0] == "kicad_sch"
        # version + generator + uuid + paper + title_block + lib_symbols + 2 components + 2 nets + sheet_instances
        # Each component emits one (symbol ...) at the top level
        symbol_instances = [
            c
            for c in node
            if isinstance(c, list)
            and c
            and c[0] == "symbol"
            and isinstance(c[1], list)
            and c[1][0] == "lib_id"
        ]
        assert len(symbol_instances) == 2

    def test_embeds_lcsc_field_per_component(self, tmp_path):
        out = tmp_path / "demo.kicad_sch"
        sch_generate(out, "demo", make_simple_spec())
        text = out.read_text()
        assert '(property "LCSC" "C25804"' in text
        assert '(property "LCSC" "C49678"' in text

    def test_embeds_net_blocks(self, tmp_path):
        out = tmp_path / "demo.kicad_sch"
        sch_generate(out, "demo", make_simple_spec())
        text = out.read_text()
        assert '(net (name "VCC")' in text
        assert '(net (name "GND")' in text
        assert '(member "R1" "1")' in text

    def test_warns_on_components_without_lcsc(self, tmp_path):
        out = tmp_path / "demo.kicad_sch"
        spec = {
            "components": [
                {"ref": "R1", "value": "10k"},
                {"ref": "C1", "value": "100nF", "lcsc": "C49678"},
            ]
        }
        result = sch_generate(out, "demo", spec)
        assert len(result["warnings"]) == 1
        assert "R1" in result["warnings"][0]

    def test_does_not_write_on_validation_failure(self, tmp_path):
        out = tmp_path / "should_not_exist.kicad_sch"
        with pytest.raises(SchematicError):
            sch_generate(out, "demo", {"components": []})
        assert not out.exists()

    def test_creates_parent_directory(self, tmp_path):
        out = tmp_path / "nested" / "sub" / "demo.kicad_sch"
        sch_generate(out, "demo", make_simple_spec())
        assert out.is_file()
