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
        # JLCPCB documents .G1/.G2 for a 4-layer stack, which is also what
        # KiCad writes natively. The old mapping said "G2L" — a
        # transposition of Altium's GL2 — which JLCPCB does not recognise.
        assert _classify_gerber("demo-In1_Cu.gbr") == ("demo", "G1")
        assert _classify_gerber("demo-In2_Cu.gbr") == ("demo", "G2")

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


class TestMultilayerBoards:
    """A 4-layer board used to ship with no inner copper at all.

    Two independent causes: `package_for_jlcpcb` exported a fixed two-layer
    set, and even when inner gerbers were present the filename matcher did
    not accept KiCad's `.g1`/`.g2` extensions. The zip uploaded, the order
    was accepted, and the board came back with every inner-layer net gone.
    """

    def _plant(self, d, stem, layers):
        d.mkdir(parents=True, exist_ok=True)
        for layer, ext in layers:
            (d / f"{stem}-{layer}.{ext}").write_text("G04*\nM02*\n")
        (d / f"{stem}.drl").write_text("M48\nM30\n")

    FOUR_LAYER = [
        ("F_Cu", "gtl"),
        ("In1_Cu", "g1"),
        ("In2_Cu", "g2"),
        ("B_Cu", "gbl"),
        ("F_Paste", "gtp"),
        ("B_Paste", "gbp"),
        ("F_Silkscreen", "gto"),
        ("B_Silkscreen", "gbo"),
        ("F_Mask", "gts"),
        ("B_Mask", "gbs"),
        ("Edge_Cuts", "gm1"),
    ]

    def test_inner_copper_reaches_the_zip(self, tmp_path):
        import zipfile

        src = tmp_path / "g"
        self._plant(src, "board", self.FOUR_LAYER)
        result = pack_for_jlcpcb(src, src, tmp_path / "out.zip", project_name="board")
        names = set(zipfile.ZipFile(result.zip_path).namelist())
        assert "board.G1" in names, "In1.Cu missing from a 4-layer zip"
        assert "board.G2" in names, "In2.Cu missing from a 4-layer zip"

    def test_kicad_g1_g2_extensions_are_recognised(self):
        """KiCad writes `<stem>-In1_Cu.g1`, not `.g2l`."""
        assert _classify_gerber("board-In1_Cu.g1") == ("board", "G1")
        assert _classify_gerber("board-In2_Cu.g2") == ("board", "G2")

    def test_six_layer_inner_copper(self):
        assert _classify_gerber("board-In3_Cu.g3") == ("board", "G3")
        assert _classify_gerber("board-In4_Cu.g4") == ("board", "G4")

    def test_two_layer_board_is_unaffected(self, tmp_path):
        import zipfile

        two = [le for le in self.FOUR_LAYER if not le[0].startswith("In")]
        src = tmp_path / "g"
        self._plant(src, "board", two)
        result = pack_for_jlcpcb(src, src, tmp_path / "out.zip", project_name="board")
        names = set(zipfile.ZipFile(result.zip_path).namelist())
        assert not any(n.endswith((".G1", ".G2")) for n in names)
        assert "board.GTL" in names and "board.GBL" in names


class TestExpectedCopperVerification:
    """`pack_for_jlcpcb` only ever sees a directory of files, so it cannot
    know on its own that a board had four copper layers. The caller passes
    the expected set, which closes the loop: export the right layers, then
    verify they all arrived."""

    def _plant(self, d, stem, layers):
        d.mkdir(parents=True, exist_ok=True)
        for layer, ext in layers:
            (d / f"{stem}-{layer}.{ext}").write_text("G04*\nM02*\n")
        (d / f"{stem}.drl").write_text("M48\nM30\n")

    TWO_LAYER = [
        ("F_Cu", "gtl"),
        ("B_Cu", "gbl"),
        ("F_Paste", "gtp"),
        ("B_Paste", "gbp"),
        ("F_Silkscreen", "gto"),
        ("B_Silkscreen", "gbo"),
        ("F_Mask", "gts"),
        ("B_Mask", "gbs"),
        ("Edge_Cuts", "gm1"),
    ]

    def test_missing_inner_copper_is_a_loud_warning(self, tmp_path):
        """The exact 0.9.0 bug: a 4-layer board whose inner gerbers never
        got exported produces a zip that looks complete."""
        src = tmp_path / "g"
        self._plant(src, "board", self.TWO_LAYER)
        result = pack_for_jlcpcb(
            src,
            src,
            tmp_path / "out.zip",
            project_name="board",
            expected_copper=["GTL", "G1", "G2", "GBL"],
        )
        joined = " ".join(result.warnings)
        assert "G1" in joined and "G2" in joined
        assert "Do NOT order" in joined

    def test_no_warning_when_every_copper_layer_arrived(self, tmp_path):
        src = tmp_path / "g"
        self._plant(
            src,
            "board",
            self.TWO_LAYER + [("In1_Cu", "g1"), ("In2_Cu", "g2")],
        )
        result = pack_for_jlcpcb(
            src,
            src,
            tmp_path / "out.zip",
            project_name="board",
            expected_copper=["GTL", "G1", "G2", "GBL"],
        )
        assert not any("did not make it" in w for w in result.warnings)

    def test_two_layer_board_needs_no_expectation(self, tmp_path):
        src = tmp_path / "g"
        self._plant(src, "board", self.TWO_LAYER)
        result = pack_for_jlcpcb(
            src,
            src,
            tmp_path / "out.zip",
            project_name="board",
            expected_copper=["GTL", "GBL"],
        )
        assert not any("did not make it" in w for w in result.warnings)


class TestSplitDrillFiles:
    """Shipping one of a split PTH/NPTH pair is a quiet way to ruin a board.

    The old code took the alphabetically-first `.drl`, which is the NPTH
    one, so the zip carried only the non-plated holes — the board came back
    with every via and every plated through-hole missing — while a warning
    claimed it had "used the merged file".
    """

    def _plant(self, d, drills):
        d.mkdir(parents=True, exist_ok=True)
        for layer, ext in [
            ("F_Cu", "gtl"),
            ("B_Cu", "gbl"),
            ("F_Paste", "gtp"),
            ("B_Paste", "gbp"),
            ("F_Silkscreen", "gto"),
            ("B_Silkscreen", "gbo"),
            ("F_Mask", "gts"),
            ("B_Mask", "gbs"),
            ("Edge_Cuts", "gm1"),
        ]:
            (d / f"b-{layer}.{ext}").write_text("G04*\nM02*\n")
        for name in drills:
            (d / name).write_text("M48\nM30\n")
        return d

    def _xln(self, zip_path):
        import zipfile

        return sorted(n for n in zipfile.ZipFile(zip_path).namelist() if n.endswith(".XLN"))

    def test_merged_drill_is_used_alone(self, tmp_path):
        src = self._plant(tmp_path / "g", ["b.drl"])
        result = pack_for_jlcpcb(src, src, tmp_path / "o.zip", project_name="b")
        assert self._xln(result.zip_path) == ["b.XLN"]

    def test_both_files_ship_when_only_a_split_pair_exists(self, tmp_path):
        src = self._plant(tmp_path / "g", ["b-PTH.drl", "b-NPTH.drl"])
        result = pack_for_jlcpcb(src, src, tmp_path / "o.zip", project_name="b")
        assert self._xln(result.zip_path) == ["b-NPTH.XLN", "b-PTH.XLN"]

    def test_the_plated_holes_are_never_the_ones_dropped(self, tmp_path):
        """The specific regression: NPTH sorts first alphabetically."""
        src = self._plant(tmp_path / "g", ["b-PTH.drl", "b-NPTH.drl"])
        result = pack_for_jlcpcb(src, src, tmp_path / "o.zip", project_name="b")
        assert any(n.endswith("-PTH.XLN") for n in self._xln(result.zip_path))

    def test_a_split_pair_is_reported_not_silently_accepted(self, tmp_path):
        src = self._plant(tmp_path / "g", ["b-PTH.drl", "b-NPTH.drl"])
        result = pack_for_jlcpcb(src, src, tmp_path / "o.zip", project_name="b")
        joined = " ".join(result.warnings)
        assert "split drill files" in joined
        assert "shipped both" in joined

    def test_a_merged_file_wins_over_a_split_pair(self, tmp_path):
        src = self._plant(tmp_path / "g", ["b.drl", "b-PTH.drl", "b-NPTH.drl"])
        result = pack_for_jlcpcb(src, src, tmp_path / "o.zip", project_name="b")
        assert self._xln(result.zip_path) == ["b.XLN"]

    def test_no_drill_at_all_is_an_explicit_warning(self, tmp_path):
        src = self._plant(tmp_path / "g", [])
        result = pack_for_jlcpcb(src, src, tmp_path / "o.zip", project_name="b")
        assert any("drill" in w.lower() for w in result.warnings)
