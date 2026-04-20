"""Repackage KiCad-exported manufacturing files into a JLCPCB-ready zip.

KiCad's `kicad-cli pcb export gerbers` writes Gerber files with names
like `<board>-F_Cu.gbr`, `<board>-Edge_Cuts.gbr`. JLCPCB's web upload
form expects Protel-style extensions: `<board>.GTL`, `<board>.GBL`,
`<board>.GM1`, etc., plus a single `.XLN` drill file.

This module:
  1. Walks a directory of KiCad-exported gerbers and drills
  2. Renames each file to its Protel equivalent using `config.JLCPCB_GERBER_EXTENSIONS`
  3. Picks the merged Excellon drill file and renames it to `.XLN`
  4. Optionally includes the CPL (pos) and BOM CSVs
  5. Zips everything into a single `jlcpcb-<project>-<date>.zip`

Pure file operations — no subprocess, no network. The companion logic
in `kicad_cli.pcb_export_gerbers` and friends produces the inputs.
"""

from __future__ import annotations

import datetime as dt
import logging
import re
import shutil
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from . import config

logger = logging.getLogger(__name__)


class GerberPackError(RuntimeError):
    """Couldn't find expected files or zip them up."""


@dataclass
class PackResult:
    """Summary of one packaging run."""

    zip_path: Path
    files_packed: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "zip_path": str(self.zip_path),
            "files_packed": self.files_packed,
            "file_count": len(self.files_packed),
            "warnings": self.warnings,
        }


# KiCad exports gerbers as `<stem>-<layer>.<ext>`. Two generations:
#   - KiCad 8: `<stem>-F_Cu.gbr`, `<stem>-B_Cu.gbr`, ...
#   - KiCad 9: `<stem>-F_Cu.gtl`, `<stem>-B_Cu.gbl`, ...  (native Protel)
# We accept both and normalize to the JLCPCB Protel extension at pack time.
_LAYER_SUFFIX_RE = re.compile(
    r"^(?P<stem>.+?)-(?P<layer>F_Cu|B_Cu|F_Paste|B_Paste|F_Silkscreen|B_Silkscreen|"
    r"F_Mask|B_Mask|Edge_Cuts|In1_Cu|In2_Cu)\."
    r"(?P<ext>gbr|gtl|gbl|gtp|gbp|gto|gbo|gts|gbs|gm1|g2l|g3l)$",
    re.IGNORECASE,
)


def _protel_extension_for(layer: str) -> str | None:
    """Return the JLCPCB Protel extension for a KiCad layer name, or None
    if the layer isn't part of the standard JLCPCB upload."""
    return config.JLCPCB_GERBER_EXTENSIONS.get(layer)


def _classify_gerber(filename: str) -> tuple[str, str] | None:
    """Match a gerber filename → (stem, protel_ext) or None if unrecognized."""
    m = _LAYER_SUFFIX_RE.match(filename)
    if not m:
        return None
    layer = m.group("layer")
    stem = m.group("stem")
    ext = _protel_extension_for(layer)
    if not ext:
        return None
    return stem, ext


def _find_drill_file(directory: Path) -> Path | None:
    """Pick the merged Excellon drill file from a directory of KiCad outputs.

    KiCad writes drill files as `<stem>.drl` (or sometimes `<stem>-PTH.drl`
    + `<stem>-NPTH.drl` for plated/non-plated). When `--generate-map` is
    used we also get a `<stem>-drl.gbr` map. We prefer the single merged
    `.drl` file; if only the split files exist, we surface a warning so
    the user can re-export with the merged option.
    """
    drls = sorted(directory.glob("*.drl"))
    if not drls:
        return None
    # Prefer a non-PTH/NPTH-suffixed file (= merged)
    for drl in drls:
        if "-PTH" not in drl.name and "-NPTH" not in drl.name:
            return drl
    return drls[0]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def pack_for_jlcpcb(
    gerber_dir: str | Path,
    drill_dir: str | Path,
    output_zip: str | Path,
    *,
    project_name: str = "board",
    cpl_path: str | Path | None = None,
    bom_path: str | Path | None = None,
) -> PackResult:
    """Build a JLCPCB-ready zip from KiCad export outputs.

    `gerber_dir` and `drill_dir` may be the same directory; we just look
    in both for the right file types. `output_zip` is the destination
    zip path. CPL and BOM are optional; when provided they go into the
    zip alongside the gerbers.

    Raises GerberPackError if no gerbers are found.
    """
    g_dir = Path(gerber_dir).expanduser().resolve()
    d_dir = Path(drill_dir).expanduser().resolve()
    out_zip = Path(output_zip).expanduser().resolve()

    if not g_dir.is_dir():
        raise GerberPackError(f"Gerber directory not found: {g_dir}")
    if not d_dir.is_dir():
        raise GerberPackError(f"Drill directory not found: {d_dir}")

    out_zip.parent.mkdir(parents=True, exist_ok=True)

    # Stage renamed files in a temp directory so the zip layout is flat
    # and our renames don't disturb the source files.
    staging = out_zip.parent / f".{out_zip.stem}.staging"
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir()

    files_packed: list[str] = []
    warnings: list[str] = []

    try:
        # Gerbers
        gerber_count = 0
        for src in sorted(g_dir.iterdir()):
            if not src.is_file():
                continue
            classified = _classify_gerber(src.name)
            if not classified:
                continue
            stem, protel_ext = classified
            dest_name = f"{project_name}.{protel_ext}"
            dest = staging / dest_name
            shutil.copy2(src, dest)
            files_packed.append(dest_name)
            gerber_count += 1

        if gerber_count == 0:
            raise GerberPackError(
                f"No recognizable Gerber files found in {g_dir}. "
                f"Check that kicad-cli pcb export gerbers ran successfully."
            )

        # Required-layer sanity check (warn, don't fail)
        required = {"GTL", "GBL", "GTO", "GTS", "GBS", "GM1"}
        present = {f.split(".")[-1] for f in files_packed}
        missing = required - present
        if missing:
            warnings.append(
                f"Missing typical JLCPCB layers: {sorted(missing)}. "
                f"Boards without all of these may not pass JLCPCB review."
            )

        # Drill file
        drill_src = _find_drill_file(d_dir)
        if drill_src is None:
            warnings.append(
                "No Excellon (.drl) drill file found. JLCPCB requires drill data; "
                "re-run kicad-cli pcb export drill before packaging."
            )
        else:
            drill_dest_name = f"{project_name}.{config.JLCPCB_DRILL_EXTENSION}"
            shutil.copy2(drill_src, staging / drill_dest_name)
            files_packed.append(drill_dest_name)
            # If split PTH/NPTH files also exist, warn — JLCPCB wants merged
            split_drills = [
                p
                for p in d_dir.glob("*.drl")
                if ("-PTH" in p.name or "-NPTH" in p.name) and p != drill_src
            ]
            if split_drills:
                warnings.append(
                    f"Found split drill files alongside the merged one: "
                    f"{[p.name for p in split_drills]}. Used the merged file."
                )

        # CPL (pick-and-place / position file) — optional
        if cpl_path:
            cpl_src = Path(cpl_path)
            if cpl_src.is_file():
                cpl_dest_name = f"{project_name}-cpl{cpl_src.suffix}"
                shutil.copy2(cpl_src, staging / cpl_dest_name)
                files_packed.append(cpl_dest_name)
            else:
                warnings.append(f"CPL file not found: {cpl_src}")

        # BOM — optional
        if bom_path:
            bom_src = Path(bom_path)
            if bom_src.is_file():
                bom_dest_name = f"{project_name}-bom{bom_src.suffix}"
                shutil.copy2(bom_src, staging / bom_dest_name)
                files_packed.append(bom_dest_name)
            else:
                warnings.append(f"BOM file not found: {bom_src}")

        # Build the zip
        with zipfile.ZipFile(out_zip, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for fname in files_packed:
                zf.write(staging / fname, arcname=fname)

        return PackResult(
            zip_path=out_zip,
            files_packed=files_packed,
            warnings=warnings,
        )
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def default_zip_path(manufacturing_dir: str | Path, project_name: str) -> Path:
    """Build the default destination zip path for `package_for_jlcpcb`.

    Format: `<manufacturing_dir>/jlcpcb-<project>-YYYYMMDD.zip`. If a zip
    with that name already exists, suffixes `-1`, `-2`, etc.
    """
    today = dt.date.today().strftime("%Y%m%d")
    base = Path(manufacturing_dir) / f"jlcpcb-{project_name}-{today}.zip"
    if not base.exists():
        return base
    i = 1
    while True:
        candidate = base.with_name(f"{base.stem}-{i}{base.suffix}")
        if not candidate.exists():
            return candidate
        i += 1
