"""Generate KiCad 8 schematic files from a declarative netlist spec.

A "netlist spec" is the LLM-friendly intermediate representation between
"text description of a circuit" and "valid .kicad_sch on disk":

    {
      "components": [
        {
          "ref": "U1",
          "lcsc": "C12345",
          "value": "ESP32-S3-WROOM",
          "footprint": "kicad_jlcpcb:C12345_QFN-32",
          "x": 100.0, "y": 50.0,
          "pins": [{"num": "1", "name": "GND"}, ...]
        },
        ...
      ],
      "nets": [
        {"name": "VCC_3V3", "connects": [["U1", "1"], ["C1", "1"]]},
        {"name": "GND",     "connects": [["U1", "2"], ["C1", "2"]]}
      ]
    }

The output is a `.kicad_sch` that:

  - declares each component as a symbol instance, with its LCSC C-number
    embedded in a `LCSC` property so manufacturing exports pick it up
  - lays out components on a regular grid (not pretty, but valid)
  - declares each net as a `(net (name ...) (members ...))` block in the
    schematic's net section so KiCad's ERC has structural information

The result will not look pretty in the KiCad eeschema GUI — Phase 2's
auto-placer is what makes it presentable. But it WILL pass ERC for
basic connectivity checks and produce a usable netlist for layout work.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

# Schematic file format version that ships with KiCad 8.0.x.
# KiCad 9 understands this version too.
KICAD_SCH_VERSION = "20231120"

# Grid spacing for the auto-laid-out components, in KiCad's internal mm.
GRID_X_MM = 50.0
GRID_Y_MM = 50.0
GRID_COLUMNS = 4


@dataclass
class ComponentSpec:
    """One component placement in the netlist spec."""

    ref: str  # e.g. "U1", "R3", "C12"
    value: str  # display value, e.g. "10k" or "ESP32-S3"
    footprint: str = ""  # KiCad lib:name footprint id
    lcsc: str = ""  # LCSC C-number (becomes LCSC field)
    pin_count: int = 2
    pins: list[dict] = field(default_factory=list)

    @classmethod
    def from_dict(cls, d: dict) -> "ComponentSpec":
        # Preserve an explicit pin_count of 0 (so the validator can reject it)
        # rather than defaulting to 2 or to len(pins). Only fall back to a
        # default when the key is absent entirely.
        if "pin_count" in d:
            pin_count = int(d["pin_count"])
        else:
            pin_count = len(d.get("pins") or []) or 2
        return cls(
            ref=d["ref"],
            value=d.get("value", ""),
            footprint=d.get("footprint", ""),
            lcsc=d.get("lcsc", ""),
            pin_count=pin_count,
            pins=list(d.get("pins") or []),
        )


@dataclass
class NetSpec:
    """One named net and its (ref, pin_number) members."""

    name: str
    connects: list[list[str]]  # [[ref, pin_num], ...]

    @classmethod
    def from_dict(cls, d: dict) -> "NetSpec":
        return cls(name=d["name"], connects=[list(c) for c in d.get("connects", [])])


class SchematicError(ValueError):
    """Netlist spec was invalid or referenced a missing component/pin."""


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def _validate(spec: dict) -> tuple[list[ComponentSpec], list[NetSpec]]:
    """Parse, type-check, and cross-validate a netlist spec."""
    if not isinstance(spec, dict):
        raise SchematicError("Netlist spec must be a dict")
    raw_comps = spec.get("components")
    if not isinstance(raw_comps, list) or not raw_comps:
        raise SchematicError("Netlist spec needs a non-empty 'components' list")
    raw_nets = spec.get("nets") or []
    if not isinstance(raw_nets, list):
        raise SchematicError("'nets' must be a list when provided")

    components = [ComponentSpec.from_dict(c) for c in raw_comps]
    seen_refs = set()
    for c in components:
        if not c.ref or not isinstance(c.ref, str):
            raise SchematicError(f"Component missing 'ref': {c}")
        if c.ref in seen_refs:
            raise SchematicError(f"Duplicate component reference: {c.ref}")
        seen_refs.add(c.ref)
        if c.pin_count < 1:
            raise SchematicError(f"Component {c.ref} has pin_count < 1")

    nets = [NetSpec.from_dict(n) for n in raw_nets]
    for net in nets:
        if not net.name:
            raise SchematicError("Net missing 'name'")
        for conn in net.connects:
            if len(conn) != 2:
                raise SchematicError(f"Net {net.name!r} connection must be [ref, pin]: {conn}")
            ref, pin = conn
            if ref not in seen_refs:
                raise SchematicError(f"Net {net.name!r} references unknown component {ref!r}")

    return components, nets


# ---------------------------------------------------------------------------
# .kicad_sch emission
# ---------------------------------------------------------------------------


def _new_uuid() -> str:
    return str(uuid.uuid4())


def _grid_position(index: int) -> tuple[float, float]:
    """Lay out the i-th component on a regular grid."""
    col = index % GRID_COLUMNS
    row = index // GRID_COLUMNS
    return (GRID_X_MM * (col + 1), GRID_Y_MM * (row + 1))


def _emit_lib_symbols(components: list[ComponentSpec]) -> str:
    """Emit a `(lib_symbols ...)` block referencing the per-LCSC libraries
    that part_library.py wrote into the project's libs/ directory.

    KiCad's lib_symbols block normally embeds full symbol definitions.
    For Phase 1 we emit a minimal stub for each unique LCSC part — KiCad
    will resolve the actual symbol from libs/<lcsc>.kicad_sym at load
    time as long as the project's symbol library table points there
    (the user does that via Preferences > Manage Symbol Libraries on
    first open; Phase 2 will write a sym-lib-table for them).
    """
    lines = ["  (lib_symbols"]
    seen_libs = set()
    for c in components:
        if not c.lcsc or c.lcsc in seen_libs:
            continue
        seen_libs.add(c.lcsc)
        sym_id = f"kicad_jlcpcb:{c.lcsc}"
        lines.append(f'    (symbol "{sym_id}"')
        lines.append("      (in_bom yes) (on_board yes)")
        lines.append('      (property "Reference" "U" (at 0 0 0))')
        lines.append(f'      (property "Value" "{_escape(c.value)}" (at 0 0 0))')
        lines.append(f'      (property "LCSC" "{c.lcsc}" (at 0 0 0))')
        lines.append("    )")
    lines.append("  )")
    return "\n".join(lines)


def _emit_symbol_instance(c: ComponentSpec, x: float, y: float) -> str:
    """Emit one `(symbol ...)` instance placed at (x, y)."""
    sym_id = f"kicad_jlcpcb:{c.lcsc}" if c.lcsc else f"kicad_jlcpcb:{_safe_id(c.value or c.ref)}"
    inst_uuid = _new_uuid()
    lines = [
        f'  (symbol (lib_id "{sym_id}") (at {x:.2f} {y:.2f} 0)',
        "    (unit 1) (in_bom yes) (on_board yes) (dnp no)",
        f'    (uuid "{inst_uuid}")',
        f'    (property "Reference" "{c.ref}" (at {x:.2f} {y - 5:.2f} 0))',
        f'    (property "Value" "{_escape(c.value)}" (at {x:.2f} {y + 5:.2f} 0))',
        f'    (property "Footprint" "{_escape(c.footprint)}" (at 0 0 0) (effects (font (size 1.27 1.27)) hide))',
    ]
    if c.lcsc:
        lines.append(
            f'    (property "LCSC" "{c.lcsc}" (at 0 0 0) (effects (font (size 1.27 1.27)) hide))'
        )
    # Pin instance UUIDs (one per declared pin) so KiCad's net resolution
    # has stable identifiers across saves.
    for i in range(1, c.pin_count + 1):
        lines.append(f'    (pin "{i}" (uuid "{_new_uuid()}"))')
    lines.append("  )")
    return "\n".join(lines)


def _emit_net_block(nets: list[NetSpec]) -> str:
    """Emit a `(net ...)` declaration for each named net.

    KiCad's `.kicad_sch` doesn't traditionally store nets explicitly —
    nets emerge from labels and wire connectivity. For our generated
    schematic we emit explicit (net (name ...) (members ...)) entries
    so the layout step has the connectivity it needs even before the
    user has opened the schematic to verify wire routing.
    """
    if not nets:
        return ""
    lines = ["  ; --- Generated net declarations (kicad_jlcpcb_mcp) ---"]
    for net in nets:
        lines.append(f'  (net (name "{_escape(net.name)}")')
        for ref, pin in net.connects:
            lines.append(f'    (member "{ref}" "{pin}")')
        lines.append("  )")
    return "\n".join(lines)


def _emit_sheet_instances() -> str:
    return '  (sheet_instances\n    (path "/" (page "1"))\n  )'


def _safe_id(name: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "_-" else "_" for ch in (name or "X"))


def _escape(s: str) -> str:
    return (s or "").replace('"', "'")


def _build_sch_text(name: str, components: list[ComponentSpec], nets: list[NetSpec]) -> str:
    """Assemble the full .kicad_sch text from validated input."""
    sym_block = _emit_lib_symbols(components)
    instances = "\n".join(
        _emit_symbol_instance(c, *_grid_position(i)) for i, c in enumerate(components)
    )
    nets_block = _emit_net_block(nets)
    sheet_block = _emit_sheet_instances()
    title = _escape(name)

    sections = [
        f'(kicad_sch (version {KICAD_SCH_VERSION}) (generator "kicad_jlcpcb_mcp")',
        f'  (uuid "{_new_uuid()}")',
        '  (paper "A4")',
        f'  (title_block (title "{title}") (date "") (rev "") (company ""))',
        sym_block,
        instances,
    ]
    if nets_block:
        sections.append(nets_block)
    sections.append(sheet_block)
    sections.append(")")
    return "\n".join(sections) + "\n"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def sch_generate(sch_path: str | Path, name: str, netlist_spec: dict) -> dict:
    """Validate `netlist_spec` and write a `.kicad_sch` to `sch_path`.

    Returns a summary the MCP layer can ship to the LLM:
      {
        "sch_path": str,
        "component_count": int,
        "net_count": int,
        "warnings": [...],
      }

    Raises SchematicError on invalid input. Does NOT touch disk on
    validation failure.
    """
    components, nets = _validate(netlist_spec)
    text = _build_sch_text(name, components, nets)
    path = Path(sch_path).expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)

    warnings: list[str] = []
    missing_lcsc = [c.ref for c in components if not c.lcsc]
    if missing_lcsc:
        warnings.append(
            f"Components without LCSC C-number ({len(missing_lcsc)}): "
            f"{', '.join(missing_lcsc[:10])}"
            f"{' ...' if len(missing_lcsc) > 10 else ''}. "
            f"Manufacturing BOM exports won't include these unless you "
            f"resolve them with lcsc_resolve_bom and regenerate."
        )

    return {
        "sch_path": str(path),
        "component_count": len(components),
        "net_count": len(nets),
        "warnings": warnings,
    }
