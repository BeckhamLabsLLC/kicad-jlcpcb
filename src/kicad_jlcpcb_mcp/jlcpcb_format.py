"""Convert KiCad's exports into the column formats JLCPCB actually accepts.

KiCad and JLCPCB disagree about both files that drive assembly:

  - **CPL / placement.** `kicad-cli pcb export pos --format csv` writes
    ``Ref,Val,Package,PosX,PosY,Rot,Side``. JLCPCB's parser wants
    ``Designator,Mid X,Mid Y,Layer,Rotation`` and expects ``Top``/``Bottom``
    capitalised. Uploading KiCad's file as-is gets it rejected or, worse,
    silently misread.

  - **BOM.** JLCPCB keys on ``Comment``, ``Designator`` and an LCSC part
    column. Without the C-numbers it cannot source anything and the order
    falls back to "parts not specified".

Both conversions are pure text, so they are unit-testable without KiCad.

A note on rotation, because it is the one thing this module does *not*
silently fix: JLCPCB's expected orientation differs from KiCad's for a
number of packages, and correcting it properly needs a per-package lookup
table that every mature toolchain maintains by hand. Footprints this
plugin generates come from EasyEDA, which shares JLCPCB's convention, so
those are consistent. Footprints taken from KiCad's standard libraries may
not be, and `convert_cpl` returns a warning saying so rather than applying
a guess. Always check the assembly preview JLCPCB renders after upload.
"""

from __future__ import annotations

import csv
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

# JLCPCB's expected CPL columns, in order.
CPL_COLUMNS = ("Designator", "Mid X", "Mid Y", "Layer", "Rotation")

# JLCPCB's expected BOM columns, in order.
BOM_COLUMNS = ("Comment", "Designator", "Footprint", "LCSC Part #")

# KiCad header spellings we accept for each field we need. KiCad has used
# more than one over the years, so match on any of them.
_CPL_SOURCE_FIELDS = {
    "designator": ("Ref", "Designator", "RefDes"),
    "x": ("PosX", "Mid X", "X"),
    "y": ("PosY", "Mid Y", "Y"),
    "layer": ("Side", "Layer"),
    "rotation": ("Rot", "Rotation"),
}


class JlcpcbFormatError(ValueError):
    """A KiCad export could not be converted to JLCPCB's format."""


def _pick(row: dict, names: tuple[str, ...]) -> str | None:
    for name in names:
        if name in row and row[name] != "":
            return row[name]
    return None


def convert_cpl(source: str | Path, dest: str | Path) -> dict:
    """Rewrite a kicad-cli `pos` CSV into JLCPCB's CPL format.

    Returns ``{"output_path", "rows", "warnings"}``. Raises
    JlcpcbFormatError if the source has no recognisable columns, which
    means KiCad changed its export and shipping the file would be worse
    than failing.
    """
    src_path = Path(source).expanduser().resolve()
    dst_path = Path(dest).expanduser().resolve()
    dst_path.parent.mkdir(parents=True, exist_ok=True)

    with src_path.open(newline="") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
        headers = reader.fieldnames or []

    if not headers:
        raise JlcpcbFormatError(f"{src_path} is empty; nothing to convert.")

    missing = [
        field for field, names in _CPL_SOURCE_FIELDS.items() if not any(n in headers for n in names)
    ]
    if missing:
        raise JlcpcbFormatError(
            f"{src_path} has columns {headers}, which do not include "
            f"{missing}. kicad-cli's position export format changed — please "
            f"open an issue at "
            f"https://github.com/BeckhamLabsLLC/kicad-jlcpcb/issues"
        )

    warnings: list[str] = []
    written = 0
    with dst_path.open("w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(CPL_COLUMNS)
        for row in rows:
            designator = _pick(row, _CPL_SOURCE_FIELDS["designator"])
            if not designator:
                continue
            side = (_pick(row, _CPL_SOURCE_FIELDS["layer"]) or "top").strip().lower()
            writer.writerow(
                [
                    designator,
                    _pick(row, _CPL_SOURCE_FIELDS["x"]) or "0",
                    _pick(row, _CPL_SOURCE_FIELDS["y"]) or "0",
                    "Bottom" if side.startswith("b") else "Top",
                    _pick(row, _CPL_SOURCE_FIELDS["rotation"]) or "0",
                ]
            )
            written += 1

    if written:
        warnings.append(
            "CPL rotations are taken from the board as-is. JLCPCB's expected "
            "orientation differs from KiCad's for some packages, so check the "
            "assembly preview after upload and rotate any part that looks wrong."
        )
    return {"output_path": str(dst_path), "rows": written, "warnings": warnings}


_BOM_SOURCE_FIELDS = {
    "comment": ("Value", "Comment", "Val"),
    "designator": ("Reference", "Designator", "References", "Ref"),
    "footprint": ("Footprint", "Package"),
    "lcsc": ("LCSC", "LCSC Part #", "LCSC Part Number", "JLCPCB Part #"),
}


def convert_bom(source: str | Path, dest: str | Path) -> dict:
    """Rewrite a kicad-cli `sch export bom` CSV into JLCPCB's BOM format.

    KiCad writes whatever `--fields` asked for — for this plugin that is
    ``Reference,Value,Footprint,LCSC,Manufacturer,MPN,Description``. JLCPCB
    keys on ``Comment``, ``Designator`` and an LCSC part column, so the
    file needs renaming and reordering before upload.
    """
    src_path = Path(source).expanduser().resolve()
    dst_path = Path(dest).expanduser().resolve()
    dst_path.parent.mkdir(parents=True, exist_ok=True)

    with src_path.open(newline="") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
        headers = reader.fieldnames or []

    if not headers:
        raise JlcpcbFormatError(f"{src_path} is empty; nothing to convert.")
    if not any(n in headers for n in _BOM_SOURCE_FIELDS["designator"]):
        raise JlcpcbFormatError(
            f"{src_path} has columns {headers}, with no reference/designator "
            f"column. kicad-cli's BOM export format changed — please open an "
            f"issue at https://github.com/BeckhamLabsLLC/kicad-jlcpcb/issues"
        )

    has_lcsc_column = any(n in headers for n in _BOM_SOURCE_FIELDS["lcsc"])
    missing_lcsc: list[str] = []
    written = 0
    with dst_path.open("w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(BOM_COLUMNS)
        for row in rows:
            designator = _pick(row, _BOM_SOURCE_FIELDS["designator"])
            if not designator:
                continue
            lcsc = _pick(row, _BOM_SOURCE_FIELDS["lcsc"]) or ""
            if not lcsc:
                missing_lcsc.append(designator)
            writer.writerow(
                [
                    _pick(row, _BOM_SOURCE_FIELDS["comment"]) or "",
                    designator,
                    _pick(row, _BOM_SOURCE_FIELDS["footprint"]) or "",
                    lcsc,
                ]
            )
            written += 1

    warnings: list[str] = []
    if not has_lcsc_column:
        warnings.append(
            "The schematic BOM has no LCSC column, so JLCPCB has nothing to "
            "source from. Set an `LCSC` field on each symbol, or generate the "
            "board with pcb_generate so the BOM comes from the resolved spec."
        )
    elif missing_lcsc:
        warnings.append(
            f"{len(missing_lcsc)} BOM line(s) have no LCSC part number "
            f"({', '.join(missing_lcsc[:5])}"
            f"{' ...' if len(missing_lcsc) > 5 else ''}). JLCPCB will leave "
            f"those pads unpopulated."
        )
    return {"output_path": str(dst_path), "rows": written, "warnings": warnings}


def write_bom(components: list[dict], dest: str | Path) -> dict:
    """Write a JLCPCB-format BOM from a component list.

    One row per unique part, with its designators joined — which is what
    JLCPCB's parser expects, not one row per placement.
    """
    dst_path = Path(dest).expanduser().resolve()
    dst_path.parent.mkdir(parents=True, exist_ok=True)

    groups: dict[tuple[str, str, str], list[str]] = {}
    for c in components:
        lcsc = str(c.get("lcsc") or "").strip()
        comment = str(c.get("value") or c.get("mfr_part") or lcsc).strip()
        lib = str(c.get("lib") or "").strip()
        fp = str(c.get("fp") or c.get("footprint") or "").strip()
        footprint = f"{lib}:{fp}" if lib and fp else (fp or lib)
        groups.setdefault((comment, footprint, lcsc), []).append(str(c.get("ref") or ""))

    missing_lcsc: list[str] = []
    with dst_path.open("w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(BOM_COLUMNS)
        for (comment, footprint, lcsc), refs in sorted(groups.items()):
            designators = ",".join(sorted(r for r in refs if r))
            if not lcsc:
                missing_lcsc.append(designators)
            writer.writerow([comment, designators, footprint, lcsc])

    warnings: list[str] = []
    if missing_lcsc:
        warnings.append(
            f"{len(missing_lcsc)} BOM line(s) have no LCSC part number "
            f"({', '.join(missing_lcsc[:5])}"
            f"{' ...' if len(missing_lcsc) > 5 else ''}). JLCPCB cannot source "
            f"those — resolve them with lcsc_resolve_bom or they will be left "
            f"unpopulated."
        )
    return {
        "output_path": str(dst_path),
        "rows": len(groups),
        "parts": len(components),
        "warnings": warnings,
    }
