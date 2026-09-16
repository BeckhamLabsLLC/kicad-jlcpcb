"""MCP Server for kicad-jlcpcb.

Tool surface (14 tools across 6 stages):
  - Project setup: detect_kicad, create_project, load_project
  - Component sourcing: lcsc_search, lcsc_resolve_bom, fetch_part_library,
    part_pin_map
  - Schematic: sch_generate, sch_run_erc
  - Board: pcb_generate, easyeda_handoff
  - Manufacturing: package_for_jlcpcb
  - Session: session_resume, session_confirm_bom

`tests/test_server.py::TestAllPhase1ToolsListed` pins this list against the
handler routing, so definitions and routing cannot drift apart silently.

Phase 2 will add auto-placement, Freerouting integration, and DRC.
Phase 3 will add vision-based schematic extraction and EasyEDA backup export.
"""

import asyncio
import json
import logging
from typing import Any

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

from . import __version__

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class KicadJlcpcbServer:
    """MCP Server for KiCad → JLCPCB workflows."""

    def __init__(self):
        self._server = Server("kicad-jlcpcb")
        # Session state — populated by load_project / create_project,
        # consumed by every tool that needs an active workspace.
        self._active_project: dict | None = None
        self._setup_handlers()

    def _setup_handlers(self):
        """Register MCP tool handlers. Each tool's handler lives in its
        own module; this method only wires routing."""

        @self._server.list_tools()
        async def list_tools() -> list[Tool]:
            return _tool_definitions()

        @self._server.call_tool()
        async def call_tool(name: str, arguments: dict) -> list[TextContent]:
            try:
                result = await self._handle_tool(name, arguments)
                return [TextContent(type="text", text=json.dumps(result, indent=2, default=str))]
            except FileNotFoundError as e:
                return [
                    TextContent(
                        type="text",
                        text=json.dumps({"error": str(e), "type": "file_not_found"}, indent=2),
                    )
                ]
            except ValueError as e:
                return [
                    TextContent(
                        type="text",
                        text=json.dumps({"error": str(e), "type": "invalid_argument"}, indent=2),
                    )
                ]
            except Exception:
                # Truly unexpected — log full traceback and re-raise so the
                # MCP framework surfaces it. Bugs should be loud, not wrapped.
                logger.exception(f"Unexpected error in tool {name}")
                raise

    async def _handle_tool(self, name: str, args: dict) -> Any:
        """Route tool calls to per-stage modules."""
        # Tool handlers are imported lazily so a single broken module
        # doesn't prevent the whole server from starting.
        if name == "detect_kicad":
            from . import kicad_cli

            return await kicad_cli.detect_kicad()

        if name == "create_project":
            from . import project as project_mod
            from . import session as session_mod

            proj = project_mod.create_project(args["parent_dir"], args["name"])
            self._active_project = proj.to_dict()
            new_sess = session_mod.new_session(proj.root, proj.name)
            new_sess["last_tool"] = "create_project"
            session_mod.save_session(new_sess)
            return {
                "active_project": proj.to_dict(),
                "session": session_mod.resume_summary(new_sess),
                "tip": "Project scaffolded. Next: source parts (lcsc_search / lcsc_resolve_bom) and generate the schematic (sch_generate).",
            }

        if name == "load_project":
            from . import project as project_mod
            from . import session as session_mod

            proj = project_mod.load_project(args["project_path"])
            self._active_project = proj.to_dict()
            existing = session_mod.load_session(proj.root)
            response: dict[str, Any] = {
                "active_project": project_mod.manifest(proj),
            }
            if existing is not None:
                response["session"] = session_mod.resume_summary(existing)
                response["resume_available"] = existing.get("stage", "created") != "created"
            else:
                response["resume_available"] = False
            return response

        if name == "lcsc_search":
            from dataclasses import asdict

            from . import lcsc_client

            results = await lcsc_client.search(
                query=args["query"],
                package=args.get("package"),
                basic_only=args.get("basic_only", True),
                stock_min=args.get("stock_min", 1),
                limit=args.get("limit", 20),
            )
            return {
                "query": args["query"],
                "count": len(results),
                "results": [asdict(p) for p in results],
            }

        if name == "lcsc_resolve_bom":
            from . import lcsc_client
            from . import session as session_mod

            result = await lcsc_client.resolve_bom(args["rows"])
            if self._active_project:
                session_mod.update_stage(
                    self._active_project["root"],
                    "parts_sourced",
                    last_tool="lcsc_resolve_bom",
                    bom=result.get("resolved", []) if isinstance(result, dict) else [],
                )
            return result

        if name == "fetch_part_library":
            from . import part_library

            libs_dir = args.get("libs_dir") or self._require_active_libs_dir()
            result = await part_library.fetch_part_library(args["lcsc"], libs_dir)
            return result.to_dict()

        if name == "sch_generate":
            from . import schematic

            sch_path = args.get("sch_path") or self._require_active_sch_path()
            project_name = args.get("name") or self._active_project_name()
            return schematic.sch_generate(sch_path, project_name, args["netlist_spec"])

        if name == "sch_run_erc":
            from . import kicad_cli

            sch_path = args.get("sch_path") or self._require_active_sch_path()
            return await kicad_cli.sch_erc(sch_path)

        if name == "package_for_jlcpcb":
            return await self._package_for_jlcpcb(args)

        if name == "pcb_generate":
            from . import pcb as pcb_mod
            from . import session as session_mod

            spec = args["spec"]
            output_path = args.get("output_path")
            if not output_path:
                proj = self._require_active_project()
                output_path = proj["pcb_path"]
            result = await pcb_mod.generate_pcb(
                spec,
                output_path,
                auto_fetch_pinmaps=args.get("auto_fetch_pinmaps", True),
                force_refresh=args.get("force_refresh", False),
                lib_dir=args.get("lib_dir"),
            )
            if self._active_project:
                session_mod.update_stage(
                    self._active_project["root"],
                    "pcb_generated",
                    last_tool="pcb_generate",
                    spec=spec,
                )
            return result.to_dict()

        if name == "part_pin_map":
            from . import part_library

            return await part_library.get_pin_map(
                args["lcsc"],
                force_refresh=args.get("force_refresh", False),
            )

        if name == "easyeda_handoff":
            result = self._easyeda_handoff(args)
            if self._active_project:
                from . import session as session_mod

                session_mod.update_stage(
                    self._active_project["root"],
                    "handoff_rendered",
                    last_tool="easyeda_handoff",
                )
            return result

        if name == "session_confirm_bom":
            from . import session as session_mod

            root = args.get("project_path") or (
                self._active_project["root"] if self._active_project else None
            )
            if not root:
                raise ValueError("session_confirm_bom requires project_path or an active project.")
            updated = session_mod.update_stage(
                root,
                "bom_confirmed",
                last_tool="session_confirm_bom",
                notes=args.get("notes") or "",
            )
            if updated is None:
                raise ValueError(
                    f"No session file at {root}. Run create_project or load_project first."
                )
            return {
                "project_path": str(root),
                "confirmed": True,
                **session_mod.resume_summary(updated),
            }

        if name == "session_resume":
            from . import session as session_mod

            project_path = args.get("project_path")
            if not project_path and self._active_project:
                project_path = self._active_project["root"]
            if not project_path:
                raise ValueError("session_resume requires project_path or an active project.")
            sess = session_mod.load_session(project_path)
            if sess is None:
                return {
                    "project_path": str(project_path),
                    "session_found": False,
                    "message": "No session file at this project. Nothing to resume.",
                }
            return {
                "project_path": str(project_path),
                "session_found": True,
                **session_mod.resume_summary(sess),
            }

        return {"error": f"Unknown tool: {name}"}

    def _easyeda_handoff(self, args: dict) -> dict:
        """Produce a handoff report the LLM can deliver verbatim to the user.

        The idea: the plugin's job is done when the .kicad_pcb is ready.
        EasyEDA's web app has a reliable auto-router and a one-click
        JLCPCB order button, so the user's next step is to drag the file
        into easyeda.com and let EasyEDA finish the job.
        """
        from pathlib import Path

        pcb_path = Path(args.get("pcb_path") or self._require_active_project()["pcb_path"])
        if not pcb_path.is_file():
            raise ValueError(f"No .kicad_pcb at {pcb_path}")

        return {
            "pcb_path": str(pcb_path),
            "size_bytes": pcb_path.stat().st_size,
            "next_steps": [
                "1. Open https://easyeda.com/editor in your browser (sign in — free account OK).",
                "2. File → Import → EasyEDA Source / Specctra / KiCad / Altium.",
                f"3. Pick 'KiCad' and select {pcb_path}.",
                "4. Once the board loads, drag components into a sensible layout if you want (the plugin's auto-placement is grid-based).",
                "5. Route → Auto Route → Start. EasyEDA's routing service is cloud-based and handles RF boards much better than local Freerouting.",
                "6. Once routed, Fabrication → PCB Order via JLCPCB. One click. No Gerber download needed.",
            ],
            "why_easyeda": (
                "EasyEDA is owned by the same company as JLCPCB and LCSC. "
                "Its auto-router is integrated with JLCPCB's manufacturing, "
                "and the order flow skips the Gerber-file step entirely. "
                "The plugin stops at 'wired .kicad_pcb' because EasyEDA's "
                "routing is more reliable than anything we could ship headlessly."
            ),
            "alternative": (
                "If you'd rather stay in KiCad, open the .kicad_pcb in "
                "KiCad's Pcbnew, route the traces yourself (manual push-and-"
                "shove router is fast) or install the Freerouting KiCad "
                "plugin for interactive auto-routing, then run package_for_jlcpcb "
                "to produce a Gerber zip for JLCPCB upload."
            ),
        }

    async def _package_for_jlcpcb(self, args: dict) -> dict:
        """Orchestrate kicad-cli exports + gerber_pack into one zip.

        Pipeline:
          1. kicad-cli pcb export gerbers -> <project>/manufacturing/gerbers/
          2. kicad-cli pcb export drill   -> <project>/manufacturing/gerbers/
          3. kicad-cli pcb export pos     -> <project>/manufacturing/<name>-pos.csv
          4. kicad-cli pcb export bom     -> <project>/manufacturing/<name>-bom.csv
          5. gerber_pack.pack_for_jlcpcb  -> <project>/manufacturing/jlcpcb-<name>-<date>.zip
        """
        from pathlib import Path

        from . import gerber_pack, jlcpcb_format, kicad_cli
        from . import session as session_mod

        proj = self._require_active_project()
        pcb_path = Path(args.get("pcb_path") or proj["pcb_path"])
        if not pcb_path.is_file():
            raise ValueError(
                f"No PCB file at {pcb_path}. Phase 1 doesn't generate the .kicad_pcb — "
                f"open the schematic in KiCad, create a board, place + route, save, "
                f"then re-run package_for_jlcpcb."
            )

        manufacturing = Path(proj["manufacturing_dir"])
        gerbers_out = manufacturing / "gerbers"
        cpl_out = manufacturing / f"{proj['name']}-pos.csv"
        bom_out = manufacturing / f"{proj['name']}-bom.csv"

        steps: list[dict] = []
        gerber_result = await kicad_cli.pcb_export_gerbers(pcb_path, gerbers_out)
        steps.append({"step": "export_gerbers", **gerber_result})

        drill_result = await kicad_cli.pcb_export_drill(pcb_path, gerbers_out)
        steps.append({"step": "export_drill", **drill_result})

        cpl_warning = None
        extra_warnings: list[str] = []
        try:
            raw_pos = cpl_out.with_name(cpl_out.stem + "-kicad.csv")
            cpl_result = await kicad_cli.pcb_export_pos(pcb_path, raw_pos)
            steps.append({"step": "export_pos", **cpl_result})
            # kicad-cli writes Ref,Val,Package,PosX,PosY,Rot,Side; JLCPCB
            # wants Designator,Mid X,Mid Y,Layer,Rotation. Uploading
            # KiCad's file unconverted gets it rejected or misread.
            conv = jlcpcb_format.convert_cpl(raw_pos, cpl_out)
            steps.append({"step": "convert_cpl_to_jlcpcb", **conv})
            extra_warnings.extend(conv.get("warnings", []))
            raw_pos.unlink(missing_ok=True)
        except (kicad_cli.KicadCliError, jlcpcb_format.JlcpcbFormatError) as e:
            cpl_warning = f"CPL export failed (continuing without it): {e}"
            cpl_out = None  # type: ignore

        bom_warning = None
        # KiCad 9 removed `kicad-cli pcb export bom`. Try sch export bom
        # if the project has a matching schematic; otherwise build a BOM
        # directly from the PCB's footprints.
        sch_path = Path(proj["sch_path"])
        if sch_path.is_file():
            try:
                raw_bom = bom_out.with_name(bom_out.stem + "-kicad.csv")
                bom_result = await kicad_cli.sch_export_bom(sch_path, raw_bom)
                steps.append({"step": "export_bom_from_sch", **bom_result})
                # KiCad writes Reference,Value,Footprint,LCSC,...; JLCPCB
                # keys on Comment,Designator,Footprint,LCSC Part #.
                conv_bom = jlcpcb_format.convert_bom(raw_bom, bom_out)
                steps.append({"step": "convert_bom_to_jlcpcb", **conv_bom})
                extra_warnings.extend(conv_bom.get("warnings", []))
                raw_bom.unlink(missing_ok=True)
            except (kicad_cli.KicadCliError, jlcpcb_format.JlcpcbFormatError) as e:
                bom_warning = f"BOM export from schematic failed (continuing without it): {e}"
                bom_out = None  # type: ignore
        else:
            # No schematic: fall back to the component list the session
            # recorded when the board was generated. JLCPCB assembly
            # *requires* a BOM, so shipping a zip without one quietly
            # produces an order the user cannot place. `bom_from_components`
            # existed for exactly this and was never wired up.
            spec_components = []
            sess = session_mod.load_session(proj["root"])
            if sess:
                spec_components = (sess.get("spec") or {}).get("components") or []
            if spec_components:
                bom_result = jlcpcb_format.write_bom(spec_components, bom_out)
                steps.append({"step": "export_bom_from_session_spec", **bom_result})
                extra_warnings.extend(bom_result.get("warnings", []))
            else:
                bom_warning = (
                    "No schematic and no recorded component spec, so no BOM was "
                    "generated. JLCPCB assembly orders require a BOM — generate "
                    "the board with pcb_generate (which records the spec) or "
                    "supply a BOM by hand."
                )
                bom_out = None  # type: ignore

        zip_path = args.get("output_zip")
        if not zip_path:
            zip_path = gerber_pack.default_zip_path(manufacturing, proj["name"])
        pack_result = gerber_pack.pack_for_jlcpcb(
            gerber_dir=gerbers_out,
            drill_dir=gerbers_out,
            output_zip=zip_path,
            project_name=proj["name"],
            cpl_path=cpl_out,
            bom_path=bom_out,
        )

        warnings = list(pack_result.warnings) + extra_warnings
        if cpl_warning:
            warnings.append(cpl_warning)
        if bom_warning:
            warnings.append(bom_warning)

        return {
            "zip_path": str(pack_result.zip_path),
            "files_packed": pack_result.files_packed,
            "file_count": len(pack_result.files_packed),
            "steps": steps,
            "warnings": warnings,
            "next": (
                "Upload this zip to https://jlcpcb.com/quote — drag the zip onto "
                "the page, JLCPCB auto-detects layers and shows a preview, then "
                "configure quantity, color, and assembly options as needed."
            ),
        }

    # ------------------------------------------------------------------
    # Active-project helpers
    # ------------------------------------------------------------------

    def _require_active_project(self) -> dict:
        if not self._active_project:
            raise ValueError(
                "No active project. Call create_project or load_project first, "
                "or pass an explicit path argument."
            )
        return self._active_project

    def _require_active_libs_dir(self) -> str:
        return self._require_active_project()["libs_dir"]

    def _require_active_sch_path(self) -> str:
        return self._require_active_project()["sch_path"]

    def _active_project_name(self) -> str:
        return self._require_active_project()["name"]

    async def run(self):
        """Run the MCP server over stdio."""
        async with stdio_server() as (read_stream, write_stream):
            await self._server.run(
                read_stream, write_stream, self._server.create_initialization_options()
            )


def _tool_definitions() -> list[Tool]:
    """Return the full list of Tool definitions exposed by Phase 1.

    Kept as a free function so tests can introspect the schema without
    instantiating the server (which would also start the MCP framework).
    """
    return [
        Tool(
            name="detect_kicad",
            description=(
                "Detect the local KiCad install. Returns version, path, and "
                "whether it meets the minimum required version. On a missing "
                "or outdated install, returns an install_hint with platform-"
                "specific guidance. Never runs installs itself."
            ),
            inputSchema={
                "type": "object",
                "properties": {},
            },
        ),
        Tool(
            name="create_project",
            description=(
                "Scaffold a new KiCad project at <parent_dir>/<name>/ with a "
                ".kicad_pro carrying JLCPCB-tuned design rules, an empty "
                ".kicad_sch, and libs/ + manufacturing/ subdirectories. "
                "Sets the new project as the active session workspace."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "parent_dir": {
                        "type": "string",
                        "description": "Existing directory under which to create the project folder.",
                    },
                    "name": {
                        "type": "string",
                        "description": "Project name. Letters, digits, underscore, hyphen only. Becomes both the folder name and the .kicad_pro/.kicad_sch stem.",
                    },
                },
                "required": ["parent_dir", "name"],
            },
        ),
        Tool(
            name="load_project",
            description=(
                "Load an existing KiCad project (path to a .kicad_pro file or "
                "its containing directory). Validates the manifest, ensures "
                "libs/ and manufacturing/ subdirectories exist, and sets the "
                "project as the active session workspace. Returns a manifest "
                "of the project's current state."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "project_path": {
                        "type": "string",
                        "description": "Path to a .kicad_pro file OR the directory containing exactly one .kicad_pro file.",
                    },
                },
                "required": ["project_path"],
            },
        ),
        Tool(
            name="lcsc_search",
            description=(
                "Search the LCSC / JLCPCB catalog for parts matching a free-text "
                "query. Defaults to basic-tier parts only (no JLCPCB assembly "
                "setup fee). Set basic_only=false to include extended parts. "
                "Results are sorted basic-first then by descending stock."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "minLength": 2,
                        "description": "Free-text search, e.g. '10k 0603 1%' or 'ESP32-S3-WROOM'.",
                    },
                    "package": {
                        "type": "string",
                        "description": "Optional package filter, e.g. '0603', 'SOT-23-5', 'QFN-32'.",
                    },
                    "basic_only": {
                        "type": "boolean",
                        "default": True,
                        "description": "When true (default), exclude extended-tier parts entirely.",
                    },
                    "stock_min": {
                        "type": "integer",
                        "minimum": 0,
                        "default": 1,
                        "description": "Minimum JLCPCB SMT stock to include a result.",
                    },
                    "limit": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 100,
                        "default": 20,
                        "description": "Maximum number of results to return.",
                    },
                },
                "required": ["query"],
            },
        ),
        Tool(
            name="fetch_part_library",
            description=(
                "Download symbol + footprint (and 3D model when available) "
                "for a single LCSC C-number and install them into the active "
                "project's libs/ directory. Geometry comes from EasyEDA: real "
                "pin names and numbers for the symbol, real pad positions, "
                "sizes and drills for the footprint. If EasyEDA is unreachable "
                "or has no geometry for the part, a placeholder is written "
                "instead and the response says so in `warnings` — read them, "
                "because a placeholder footprint will NOT match the real part "
                "and must be replaced before ordering."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "lcsc": {
                        "type": "string",
                        "description": "LCSC C-number, e.g. 'C25804'.",
                    },
                    "libs_dir": {
                        "type": "string",
                        "description": "Optional override for the install directory. Defaults to the active project's libs/.",
                    },
                },
                "required": ["lcsc"],
            },
        ),
        Tool(
            name="sch_generate",
            description=(
                "Generate a .kicad_sch from a declarative netlist spec. The "
                "spec lists components (with LCSC C-numbers and footprints) "
                "and named nets connecting their pins. The generated schematic "
                "passes basic ERC and is ready to open in KiCad for human "
                "review and PCB layout."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "netlist_spec": {
                        "type": "object",
                        "description": "Components + nets. See plugin docs for the schema.",
                        "properties": {
                            "components": {"type": "array"},
                            "nets": {"type": "array"},
                        },
                        "required": ["components"],
                    },
                    "sch_path": {
                        "type": "string",
                        "description": "Optional override for the output path. Defaults to the active project's .kicad_sch.",
                    },
                    "name": {
                        "type": "string",
                        "description": "Title block name. Defaults to the active project's name.",
                    },
                },
                "required": ["netlist_spec"],
            },
        ),
        Tool(
            name="pcb_generate",
            description=(
                "Generate a fully-wired .kicad_pcb from a declarative spec "
                "using KiCad's pcbnew Python API. Each component in the spec "
                "needs an LCSC C-number — the plugin auto-fetches pin maps "
                "from EasyEDA so your nets can reference pin NAMES (like "
                "'GPIO10' or 'VCC') instead of numbers. First run may pause "
                "~12 seconds per unique IC for EasyEDA's rate limit; results "
                "are cached in SQLite so subsequent runs are instant. The "
                "tool then auto-places footprints in three bands (connectors "
                "on top, ICs in middle, passives below), wires every net "
                "pad-to-pad, and draws a board outline. Result: a .kicad_pcb "
                "ready to open in KiCad or EasyEDA for routing. REQUIRES "
                "KiCad 8+ installed (pcbnew is KiCad-side Python)."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "spec": {
                        "type": "object",
                        "description": "PCB spec with board/components/nets. See plugin docs for the full schema.",
                        "properties": {
                            "name": {"type": "string"},
                            "board": {"type": "object"},
                            "components": {"type": "array"},
                            "nets": {"type": "object"},
                        },
                        "required": ["components", "nets"],
                    },
                    "output_path": {
                        "type": "string",
                        "description": "Optional output .kicad_pcb path. Defaults to the active project's .kicad_pcb.",
                    },
                    "auto_fetch_pinmaps": {
                        "type": "boolean",
                        "default": True,
                        "description": (
                            "Fetch pin-name to pad-number maps from EasyEDA for "
                            "components that have an 'lcsc' field and no explicit "
                            "'pinmap'. Set false to work offline — nets must then "
                            "reference bare pad numbers."
                        ),
                    },
                    "force_refresh": {
                        "type": "boolean",
                        "default": False,
                        "description": (
                            "Bypass cached pin maps and refetch from EasyEDA. Slow "
                            "(rate-limited to one request per 12s); use only when a "
                            "cached map is known to be wrong."
                        ),
                    },
                    "lib_dir": {
                        "type": "string",
                        "description": (
                            "Directory holding KiCad's stock .pretty footprint "
                            "libraries. Normally omit it — the plugin finds KiCad's "
                            "libraries on Linux, macOS, Windows and Flatpak, and "
                            "honours KJLC_FOOTPRINT_DIR / KICAD*_FOOTPRINT_DIR."
                        ),
                    },
                },
                "required": ["spec"],
            },
        ),
        Tool(
            name="part_pin_map",
            description=(
                "Fetch the pin-name → pad-number map for an LCSC C-number "
                "from EasyEDA's component endpoint. Used internally by "
                "pcb_generate; exposed as a standalone tool so Claude can "
                "inspect a part's pinout before constructing a nets spec. "
                "Rate-limited (~12s between unique fetches); cached "
                "indefinitely in SQLite after first fetch."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "lcsc": {
                        "type": "string",
                        "description": "LCSC C-number, e.g. 'C82942' for ME6211C33M5G-N LDO.",
                    },
                    "force_refresh": {
                        "type": "boolean",
                        "default": False,
                        "description": "Bypass cache and re-fetch from EasyEDA.",
                    },
                },
                "required": ["lcsc"],
            },
        ),
        Tool(
            name="easyeda_handoff",
            description=(
                "Produce the final handoff message for the user: the path "
                "to the generated .kicad_pcb and step-by-step instructions "
                "for importing it into easyeda.com for routing and ordering. "
                "This is the recommended terminal tool of the plugin — EasyEDA "
                "handles the final routing + JLCPCB ordering step far more "
                "reliably than any headless auto-router we could ship."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "pcb_path": {
                        "type": "string",
                        "description": "Optional .kicad_pcb path. Defaults to the active project's board file.",
                    },
                },
            },
        ),
        Tool(
            name="package_for_jlcpcb",
            description=(
                "Run all kicad-cli manufacturing exports on the active project's "
                ".kicad_pcb (gerbers, drill, position, BOM), rename gerbers to "
                "Protel extensions, drill to .XLN, and zip everything into a "
                "JLCPCB-ready manufacturing/jlcpcb-<name>-<date>.zip. "
                "Requires that placement and routing are already done in KiCad — "
                "this is the terminal Phase 1 tool."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "pcb_path": {
                        "type": "string",
                        "description": "Optional .kicad_pcb path. Defaults to the active project's .kicad_pcb.",
                    },
                    "output_zip": {
                        "type": "string",
                        "description": "Optional output zip path. Defaults to <project>/manufacturing/jlcpcb-<name>-<date>.zip.",
                    },
                },
            },
        ),
        Tool(
            name="sch_run_erc",
            description=(
                "Run KiCad's Electrical Rules Check (ERC) on a schematic via "
                "kicad-cli. Returns counts of errors and warnings plus a path "
                "to the full report. Errors > 0 means the schematic has "
                "structural problems that should be fixed before layout."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "sch_path": {
                        "type": "string",
                        "description": "Optional schematic path. Defaults to the active project's .kicad_sch.",
                    },
                },
            },
        ),
        Tool(
            name="session_confirm_bom",
            description=(
                "Record that the user has reviewed and approved the BOM. Call "
                "this immediately after they confirm at the BOM checkpoint, "
                "before pcb_generate. Without it the approval lives only in "
                "the conversation: if Claude Code restarts, session_resume "
                "reports the BOM as merely 'sourced' and the user is asked to "
                "approve the same BOM again. Spending money on a board is the "
                "one decision in this workflow worth making durable."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "project_path": {
                        "type": "string",
                        "description": "Defaults to the active project.",
                    },
                    "notes": {
                        "type": "string",
                        "description": (
                            "Optional note about what the user approved or "
                            "asked to change, carried into the resume summary."
                        ),
                    },
                },
            },
        ),
        Tool(
            name="session_resume",
            description=(
                "Inspect the .kicad_jlcpcb_session.json in a project directory "
                "to see where a prior workflow left off. Returns the current "
                "stage (created / parts_sourced / bom_confirmed / pcb_generated "
                "/ handoff_rendered), which checkpoints are complete, and a "
                "recommended next step. Use at the start of /pcb-new or "
                "/pcb-from-bom on an existing project to offer the user a "
                "resume-or-restart choice."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "project_path": {
                        "type": "string",
                        "description": "Project directory or .kicad_pro path. Defaults to the active project when set.",
                    },
                },
            },
        ),
        Tool(
            name="lcsc_resolve_bom",
            description=(
                "Resolve a list of BOM rows to concrete LCSC parts. Each row "
                "may specify an LCSC C-number directly or a free-text query "
                "with an optional package. Hard-prefers basic-tier parts; for "
                "any row that resolves to an extended part, attaches a cost-"
                "impact warning and tallies the JLCPCB assembly setup fee."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "rows": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "lcsc": {
                                    "type": "string",
                                    "description": "LCSC C-number, e.g. 'C25804'.",
                                },
                                "query": {
                                    "type": "string",
                                    "description": "Free-text spec used when no LCSC C-number is given.",
                                },
                                "package": {
                                    "type": "string",
                                    "description": "Optional package filter for query rows.",
                                },
                                "qty": {
                                    "type": "integer",
                                    "description": "Quantity per board (passed through, not used for matching).",
                                },
                            },
                        },
                        "description": "BOM rows. Each row needs either 'lcsc' or 'query'.",
                    },
                },
                "required": ["rows"],
            },
        ),
    ]


def main():
    """Entry point for the MCP server."""
    logger.info(
        f"kicad-jlcpcb v{__version__} starting | "
        f"phase=1.6 | tools=detect_kicad,create_project,load_project,"
        f"lcsc_search,lcsc_resolve_bom,fetch_part_library,part_pin_map,"
        f"sch_generate,sch_run_erc,pcb_generate,easyeda_handoff,"
        f"package_for_jlcpcb,session_resume"
    )
    server = KicadJlcpcbServer()
    asyncio.run(server.run())


if __name__ == "__main__":
    main()
