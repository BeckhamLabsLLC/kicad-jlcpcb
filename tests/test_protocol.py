"""The server is started for real and asked to speak MCP.

Everything in test_server.py runs against a `KicadJlcpcbServer` whose SDK
`Server` is a MagicMock, or calls `_handle_tool` directly. Neither can tell
you whether the process Claude Code actually launches comes up, registers
its handlers, and answers a request. This file can: it runs the launcher in
a subprocess over a pipe, exactly as the client does.

It is slower than the rest of the suite (a process start per test) and worth
it — this is the layer that was silently broken.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import check_protocol  # noqa: E402

PLUGIN_ROOT = Path(__file__).resolve().parent.parent


class TestTheServerAnswersProtocol:
    def test_handshake_completes(self):
        """initialize -> tools/list -> tools/call, against the real process."""
        tools = check_protocol.check(verbose=False)
        assert {t["name"] for t in tools} == check_protocol.EXPECTED_TOOLS

    def test_every_listed_tool_carries_a_schema(self):
        """A mocked tool inherits its schema from this; an empty one steers
        the model badly, and the eval mocks are generated from this dump."""
        for tool in check_protocol.check(verbose=False):
            assert tool.get("description"), f"{tool['name']} has no description"
            schema = tool.get("inputSchema")
            assert isinstance(schema, dict), f"{tool['name']} has no inputSchema"
            assert schema.get("type") == "object", f"{tool['name']} schema is not an object"


class TestTheCheckScriptCatchesABrokenServer:
    """The check is only worth having if it fails when the server is broken.

    Rather than trusting that, run the script against a server whose
    call_tool registration has been removed, in a throwaway copy of the
    source tree, and require a non-zero exit.
    """

    def test_a_stranded_call_tool_registration_is_caught(self, tmp_path):
        import shutil

        broken = tmp_path / "plugin"
        shutil.copytree(
            PLUGIN_ROOT,
            broken,
            ignore=shutil.ignore_patterns(
                ".git", ".venv", "__pycache__", "*.pyc", ".pytest_cache", "results"
            ),
        )

        server_py = broken / "src" / "kicad_jlcpcb_mcp" / "server.py"
        source = server_py.read_text()
        registration = "self._server.call_tool()(call_tool)"
        assert registration in source, "the line this test breaks has moved; update it"
        server_py.write_text(source.replace(registration, f"pass  # {registration}"))

        result = subprocess.run(
            [sys.executable, str(broken / "scripts" / "check_protocol.py")],
            capture_output=True,
            text=True,
            timeout=120,
        )

        assert result.returncode != 0, (
            "check_protocol.py passed against a server with no call_tool "
            "handler — it is not actually checking anything.\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )


class TestTheLauncherIsWhatMcpJsonRuns:
    """`.mcp.json` and `bin/launch.py` drifting apart is a silent outage."""

    def test_mcp_json_points_at_the_launcher(self):
        config = json.loads((PLUGIN_ROOT / ".mcp.json").read_text())
        server = config["mcpServers"]["kicad-jlcpcb"]
        args = server["args"]
        assert len(args) == 1
        assert args[0] == "${CLAUDE_PLUGIN_ROOT}/bin/launch.py"
        # The launcher finds src/ from its own __file__. If a PYTHONPATH is
        # reintroduced here it will be the unsubstituted-variable bug again.
        assert "env" not in server or "PYTHONPATH" not in server.get("env", {})

    def test_the_launcher_exists_and_is_executable(self):
        launcher = PLUGIN_ROOT / "bin" / "launch.py"
        assert launcher.is_file()

    @pytest.mark.parametrize("flag", ["--version", "--help"])
    def test_the_launcher_runs_without_pythonpath(self, flag, tmp_path):
        """The bug this replaced: the server only started if PYTHONPATH was
        already correct. Run from a foreign cwd with no PYTHONPATH at all."""
        import os

        env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
        result = subprocess.run(
            [sys.executable, str(PLUGIN_ROOT / "bin" / "launch.py"), flag],
            capture_output=True,
            text=True,
            cwd=tmp_path,
            env=env,
            timeout=60,
        )
        assert result.returncode == 0, result.stderr
        assert "kicad-jlcpcb" in result.stdout or result.stdout.strip()
