"""Generate KiCad schematic files from a declarative netlist spec.

A "netlist spec" is the LLM-friendly intermediate representation between
"text description of a circuit" and "valid .kicad_sch on disk":

    {
      "components": [
        {
          "ref": "U1",
          "lcsc": "C12345",
          "value": "ESP32-S3-WROOM",
          "footprint": "kicad_jlcpcb:C12345_QFN-32",
          "pin_count": 5,
          "pins": [{"num": "1", "name": "GND"}, ...]
        },
        ...
      ],
      "nets": [
        {"name": "VCC_3V3", "connects": [["U1", "1"], ["C1", "1"]]},
        {"name": "GND",     "connects": [["U1", "GND"], ["C1", "2"]]}
      ]
    }

Net members may name a pin by number or by name; both are matched.

The output is a `.kicad_sch` that KiCad opens, whose netlist matches the
spec, and which passes ERC apart from genuinely unconnected pins.

**Connectivity is expressed with global labels, not drawn wires.** Two
global labels sharing a name are electrically one net in KiCad, so the
netlist is correct without solving schematic wire routing — which is a
layout problem, not a netlist one. The result is not pretty; it is
correct, and the pretty version is Phase 2's job.

Three things here are load-bearing and easy to break, each of which made
earlier output unloadable or silently unconnected:

  - **No comments.** KiCad's S-expression grammar has no `;` syntax. One
    comment line anywhere makes KiCad refuse the whole file with a bare
    "Failed to load schematic".
  - **No `(net ...)` nodes.** That is netlist grammar, not schematic
    grammar, and is likewise fatal on load.
  - **Every coordinate on the 1.27 mm grid.** Off-grid pins load fine and
    then refuse to connect to anything, so the schematic looks right while
    its nets silently do not exist.

`tests/test_schematic.py` guards all three, and its `TestKicadActuallyLoadsIt`
class runs the output through `kicad-cli` — the only check that can
actually answer whether the file is valid.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

# Schematic file format version. 20250114 is what KiCad 9 and 10 write.
# Older KiCad reads it; emitting the *older* 20231120 was not the problem,
# but matching what current KiCad writes avoids a silent upgrade on save.
KICAD_SCH_VERSION = "20250114"
KICAD_GENERATOR_VERSION = "9.0"

# Grid spacing for the auto-laid-out components, in KiCad's internal mm.
# Every coordinate must land on KiCad's 1.27 mm connection grid, origin
# included. Off-grid pins still *load*, but KiCad flags every one of them
# with `endpoint_off_grid` and refuses to connect them to anything, so the
# schematic looks right and nets silently don't exist. All of these are
# multiples of 2.54.
GRID_X_MM = 50.8
GRID_Y_MM = 50.8
GRID_COLUMNS = 4

# Keep the whole layout inside A4 (297 x 210 mm) so it opens on-sheet.
GRID_ORIGIN_X = 38.1
GRID_ORIGIN_Y = 38.1


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


def _safe_id(name: str) -> str:
    """Sanitise a string for use as a KiCad library symbol id."""
    return "".join(ch if ch.isalnum() or ch in "_-." else "_" for ch in name) or "SYM"


def _escape(s: str) -> str:
    """Escape a string for a KiCad S-expression quoted atom."""
    return str(s).replace("\\", "\\\\").replace('"', '\\"')


def _grid_position(index: int) -> tuple[float, float]:
    """Lay components out left-to-right, top-to-bottom on a fixed grid."""
    col = index % GRID_COLUMNS
    row = index // GRID_COLUMNS
    return (GRID_ORIGIN_X + col * GRID_X_MM, GRID_ORIGIN_Y + row * GRID_Y_MM)


# ---------------------------------------------------------------------------
# Symbol geometry
#
# KiCad symbol-local coordinates are Y-up, while sheet coordinates are
# Y-down, so a pin at local (px, py) on a symbol placed at (sx, sy) has its
# connection point at (sx + px, sy - py). Getting that inversion wrong puts
# every label a few millimetres off its pin, which KiCad reports as an
# unconnected pin rather than as a geometry error — so it is worth stating.
# ---------------------------------------------------------------------------

PIN_PITCH_MM = 2.54
PIN_LENGTH_MM = 2.54
BODY_HALF_WIDTH_MM = 6.35
BODY_MARGIN_MM = 2.54


def _pin_slots(pin_count: int) -> list[tuple[float, float, int]]:
    """Return one (local_x, local_y, angle) slot per pin.

    Pins fill the left edge top-to-bottom, then the right edge. `angle` is
    the direction the pin body points, which is what KiCad wants: 0 means
    the pin extends to the right (so it sits on the left edge).
    """
    left_count = (pin_count + 1) // 2
    rows = max(left_count, pin_count - left_count)
    top = (rows - 1) * PIN_PITCH_MM / 2.0
    edge = BODY_HALF_WIDTH_MM + PIN_LENGTH_MM

    slots: list[tuple[float, float, int]] = []
    for i in range(pin_count):
        if i < left_count:
            row, x, angle = i, -edge, 0
        else:
            row, x, angle = i - left_count, edge, 180
        slots.append((x, top - row * PIN_PITCH_MM, angle))
    return slots


def _body_half_height(pin_count: int) -> float:
    left_count = (pin_count + 1) // 2
    rows = max(left_count, pin_count - left_count)
    return (rows - 1) * PIN_PITCH_MM / 2.0 + BODY_MARGIN_MM


def _pin_labels(c: "ComponentSpec") -> list[tuple[str, str]]:
    """Return (number, name) for each pin, in slot order.

    A spec may give pins as dicts ({"num": "1", "name": "VIN"}), as bare
    strings, or not at all; all three appear in the wild. Fall back to
    sequential numbering so a component is never silently dropped.
    """
    out: list[tuple[str, str]] = []
    for i in range(c.pin_count):
        raw = c.pins[i] if i < len(c.pins) else None
        if isinstance(raw, dict):
            num = str(raw.get("num") or raw.get("number") or i + 1)
            name = str(raw.get("name") or num)
        elif isinstance(raw, str) and raw:
            num, name = str(i + 1), raw
        else:
            num = name = str(i + 1)
        out.append((num, name))
    return out


def _symbol_id(c: "ComponentSpec") -> str:
    """Library id for a component. Parts sharing an LCSC number share a symbol."""
    return _safe_id(c.lcsc or f"{c.ref}_{c.pin_count}")


# ---------------------------------------------------------------------------
# .kicad_sch emission
# ---------------------------------------------------------------------------

_FONT = "(effects\n\t\t\t\t\t(font\n\t\t\t\t\t\t(size 1.27 1.27)\n\t\t\t\t\t)\n\t\t\t\t)"


def _emit_lib_symbols(components: list["ComponentSpec"]) -> str:
    """Emit one library symbol per distinct part.

    The body is a plain rectangle with pins down each edge. It is not a
    faithful rendering of the real part, but it is a *valid* symbol with
    real pins, which is what connectivity and ERC need.
    """
    seen: dict[str, ComponentSpec] = {}
    for c in components:
        seen.setdefault(_symbol_id(c), c)

    blocks = []
    for sym_id, c in seen.items():
        half_h = _body_half_height(c.pin_count)
        pins = []
        for (x, y, angle), (num, pname) in zip(_pin_slots(c.pin_count), _pin_labels(c)):
            pins.append(
                f"\t\t\t\t(pin passive line\n"
                f"\t\t\t\t\t(at {x:g} {y:g} {angle})\n"
                f"\t\t\t\t\t(length {PIN_LENGTH_MM:g})\n"
                f'\t\t\t\t\t(name "{_escape(pname)}" {_FONT})\n'
                f'\t\t\t\t\t(number "{_escape(num)}" {_FONT})\n'
                f"\t\t\t\t)"
            )
        pin_text = "\n".join(pins)
        blocks.append(
            f'\t\t(symbol "kicad_jlcpcb:{sym_id}"\n'
            f"\t\t\t(pin_names\n\t\t\t\t(offset 0.762)\n\t\t\t)\n"
            f"\t\t\t(exclude_from_sim no)\n"
            f"\t\t\t(in_bom yes)\n"
            f"\t\t\t(on_board yes)\n"
            f'\t\t\t(property "Reference" "U"\n'
            f"\t\t\t\t(at 0 {half_h + 1.27:g} 0)\n\t\t\t\t{_FONT}\n\t\t\t)\n"
            f'\t\t\t(property "Value" "{_escape(c.value or sym_id)}"\n'
            f"\t\t\t\t(at 0 {-(half_h + 1.27):g} 0)\n\t\t\t\t{_FONT}\n\t\t\t)\n"
            f'\t\t\t(symbol "{sym_id}_0_1"\n'
            f"\t\t\t\t(rectangle\n"
            f"\t\t\t\t\t(start {-BODY_HALF_WIDTH_MM:g} {half_h:g})\n"
            f"\t\t\t\t\t(end {BODY_HALF_WIDTH_MM:g} {-half_h:g})\n"
            f"\t\t\t\t\t(stroke\n\t\t\t\t\t\t(width 0.254)\n\t\t\t\t\t\t(type default)\n\t\t\t\t\t)\n"
            f"\t\t\t\t\t(fill\n\t\t\t\t\t\t(type background)\n\t\t\t\t\t)\n"
            f"\t\t\t\t)\n"
            f"\t\t\t)\n"
            f'\t\t\t(symbol "{sym_id}_1_1"\n{pin_text}\n\t\t\t)\n'
            f"\t\t)"
        )
    return "\t(lib_symbols\n" + "\n".join(blocks) + "\n\t)" if blocks else "\t(lib_symbols)"


def _emit_symbol_instance(
    c: "ComponentSpec", x: float, y: float, sheet_uuid: str, project: str
) -> str:
    """Emit one placed symbol, including the `instances` block.

    KiCad 7+ requires `instances` for a reference designator to stick;
    without it the symbol loads but shows up as an unannotated `U?`.
    """
    half_h = _body_half_height(c.pin_count)
    lcsc_prop = ""
    if c.lcsc:
        lcsc_prop = (
            f'\t\t(property "LCSC" "{_escape(c.lcsc)}"\n'
            f"\t\t\t(at {x:g} {y:g} 0)\n"
            f"\t\t\t(effects\n\t\t\t\t(font\n\t\t\t\t\t(size 1.27 1.27)\n\t\t\t\t)\n"
            f"\t\t\t\t(hide yes)\n\t\t\t)\n\t\t)\n"
        )
    footprint_prop = (
        f'\t\t(property "Footprint" "{_escape(c.footprint)}"\n'
        f"\t\t\t(at {x:g} {y:g} 0)\n"
        f"\t\t\t(effects\n\t\t\t\t(font\n\t\t\t\t\t(size 1.27 1.27)\n\t\t\t\t)\n"
        f"\t\t\t\t(hide yes)\n\t\t\t)\n\t\t)\n"
    )
    return (
        f"\t(symbol\n"
        f'\t\t(lib_id "kicad_jlcpcb:{_symbol_id(c)}")\n'
        f"\t\t(at {x:g} {y:g} 0)\n"
        f"\t\t(unit 1)\n"
        f"\t\t(exclude_from_sim no)\n"
        f"\t\t(in_bom yes)\n"
        f"\t\t(on_board yes)\n"
        f"\t\t(dnp no)\n"
        f'\t\t(uuid "{_new_uuid()}")\n'
        f'\t\t(property "Reference" "{_escape(c.ref)}"\n'
        f"\t\t\t(at {x:g} {y - half_h - 2.54:g} 0)\n\t\t\t{_FONT}\n\t\t)\n"
        f'\t\t(property "Value" "{_escape(c.value)}"\n'
        f"\t\t\t(at {x:g} {y + half_h + 2.54:g} 0)\n\t\t\t{_FONT}\n\t\t)\n"
        f"{footprint_prop}{lcsc_prop}"
        f"\t\t(instances\n"
        f'\t\t\t(project "{_escape(project)}"\n'
        f'\t\t\t\t(path "/{sheet_uuid}"\n'
        f'\t\t\t\t\t(reference "{_escape(c.ref)}")\n'
        f"\t\t\t\t\t(unit 1)\n"
        f"\t\t\t\t)\n"
        f"\t\t\t)\n"
        f"\t\t)\n"
        f"\t)"
    )


def _emit_global_labels(
    components: list["ComponentSpec"], nets: list["NetSpec"]
) -> tuple[str, list[str]]:
    """Attach a global label to every pin that belongs to a net.

    Connectivity is expressed with global labels rather than drawn wires.
    Two global labels sharing a name are electrically one net in KiCad, so
    the netlist and ERC are correct without solving schematic routing —
    which is a layout problem, not a netlist one.

    Returns (text, warnings). A net member naming a pin the component
    doesn't have is reported rather than silently dropped.
    """
    placement = {c.ref: _grid_position(i) for i, c in enumerate(components)}
    by_ref = {c.ref: c for c in components}

    blocks: list[str] = []
    warnings: list[str] = []
    for net in nets:
        for ref, pin in net.connects:
            c = by_ref.get(ref)
            if c is None:
                continue  # already rejected by _validate
            labels = _pin_labels(c)
            slots = _pin_slots(c.pin_count)
            idx = next(
                (i for i, (num, pname) in enumerate(labels) if str(pin) in (num, pname)),
                None,
            )
            if idx is None:
                warnings.append(
                    f"net {net.name!r}: {ref} has no pin {pin!r} "
                    f"(has {', '.join(n for n, _ in labels[:8])}"
                    f"{' ...' if len(labels) > 8 else ''})"
                )
                continue
            sx, sy = placement[ref]
            px, py, angle = slots[idx]
            # Symbol-local Y is inverted relative to sheet Y.
            ax, ay = sx + px, sy - py
            # A left-edge pin (angle 0) takes its wire leftwards, so its
            # label must face left, and vice versa.
            label_angle = 180 if angle == 0 else 0
            blocks.append(
                f'\t(global_label "{_escape(net.name)}"\n'
                f"\t\t(shape input)\n"
                f"\t\t(at {ax:g} {ay:g} {label_angle})\n"
                f"\t\t(effects\n\t\t\t(font\n\t\t\t\t(size 1.27 1.27)\n\t\t\t)\n"
                f"\t\t\t(justify {'right' if label_angle == 180 else 'left'})\n\t\t)\n"
                f'\t\t(uuid "{_new_uuid()}")\n'
                f"\t)"
            )
    return "\n".join(blocks), warnings


def _emit_sheet_instances() -> str:
    return '\t(sheet_instances\n\t\t(path "/"\n\t\t\t(page "1")\n\t\t)\n\t)'


def _build_sch_text(
    name: str, components: list["ComponentSpec"], nets: list["NetSpec"]
) -> tuple[str, list[str]]:
    """Assemble the full .kicad_sch text. Returns (text, warnings)."""
    sheet_uuid = _new_uuid()
    project = _safe_id(name)

    instances = "\n".join(
        _emit_symbol_instance(c, *_grid_position(i), sheet_uuid, project)
        for i, c in enumerate(components)
    )
    labels, warnings = _emit_global_labels(components, nets)

    sections = [
        "(kicad_sch",
        f"\t(version {KICAD_SCH_VERSION})",
        '\t(generator "kicad_jlcpcb_mcp")',
        f'\t(generator_version "{KICAD_GENERATOR_VERSION}")',
        f'\t(uuid "{sheet_uuid}")',
        '\t(paper "A4")',
        f'\t(title_block\n\t\t(title "{_escape(name)}")\n\t)',
        _emit_lib_symbols(components),
        instances,
    ]
    if labels:
        sections.append(labels)
    sections.append(_emit_sheet_instances())
    sections.append(")")
    return "\n".join(x for x in sections if x) + "\n", warnings


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
    text, warnings = _build_sch_text(name, components, nets)
    path = Path(sch_path).expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)

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
