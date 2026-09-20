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


class TestTheLauncherResolvesDependencies:
    """`bin/launch.py` is what `.mcp.json` runs, and it runs before the
    dependencies necessarily exist.

    The failure it was written for: a marketplace install never clones and
    never runs `pip install`, so `mcp` and `httpx` are absent and the server
    exited at preflight — every new user, first command. These tests pin the
    three routes it can take without actually installing anything.
    """

    @staticmethod
    def _load_launcher():
        """Import bin/launch.py by path; it is a script, not a package."""
        import importlib.util
        from pathlib import Path

        path = Path(__file__).resolve().parent.parent / "bin" / "launch.py"
        spec = importlib.util.spec_from_file_location("kjlc_launch", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_it_runs_the_server_when_dependencies_are_present(self, monkeypatch):
        launcher = self._load_launcher()
        monkeypatch.setattr(launcher, "_missing_dependencies", lambda: [])

        called = []
        monkeypatch.setattr(
            "kicad_jlcpcb_mcp.main", lambda *a, **k: called.append(True), raising=False
        )
        launcher.main()
        assert called == [True]

    def test_it_reexecs_through_uv_when_dependencies_are_missing(self, monkeypatch):
        launcher = self._load_launcher()
        monkeypatch.setattr(launcher, "_missing_dependencies", lambda: ["mcp"])
        monkeypatch.setattr(launcher.shutil, "which", lambda name: "/usr/bin/uv")
        monkeypatch.delenv("KJLC_LAUNCH_VIA_UV", raising=False)

        argv = {}

        def fake_execvp(file, args):
            argv["file"] = file
            argv["args"] = args
            raise SystemExit(0)

        monkeypatch.setattr(launcher.os, "execvp", fake_execvp)
        with pytest.raises(SystemExit):
            launcher.main()

        assert argv["file"] == "uv"
        assert argv["args"][:4] == ["uv", "run", "--directory", str(launcher.PLUGIN_ROOT)]
        assert "kicad-jlcpcb" in argv["args"]

    def test_it_does_not_loop_if_uv_also_lacks_the_dependencies(self, monkeypatch, capsys):
        """uv's child re-enters this same script. Without the guard it would
        exec uv again, forever, and Claude Code would just see a hang."""
        launcher = self._load_launcher()
        monkeypatch.setattr(launcher, "_missing_dependencies", lambda: ["mcp"])
        monkeypatch.setattr(launcher.shutil, "which", lambda name: "/usr/bin/uv")
        monkeypatch.setenv("KJLC_LAUNCH_VIA_UV", "1")

        def explode(*a, **k):
            raise AssertionError("re-execed through uv a second time")

        monkeypatch.setattr(launcher.os, "execvp", explode)
        with pytest.raises(SystemExit) as exc:
            launcher.main()
        assert exc.value.code == 2

    def test_the_no_uv_message_names_both_ways_out(self, monkeypatch, capsys):
        launcher = self._load_launcher()
        monkeypatch.setattr(launcher, "_missing_dependencies", lambda: ["mcp", "httpx"])
        monkeypatch.setattr(launcher.shutil, "which", lambda name: None)

        with pytest.raises(SystemExit) as exc:
            launcher.main()
        assert exc.value.code == 2

        message = capsys.readouterr().err
        assert "mcp, httpx" in message
        # A stdio server's stderr is the only thing the user will ever see,
        # so it has to carry a runnable command, not just a diagnosis.
        assert "uv" in message
        assert "pip install mcp httpx" in message
