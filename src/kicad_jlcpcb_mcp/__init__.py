"""kicad-jlcpcb MCP Server.

Generates JLCPCB-ready KiCad PCB projects from descriptions, BOMs, or
existing KiCad projects. Sources LCSC parts with a hard preference for
JLCPCB basic-tier parts, generates schematics, and packages routed boards
into manufacturing zips ready to upload to JLCPCB.
"""

__version__ = "0.1.0"


def main() -> None:
    """Entry point for the MCP server.

    Performs a preflight dependency check so a misconfigured install fails
    with a human-readable message instead of a silent ImportError that
    Claude Code's MCP bootstrap sometimes swallows.
    """
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


__all__ = ["main", "__version__"]
