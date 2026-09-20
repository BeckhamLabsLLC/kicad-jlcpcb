#!/usr/bin/env python3
"""Speak MCP to the real server and fail loudly if it does not answer.

**Why this exists.** `tests/test_server.py` patches `mcp.server.Server`, so it
cannot see whether a handler is actually registered with the SDK — a refactor
that strands `@call_tool` behind a `return` leaves every test green and every
tool call answering "Method not found". This script is the other half: it
starts the exact command `.mcp.json` names, in a subprocess, and holds a real
JSON-RPC conversation with it.

    initialize -> tools/list -> tools/call

If any step does not answer, the plugin is broken for every user regardless of
what the unit suite says. Exits non-zero with the reason.

Run it directly, or via `pytest tests/test_protocol.py`.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
LAUNCHER = PLUGIN_ROOT / "bin" / "launch.py"

# Tools the server is contractually required to expose. Deliberately the full
# list rather than a count: a rename should fail here, not just a removal.
EXPECTED_TOOLS = {
    "detect_kicad",
    "create_project",
    "load_project",
    "lcsc_search",
    "fetch_part_library",
    "sch_generate",
    "pcb_generate",
    "part_pin_map",
    "easyeda_handoff",
    "package_for_jlcpcb",
    "sch_run_erc",
    "session_confirm_bom",
    "session_resume",
    "lcsc_resolve_bom",
}

TIMEOUT_SECONDS = 60


class ProtocolError(RuntimeError):
    pass


class Server:
    """A live server subprocess you can send JSON-RPC to."""

    def __init__(self) -> None:
        # A clean environment on purpose: PYTHONPATH from the developer's
        # shell is exactly the crutch this check exists to not depend on.
        env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "PYTHONHOME")}
        self.proc = subprocess.Popen(
            [sys.executable, str(LAUNCHER)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            text=True,
            bufsize=1,
        )

    def send(self, payload: dict) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.write(json.dumps(payload) + "\n")
        self.proc.stdin.flush()

    def receive(self, what: str) -> dict:
        assert self.proc.stdout is not None
        line = self.proc.stdout.readline()
        if not line:
            raise ProtocolError(f"server closed the connection during {what}.\n{self.stderr()}")
        try:
            message = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ProtocolError(f"{what} returned non-JSON: {line!r}") from exc
        if "error" in message:
            raise ProtocolError(f"{what} returned an error: {message['error']}")
        if "result" not in message:
            raise ProtocolError(f"{what} returned no result: {message}")
        return message["result"]

    def request(self, what: str, method: str, params: dict, req_id: int) -> dict:
        self.send({"jsonrpc": "2.0", "id": req_id, "method": method, "params": params})
        return self.receive(what)

    def stderr(self) -> str:
        self.proc.terminate()
        try:
            _, err = self.proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            _, err = self.proc.communicate()
        return f"server stderr:\n{err}" if err else "server wrote nothing to stderr."

    def close(self) -> None:
        self.proc.terminate()
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.proc.kill()


def check(verbose: bool = True) -> list[dict]:
    """Run the handshake. Returns the tools/list payload. Raises on failure."""

    def say(msg: str) -> None:
        if verbose:
            print(msg)

    server = Server()
    try:
        result = server.request(
            "initialize",
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "check_protocol", "version": "1"},
            },
            1,
        )
        info = result.get("serverInfo", {})
        say(f"  initialize      -> {info.get('name')} {info.get('version')}")

        server.send({"jsonrpc": "2.0", "method": "notifications/initialized"})

        result = server.request("tools/list", "tools/list", {}, 2)
        tools = result.get("tools")
        if not isinstance(tools, list):
            raise ProtocolError(f"tools/list returned no tool array: {result}")
        names = {t["name"] for t in tools}
        say(f"  tools/list      -> {len(tools)} tools")

        missing = EXPECTED_TOOLS - names
        if missing:
            raise ProtocolError(f"tools/list is missing {sorted(missing)}")
        extra = names - EXPECTED_TOOLS
        if extra:
            raise ProtocolError(
                f"tools/list exposes undeclared tools {sorted(extra)}; "
                "update EXPECTED_TOOLS in this script if that is intended."
            )

        # detect_kicad is the only tool that is safe to call for real: it
        # touches no network and writes nothing.
        result = server.request(
            "tools/call", "tools/call", {"name": "detect_kicad", "arguments": {}}, 3
        )
        if result.get("isError"):
            raise ProtocolError(f"tools/call detect_kicad reported an error: {result}")
        content = result.get("content") or []
        if not content or "text" not in content[0]:
            raise ProtocolError(f"tools/call returned no text content: {result}")
        json.loads(content[0]["text"])  # must be the JSON the server promises
        say("  tools/call      -> detect_kicad answered")

        return tools
    finally:
        server.close()


def main() -> int:
    print(f"Speaking MCP to {LAUNCHER.relative_to(PLUGIN_ROOT)} ...")
    try:
        check()
    except ProtocolError as exc:
        print(f"\nFAIL: {exc}", file=sys.stderr)
        return 1
    print("\nOK: the server answers initialize, tools/list and tools/call.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
