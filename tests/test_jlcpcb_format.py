"""Tests for the KiCad -> JLCPCB column conversions.

These files are the difference between an order that goes through and one
that comes back rejected, so the column names are asserted literally.
"""

from __future__ import annotations

import csv

import pytest

from kicad_jlcpcb_mcp.jlcpcb_format import (
    BOM_COLUMNS,
    CPL_COLUMNS,
    JlcpcbFormatError,
    convert_bom,
    convert_cpl,
    write_bom,
)

# Verbatim from `kicad-cli pcb export pos --format csv` on KiCad 10.0.5.
KICAD_POS = (
    "Ref,Val,Package,PosX,PosY,Rot,Side\n"
    '"C1","100nF","C_0402_1005Metric",10.000000,-40.000000,0.000000,top\n'
    '"R1","10k","R_0603_1608Metric",17.000000,-40.000000,90.000000,top\n'
    '"U1","LDO","SOT-23-5",25.500000,-33.250000,270.000000,bottom\n'
)


def _rows(path):
    with open(path, newline="") as f:
        return list(csv.reader(f))


class TestConvertCpl:
    def test_header_is_exactly_what_jlcpcb_expects(self, tmp_path):
        src = tmp_path / "pos.csv"
        src.write_text(KICAD_POS)
        convert_cpl(src, tmp_path / "cpl.csv")
        assert _rows(tmp_path / "cpl.csv")[0] == list(CPL_COLUMNS)

    def test_kicad_column_names_do_not_survive(self, tmp_path):
        src = tmp_path / "pos.csv"
        src.write_text(KICAD_POS)
        convert_cpl(src, tmp_path / "cpl.csv")
        text = (tmp_path / "cpl.csv").read_text()
        for stale in ("PosX", "PosY", "Rot,", "Side", "Package"):
            assert stale not in text

    def test_every_part_is_carried_over(self, tmp_path):
        src = tmp_path / "pos.csv"
        src.write_text(KICAD_POS)
        result = convert_cpl(src, tmp_path / "cpl.csv")
        assert result["rows"] == 3
        designators = [r[0] for r in _rows(tmp_path / "cpl.csv")[1:]]
        assert designators == ["C1", "R1", "U1"]

    def test_side_is_capitalised_the_way_jlcpcb_wants(self, tmp_path):
        src = tmp_path / "pos.csv"
        src.write_text(KICAD_POS)
        convert_cpl(src, tmp_path / "cpl.csv")
        layers = {r[3] for r in _rows(tmp_path / "cpl.csv")[1:]}
        assert layers == {"Top", "Bottom"}

    def test_rotation_is_preserved_verbatim(self, tmp_path):
        """We deliberately do not 'correct' rotation — see the module
        docstring. Silently rotating parts would be worse than a warning."""
        src = tmp_path / "pos.csv"
        src.write_text(KICAD_POS)
        convert_cpl(src, tmp_path / "cpl.csv")
        rots = [r[4] for r in _rows(tmp_path / "cpl.csv")[1:]]
        assert rots == ["0.000000", "90.000000", "270.000000"]

    def test_warns_about_rotation(self, tmp_path):
        src = tmp_path / "pos.csv"
        src.write_text(KICAD_POS)
        result = convert_cpl(src, tmp_path / "cpl.csv")
        assert any("assembly preview" in w for w in result["warnings"])

    def test_accepts_alternate_kicad_header_spellings(self, tmp_path):
        src = tmp_path / "pos.csv"
        src.write_text("Designator,X,Y,Rotation,Layer\nR1,1.0,2.0,90,bottom\n")
        convert_cpl(src, tmp_path / "cpl.csv")
        assert _rows(tmp_path / "cpl.csv")[1] == ["R1", "1.0", "2.0", "Bottom", "90"]

    def test_unrecognised_columns_fail_loudly(self, tmp_path):
        """Shipping a CPL we could not parse is worse than failing: the
        order is accepted and the parts go on wrong."""
        src = tmp_path / "pos.csv"
        src.write_text("Alpha,Beta,Gamma\n1,2,3\n")
        with pytest.raises(JlcpcbFormatError, match="format changed"):
            convert_cpl(src, tmp_path / "cpl.csv")

    def test_empty_file_fails_loudly(self, tmp_path):
        src = tmp_path / "pos.csv"
        src.write_text("")
        with pytest.raises(JlcpcbFormatError, match="empty"):
            convert_cpl(src, tmp_path / "cpl.csv")

    def test_rows_without_a_designator_are_dropped(self, tmp_path):
        src = tmp_path / "pos.csv"
        src.write_text("Ref,PosX,PosY,Rot,Side\n,1,2,0,top\nR1,3,4,0,top\n")
        assert convert_cpl(src, tmp_path / "cpl.csv")["rows"] == 1


class TestWriteBom:
    COMPONENTS = [
        {"ref": "R1", "value": "10k", "lcsc": "C25804", "lib": "Resistor_SMD", "fp": "R_0603"},
        {"ref": "R2", "value": "10k", "lcsc": "C25804", "lib": "Resistor_SMD", "fp": "R_0603"},
        {"ref": "C1", "value": "100nF", "lcsc": "C1525", "lib": "Capacitor_SMD", "fp": "C_0402"},
    ]

    def test_header_is_exactly_what_jlcpcb_expects(self, tmp_path):
        write_bom(self.COMPONENTS, tmp_path / "bom.csv")
        assert _rows(tmp_path / "bom.csv")[0] == list(BOM_COLUMNS)

    def test_identical_parts_share_one_row_with_joined_designators(self, tmp_path):
        """JLCPCB expects one row per unique part, not per placement."""
        result = write_bom(self.COMPONENTS, tmp_path / "bom.csv")
        assert result["rows"] == 2
        assert result["parts"] == 3
        rows = {r[3]: r for r in _rows(tmp_path / "bom.csv")[1:]}
        assert rows["C25804"][1] == "R1,R2"

    def test_lcsc_number_lands_in_the_part_column(self, tmp_path):
        write_bom(self.COMPONENTS, tmp_path / "bom.csv")
        lcsc_col = [r[3] for r in _rows(tmp_path / "bom.csv")[1:]]
        assert set(lcsc_col) == {"C25804", "C1525"}

    def test_missing_lcsc_is_warned_not_silently_shipped(self, tmp_path):
        """JLCPCB cannot source a line with no part number; it just leaves
        the pads unpopulated."""
        comps = [{"ref": "R1", "value": "10k", "lib": "Resistor_SMD", "fp": "R_0603"}]
        result = write_bom(comps, tmp_path / "bom.csv")
        assert any("no LCSC part number" in w for w in result["warnings"])
        assert "R1" in result["warnings"][0]

    def test_footprint_is_lib_colon_fp(self, tmp_path):
        write_bom(self.COMPONENTS, tmp_path / "bom.csv")
        fps = {r[2] for r in _rows(tmp_path / "bom.csv")[1:]}
        assert "Resistor_SMD:R_0603" in fps

    def test_empty_component_list(self, tmp_path):
        result = write_bom([], tmp_path / "bom.csv")
        assert result["rows"] == 0
        assert _rows(tmp_path / "bom.csv") == [list(BOM_COLUMNS)]


class TestConvertBom:
    """kicad-cli writes whatever `--fields` asked for; JLCPCB keys on
    Comment, Designator and an LCSC column."""

    # Verbatim header from this plugin's `sch export bom` invocation.
    KICAD_BOM = (
        '"Reference","Value","Footprint","LCSC","Manufacturer","MPN","Description"\n'
        '"R1","10k","Resistor_SMD:R_0603_1608Metric","C25804","UNI-ROYAL","x","res"\n'
        '"C1","100nF","Capacitor_SMD:C_0402_1005Metric","C1525","Samsung","y","cap"\n'
    )

    def test_header_is_exactly_what_jlcpcb_expects(self, tmp_path):
        src = tmp_path / "bom.csv"
        src.write_text(self.KICAD_BOM)
        convert_bom(src, tmp_path / "out.csv")
        assert _rows(tmp_path / "out.csv")[0] == list(BOM_COLUMNS)

    def test_maps_value_to_comment_and_reference_to_designator(self, tmp_path):
        src = tmp_path / "bom.csv"
        src.write_text(self.KICAD_BOM)
        convert_bom(src, tmp_path / "out.csv")
        rows = {r[1]: r for r in _rows(tmp_path / "out.csv")[1:]}
        assert rows["R1"][0] == "10k"
        assert rows["R1"][3] == "C25804"

    def test_kicad_only_columns_are_dropped(self, tmp_path):
        src = tmp_path / "bom.csv"
        src.write_text(self.KICAD_BOM)
        convert_bom(src, tmp_path / "out.csv")
        text = (tmp_path / "out.csv").read_text()
        assert "Manufacturer" not in text and "MPN" not in text

    def test_a_bom_with_no_lcsc_column_is_called_out(self, tmp_path):
        """JLCPCB has nothing to source from — worth saying loudly rather
        than shipping a BOM that quietly populates nothing."""
        src = tmp_path / "bom.csv"
        src.write_text('"Reference","Value"\n"R1","10k"\n')
        result = convert_bom(src, tmp_path / "out.csv")
        assert any("nothing to source" in w for w in result["warnings"])

    def test_individual_missing_part_numbers_are_listed(self, tmp_path):
        src = tmp_path / "bom.csv"
        src.write_text('"Reference","Value","LCSC"\n"R1","10k",""\n"C1","1u","C1525"\n')
        result = convert_bom(src, tmp_path / "out.csv")
        assert any("R1" in w for w in result["warnings"])

    def test_unrecognised_columns_fail_loudly(self, tmp_path):
        src = tmp_path / "bom.csv"
        src.write_text("Alpha,Beta\n1,2\n")
        with pytest.raises(JlcpcbFormatError, match="format changed"):
            convert_bom(src, tmp_path / "out.csv")

    def test_empty_file_fails_loudly(self, tmp_path):
        src = tmp_path / "bom.csv"
        src.write_text("")
        with pytest.raises(JlcpcbFormatError, match="empty"):
            convert_bom(src, tmp_path / "out.csv")
