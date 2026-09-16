"""Tests for per-project session persistence."""

import json
from pathlib import Path

from kicad_jlcpcb_mcp.session import (
    SESSION_FILENAME,
    Session,
    _checkpoints_up_to,
    load_session,
    new_session,
    resume_summary,
    save_session,
    update_stage,
)


class TestNewSession:
    def test_initial_fields(self, tmp_path):
        sess = new_session(tmp_path, "demo")
        assert sess["schema_version"] == 1
        assert sess["project_name"] == "demo"
        assert sess["stage"] == "created"
        assert sess["created_at"] == sess["updated_at"]
        assert sess["checkpoints"]["project_created"] is True
        assert sess["checkpoints"]["pcb_generated"] is False
        assert sess["bom"] == []
        assert sess["spec"] == {}

    def test_resolves_relative_project_path(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        sess = new_session(".", "demo")
        # Path should be absolute after resolve()
        # Absolute, but a Windows path starts with a drive letter, not "/".
        assert Path(sess["project_path"]).is_absolute()


class TestSaveAndLoad:
    def test_round_trip(self, tmp_path):
        sess = new_session(tmp_path, "demo")
        path = save_session(sess)
        assert path == tmp_path / SESSION_FILENAME
        assert path.is_file()

        loaded = load_session(tmp_path)
        assert loaded is not None
        assert loaded["project_name"] == "demo"
        assert loaded["stage"] == "created"

    def test_save_refreshes_updated_at(self, tmp_path):
        sess = new_session(tmp_path, "demo")
        original_updated = sess["updated_at"]
        sess["stage"] = "parts_sourced"  # type: ignore[typeddict-item]
        # Force a clock tick by manipulating the timestamp
        sess["updated_at"] = "1970-01-01T00:00:00+00:00"
        save_session(sess)
        loaded = load_session(tmp_path)
        assert loaded is not None
        assert loaded["updated_at"] != "1970-01-01T00:00:00+00:00"
        assert loaded["updated_at"] >= original_updated

    def test_load_returns_none_when_missing(self, tmp_path):
        assert load_session(tmp_path) is None

    def test_load_ignores_corrupt_json(self, tmp_path, caplog):
        (tmp_path / SESSION_FILENAME).write_text("not valid json {")
        with caplog.at_level("WARNING"):
            assert load_session(tmp_path) is None
        assert "corrupt" in caplog.text.lower()

    def test_load_ignores_wrong_schema_version(self, tmp_path, caplog):
        (tmp_path / SESSION_FILENAME).write_text(
            json.dumps({"schema_version": 99, "project_name": "x"})
        )
        with caplog.at_level("WARNING"):
            assert load_session(tmp_path) is None
        assert "schema" in caplog.text.lower()

    def test_load_accepts_project_file_path(self, tmp_path):
        sess = new_session(tmp_path, "demo")
        save_session(sess)
        # Passing a .kicad_pro path should resolve to its parent dir
        pro = tmp_path / "demo.kicad_pro"
        pro.touch()
        loaded = load_session(pro)
        assert loaded is not None


class TestUpdateStage:
    def test_advances_stage(self, tmp_path):
        save_session(new_session(tmp_path, "demo"))
        updated = update_stage(tmp_path, "parts_sourced", last_tool="lcsc_resolve_bom")
        assert updated is not None
        assert updated["stage"] == "parts_sourced"
        assert updated["last_tool"] == "lcsc_resolve_bom"
        assert updated["checkpoints"]["parts_sourced"] is True
        # Earlier checkpoint stays true
        assert updated["checkpoints"]["project_created"] is True
        # Later checkpoints still false
        assert updated["checkpoints"]["pcb_generated"] is False

    def test_persists_bom_and_spec(self, tmp_path):
        save_session(new_session(tmp_path, "demo"))
        bom = [{"ref": "R1", "lcsc": "C25804"}]
        spec = {"name": "demo", "components": [], "nets": {}}
        update_stage(tmp_path, "parts_sourced", bom=bom)
        update_stage(tmp_path, "pcb_generated", spec=spec)

        reloaded = load_session(tmp_path)
        assert reloaded is not None
        assert reloaded["bom"] == bom
        assert reloaded["spec"] == spec
        assert reloaded["stage"] == "pcb_generated"

    def test_returns_none_when_no_session(self, tmp_path):
        # No session was saved beforehand
        assert update_stage(tmp_path, "parts_sourced") is None


class TestCheckpointsUpTo:
    def test_first_stage(self):
        cps = _checkpoints_up_to("created")
        assert cps == {"project_created": True}

    def test_middle_stage(self):
        cps = _checkpoints_up_to("bom_confirmed")
        assert cps == {
            "project_created": True,
            "parts_sourced": True,
            "bom_confirmed": True,
        }

    def test_final_stage(self):
        cps = _checkpoints_up_to("handoff_rendered")
        assert all(cps.values())
        assert len(cps) == 5


class TestResumeSummary:
    def test_summarizes_fresh_session(self, tmp_path):
        sess = new_session(tmp_path, "demo")
        summary = resume_summary(sess)
        assert summary["project_name"] == "demo"
        assert summary["stage"] == "created"
        assert summary["completed_checkpoints"] == ["project_created"]
        assert "parts_sourced" in summary["pending_checkpoints"]
        assert "Source parts" in summary["next_step"]
        assert summary["bom_rows"] == 0
        assert summary["has_spec"] is False

    def test_summarizes_advanced_session(self, tmp_path):
        sess = new_session(tmp_path, "demo")
        save_session(sess)
        update_stage(tmp_path, "parts_sourced", bom=[{"ref": "R1"}])
        reloaded = load_session(tmp_path)
        assert reloaded is not None
        summary = resume_summary(reloaded)
        assert summary["stage"] == "parts_sourced"
        assert summary["bom_rows"] == 1
        assert "Review the BOM" in summary["next_step"]

    def test_unknown_stage(self, tmp_path):
        sess: Session = {"stage": "bogus_stage"}  # type: ignore[typeddict-item]
        summary = resume_summary(sess)
        assert "Unknown stage" in summary["next_step"]
