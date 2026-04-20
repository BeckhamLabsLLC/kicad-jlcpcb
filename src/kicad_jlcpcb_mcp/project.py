"""Project workspace management.

A "project" is a directory containing:
  - <name>.kicad_pro    project manifest (KiCad 8/9 JSON)
  - <name>.kicad_sch    root schematic (s-expression)
  - <name>.kicad_pcb    board file (s-expression, optional until layout starts)
  - libs/               local symbol/footprint/3D libraries fetched from LCSC
  - manufacturing/      output Gerber zips (created on first package run)

This module handles create / load / validate. It does NOT touch KiCad files
beyond writing the minimum scaffolding required for `kicad-cli` to recognize
the project — schematic content is added by `schematic.py`.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path

from . import config

logger = logging.getLogger(__name__)

# KiCad project manifest schema version. KiCad 8/9 use schema 1.
# We pin this so we can detect future incompatibilities at load time.
_PROJECT_SCHEMA_VERSION = 1

# Filename validation: KiCad allows most printable chars in project names,
# but we lock down to a safe subset to avoid path-injection and to keep
# generated filenames cross-platform clean.
_VALID_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_\-]*$")


class ProjectError(ValueError):
    """Raised when a project create/load/validate operation fails."""


@dataclass
class Project:
    """A loaded KiCad project workspace.

    `root` is the project directory. The four file paths are derived from
    `<root>/<name>.kicad_*` and may not exist yet (in particular .kicad_pcb
    won't exist until the user opens the schematic in KiCad and creates a
    board, or until Phase 2 generates one).
    """

    name: str
    root: Path
    pro_path: Path
    sch_path: Path
    pcb_path: Path
    libs_dir: Path
    manufacturing_dir: Path

    def to_dict(self) -> dict:
        """Serialize for MCP responses. Paths become strings."""
        return {
            "name": self.name,
            "root": str(self.root),
            "pro_path": str(self.pro_path),
            "sch_path": str(self.sch_path),
            "pcb_path": str(self.pcb_path),
            "libs_dir": str(self.libs_dir),
            "manufacturing_dir": str(self.manufacturing_dir),
            "has_pcb": self.pcb_path.exists(),
            "has_sch": self.sch_path.exists(),
        }


def _validate_name(name: str) -> None:
    if not _VALID_NAME.match(name):
        raise ProjectError(
            f"Invalid project name {name!r}: must start with a letter or digit "
            "and contain only letters, digits, underscore, or hyphen."
        )


def _project_paths(root: Path, name: str) -> Project:
    return Project(
        name=name,
        root=root,
        pro_path=root / f"{name}.kicad_pro",
        sch_path=root / f"{name}.kicad_sch",
        pcb_path=root / f"{name}.kicad_pcb",
        libs_dir=root / "libs",
        manufacturing_dir=root / "manufacturing",
    )


def _empty_pro_manifest(name: str) -> dict:
    """Minimum .kicad_pro JSON that KiCad 8/9 will open without complaint.

    KiCad's full schema is large; we write only the keys it actually requires
    and let KiCad fill in the rest on first open. The board_design_settings
    block carries our JLCPCB-tuned design rules so they're applied as soon
    as a PCB is created.
    """
    rules = config.JLCPCB_DEFAULT_RULES
    return {
        "meta": {
            "filename": f"{name}.kicad_pro",
            "version": _PROJECT_SCHEMA_VERSION,
        },
        "board": {
            "design_settings": {
                "rules": {
                    "min_clearance": rules["min_clearance_mm"],
                    "min_track_width": rules["min_track_width_mm"],
                    "min_via_diameter": rules["min_via_diameter_mm"],
                    "min_via_drill": rules["min_via_drill_mm"],
                    "min_hole_to_hole": rules["min_hole_size_mm"],
                },
            },
        },
        "schematic": {
            "legacy_lib_dir": "",
            "legacy_lib_list": [],
        },
        "libraries": {
            "pinned_symbol_libs": [],
            "pinned_footprint_libs": [],
        },
        "net_settings": {
            "classes": [
                {
                    "name": "Default",
                    "clearance": rules["min_clearance_mm"],
                    "track_width": rules["min_track_width_mm"],
                    "via_diameter": rules["min_via_diameter_mm"],
                    "via_drill": rules["min_via_drill_mm"],
                }
            ],
        },
    }


def _empty_sch_skeleton(name: str) -> str:
    """Minimal valid .kicad_sch for KiCad 8.

    A real schematic file is much larger but KiCad will accept this as a
    valid empty root sheet and fill in defaults on save. The s-expression
    layout matches KiCad 8's `version 20231120` format.
    """
    return (
        '(kicad_sch (version 20231120) (generator "kicad_jlcpcb_mcp")\n'
        f'  (uuid "00000000-0000-0000-0000-000000000000")\n'
        f'  (paper "A4")\n'
        f'  (title_block (title "{name}") (date "") (rev "") (company ""))\n'
        f"  (lib_symbols)\n"
        f"  (sheet_instances\n"
        f'    (path "/" (page "1"))\n'
        f"  )\n"
        f")\n"
    )


def create_project(parent_dir: str | Path, name: str) -> Project:
    """Create a new KiCad project under `parent_dir/name/`.

    Writes:
      - <name>.kicad_pro with JLCPCB-tuned default design rules
      - <name>.kicad_sch as a minimal valid empty schematic
      - libs/             empty (populated by fetch_part_library)
      - manufacturing/    empty (populated by package_for_jlcpcb)

    Raises ProjectError if the directory already exists or the name is
    invalid. The .kicad_pcb is intentionally NOT created — KiCad creates
    it the first time the user opens the schematic and creates a board,
    and Phase 2 will generate one programmatically.
    """
    _validate_name(name)
    parent = Path(parent_dir).expanduser().resolve()
    if not parent.is_dir():
        raise ProjectError(f"Parent directory does not exist: {parent}")

    root = parent / name
    if root.exists():
        raise ProjectError(f"Project directory already exists: {root}")

    project = _project_paths(root, name)
    root.mkdir()
    project.libs_dir.mkdir()
    project.manufacturing_dir.mkdir()

    project.pro_path.write_text(json.dumps(_empty_pro_manifest(name), indent=2))
    project.sch_path.write_text(_empty_sch_skeleton(name))

    return project


def load_project(project_path: str | Path) -> Project:
    """Load and validate an existing KiCad project.

    `project_path` may be either the .kicad_pro file or its containing dir.
    Returns a Project. Raises ProjectError if the .kicad_pro is missing,
    unparseable, or uses an unsupported schema version.
    """
    p = Path(project_path).expanduser().resolve()
    if p.is_dir():
        candidates = sorted(p.glob("*.kicad_pro"))
        if not candidates:
            raise ProjectError(f"No .kicad_pro file found in {p}")
        if len(candidates) > 1:
            raise ProjectError(
                f"Multiple .kicad_pro files in {p}: {[c.name for c in candidates]}. "
                "Pass the specific .kicad_pro path you want."
            )
        pro_path = candidates[0]
    elif p.is_file() and p.suffix == ".kicad_pro":
        pro_path = p
    else:
        raise ProjectError(f"Not a KiCad project: {p}")

    try:
        manifest = json.loads(pro_path.read_text())
    except json.JSONDecodeError as e:
        raise ProjectError(f"{pro_path} is not valid JSON: {e}") from e

    schema = manifest.get("meta", {}).get("version")
    if schema is not None and schema > _PROJECT_SCHEMA_VERSION + 1:
        # Allow current + 1 for forward compat; reject anything wildly newer.
        raise ProjectError(
            f"{pro_path} uses schema version {schema}; this plugin supports "
            f"up to {_PROJECT_SCHEMA_VERSION + 1}. Upgrade the plugin."
        )

    name = pro_path.stem
    project = _project_paths(pro_path.parent, name)

    # Ensure libs/ and manufacturing/ exist (idempotent for round-tripping
    # an existing project that was created outside this plugin).
    project.libs_dir.mkdir(exist_ok=True)
    project.manufacturing_dir.mkdir(exist_ok=True)

    return project


def manifest(project: Project) -> dict:
    """Return a structured manifest of the project's current state.

    Used by load_project's MCP response so the LLM can see what's already
    in the project (schematic? PCB? library count?) before deciding what
    to do next.
    """
    lib_files = sorted(project.libs_dir.rglob("*")) if project.libs_dir.exists() else []
    return {
        **project.to_dict(),
        "lib_file_count": sum(1 for f in lib_files if f.is_file()),
    }
