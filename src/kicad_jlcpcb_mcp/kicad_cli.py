"""Async wrapper around the `kicad-cli` executable.

All KiCad invocations go through this module. We never `import pcbnew` or
the swig binding — kicad-cli is the supported, headless, version-stable
interface in KiCad 8/9, and the swig binding is being removed in KiCad 11.

Subprocess calls go through `subprocess.run` in a thread pool (via
`asyncio.to_thread`) rather than the asyncio subprocess primitives. This
keeps the call sites synchronous-looking and lets us pass an explicit
timeout per invocation. Each public function returns a structured dict
(never raw stdout) so the MCP layer can serialize it directly.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Sequence

from . import config

logger = logging.getLogger(__name__)


class KicadCliError(RuntimeError):
    """A `kicad-cli` invocation failed (binary missing, timeout, or unparseable output).

    `str(e)` deliberately includes kicad-cli's own stderr. The MCP layer
    only ever surfaces `str(e)` to the model, so anything kept solely as an
    attribute is invisible where it matters: a caller would see
    "kicad-cli pcb export gerbers failed (exit 1)" and have nothing to act
    on, while the actual reason sat in `.stderr` on the server.
    """

    #: Cap on how much captured output goes into the message. Enough for a
    #: real diagnostic, not so much that it floods the model's context.
    OUTPUT_EXCERPT_CHARS = 800

    def __init__(
        self, message: str, *, returncode: int | None = None, stdout: str = "", stderr: str = ""
    ):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        super().__init__(self._format(message, stdout, stderr))

    @classmethod
    def _format(cls, message: str, stdout: str, stderr: str) -> str:
        detail = (stderr or "").strip() or (stdout or "").strip()
        if not detail:
            return message
        if len(detail) > cls.OUTPUT_EXCERPT_CHARS:
            detail = detail[: cls.OUTPUT_EXCERPT_CHARS] + " ...[truncated]"
        return f"{message}\nkicad-cli output:\n{detail}"


# Regex for parsing `kicad-cli --version` output. KiCad prints lines like:
#   "9.0.1"                                  (some builds, just the version)
#   "kicad-cli (9.0.1-1)"                    (Fedora dnf packaging)
#   "8.0.4 release build"                    (older builds)
# We accept any line containing major.minor[.patch] anchored at a word
# boundary so all of these parse.
_VERSION_RE = re.compile(r"\b(\d+)\.(\d+)(?:\.(\d+))?")


def _find_executable() -> str | None:
    """Locate the kicad-cli binary. Returns None if not found.

    PATH first, then the well-known install locations that don't put
    anything on PATH. Both of the fallbacks matter in practice: the macOS
    .app bundle never adds itself to PATH, and Flatpak — which this
    module's own install hint recommends — only exposes kicad-cli through
    `flatpak run`. Probing PATH alone means telling a macOS or Flatpak
    user to install KiCad when they already have it.
    """
    for candidate in config.KICAD_CLI_CANDIDATES:
        path = shutil.which(candidate)
        if path:
            return path

    for path in config.KICAD_CLI_FALLBACK_PATHS:
        expanded = Path(path).expanduser()
        if expanded.is_file() and os.access(expanded, os.X_OK):
            logger.info("Found kicad-cli outside PATH at %s", expanded)
            return str(expanded)

    return None


def _find_flatpak_argv() -> list[str] | None:
    """Return the argv prefix for a Flatpak KiCad, or None.

    A Flatpak install puts only a GUI launcher on PATH, so kicad-cli is
    only reachable via `flatpak run --command=kicad-cli`. This module's own
    install hint recommends Flatpak, so not supporting it was a gap.
    """
    flatpak = shutil.which("flatpak")
    if not flatpak:
        return None
    try:
        r = subprocess.run(
            [flatpak, "info", config.KICAD_FLATPAK_ID],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode != 0:
        return None
    return [flatpak, "run", f"--command={config.KICAD_CLI_CANDIDATES[0]}", config.KICAD_FLATPAK_ID]


def _find_kicad_argv() -> list[str] | None:
    """Full argv prefix needed to invoke kicad-cli, or None if unavailable.

    A plain install is one element; a Flatpak install is four. Everything
    that shells out goes through this so both work identically.
    """
    exe = _find_executable()
    if exe:
        return [exe]
    return _find_flatpak_argv()


def _install_hint() -> str:
    """Platform-appropriate install instructions.

    Previously this always printed Fedora dnf commands, including on macOS
    and Windows.
    """
    if sys.platform == "darwin":
        return (
            "Install KiCad from https://www.kicad.org/download/macos/ .\n"
            "kicad-cli lives inside the app bundle at\n"
            "  /Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli\n"
            "which is not on PATH by default — this plugin looks there "
            "automatically.\n"
            "After installing, restart Claude Code and re-run detect_kicad."
        )
    if sys.platform.startswith("win"):
        return (
            "Install KiCad from https://www.kicad.org/download/windows/ .\n"
            "kicad-cli.exe lands in C:\\Program Files\\KiCad\\<version>\\bin\\ ;\n"
            "add that directory to PATH if the installer did not.\n"
            "After installing, restart Claude Code and re-run detect_kicad."
        )
    return (
        "Install KiCad with whichever fits your distro:\n"
        "  sudo dnf install kicad                     # Fedora\n"
        "  sudo apt install kicad                     # Debian / Ubuntu\n"
        "  sudo pacman -S kicad                       # Arch\n"
        "  flatpak install flathub org.kicad.KiCad    # any distro, latest stable\n"
        "After installing, restart Claude Code and re-run detect_kicad."
    )


def _parse_version(text: str) -> tuple[int, int, int] | None:
    """Extract a (major, minor, patch) tuple from kicad-cli version output."""
    match = _VERSION_RE.search(text)
    if not match:
        return None
    major = int(match.group(1))
    minor = int(match.group(2))
    patch = int(match.group(3) or 0)
    return (major, minor, patch)


def _run_blocking(
    argv: Sequence[str], args: Sequence[str], cwd: str | None, timeout: float
) -> tuple[int, str, str]:
    """Synchronous subprocess invocation. Runs in a worker thread via _run.

    `argv` is the launcher prefix from `_find_kicad_argv` — one element for
    a normal install, four for Flatpak.
    """
    if isinstance(argv, str):  # tolerate a bare path from older callers/tests
        argv = [argv]
    try:
        result = subprocess.run(
            [*argv, *args],
            capture_output=True,
            text=True,
            cwd=cwd,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as e:
        raise KicadCliError(f"kicad-cli {' '.join(args[:2])} timed out after {timeout}s") from e
    return result.returncode, result.stdout or "", result.stderr or ""


async def _run(
    args: Sequence[str], *, cwd: str | Path | None = None, timeout: float = 60.0
) -> tuple[int, str, str]:
    """Run kicad-cli with `args` in a worker thread.

    Returns (returncode, stdout, stderr). Raises KicadCliError if the
    executable isn't found. Does NOT raise on non-zero exit — callers
    decide whether non-zero is an error (e.g. ERC flags exit-non-zero on
    rule violations, which we want to surface as structured warnings).
    """
    argv = _find_kicad_argv()
    if not argv:
        raise KicadCliError(
            "kicad-cli not found. Run detect_kicad for platform-specific install guidance."
        )
    cwd_str = str(cwd) if cwd else None
    return await asyncio.to_thread(_run_blocking, argv, list(args), cwd_str, timeout)


# ---------------------------------------------------------------------------
# detect_kicad
# ---------------------------------------------------------------------------


async def detect_kicad() -> dict:
    """Probe the local KiCad install via `kicad-cli --version`.

    Returns a structured result the LLM can act on:
      {
        "found": bool,
        "version": "9.0.1" or None,
        "version_tuple": [9, 0, 1] or None,
        "path": "/usr/bin/kicad-cli" or None,
        "meets_min": bool,
        "min_required": "8.0",
        "install_hint": "..." or None,
      }
    """
    exe = _find_executable()
    min_major, min_minor = config.KICAD_MIN_VERSION
    base = {"min_required": f"{min_major}.{min_minor}"}

    if not exe:
        return {
            **base,
            "found": False,
            "version": None,
            "version_tuple": None,
            "path": None,
            "meets_min": False,
            "install_hint": _install_hint(),
        }

    rc, stdout, stderr = await _run(["--version"], timeout=10.0)
    raw_output = (stdout or stderr).strip()
    parsed = _parse_version(raw_output)

    if parsed is None:
        return {
            **base,
            "found": True,
            "version": None,
            "version_tuple": None,
            "path": exe,
            "meets_min": False,
            "raw_version_output": raw_output,
            "install_hint": (
                "Could not parse kicad-cli --version output. Your install may "
                "be too old or non-standard. " + _install_hint()
            ),
        }

    major, minor, patch = parsed
    meets_min = (major, minor) >= (min_major, min_minor)
    return {
        **base,
        "found": True,
        "version": f"{major}.{minor}.{patch}",
        "version_tuple": [major, minor, patch],
        "path": exe,
        "meets_min": meets_min,
        "install_hint": None if meets_min else _install_hint(),
    }


# ---------------------------------------------------------------------------
# Schematic ERC
# ---------------------------------------------------------------------------


async def sch_erc(sch_path: str | Path, *, output_path: str | Path | None = None) -> dict:
    """Run `kicad-cli sch erc` on a schematic.

    Returns:
      {
        "errors": int,
        "warnings": int,
        "report_path": str,
        "passed": bool,        # True iff errors == 0
      }

    Raises KicadCliError if kicad-cli is missing or the report file can't
    be produced. Rule violations themselves are NOT raised — they're
    counted and returned.
    """
    sch = Path(sch_path).expanduser().resolve()
    if not sch.is_file():
        raise KicadCliError(f"Schematic not found: {sch}")

    report = Path(output_path) if output_path else sch.with_name(f"{sch.stem}-erc.rpt")
    args = [
        "sch",
        "erc",
        "--output",
        str(report),
        "--severity-error",
        "--severity-warning",
        "--exit-code-violations",
        str(sch),
    ]
    rc, stdout, stderr = await _run(args, timeout=120.0)

    # Even on rule violations kicad-cli writes the report; the report is
    # the source of truth, not stdout. We grep it for the standard summary
    # lines KiCad emits at the end.
    if not report.is_file():
        raise KicadCliError(
            f"kicad-cli sch erc did not produce a report at {report}",
            returncode=rc,
            stdout=stdout,
            stderr=stderr,
        )

    text = report.read_text()
    errors, warnings = _parse_erc_report(text)
    return {
        "errors": errors,
        "warnings": warnings,
        "report_path": str(report),
        "passed": errors == 0,
    }


def _parse_erc_report(text: str) -> tuple[int, int]:
    """Extract (errors, warnings) totals from a kicad-cli ERC report.

    The report format has changed across KiCad versions, and getting this
    wrong is worse than failing: an unrecognised format parses as (0, 0),
    which `sch_erc` then reports as `passed: True`. KiCad 9 and 10 write
    per-violation severity on its own line, which none of the older
    patterns matched, so ERC silently passed on every modern install.

    Recognised shapes, most specific first:

        KiCad 9/10, per violation:
            [pin_not_connected]: Pin not connected
                ; error
                @(46.99 mm, 35.56 mm): Symbol U1 Pin 4 [NC, Passive, Line]

        Newer summary line:
            Found 2 errors, 3 warnings.

        Legacy summary lines:
            ** Errors 2 ****
            ** Warnings 3 ****

        Older per-violation:
            Severity: error
    """
    # KiCad 9/10 per-violation severity lines. Checked first because a
    # report can contain both these and a summary line, and counting the
    # violations is the more reliable of the two.
    errors = len(re.findall(r"^\s*;\s*error\s*$", text, re.IGNORECASE | re.MULTILINE))
    warnings = len(re.findall(r"^\s*;\s*warning\s*$", text, re.IGNORECASE | re.MULTILINE))
    if errors or warnings:
        return errors, warnings

    # "Found N errors, M warnings"
    m = re.search(r"Found\s+(\d+)\s+errors?,\s*(\d+)\s+warnings?", text, re.IGNORECASE)
    if m:
        return int(m.group(1)), int(m.group(2))

    # Legacy "** Errors N ****" / "** Warnings N ****"
    err_m = re.search(r"\*\*\s*Errors\s+(\d+)", text)
    warn_m = re.search(r"\*\*\s*Warnings\s+(\d+)", text)
    if err_m or warn_m:
        return int(err_m.group(1)) if err_m else 0, int(warn_m.group(1)) if warn_m else 0

    # "Severity: error" / "Severity: warning"
    errors = len(re.findall(r"Severity:\s*error", text, re.IGNORECASE))
    warnings = len(re.findall(r"Severity:\s*warning", text, re.IGNORECASE))
    return errors, warnings


# ---------------------------------------------------------------------------
# Manufacturing exports (used by package_for_jlcpcb)
# ---------------------------------------------------------------------------


# Standard JLCPCB 2-layer Gerber layer set. For 4-layer boards callers
# should pass layers= explicitly including In1.Cu, In2.Cu.
STANDARD_GERBER_LAYERS_2L = (
    "F.Cu,B.Cu,F.Paste,B.Paste,F.Silkscreen,B.Silkscreen,F.Mask,B.Mask,Edge.Cuts"
)


async def pcb_export_gerbers(
    pcb_path: str | Path,
    output_dir: str | Path,
    *,
    layers: str = STANDARD_GERBER_LAYERS_2L,
) -> dict:
    """Export Gerber files for the specified layer set.

    KiCad 9 requires `--layers` to be explicit (the old "export all" mode
    was dropped). Defaults to the JLCPCB 2-layer stack; pass `layers=`
    with In1.Cu/In2.Cu for 4-layer boards.

    Forces RS-274X (NOT X2) per JLCPCB requirements via --no-x2. Protel
    filename extensions are emitted directly by KiCad 9; gerber_pack
    classifies both old-style .gbr and new-style .gtl/.gbl filenames.
    """
    pcb = Path(pcb_path).expanduser().resolve()
    out = Path(output_dir).expanduser().resolve()
    out.mkdir(parents=True, exist_ok=True)
    args = [
        "pcb",
        "export",
        "gerbers",
        "--output",
        str(out),
        "--layers",
        layers,
        "--no-x2",
        "--subtract-soldermask",
        str(pcb),
    ]
    rc, stdout, stderr = await _run(args, timeout=300.0)
    if rc != 0:
        raise KicadCliError(
            f"kicad-cli pcb export gerbers failed (exit {rc})",
            returncode=rc,
            stdout=stdout,
            stderr=stderr,
        )
    files = sorted(p.name for p in out.iterdir() if p.is_file())
    return {"output_dir": str(out), "files": files}


async def pcb_export_drill(pcb_path: str | Path, output_dir: str | Path) -> dict:
    """Export a merged Excellon drill file. JLCPCB wants a single drill file."""
    pcb = Path(pcb_path).expanduser().resolve()
    out = Path(output_dir).expanduser().resolve()
    out.mkdir(parents=True, exist_ok=True)
    args = [
        "pcb",
        "export",
        "drill",
        "--output",
        str(out) + "/",
        "--format",
        "excellon",
        "--excellon-units",
        "mm",
        "--generate-map",
        "--map-format",
        "gerberx2",
        "--excellon-zeros-format",
        "decimal",
        str(pcb),
    ]
    rc, stdout, stderr = await _run(args, timeout=120.0)
    if rc != 0:
        raise KicadCliError(
            f"kicad-cli pcb export drill failed (exit {rc})",
            returncode=rc,
            stdout=stdout,
            stderr=stderr,
        )
    files = sorted(p.name for p in out.iterdir() if p.is_file())
    return {"output_dir": str(out), "files": files}


async def pcb_export_pos(pcb_path: str | Path, output_path: str | Path) -> dict:
    """Export a CPL (component placement) file for JLCPCB assembly."""
    pcb = Path(pcb_path).expanduser().resolve()
    out = Path(output_path).expanduser().resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    args = [
        "pcb",
        "export",
        "pos",
        "--output",
        str(out),
        "--format",
        "csv",
        "--units",
        "mm",
        "--use-drill-file-origin",
        "--side",
        "both",
        str(pcb),
    ]
    rc, stdout, stderr = await _run(args, timeout=120.0)
    if rc != 0:
        raise KicadCliError(
            f"kicad-cli pcb export pos failed (exit {rc})",
            returncode=rc,
            stdout=stdout,
            stderr=stderr,
        )
    return {"output_path": str(out)}


async def sch_export_bom(sch_path: str | Path, output_path: str | Path) -> dict:
    """Export a BOM CSV from the schematic.

    KiCad 9's `kicad-cli pcb export bom` was removed — BOM export is
    schematic-only now. Callers with a PCB but no schematic should
    either (a) build the BOM from their own components list, or (b) use
    the pcb module's `bom_from_components()` helper.
    """
    sch = Path(sch_path).expanduser().resolve()
    out = Path(output_path).expanduser().resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    args = [
        "sch",
        "export",
        "bom",
        "--output",
        str(out),
        "--fields",
        "Reference,Value,Footprint,LCSC,Manufacturer,MPN,Description",
        "--group-by",
        "Value,LCSC",
        str(sch),
    ]
    rc, stdout, stderr = await _run(args, timeout=120.0)
    if rc != 0:
        raise KicadCliError(
            f"kicad-cli sch export bom failed (exit {rc})",
            returncode=rc,
            stdout=stdout,
            stderr=stderr,
        )
    return {"output_path": str(out)}
