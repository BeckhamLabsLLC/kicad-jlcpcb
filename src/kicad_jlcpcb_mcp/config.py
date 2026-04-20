"""Configuration for kicad-jlcpcb MCP server.

All module-level constants live here. Other modules import this module
(`from . import config`) and access values as `config.FOO` so tests can
monkeypatch with `monkeypatch.setattr(config, "FOO", value)`.
"""

from pathlib import Path

# ---------------------------------------------------------------------------
# KiCad
# ---------------------------------------------------------------------------

# Minimum supported KiCad major.minor for `kicad-cli`.
# 8.0 introduced the modern `kicad-cli` verbs we depend on.
KICAD_MIN_VERSION = (8, 0)

# Candidate executable names to probe when detecting a KiCad install.
KICAD_CLI_CANDIDATES = ("kicad-cli",)

# ---------------------------------------------------------------------------
# Cache and workspace
# ---------------------------------------------------------------------------

# Local cache for LCSC part metadata SQLite db, downloaded library files,
# and any other intermediate state. Created on first use.
CACHE_DIR = Path.home() / ".cache" / "kicad-jlcpcb"

# SQLite filename inside CACHE_DIR for resolved LCSC parts.
LCSC_CACHE_DB = "lcsc_parts.sqlite"

# Subdirectory inside CACHE_DIR for downloaded LCSC component files
# (symbols, footprints, 3D models) before they're copied into a project.
LCSC_LIB_CACHE = "lcsc_libs"

# ---------------------------------------------------------------------------
# JLCPCB part sourcing
# ---------------------------------------------------------------------------

# Base URL for the jlcparts community mirror. Real data layout is:
#   /data/index.json                       — category manifest
#   /data/<sourcename>.json.gz             — per-category component dump
#   /data/<sourcename>.stock.json          — per-category stock map
# lcsc_client.populate_cache() downloads all categories on first use
# (~17 MB total, ~1300 files) into a local SQLite cache; subsequent
# queries are pure local SQL.
JLCSEARCH_BASE = "https://yaqwsx.github.io/jlcparts"

# When sourcing parts, hard-prefer JLCPCB basic library (no setup fee).
# Extended-tier parts are allowed but always warned with cost impact.
PREFER_BASIC_PARTS = True

# Per-unique-extended-part assembly setup fee in USD. Used to compute
# cost-delta warnings when an extended part is the only viable option.
# Source: JLCPCB SMT assembly pricing as of 2026.
EXTENDED_PART_SETUP_FEE_USD = 3.00

# ---------------------------------------------------------------------------
# JLCPCB fab defaults (used when generating manufacturing files)
# ---------------------------------------------------------------------------

# Defaults match the most common JLCPCB 2-layer order. Can be overridden
# per-project in the project's manifest.
JLCPCB_DEFAULT_RULES = {
    "layers": 2,
    "thickness_mm": 1.6,
    "min_track_width_mm": 0.127,  # 5 mil
    "min_clearance_mm": 0.127,
    "min_via_diameter_mm": 0.45,
    "min_via_drill_mm": 0.30,
    "min_hole_size_mm": 0.30,
}

# Protel-style file extensions JLCPCB expects in the Gerber zip.
# Maps KiCad layer name -> Protel extension. The gerber_pack module
# renames KiCad's defaults to this scheme before zipping.
JLCPCB_GERBER_EXTENSIONS = {
    "F_Cu": "GTL",
    "B_Cu": "GBL",
    "F_Paste": "GTP",
    "B_Paste": "GBP",
    "F_Silkscreen": "GTO",
    "B_Silkscreen": "GBO",
    "F_Mask": "GTS",
    "B_Mask": "GBS",
    "Edge_Cuts": "GM1",
    # Inner layers for 4-layer boards
    "In1_Cu": "G2L",
    "In2_Cu": "G3L",
}

# Drill file extension JLCPCB expects (merged Excellon).
JLCPCB_DRILL_EXTENSION = "XLN"
