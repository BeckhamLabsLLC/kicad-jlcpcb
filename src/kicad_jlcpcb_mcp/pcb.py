"""PCB generation from a declarative spec using KiCad's pcbnew Python API.

This is the Phase 1.5 addition that closes the gap between "I have a
BOM and I know how things connect" and "I have a .kicad_pcb ready for
routing in KiCad."

A **PCB spec** is a JSON-serializable dict:

    {
      "name": "soilnode",
      "board": {
        "width_mm": 150.0,
        "height_mm": 100.0,
        "layer_count": 2
      },
      "components": [
        {
          "ref": "U1",
          "value": "ESP32-C3-WROOM-02-N4",
          "lcsc": "C2934560",
          "lib": "RF_Module",
          "fp": "ESP32-C3-WROOM-02",
          "pinmap": {"GND": "1", "3V3": "2", "GPIO10": "15", ...}
        },
        ...
      ],
      "nets": {
        "VCC": [["U1", "3V3"], ["C1", "1"], ["U2", "VBAT"]],
        "GND": [["U1", "GND"], ["C1", "2"], ["U1", "EP"]],
        ...
      }
    }

Each component's `pinmap` maps **function names** (like "GPIO10" or
"VCC") to **pad numbers** (strings like "15" or "A4"). Nets reference
either function names (resolved via the pinmap) or bare pad numbers.

The tool places components in a type-classified auto-layout (connectors
first, then ICs with generous spacing, then passives in a tight grid),
assigns nets to every referenced pad, draws a board outline on
Edge.Cuts, and saves. Result: a `.kicad_pcb` the user can open in KiCad,
rearrange to their liking, and then route (manually or with Freerouting
from KiCad's menu).

This module does NOT route traces. Routing is Phase 2 and is best done
interactively in KiCad where the user has visual feedback. Phase 1.5's
job is to eliminate the weeks of tedious wire-drawing from the workflow.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import config

logger = logging.getLogger(__name__)

# pcbnew is a KiCad-side module and may not be importable in tests where
# KiCad isn't installed. We import it lazily so the plugin can still
# start and other tools can still run when KiCad is missing.
pcbnew = None  # populated by _ensure_pcbnew()


def resolve_footprint_dir(explicit: str | None = None) -> str | None:
    """Locate KiCad's stock footprint libraries. Returns None if not found.

    Order: an explicit path, then the environment variables KiCad itself
    uses (the same ones its `fp-lib-table` entries expand), then the
    known install locations per platform.

    This used to be the hardcoded string "/usr/share/kicad/footprints",
    which meant `pcb_generate` could not place a single footprint on
    macOS or Windows — including for users whose KiCad the plugin had
    just successfully detected.
    """
    if explicit:
        return explicit if Path(explicit).expanduser().is_dir() else None

    for var in config.FOOTPRINT_DIR_ENV_VARS:
        value = os.environ.get(var)
        if value and Path(value).expanduser().is_dir():
            return str(Path(value).expanduser())

    for candidate in config.FOOTPRINT_DIR_CANDIDATES:
        path = Path(candidate).expanduser()
        if path.is_dir():
            return str(path)
    return None


def _footprint_dir_error() -> str:
    """Message for when no footprint library could be found anywhere."""
    return (
        "KiCad's footprint libraries could not be found. Looked at "
        f"{', '.join(config.FOOTPRINT_DIR_ENV_VARS)} and "
        f"{len(config.FOOTPRINT_DIR_CANDIDATES)} standard install locations.\n"
        "Set KJLC_FOOTPRINT_DIR to the directory containing the .pretty "
        "folders (e.g. /usr/share/kicad/footprints), or pass lib_dir to "
        "pcb_generate."
    )


# Kept for backwards compatibility; prefer resolve_footprint_dir().
LIB_DIR_DEFAULT = "/usr/share/kicad/footprints"

# Layout constants — match the SoilNode trial that produced a usable board
IC_SPACING_MM_DEFAULT = 20.0
PASSIVE_SPACING_MM_DEFAULT = 7.0
CONN_SPACING_MM_DEFAULT = 15.0
GRID_ORIGIN_MM_DEFAULT = 10.0


class PcbGenerationError(RuntimeError):
    """Something went wrong during PCB generation (spec invalid, footprint
    not found, pad missing, pcbnew unavailable, etc)."""


def _ensure_pcbnew():
    """Import pcbnew lazily. Raises PcbGenerationError if KiCad isn't installed."""
    global pcbnew
    if pcbnew is not None:
        return pcbnew
    try:
        import pcbnew as _pcbnew  # type: ignore
    except ImportError as e:
        raise PcbGenerationError(
            "KiCad's pcbnew Python module is not available. Install KiCad "
            "8 or newer (`sudo dnf install kicad` on Fedora) — pcbnew ships "
            "with the main KiCad package."
        ) from e
    pcbnew = _pcbnew
    return pcbnew


@dataclass
class GenerationResult:
    pcb_path: Path
    footprints_placed: int
    nets_created: int
    net_stats: dict[str, int] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "pcb_path": str(self.pcb_path),
            "footprints_placed": self.footprints_placed,
            "nets_created": self.nets_created,
            "net_stats": self.net_stats,
            "warnings": self.warnings,
            "errors": self.errors,
            "success": not self.errors,
        }


# ---------------------------------------------------------------------------
# Spec validation
# ---------------------------------------------------------------------------


def _validate_spec(spec: dict) -> tuple[dict, list[dict], dict[str, list]]:
    """Parse and type-check a PCB spec. Returns (board_cfg, components, nets)."""
    if not isinstance(spec, dict):
        raise PcbGenerationError("Spec must be a dict")

    board = spec.get("board") or {}
    if not isinstance(board, dict):
        raise PcbGenerationError("spec.board must be a dict")
    board_cfg = {
        "width_mm": float(board.get("width_mm", 100.0)),
        "height_mm": float(board.get("height_mm", 80.0)),
        "layer_count": int(board.get("layer_count", 2)),
    }
    if board_cfg["layer_count"] not in (2, 4):
        raise PcbGenerationError(
            f"Only 2 or 4 layer boards supported (got {board_cfg['layer_count']})"
        )

    raw_components = spec.get("components")
    if not isinstance(raw_components, list) or not raw_components:
        raise PcbGenerationError("spec.components must be a non-empty list")
    seen_refs: set[str] = set()
    for i, c in enumerate(raw_components):
        if not isinstance(c, dict):
            raise PcbGenerationError(f"components[{i}] must be a dict")
        for key in ("ref", "lib", "fp"):
            if not isinstance(c.get(key), str) or not c[key]:
                raise PcbGenerationError(f"components[{i}].{key} is required")
        if c["ref"] in seen_refs:
            raise PcbGenerationError(f"Duplicate component ref: {c['ref']}")
        seen_refs.add(c["ref"])
        if "pinmap" in c and not isinstance(c["pinmap"], dict):
            raise PcbGenerationError(f"components[{i}].pinmap must be a dict if provided")

    raw_nets = spec.get("nets") or {}
    if not isinstance(raw_nets, dict):
        raise PcbGenerationError("spec.nets must be a dict")
    nets: dict[str, list[tuple[str, str]]] = {}
    for name, members in raw_nets.items():
        if not isinstance(members, list):
            raise PcbGenerationError(f"nets[{name!r}] must be a list")
        resolved: list[tuple[str, str]] = []
        for m in members:
            if not isinstance(m, (list, tuple)) or len(m) != 2:
                raise PcbGenerationError(f"nets[{name!r}] member must be [ref, pin]: got {m!r}")
            ref, pin = str(m[0]), str(m[1])
            if ref not in seen_refs:
                raise PcbGenerationError(f"nets[{name!r}] references unknown component {ref!r}")
            resolved.append((ref, pin))
        nets[name] = resolved

    return board_cfg, list(raw_components), nets


# ---------------------------------------------------------------------------
# Component classification + placement
# ---------------------------------------------------------------------------


def _classify(ref: str) -> str:
    """Bucket a reference designator by physical size class."""
    if ref.startswith(("U", "Q", "Y")):
        return "ic"
    if ref.startswith("J"):
        return "conn"
    return "passive"


def _place(
    components: list[dict],
    *,
    board_width_mm: float,
    board_height_mm: float,
    ic_spacing: float = IC_SPACING_MM_DEFAULT,
    passive_spacing: float = PASSIVE_SPACING_MM_DEFAULT,
    conn_spacing: float = CONN_SPACING_MM_DEFAULT,
    origin: float = GRID_ORIGIN_MM_DEFAULT,
) -> dict[str, tuple[float, float]]:
    """Three-band layout: connectors on top, ICs in middle, passives below."""
    positions: dict[str, tuple[float, float]] = {}
    by_class: dict[str, list[dict]] = {"conn": [], "ic": [], "passive": []}
    for c in components:
        by_class[_classify(c["ref"])].append(c)

    # Band 1: connectors
    x, y = origin, origin
    for c in by_class["conn"]:
        positions[c["ref"]] = (x, y)
        x += conn_spacing
        if x > board_width_mm - origin:
            x = origin
            y += conn_spacing

    # Band 2: ICs
    y += 16.0
    x = origin
    for c in by_class["ic"]:
        positions[c["ref"]] = (x, y)
        x += ic_spacing
        if x > board_width_mm - origin:
            x = origin
            y += ic_spacing

    # Band 3: passives
    y += 14.0
    x = origin
    max_cols = max(1, int((board_width_mm - 2 * origin) / passive_spacing))
    col = 0
    for c in by_class["passive"]:
        positions[c["ref"]] = (x, y)
        col += 1
        x += passive_spacing
        if col >= max_cols:
            col = 0
            x = origin
            y += passive_spacing

    return positions


# ---------------------------------------------------------------------------
# pcbnew board construction
# ---------------------------------------------------------------------------


def _mm(pcb, x: float) -> int:
    return pcb.FromMM(x)


def _load_footprint(pcb, board, lib: str, fp: str, lib_dir: str):
    """Load a footprint from the KiCad standard library and add it to the board."""
    lib_path = f"{lib_dir}/{lib}.pretty"
    if not Path(lib_path).is_dir():
        available = sorted(p.stem for p in Path(lib_dir).glob("*.pretty"))
        hint = ""
        if available:
            close = [n for n in available if lib.lower() in n.lower() or n.lower() in lib.lower()]
            hint = f" Did you mean: {', '.join(close[:5])}?" if close else ""
        raise PcbGenerationError(
            f"Footprint library not found: {lib_path} "
            f"({len(available)} libraries present in {lib_dir}).{hint}"
        )
    f = pcb.FootprintLoad(lib_path, fp)
    if f is None:
        raise PcbGenerationError(
            f"Footprint not found: {lib}/{fp}. The library exists but has no "
            f"footprint by that name — check the exact spelling in KiCad's "
            f"footprint browser."
        )
    board.Add(f)
    return f


def _add_board_outline(pcb, board, width_mm: float, height_mm: float) -> None:
    """Draw a rectangular board outline on Edge.Cuts."""
    edge_layer = board.GetLayerID("Edge.Cuts")
    corners = [(0, 0), (width_mm, 0), (width_mm, height_mm), (0, height_mm)]
    for i in range(len(corners)):
        (x1, y1) = corners[i]
        (x2, y2) = corners[(i + 1) % len(corners)]
        seg = pcb.PCB_SHAPE(board)
        seg.SetLayer(edge_layer)
        seg.SetShape(pcb.SHAPE_T_SEGMENT)
        seg.SetStart(pcb.VECTOR2I(_mm(pcb, x1), _mm(pcb, y1)))
        seg.SetEnd(pcb.VECTOR2I(_mm(pcb, x2), _mm(pcb, y2)))
        seg.SetWidth(_mm(pcb, 0.1))
        board.Add(seg)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def _refs_needing_pin_names(nets: dict[str, list[tuple[str, str]]]) -> set[str]:
    """Refs whose nets reference at least one pin by name rather than number.

    Only these need an EasyEDA pin map. A decoupling cap wired as
    ("C1", "1") needs nothing fetched — its pads are already numbers.

    This is most of the wait on a typical board. EasyEDA is rate-limited
    to one request per 12 seconds, and the old code fetched for every
    component carrying an LCSC number, so a 13-part board with 9 passives
    spent ~108 seconds fetching maps it then never used.
    """
    needed: set[str] = set()
    for members in nets.values():
        for ref, pin in members:
            if not str(pin).isdigit():
                needed.add(ref)
    return needed


async def _resolve_pinmaps(
    components: list[dict],
    *,
    auto_fetch: bool,
    force_refresh: bool,
    needed_refs: set[str] | None = None,
) -> dict[str, dict[str, str]]:
    """For each component, build the pin-name → pad-number map.

    Priority:
      1. An explicit `pinmap` on the component spec (user-provided override)
      2. Auto-fetched from EasyEDA via `part_library.get_pin_map` if
         `auto_fetch=True` and the component has an `lcsc` field
      3. Empty dict (nets must use bare pad numbers)

    Returns (maps_by_ref, warnings). A failed fetch yields an empty map
    *and* a warning naming the cause.

    The warning matters more than it looks. When an EasyEDA fetch fails,
    every net referencing that part by pin name fails to resolve, and the
    net-assignment phase reports "U1 has no pad 'GPIO10'" once per pin.
    Read on its own, that says the caller's net names are wrong; the real
    cause is a 403 or a timeout. Without this, the model confidently
    rewrites a correct netlist to chase an error that was never about the
    netlist.
    """
    from . import part_library

    resolved: dict[str, dict[str, str]] = {}
    warnings: list[str] = []
    for comp in components:
        ref = comp["ref"]
        explicit = comp.get("pinmap") or {}
        if explicit:
            resolved[ref] = dict(explicit)
            continue
        if needed_refs is not None and ref not in needed_refs:
            # Every net touching this part already uses pad numbers, so a
            # pin-name map would be fetched and discarded.
            resolved[ref] = {}
            continue
        if auto_fetch and comp.get("lcsc"):
            try:
                pm = await part_library.get_pin_map(comp["lcsc"], force_refresh=force_refresh)
                resolved[ref] = pm.get("pinmap", {})
                if not resolved[ref]:
                    warnings.append(
                        f"{ref} ({comp['lcsc']}): EasyEDA returned no named pins. "
                        f"Nets must reference this part by pad number, or supply "
                        f"an explicit 'pinmap' on the component."
                    )
            except part_library.PartLibraryError as e:
                resolved[ref] = {}
                warnings.append(
                    f"{ref} ({comp['lcsc']}): pin-map fetch failed — {e}. "
                    f"Any 'no pad <name>' errors below are caused by this, not "
                    f"by your net names. Retry, or supply an explicit 'pinmap'."
                )
                logger.warning("Pin-map fetch failed for %s (%s): %s", ref, comp["lcsc"], e)
        else:
            resolved[ref] = {}
    return resolved, warnings


async def generate_pcb(
    spec: dict,
    output_path: str | Path,
    *,
    lib_dir: str | None = None,
    auto_fetch_pinmaps: bool = True,
    force_refresh: bool = False,
) -> GenerationResult:
    """Generate a `.kicad_pcb` from a declarative spec.

    For any component spec without an explicit `pinmap`, this will
    auto-fetch the pin-name map from EasyEDA (by LCSC C-number). First
    run on a fresh board is bounded by EasyEDA's ~1 req/min rate limit;
    subsequent runs on the same parts are instant (SQLite cache).

    Returns a GenerationResult summarizing what was placed and wired.
    Raises PcbGenerationError on spec validation failure or pcbnew-missing.
    Non-fatal issues (missing pads, unknown refs in a net) are collected
    in the `errors` list so partial boards still land on disk.
    """
    pcb = _ensure_pcbnew()

    resolved_lib_dir = resolve_footprint_dir(lib_dir)
    if not resolved_lib_dir:
        raise PcbGenerationError(_footprint_dir_error())

    board_cfg, components, nets = _validate_spec(spec)
    out = Path(output_path).expanduser().resolve()
    out.parent.mkdir(parents=True, exist_ok=True)

    # Phase 1: resolve pinmaps (async — may hit EasyEDA).
    needed = _refs_needing_pin_names(nets)
    skipped = sum(
        1 for c in components if c.get("lcsc") and not c.get("pinmap") and c["ref"] not in needed
    )
    if skipped:
        logger.info(
            "Skipping EasyEDA pin-map fetch for %d component(s) whose nets "
            "use pad numbers only (~%ds saved)",
            skipped,
            int(skipped * 12),
        )
    pinmaps, pinmap_warnings = await _resolve_pinmaps(
        components,
        auto_fetch=auto_fetch_pinmaps,
        force_refresh=force_refresh,
        needed_refs=needed,
    )

    # Phase 2: pcbnew board construction (sync).
    board = pcb.BOARD()
    board.SetCopperLayerCount(board_cfg["layer_count"])

    # Seeded first so a fetch failure is read before the "no pad X" errors
    # it causes.
    warnings: list[str] = list(pinmap_warnings)
    errors: list[str] = []

    _add_board_outline(pcb, board, board_cfg["width_mm"], board_cfg["height_mm"])

    positions = _place(
        components,
        board_width_mm=board_cfg["width_mm"],
        board_height_mm=board_cfg["height_mm"],
    )

    fp_by_ref: dict[str, Any] = {}
    comp_by_ref: dict[str, dict] = {}
    for comp in components:
        ref = comp["ref"]
        try:
            f = _load_footprint(pcb, board, comp["lib"], comp["fp"], resolved_lib_dir)
        except PcbGenerationError as e:
            errors.append(f"{ref}: {e}")
            continue
        x, y = positions[ref]
        f.SetPosition(pcb.VECTOR2I(_mm(pcb, x), _mm(pcb, y)))
        f.SetReference(ref)
        f.SetValue(comp.get("value", ""))
        fp_by_ref[ref] = f
        comp_by_ref[ref] = comp

    net_stats: dict[str, int] = {}
    for net_name, members in nets.items():
        net = pcb.NETINFO_ITEM(board, net_name)
        board.Add(net)
        assigned = 0
        for ref, pin_ref in members:
            f = fp_by_ref.get(ref)
            if f is None:
                errors.append(f"net {net_name}: unknown ref {ref}")
                continue
            pad_num = pinmaps.get(ref, {}).get(pin_ref, pin_ref)
            pad = f.FindPadByNumber(pad_num)
            if pad is None:
                errors.append(
                    f"net {net_name}: {ref} has no pad {pin_ref!r} (resolved to {pad_num!r})"
                )
                continue
            pad.SetNet(net)
            assigned += 1
        net_stats[net_name] = assigned
        if assigned < 2:
            warnings.append(
                f"net {net_name!r} has only {assigned} connected pad{'s' if assigned != 1 else ''}"
            )

    ok = pcb.SaveBoard(str(out), board)
    if not ok:
        raise PcbGenerationError(f"SaveBoard returned False for {out}")

    return GenerationResult(
        pcb_path=out,
        footprints_placed=len(fp_by_ref),
        nets_created=len(nets),
        net_stats=net_stats,
        warnings=warnings,
        errors=errors,
    )


# ---------------------------------------------------------------------------
# BOM helper (replaces KiCad 9's removed `pcb export bom`)
# ---------------------------------------------------------------------------


def bom_from_components(components: list[dict], output_path: str | Path) -> dict:
    """Write a JLCPCB-friendly BOM CSV directly from a component list.

    Used by the server's package_for_jlcpcb orchestration when there is
    no matching schematic (e.g. the user generated the PCB directly via
    pcb_generate rather than via schematic).
    """
    out = Path(output_path).expanduser().resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    from collections import defaultdict

    groups: dict[tuple[str, str, str], list[str]] = defaultdict(list)
    for c in components:
        key = (c.get("value", ""), c.get("lcsc", ""), f"{c.get('lib', '')}:{c.get('fp', '')}")
        groups[key].append(c.get("ref", ""))

    rows_written = 0
    with out.open("w") as f:
        f.write("Qty,Value,LCSC,Footprint,References\n")
        for (value, lcsc, footprint), refs in sorted(
            groups.items(), key=lambda x: (x[0][1], x[0][0])
        ):
            refs_str = ",".join(sorted(refs))
            f.write(f'{len(refs)},"{value}","{lcsc}","{footprint}","{refs_str}"\n')
            rows_written += 1

    return {"output_path": str(out), "rows": rows_written, "parts": len(components)}
