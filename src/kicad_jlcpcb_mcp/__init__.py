"""kicad-jlcpcb MCP Server.

Generates JLCPCB-ready KiCad PCB projects from descriptions, BOMs, or
existing KiCad projects. Sources LCSC parts with a hard preference for
JLCPCB basic-tier parts, generates schematics, and packages routed boards
into manufacturing zips ready to upload to JLCPCB.
"""

__version__ = "0.8.0"


USAGE = """kicad-jlcpcb {version} — MCP server for KiCad + JLCPCB workflows

This is a Model Context Protocol server, not an interactive CLI. Started
with no arguments it speaks JSON-RPC over stdio and waits for a client, so
running it by hand will appear to hang — that is correct behaviour.

  --version    print the version and exit
  --help, -h   print this message and exit

Normal use is through Claude Code, which launches it via .mcp.json:

  /plugin marketplace add /abs/path/to/kicad-jlcpcb
  /plugin install kicad-jlcpcb@beckhamlabs

Docs and issues: https://github.com/BeckhamLabsLLC/kicad-jlcpcb
"""


def main(argv: list[str] | None = None) -> None:
    """Entry point for the MCP server.

    Handles --help/--version first so the documented install sanity check
    exits instead of blocking on stdin, then performs a preflight
    dependency check so a misconfigured install fails with a
    human-readable message rather than a silent ImportError that Claude
    Code's MCP bootstrap sometimes swallows.
    """
    import sys

    args = sys.argv[1:] if argv is None else argv
    if "--version" in args:
        print(__version__)
        return
    if "--help" in args or "-h" in args:
        print(USAGE.format(version=__version__), end="")
        return

    _preflight()
    from .server import main as _main

    _main()


def _preflight() -> None:
    """Verify required third-party dependencies are importable."""
    import sys

    missing: list[str] = []
    try:
        import mcp  # noqa: F401
    except ImportError:
        missing.append("mcp")
    try:
        import httpx  # noqa: F401
    except ImportError:
        missing.append("httpx")

    if missing:
        sys.stderr.write(
            "kicad-jlcpcb failed to start: missing Python dependencies "
            f"({', '.join(missing)}).\n"
            "Install with:  pip install -e .  (run from the plugin directory)\n"
            "See CONTRIBUTING.md for the full dev setup.\n"
        )
        sys.exit(2)


__all__ = ["main", "__version__", "USAGE"]
