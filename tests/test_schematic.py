"""Tests for the schematic generator."""

import os
import re
import subprocess

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
        assert "(version 20250114)" in text

    def test_output_contains_no_comments(self):
        """KiCad's S-expression grammar has no comment syntax.

        A `;` line anywhere in the file makes KiCad refuse to load it with a
        bare "Failed to load schematic". The old generator emitted one above
        its net block, and the round-trip test below passed anyway because
        our own tokenizer accepts `;` as a bare atom — so nothing caught it.
        """
        import inspect

        from kicad_jlcpcb_mcp import schematic

        assert '";' not in inspect.getsource(schematic)

    def test_output_has_no_comment_lines(self, tmp_path):
        out = tmp_path / "demo.kicad_sch"
        sch_generate(out, "demo", make_simple_spec())
        for line in out.read_text().splitlines():
            assert not line.lstrip().startswith(";"), f"comment line: {line!r}"

    def test_output_is_parseable_sexpr(self, tmp_path):
        out = tmp_path / "demo.kicad_sch"
        sch_generate(out, "demo", make_simple_spec())
        text = out.read_text()
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

    def test_expresses_nets_as_global_labels(self, tmp_path):
        """Connectivity is carried by global labels, not `(net ...)` nodes.

        `(net (name ...) (member ...))` is not valid `kicad_sch` grammar —
        KiCad refuses to load a file containing it. Two global labels
        sharing a name are one net, which gives correct connectivity
        without solving schematic wire routing.
        """
        out = tmp_path / "demo.kicad_sch"
        sch_generate(out, "demo", make_simple_spec())
        text = out.read_text()
        assert '(global_label "VCC"' in text
        assert '(global_label "GND"' in text
        assert "(net (name" not in text
        assert "(member " not in text

    def test_symbol_instances_carry_an_instances_block(self, tmp_path):
        """KiCad 7+ needs `instances` or the reference designator won't stick."""
        out = tmp_path / "demo.kicad_sch"
        sch_generate(out, "demo", make_simple_spec())
        text = out.read_text()
        assert "(instances" in text
        assert '(reference "R1")' in text

    def test_every_coordinate_is_on_the_connection_grid(self, tmp_path):
        """Off-grid pins load fine and then silently refuse to connect.

        KiCad flags them as `endpoint_off_grid` and the nets just don't
        exist, so the schematic looks correct and isn't.
        """
        import re

        out = tmp_path / "demo.kicad_sch"
        sch_generate(out, "demo", make_simple_spec())
        for x, y in re.findall(r"\(at (-?[\d.]+) (-?[\d.]+)", out.read_text()):
            for v in (float(x), float(y)):
                assert abs(round(v / 1.27) * 1.27 - v) < 1e-6, f"{v} is off the 1.27mm grid"

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


@pytest.mark.skipif(
    os.environ.get("KICAD_INSTALLED") != "1",
    reason="requires kicad-cli (set KICAD_INSTALLED=1)",
)
class TestKicadActuallyLoadsIt:
    """Hand the generated file to KiCad and see what it says.

    Every other test in this file inspects our own output with our own
    parser, which is how a schematic that KiCad flatly refused to load
    ("Failed to load schematic", no detail) shipped with 16 green tests.
    Only kicad-cli can answer whether the file is valid.
    """

    def _write(self, tmp_path, spec, name="probe"):
        out = tmp_path / f"{name}.kicad_sch"
        sch_generate(out, name, spec)
        return out

    def _run(self, *args):
        return subprocess.run(
            ["kicad-cli", "sch", *args], capture_output=True, text=True, timeout=120
        )

    def test_kicad_loads_the_schematic(self, tmp_path):
        out = self._write(tmp_path, make_simple_spec())
        r = self._run("erc", "--output", str(tmp_path / "erc.rpt"), str(out))
        assert "Failed to load schematic" not in (r.stdout + r.stderr), r.stdout + r.stderr

    def test_netlist_connectivity_matches_the_spec(self, tmp_path):
        """The real contract: the nets KiCad computes are the nets we asked for."""
        spec = {
            "components": [
                {
                    "ref": "U1",
                    "value": "LDO",
                    "lcsc": "C82942",
                    "pin_count": 3,
                    "pins": [
                        {"num": "1", "name": "VIN"},
                        {"num": "2", "name": "GND"},
                        {"num": "3", "name": "VOUT"},
                    ],
                },
                {"ref": "C1", "value": "100nF", "lcsc": "C1525", "pin_count": 2},
            ],
            "nets": [
                {"name": "VBUS", "connects": [["U1", "VIN"], ["C1", "1"]]},
                {"name": "AGND", "connects": [["U1", "GND"], ["C1", "2"]]},
            ],
        }
        out = self._write(tmp_path, spec)
        net_path = tmp_path / "probe.net"
        r = self._run("export", "netlist", "--output", str(net_path), str(out))
        assert "Failed to load schematic" not in (r.stdout + r.stderr), r.stdout + r.stderr

        text = net_path.read_text()
        nets: dict[str, set[str]] = {}
        for chunk in text[text.index("(nets") :].split("(net\n")[1:]:
            m = re.search(r'\(name "([^"]*)"\)', chunk)
            if not m:
                continue
            nodes = re.findall(r'\(ref "([^"]+)"\)\s*\n\s*\(pin "([^"]+)"\)', chunk)
            nets[m.group(1)] = {f"{ref}.{pin}" for ref, pin in nodes}

        assert nets.get("VBUS") == {"U1.1", "C1.1"}, nets
        assert nets.get("AGND") == {"U1.2", "C1.2"}, nets

    def test_no_off_grid_endpoints(self, tmp_path):
        out = self._write(tmp_path, make_simple_spec())
        rpt = tmp_path / "erc.rpt"
        self._run("erc", "--output", str(rpt), str(out))
        assert "endpoint_off_grid" not in rpt.read_text()
