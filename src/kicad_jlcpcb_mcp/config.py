"""Configuration for kicad-jlcpcb MCP server.

All module-level constants live here. Other modules import this module
(`from . import config`) and access values as `config.FOO` so tests can
monkeypatch with `monkeypatch.setattr(config, "FOO", value)`.
"""

import os
from pathlib import Path

# ---------------------------------------------------------------------------
# KiCad
# ---------------------------------------------------------------------------

# Minimum supported KiCad major.minor for `kicad-cli`.
# 8.0 introduced the modern `kicad-cli` verbs we depend on.
KICAD_MIN_VERSION = (8, 0)

# Candidate executable names to probe on PATH.
KICAD_CLI_CANDIDATES = ("kicad-cli",)

# Well-known locations that never put kicad-cli on PATH. Probed in order
# after PATH comes up empty, so a macOS or Flatpak user isn't told to
# install KiCad when they already have it.
KICAD_CLI_FALLBACK_PATHS = (
    # macOS .app bundle — the official installer adds nothing to PATH.
    "/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli",
    "~/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli",
    # Windows default install roots.
    "C:/Program Files/KiCad/10.0/bin/kicad-cli.exe",
    "C:/Program Files/KiCad/9.0/bin/kicad-cli.exe",
    "C:/Program Files/KiCad/8.0/bin/kicad-cli.exe",
)

# Flatpak application id. A Flatpak KiCad exposes only a GUI launcher on
# PATH, so kicad-cli has to be reached through `flatpak run --command=`.
KICAD_FLATPAK_ID = "org.kicad.KiCad"

# ---------------------------------------------------------------------------
# KiCad footprint libraries
# ---------------------------------------------------------------------------

# Where KiCad's stock .pretty footprint libraries live. Resolved at call
# time by pcb.resolve_footprint_dir(), in this order:
#
#   1. KJLC_FOOTPRINT_DIR              — this plugin's override
#   2. KICAD{10,9,8}_FOOTPRINT_DIR     — KiCad's own convention, the same
#      variables its fp-lib-table entries expand
#   3. the candidates below
#
# Hardcoding the Linux path meant pcb_generate could not place a single
# footprint on macOS or Windows, even once kicad-cli was found there.
FOOTPRINT_DIR_ENV_VARS = (
    "KJLC_FOOTPRINT_DIR",
    "KICAD10_FOOTPRINT_DIR",
    "KICAD9_FOOTPRINT_DIR",
    "KICAD8_FOOTPRINT_DIR",
    "KICAD_FOOTPRINT_DIR",
)

FOOTPRINT_DIR_CANDIDATES = (
    # Linux distro packages
    "/usr/share/kicad/footprints",
    "/usr/local/share/kicad/footprints",
    # Flatpak
    "/var/lib/flatpak/app/org.kicad.KiCad/current/active/files/share/kicad/footprints",
    "~/.local/share/flatpak/app/org.kicad.KiCad/current/active/files/share/kicad/footprints",
    # macOS app bundle
    "/Applications/KiCad/KiCad.app/Contents/SharedSupport/footprints",
    "~/Applications/KiCad/KiCad.app/Contents/SharedSupport/footprints",
    # Windows
    "C:/Program Files/KiCad/10.0/share/kicad/footprints",
    "C:/Program Files/KiCad/9.0/share/kicad/footprints",
    "C:/Program Files/KiCad/8.0/share/kicad/footprints",
)

# ---------------------------------------------------------------------------
# Cache and workspace
# ---------------------------------------------------------------------------

# Local cache for LCSC part metadata SQLite db, downloaded library files,
# and any other intermediate state. Created on first use.
CACHE_DIR = Path.home() / ".cache" / "kicad-jlcpcb"

# SQLite filename inside CACHE_DIR for the resolved-part row cache.
# Rows carry live stock figures and expire after a short TTL; see
# lcsc_client.CACHE_TTL_SECONDS. There is no bulk catalog download.
LCSC_CACHE_DB = "lcsc_parts.sqlite"

# Subdirectory inside CACHE_DIR for downloaded LCSC component files
# (symbols, footprints, 3D models) before they're copied into a project.
LCSC_LIB_CACHE = "lcsc_libs"

# ---------------------------------------------------------------------------
# JLCPCB part sourcing
# ---------------------------------------------------------------------------

# Part data comes from two live services. Neither is an official JLCPCB
# API; both are overridable so a fork can point at a self-hosted mirror
# without editing code.
#
# This matters: the previous release hardcoded a single third-party URL
# (the jlcparts GitHub Pages mirror). Upstream retired that data layout,
# the URL started returning 404, and every part lookup broke silently for
# months. Keep these overridable.
#
#   JLCSEARCH_BASE  — free-text catalog search, with JLCPCB SMT stock and
#                     the basic/extended tier. Route: /components/list
#   EASYEDA_BASE    — per-C-number lookup (the only source that supports
#                     exact C-number resolution) and symbol/pin data.
JLCSEARCH_BASE = os.environ.get("KJLC_JLCSEARCH_BASE", "https://jlcsearch.tscircuit.com").rstrip(
    "/"
)

EASYEDA_BASE = os.environ.get("KJLC_EASYEDA_BASE", "https://easyeda.com").rstrip("/")

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
