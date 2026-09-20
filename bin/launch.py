#!/usr/bin/env python3
"""Start the kicad-jlcpcb MCP server, installing dependencies if it must.

**Why this file exists.** `.mcp.json` used to run `python3 -m kicad_jlcpcb_mcp`
directly, which works on the author's machine and nowhere else:

* A marketplace install never clones and never runs `pip install`, so `mcp`
  and `httpx` are simply absent. The server exited at the preflight check and
  Claude Code reported `CONNECTION_CLOSED` — every new user, first command.
* `${CLAUDE_PLUGIN_ROOT}` is only substituted for plugin-provided MCP configs.
  Loaded as a *project* `.mcp.json` (a clone, which is how you develop on
  this) it stays a literal string, `PYTHONPATH` points at a directory named
  `${CLAUDE_PLUGIN_ROOT}`, and you get `No module named kicad_jlcpcb_mcp`.

So this script trusts nothing it is handed. It locates `src/` from its own
`__file__`, and if the dependencies are missing it re-execs through `uv`,
which resolves them from pyproject.toml without a venv the user has to know
about. Only when both routes are gone does it fail — and then it says what to
run, because a stdio server's stderr is the only thing the user will see.

Stdlib only, and no package imports at module scope: this has to be able to
run *before* the dependencies exist.
"""

import os
import shutil
import sys
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
SRC = PLUGIN_ROOT / "src"

REQUIRED = ("mcp", "httpx")


def _missing_dependencies() -> list[str]:
    """Return the required distributions that cannot be imported."""
    from importlib.util import find_spec

    missing = []
    for name in REQUIRED:
        try:
            if find_spec(name) is None:
                missing.append(name)
        except (ImportError, ValueError):
            missing.append(name)
    return missing


def _reexec_via_uv() -> None:
    """Hand off to `uv run`, which installs the deps from pyproject.toml.

    Never returns: on success `execvp` replaces this process, so uv's child
    inherits the stdio pipes Claude Code is already speaking JSON-RPC over.
    A new interpreter is started, so `KJLC_LAUNCH_VIA_UV` guards against
    looping if uv's environment somehow still lacks the dependencies.
    """
    os.environ["KJLC_LAUNCH_VIA_UV"] = "1"
    # `--directory` makes uv resolve *this* plugin's pyproject.toml no matter
    # what the working directory is when Claude Code spawns us.
    os.execvp(
        "uv",
        ["uv", "run", "--directory", str(PLUGIN_ROOT), "kicad-jlcpcb", *sys.argv[1:]],
    )


def _die_unsatisfied(missing: list[str]) -> None:
    sys.stderr.write(
        "kicad-jlcpcb failed to start: missing Python dependencies "
        f"({', '.join(missing)}).\n"
        "\n"
        "Fix it with either of these:\n"
        "\n"
        "  1. Install uv, and this plugin manages its own dependencies:\n"
        "       curl -LsSf https://astral.sh/uv/install.sh | sh\n"
        "\n"
        f"  2. Install the dependencies into the Python that runs the plugin:\n"
        f"       {sys.executable} -m pip install mcp httpx\n"
        "\n"
        "Then restart Claude Code.\n"
    )
    sys.exit(2)


def main() -> None:
    # Prepend rather than append: if an older copy of the package is installed
    # site-wide, the checkout Claude Code actually launched should still win.
    if str(SRC) not in sys.path:
        sys.path.insert(0, str(SRC))

    missing = _missing_dependencies()
    if missing:
        if os.environ.get("KJLC_LAUNCH_VIA_UV") != "1" and shutil.which("uv"):
            _reexec_via_uv()
        _die_unsatisfied(missing)

    from kicad_jlcpcb_mcp import main as server_main

    server_main()


if __name__ == "__main__":
    main()
