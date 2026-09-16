"""Tests for the kicad-cli async wrapper.

Mocks subprocess execution; integration with a real kicad-cli is gated
behind the KICAD_INSTALLED env var so the suite passes on machines
without KiCad.
"""

import os

import pytest

from kicad_jlcpcb_mcp import config, kicad_cli
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
        monkeypatch.setattr(kicad_cli, "_find_flatpak_argv", lambda: None)
        with pytest.raises(KicadCliError, match="not found"):
            await kicad_cli._run(["--version"])


class TestCrossPlatformDiscovery:
    """The README promises macOS, and this module's own install hint
    recommends Flatpak. Neither puts kicad-cli on PATH, so probing PATH
    alone told those users to install KiCad when they already had it."""

    def test_falls_back_to_a_known_path_when_path_is_empty(self, tmp_path, monkeypatch):
        fake = tmp_path / "kicad-cli"
        fake.write_text("#!/bin/sh\n")
        fake.chmod(0o755)
        monkeypatch.setattr(kicad_cli.shutil, "which", lambda _: None)
        monkeypatch.setattr(config, "KICAD_CLI_FALLBACK_PATHS", (str(fake),))
        assert kicad_cli._find_executable() == str(fake)

    def test_ignores_a_fallback_path_that_is_not_executable(self, tmp_path, monkeypatch):
        fake = tmp_path / "kicad-cli"
        fake.write_text("not executable")
        fake.chmod(0o644)
        monkeypatch.setattr(kicad_cli.shutil, "which", lambda _: None)
        monkeypatch.setattr(config, "KICAD_CLI_FALLBACK_PATHS", (str(fake),))
        monkeypatch.setattr(kicad_cli, "_find_flatpak_argv", lambda: None)
        assert kicad_cli._find_executable() is None

    def test_flatpak_argv_is_a_full_prefix_not_a_bare_path(self, monkeypatch):
        """A Flatpak install needs four argv elements, not one."""
        monkeypatch.setattr(kicad_cli, "_find_executable", lambda: None)
        monkeypatch.setattr(
            kicad_cli,
            "_find_flatpak_argv",
            lambda: ["/usr/bin/flatpak", "run", "--command=kicad-cli", "org.kicad.KiCad"],
        )
        argv = kicad_cli._find_kicad_argv()
        assert argv[:2] == ["/usr/bin/flatpak", "run"]
        assert argv[-1] == "org.kicad.KiCad"

    def test_plain_install_is_a_single_element_argv(self, monkeypatch):
        monkeypatch.setattr(kicad_cli, "_find_executable", lambda: "/usr/bin/kicad-cli")
        assert kicad_cli._find_kicad_argv() == ["/usr/bin/kicad-cli"]


class TestInstallHintIsPlatformAppropriate:
    """It used to print Fedora dnf commands on macOS and Windows."""

    def test_macos(self, monkeypatch):
        monkeypatch.setattr(kicad_cli.sys, "platform", "darwin")
        hint = kicad_cli._install_hint()
        assert "KiCad.app" in hint
        assert "dnf" not in hint

    def test_windows(self, monkeypatch):
        monkeypatch.setattr(kicad_cli.sys, "platform", "win32")
        hint = kicad_cli._install_hint()
        assert "Program Files" in hint
        assert "dnf" not in hint

    def test_linux_covers_more_than_one_distro(self, monkeypatch):
        monkeypatch.setattr(kicad_cli.sys, "platform", "linux")
        hint = kicad_cli._install_hint()
        for mgr in ("dnf", "apt", "pacman", "flatpak"):
            assert mgr in hint


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


class TestErrorMessagesReachTheModel:
    """The MCP layer only surfaces `str(e)`, so detail kept as an attribute
    is detail the model never sees."""

    def test_stderr_is_in_the_message(self):
        e = KicadCliError(
            "kicad-cli pcb export gerbers failed (exit 1)",
            returncode=1,
            stderr="Error: could not open board file",
        )
        assert "could not open board file" in str(e)
        assert e.returncode == 1

    def test_falls_back_to_stdout(self):
        e = KicadCliError("failed", stdout="something on stdout")
        assert "something on stdout" in str(e)

    def test_message_alone_when_nothing_was_captured(self):
        assert str(KicadCliError("plain failure")) == "plain failure"

    def test_long_output_is_truncated(self):
        e = KicadCliError("failed", stderr="x" * 5000)
        assert len(str(e)) < 2000
        assert "truncated" in str(e)


class TestErcReportParsingAcrossKicadVersions:
    """An unrecognised report format parses as (0, 0), which `sch_erc` then
    reports as `passed: True`. A parser miss here is a silent false pass,
    not a visible failure — so every format gets a test."""

    # Verbatim from `kicad-cli sch erc` on KiCad 10.0.5.
    KICAD_10_REPORT = """ERC report (2026-09-16T10:56:28, Encoding UTF8)
Report includes: Errors, Warnings

***** Sheet /
[pin_not_connected]: Pin not connected
    ; error
    @(46.99 mm, 35.56 mm): Symbol U1 Pin 4 [NC, Passive, Line]
[lib_symbol_issues]: The current configuration does not include the symbol library 'kicad_jlcpcb'
    ; warning
    @(38.10 mm, 38.10 mm): Symbol U1 [C82942]
[lib_symbol_issues]: The current configuration does not include the symbol library 'kicad_jlcpcb'
    ; warning
    @(88.90 mm, 38.10 mm): Symbol C1 [C1525]

** ERC messages: 3  Errors 1  Warnings 2
"""

    def test_kicad_10_per_violation_severity_lines(self):
        assert _parse_erc_report(self.KICAD_10_REPORT) == (1, 2)

    def test_found_summary_line(self):
        assert _parse_erc_report("Found 2 errors, 3 warnings.") == (2, 3)

    def test_legacy_star_summary(self):
        assert _parse_erc_report("** Errors 2 ****\n** Warnings 3 ****") == (2, 3)

    def test_severity_lines(self):
        assert _parse_erc_report("Severity: error\nSeverity: warning\nSeverity: warning") == (1, 2)

    def test_clean_report_is_zero(self):
        assert _parse_erc_report("ERC report\n\n***** Sheet /\n") == (0, 0)

    def test_a_report_with_violations_never_parses_as_clean(self):
        """The regression that mattered: this report has violations, and
        the old parser returned (0, 0) for it."""
        errors, warnings = _parse_erc_report(self.KICAD_10_REPORT)
        assert (errors, warnings) != (0, 0)
