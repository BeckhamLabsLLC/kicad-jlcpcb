"""Tests for the console entry point.

`main()` and `_preflight()` are the first code a user's machine runs, and
were the least-tested in the package. Both have already been wrong once:
`--help` used to block on stdin because argv was ignored entirely, and the
missing-dependency message is the only thing a user sees when Claude Code's
MCP bootstrap swallows an ImportError.
"""

from __future__ import annotations

import subprocess
import sys

import pytest

from kicad_jlcpcb_mcp import USAGE, __version__, main


class TestVersionAndHelp:
    def test_version_prints_and_returns(self, capsys):
        main(["--version"])
        assert capsys.readouterr().out.strip() == __version__

    def test_help_prints_and_returns(self, capsys):
        main(["--help"])
        out = capsys.readouterr().out
        assert "kicad-jlcpcb" in out
        assert __version__ in out

    def test_short_help_flag(self, capsys):
        main(["-h"])
        assert "kicad-jlcpcb" in capsys.readouterr().out

    def test_help_explains_that_hanging_is_correct(self, capsys):
        """The documented sanity check used to hang, and a server waiting on
        stdio looks identical to one that has crashed."""
        main(["--help"])
        out = capsys.readouterr().out.lower()
        assert "hang" in out
        assert "stdio" in out

    def test_help_names_the_install_command(self):
        assert "/plugin install" in USAGE

    def test_neither_flag_starts_the_server(self, monkeypatch):
        """If these fell through to server.run() they would block forever."""
        started = []
        monkeypatch.setattr("kicad_jlcpcb_mcp._preflight", lambda: started.append("preflight"))
        main(["--version"])
        main(["--help"])
        assert started == []


class TestPreflight:
    def test_passes_when_dependencies_are_importable(self):
        from kicad_jlcpcb_mcp import _preflight

        _preflight()  # mcp and httpx are installed in the test env

    def test_missing_dependency_exits_with_a_usable_message(self, monkeypatch, capsys):
        """Claude Code's MCP bootstrap can swallow an ImportError, leaving a
        server that simply never starts and says nothing."""
        import builtins

        from kicad_jlcpcb_mcp import _preflight

        real_import = builtins.__import__

        def fake_import(name, *args, **kwargs):
            if name == "httpx":
                raise ImportError("no httpx")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", fake_import)
        with pytest.raises(SystemExit) as exc:
            _preflight()
        monkeypatch.undo()

        assert exc.value.code == 2
        err = capsys.readouterr().err
        assert "httpx" in err
        assert "pip install -e ." in err


class TestAsASubprocess:
    """What a user actually types, run the way they would run it."""

    def _run(self, *args):
        return subprocess.run(
            [sys.executable, "-m", "kicad_jlcpcb_mcp", *args],
            capture_output=True,
            text=True,
            timeout=30,
            env={"PYTHONPATH": "src", "PATH": "/usr/bin:/bin"},
        )

    def test_version_exits_zero_without_blocking(self):
        r = self._run("--version")
        assert r.returncode == 0
        assert r.stdout.strip() == __version__

    def test_help_exits_zero_without_blocking(self):
        r = self._run("--help")
        assert r.returncode == 0
        assert "JSON-RPC" in r.stdout
