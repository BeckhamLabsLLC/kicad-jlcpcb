"""Tests for project workspace management."""

import json

import pytest

from kicad_jlcpcb_mcp.project import (
    Project,
    ProjectError,
    create_project,
    load_project,
    manifest,
)


class TestCreateProject:
    def test_creates_directory_and_files(self, tmp_path):
        proj = create_project(tmp_path, "test_board")
        assert isinstance(proj, Project)
        assert proj.root == tmp_path / "test_board"
        assert proj.pro_path.is_file()
        assert proj.sch_path.is_file()
        assert proj.libs_dir.is_dir()
        assert proj.manufacturing_dir.is_dir()
        # PCB is intentionally not created at scaffold time
        assert not proj.pcb_path.exists()

    def test_pro_manifest_has_jlcpcb_rules(self, tmp_path):
        proj = create_project(tmp_path, "demo")
        data = json.loads(proj.pro_path.read_text())
        rules = data["board"]["design_settings"]["rules"]
        assert rules["min_clearance"] == 0.127
        assert rules["min_track_width"] == 0.127

    def test_sch_skeleton_is_kicad_sexpr(self, tmp_path):
        proj = create_project(tmp_path, "demo")
        sch = proj.sch_path.read_text()
        assert sch.startswith("(kicad_sch")
        assert "(version 20231120)" in sch
        assert '(title "demo")' in sch

    def test_rejects_existing_dir(self, tmp_path):
        (tmp_path / "exists").mkdir()
        with pytest.raises(ProjectError, match="already exists"):
            create_project(tmp_path, "exists")

    def test_rejects_invalid_name(self, tmp_path):
        for bad in ["", "with space", "../escape", "1-leading-digit-ok-actually"]:
            # Last one is actually allowed (starts with digit) — pop it
            pass
        for bad in ["", "with space", "../escape", "-leading-dash"]:
            with pytest.raises(ProjectError, match="Invalid project name"):
                create_project(tmp_path, bad)

    def test_accepts_alphanumeric_names(self, tmp_path):
        for good in ["proj", "Proj1", "my_board", "esp32-dev", "v2"]:
            proj = create_project(tmp_path, good)
            assert proj.root.is_dir()

    def test_rejects_missing_parent(self, tmp_path):
        with pytest.raises(ProjectError, match="does not exist"):
            create_project(tmp_path / "nonexistent", "x")


class TestLoadProject:
    def test_loads_from_pro_file(self, tmp_path):
        created = create_project(tmp_path, "demo")
        loaded = load_project(created.pro_path)
        assert loaded.name == "demo"
        assert loaded.pro_path == created.pro_path

    def test_loads_from_directory(self, tmp_path):
        created = create_project(tmp_path, "demo")
        loaded = load_project(created.root)
        assert loaded.name == "demo"

    def test_rejects_non_project(self, tmp_path):
        with pytest.raises(ProjectError, match="Not a KiCad project"):
            load_project(tmp_path / "nope.txt")

    def test_rejects_directory_without_pro(self, tmp_path):
        empty = tmp_path / "empty"
        empty.mkdir()
        with pytest.raises(ProjectError, match="No .kicad_pro file found"):
            load_project(empty)

    def test_rejects_directory_with_multiple_pros(self, tmp_path):
        d = tmp_path / "multi"
        d.mkdir()
        (d / "a.kicad_pro").write_text("{}")
        (d / "b.kicad_pro").write_text("{}")
        with pytest.raises(ProjectError, match="Multiple .kicad_pro files"):
            load_project(d)

    def test_rejects_invalid_json(self, tmp_path):
        d = tmp_path / "bad"
        d.mkdir()
        (d / "broken.kicad_pro").write_text("{not json")
        with pytest.raises(ProjectError, match="not valid JSON"):
            load_project(d / "broken.kicad_pro")

    def test_rejects_too_new_schema(self, tmp_path):
        d = tmp_path / "future"
        d.mkdir()
        (d / "f.kicad_pro").write_text(json.dumps({"meta": {"version": 999}}))
        with pytest.raises(ProjectError, match="schema version 999"):
            load_project(d / "f.kicad_pro")

    def test_creates_missing_libs_and_manufacturing(self, tmp_path):
        # Simulate a project created by KiCad itself, without our subdirs
        d = tmp_path / "external"
        d.mkdir()
        (d / "external.kicad_pro").write_text(json.dumps({"meta": {"version": 1}}))
        proj = load_project(d / "external.kicad_pro")
        assert proj.libs_dir.is_dir()
        assert proj.manufacturing_dir.is_dir()


class TestManifest:
    def test_empty_project_manifest(self, tmp_path):
        proj = create_project(tmp_path, "demo")
        m = manifest(proj)
        assert m["name"] == "demo"
        assert m["has_sch"] is True
        assert m["has_pcb"] is False
        assert m["lib_file_count"] == 0

    def test_manifest_counts_lib_files(self, tmp_path):
        proj = create_project(tmp_path, "demo")
        (proj.libs_dir / "a.kicad_sym").write_text("(kicad_symbol_lib)")
        (proj.libs_dir / "b.pretty").mkdir()
        (proj.libs_dir / "b.pretty" / "footprint.kicad_mod").write_text("(footprint)")
        m = manifest(proj)
        assert m["lib_file_count"] == 2
