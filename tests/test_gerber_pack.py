"""Tests for the JLCPCB gerber packager."""

import datetime as dt
import zipfile
from pathlib import Path

import pytest

from kicad_jlcpcb_mcp.gerber_pack import (
    GerberPackError,
    _classify_gerber,
    _find_drill_file,
    default_zip_path,
    pack_for_jlcpcb,
)


def make_kicad_export(directory: Path, board_stem: str = "demo") -> None:
    """Create a fake set of KiCad-exported gerber files in a directory.

    Mimics the file naming kicad-cli pcb export gerbers produces.
    """
    layers = [
        "F_Cu",
        "B_Cu",
        "F_Silkscreen",
        "B_Silkscreen",
        "F_Mask",
        "B_Mask",
        "F_Paste",
        "B_Paste",
        "Edge_Cuts",
    ]
    for layer in layers:
        (directory / f"{board_stem}-{layer}.gbr").write_text(f"; gerber for {layer}\nG04*\nM02*\n")


def make_kicad_drill(directory: Path, board_stem: str = "demo") -> None:
    (directory / f"{board_stem}.drl").write_text("; drill file\nM48\nM30\n")


class TestClassifyGerber:
    def test_top_copper(self):
        assert _classify_gerber("demo-F_Cu.gbr") == ("demo", "GTL")

    def test_bottom_copper(self):
        assert _classify_gerber("demo-B_Cu.gbr") == ("demo", "GBL")

    def test_edge_cuts(self):
        assert _classify_gerber("demo-Edge_Cuts.gbr") == ("demo", "GM1")

    def test_inner_layer(self):
        assert _classify_gerber("demo-In1_Cu.gbr") == ("demo", "G2L")

    def test_unrecognized_returns_none(self):
        assert _classify_gerber("README.txt") is None

    def test_drill_file_not_classified(self):
        assert _classify_gerber("demo.drl") is None


class TestFindDrillFile:
    def test_finds_merged_drill(self, tmp_path):
        (tmp_path / "demo.drl").write_text("M48")
        assert _find_drill_file(tmp_path) == tmp_path / "demo.drl"

    def test_prefers_merged_over_split(self, tmp_path):
        (tmp_path / "demo-PTH.drl").write_text("M48")
        (tmp_path / "demo-NPTH.drl").write_text("M48")
        (tmp_path / "demo.drl").write_text("M48")
        assert _find_drill_file(tmp_path) == tmp_path / "demo.drl"

    def test_falls_back_to_split(self, tmp_path):
        (tmp_path / "demo-PTH.drl").write_text("M48")
        result = _find_drill_file(tmp_path)
        assert result is not None
        assert result.name == "demo-PTH.drl"

    def test_returns_none_when_empty(self, tmp_path):
        assert _find_drill_file(tmp_path) is None


class TestPackForJlcpcb:
    def test_happy_path(self, tmp_path):
        gerber_dir = tmp_path / "gerbers"
        gerber_dir.mkdir()
        make_kicad_export(gerber_dir)
        make_kicad_drill(gerber_dir)

        out = tmp_path / "out.zip"
        result = pack_for_jlcpcb(gerber_dir, gerber_dir, out, project_name="demo")

        assert out.is_file()
        assert result.warnings == []
        # 9 gerbers + 1 drill = 10 files
        assert len(result.files_packed) == 10

        with zipfile.ZipFile(out) as zf:
            names = set(zf.namelist())
        # Required Protel layers
        assert "demo.GTL" in names
        assert "demo.GBL" in names
        assert "demo.GTO" in names
        assert "demo.GBO" in names
        assert "demo.GTS" in names
        assert "demo.GBS" in names
        assert "demo.GM1" in names
        # Drill renamed to .XLN
        assert "demo.XLN" in names

    def test_missing_gerber_dir_raises(self, tmp_path):
        with pytest.raises(GerberPackError, match="Gerber directory not found"):
            pack_for_jlcpcb(tmp_path / "nope", tmp_path, tmp_path / "out.zip")

    def test_no_gerbers_raises(self, tmp_path):
        empty = tmp_path / "empty"
        empty.mkdir()
        with pytest.raises(GerberPackError, match="No recognizable Gerber"):
            pack_for_jlcpcb(empty, empty, tmp_path / "out.zip")

    def test_warns_on_missing_drill(self, tmp_path):
        gerber_dir = tmp_path / "g"
        gerber_dir.mkdir()
        make_kicad_export(gerber_dir)
        # No drill file
        result = pack_for_jlcpcb(gerber_dir, gerber_dir, tmp_path / "out.zip", project_name="demo")
        assert any("drill" in w.lower() for w in result.warnings)

    def test_warns_on_missing_layers(self, tmp_path):
        gerber_dir = tmp_path / "g"
        gerber_dir.mkdir()
        # Only top copper, missing the rest
        (gerber_dir / "demo-F_Cu.gbr").write_text("G04*\nM02*\n")
        make_kicad_drill(gerber_dir)
        result = pack_for_jlcpcb(gerber_dir, gerber_dir, tmp_path / "out.zip", project_name="demo")
        assert any("Missing typical JLCPCB layers" in w for w in result.warnings)

    def test_includes_cpl_when_provided(self, tmp_path):
        gerber_dir = tmp_path / "g"
        gerber_dir.mkdir()
        make_kicad_export(gerber_dir)
        make_kicad_drill(gerber_dir)
        cpl = tmp_path / "demo-pos.csv"
        cpl.write_text("Ref,Val,Pkg,X,Y,Rot,Side\n")

        out = tmp_path / "out.zip"
        pack_for_jlcpcb(gerber_dir, gerber_dir, out, project_name="demo", cpl_path=cpl)
        with zipfile.ZipFile(out) as zf:
            names = zf.namelist()
        assert any("cpl" in n for n in names)

    def test_includes_bom_when_provided(self, tmp_path):
        gerber_dir = tmp_path / "g"
        gerber_dir.mkdir()
        make_kicad_export(gerber_dir)
        make_kicad_drill(gerber_dir)
        bom = tmp_path / "demo-bom.csv"
        bom.write_text("Ref,Val,LCSC\n")

        out = tmp_path / "out.zip"
        pack_for_jlcpcb(gerber_dir, gerber_dir, out, project_name="demo", bom_path=bom)
        with zipfile.ZipFile(out) as zf:
            names = zf.namelist()
        assert any("bom" in n for n in names)

    def test_warns_on_missing_cpl(self, tmp_path):
        gerber_dir = tmp_path / "g"
        gerber_dir.mkdir()
        make_kicad_export(gerber_dir)
        make_kicad_drill(gerber_dir)
        result = pack_for_jlcpcb(
            gerber_dir,
            gerber_dir,
            tmp_path / "out.zip",
            project_name="demo",
            cpl_path=tmp_path / "missing.csv",
        )
        assert any("CPL" in w for w in result.warnings)

    def test_staging_dir_cleaned_up(self, tmp_path):
        gerber_dir = tmp_path / "g"
        gerber_dir.mkdir()
        make_kicad_export(gerber_dir)
        make_kicad_drill(gerber_dir)
        out = tmp_path / "out.zip"
        pack_for_jlcpcb(gerber_dir, gerber_dir, out, project_name="demo")
        # No leftover .staging directory
        staging = tmp_path / ".out.staging"
        assert not staging.exists()


class TestDefaultZipPath:
    def test_first_today(self, tmp_path):
        path = default_zip_path(tmp_path, "demo")
        today = dt.date.today().strftime("%Y%m%d")
        assert path.name == f"jlcpcb-demo-{today}.zip"

    def test_collides_appends_suffix(self, tmp_path):
        first = default_zip_path(tmp_path, "demo")
        first.touch()
        second = default_zip_path(tmp_path, "demo")
        assert second != first
        assert "-1" in second.name
