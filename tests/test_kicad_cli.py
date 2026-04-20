"""Tests for the kicad-cli async wrapper.

Mocks subprocess execution; integration with a real kicad-cli is gated
behind the KICAD_INSTALLED env var so the suite passes on machines
without KiCad.
"""

import os

import pytest

from kicad_jlcpcb_mcp import kicad_cli
from kicad_jlcpcb_mcp.kicad_cli import (
    KicadCliError,
    _parse_erc_report,
    _parse_version,
    detect_kicad,
    sch_erc,
)


class TestParseVersion:
    def test_plain_version(self):
        assert _parse_version("9.0.1") == (9, 0, 1)

    def test_two_part_version(self):
        assert _parse_version("8.0") == (8, 0, 0)

    def test_fedora_packaging(self):
        assert _parse_version("kicad-cli (9.0.1-1)") == (9, 0, 1)

    def test_release_build(self):
        assert _parse_version("8.0.4 release build") == (8, 0, 4)

    def test_garbage_returns_none(self):
        assert _parse_version("hello world") is None


class TestParseErcReport:
    def test_modern_summary(self):
        text = "Some report\nFound 2 errors, 3 warnings.\nDone"
        assert _parse_erc_report(text) == (2, 3)

    def test_legacy_summary(self):
        text = "** ERC messages: 5 ****\n** Errors 4 ****\n** Warnings 1 ****"
        assert _parse_erc_report(text) == (4, 1)

    def test_legacy_only_errors(self):
        text = "** Errors 7 ****"
        assert _parse_erc_report(text) == (7, 0)

    def test_severity_fallback(self):
        text = "Severity: error\nSeverity: error\nSeverity: warning"
        assert _parse_erc_report(text) == (2, 1)

    def test_empty_clean_report(self):
        assert _parse_erc_report("") == (0, 0)


class TestDetectKicad:
    @pytest.mark.asyncio
    async def test_not_found(self, monkeypatch):
        monkeypatch.setattr(kicad_cli, "_find_executable", lambda: None)
        result = await detect_kicad()
        assert result["found"] is False
        assert result["meets_min"] is False
        assert "Fedora" in result["install_hint"]
        assert result["min_required"] == "8.0"

    @pytest.mark.asyncio
    async def test_meets_min(self, monkeypatch):
        monkeypatch.setattr(kicad_cli, "_find_executable", lambda: "/usr/bin/kicad-cli")

        async def fake_run(args, **kwargs):
            return 0, "9.0.1\n", ""

        monkeypatch.setattr(kicad_cli, "_run", fake_run)
        result = await detect_kicad()
        assert result["found"] is True
        assert result["version"] == "9.0.1"
        assert result["version_tuple"] == [9, 0, 1]
        assert result["meets_min"] is True
        assert result["install_hint"] is None

    @pytest.mark.asyncio
    async def test_too_old(self, monkeypatch):
        monkeypatch.setattr(kicad_cli, "_find_executable", lambda: "/usr/bin/kicad-cli")

        async def fake_run(args, **kwargs):
            return 0, "7.0.10\n", ""

        monkeypatch.setattr(kicad_cli, "_run", fake_run)
        result = await detect_kicad()
        assert result["found"] is True
        assert result["version"] == "7.0.10"
        assert result["meets_min"] is False
        assert "Fedora" in result["install_hint"]

    @pytest.mark.asyncio
    async def test_unparseable_version(self, monkeypatch):
        monkeypatch.setattr(kicad_cli, "_find_executable", lambda: "/usr/bin/kicad-cli")

        async def fake_run(args, **kwargs):
            return 0, "no version here", ""

        monkeypatch.setattr(kicad_cli, "_run", fake_run)
        result = await detect_kicad()
        assert result["found"] is True
        assert result["version"] is None
        assert result["meets_min"] is False
        assert result["raw_version_output"] == "no version here"


class TestSchErc:
    @pytest.mark.asyncio
    async def test_passes_when_no_errors(self, tmp_path, monkeypatch):
        sch = tmp_path / "demo.kicad_sch"
        sch.write_text("(kicad_sch)")
        report = tmp_path / "demo-erc.rpt"

        async def fake_run(args, **kwargs):
            # Simulate kicad-cli writing a clean report file
            report.write_text("Found 0 errors, 0 warnings.\n")
            return 0, "", ""

        monkeypatch.setattr(kicad_cli, "_run", fake_run)
        result = await sch_erc(sch)
        assert result["errors"] == 0
        assert result["warnings"] == 0
        assert result["passed"] is True
        assert result["report_path"] == str(report)

    @pytest.mark.asyncio
    async def test_fails_when_errors_present(self, tmp_path, monkeypatch):
        sch = tmp_path / "demo.kicad_sch"
        sch.write_text("(kicad_sch)")

        async def fake_run(args, **kwargs):
            (tmp_path / "demo-erc.rpt").write_text("Found 3 errors, 1 warnings.\n")
            return 5, "", ""  # kicad-cli exits non-zero on violations

        monkeypatch.setattr(kicad_cli, "_run", fake_run)
        result = await sch_erc(sch)
        assert result["errors"] == 3
        assert result["warnings"] == 1
        assert result["passed"] is False

    @pytest.mark.asyncio
    async def test_missing_schematic_raises(self, tmp_path):
        with pytest.raises(KicadCliError, match="Schematic not found"):
            await sch_erc(tmp_path / "nope.kicad_sch")

    @pytest.mark.asyncio
    async def test_missing_report_raises(self, tmp_path, monkeypatch):
        sch = tmp_path / "demo.kicad_sch"
        sch.write_text("(kicad_sch)")

        async def fake_run(args, **kwargs):
            return 0, "", ""  # No report file written

        monkeypatch.setattr(kicad_cli, "_run", fake_run)
        with pytest.raises(KicadCliError, match="did not produce a report"):
            await sch_erc(sch)


class TestRunMissingExecutable:
    @pytest.mark.asyncio
    async def test_raises_when_no_kicad_cli(self, monkeypatch):
        monkeypatch.setattr(kicad_cli, "_find_executable", lambda: None)
        with pytest.raises(KicadCliError, match="not found on PATH"):
            await kicad_cli._run(["--version"])


# Optional integration test — only runs when KICAD_INSTALLED=1
@pytest.mark.skipif(
    os.environ.get("KICAD_INSTALLED") != "1",
    reason="Set KICAD_INSTALLED=1 to run integration tests against a real kicad-cli",
)
class TestIntegration:
    @pytest.mark.asyncio
    async def test_real_detect(self):
        result = await detect_kicad()
        assert result["found"] is True
        assert result["version_tuple"] is not None
