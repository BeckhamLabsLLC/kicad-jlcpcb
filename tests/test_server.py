"""Tests for MCP server tool routing and tool definitions."""

from unittest.mock import patch

import pytest

from kicad_jlcpcb_mcp.server import KicadJlcpcbServer, _tool_definitions


@pytest.fixture
def server():
    with patch("kicad_jlcpcb_mcp.server.Server"):
        return KicadJlcpcbServer()


class TestToolDefinitions:
    def test_phase1_tools_present(self):
        names = [t.name for t in _tool_definitions()]
        # A smoke check. TestAllToolsListed below is the real contract.
        for required in ("detect_kicad", "create_project", "load_project"):
            assert required in names

    def test_each_tool_has_input_schema(self):
        for tool in _tool_definitions():
            # Read through the serialised form: mcp 1.x exposes the field
            # as `inputSchema`, 2.x renamed the attribute to `input_schema`
            # while keeping `inputSchema` on the wire. The wire name is the
            # part that actually has to be right.
            schema = tool.model_dump(by_alias=True)["inputSchema"]
            assert schema is not None
            assert schema["type"] == "object"

    def test_no_duplicate_tool_names(self):
        names = [t.name for t in _tool_definitions()]
        assert len(names) == len(set(names))


class TestRouteDetectKicad:
    @pytest.mark.asyncio
    async def test_routes_to_kicad_cli(self, server):
        with patch("kicad_jlcpcb_mcp.kicad_cli.detect_kicad") as mock:
            mock.return_value = {"found": True, "version": "9.0.1", "meets_min": True}
            result = await server._handle_tool("detect_kicad", {})
        assert result["found"] is True
        assert result["version"] == "9.0.1"


class TestRouteCreateProject:
    @pytest.mark.asyncio
    async def test_creates_and_sets_active(self, server, tmp_path):
        result = await server._handle_tool(
            "create_project", {"parent_dir": str(tmp_path), "name": "demo"}
        )
        assert result["active_project"]["name"] == "demo"
        assert "tip" in result
        assert server._active_project is not None
        assert server._active_project["name"] == "demo"

    @pytest.mark.asyncio
    async def test_invalid_name_raises_value_error(self, server, tmp_path):
        from kicad_jlcpcb_mcp.project import ProjectError

        with pytest.raises(ProjectError):
            await server._handle_tool(
                "create_project", {"parent_dir": str(tmp_path), "name": "bad name"}
            )


class TestRouteLoadProject:
    @pytest.mark.asyncio
    async def test_loads_existing(self, server, tmp_path):
        await server._handle_tool("create_project", {"parent_dir": str(tmp_path), "name": "demo"})
        # Load it back via the path
        result = await server._handle_tool("load_project", {"project_path": str(tmp_path / "demo")})
        assert result["active_project"]["name"] == "demo"
        assert result["active_project"]["lib_file_count"] == 0


class TestUnknownTool:
    @pytest.mark.asyncio
    async def test_returns_error_dict(self, server):
        result = await server._handle_tool("nonexistent", {})
        assert "error" in result
        assert "Unknown tool" in result["error"]


class TestAllPhase1ToolsListed:
    """The contract: every tool the server can route must appear in
    list_tools, and vice versa. Catches drift between definitions and
    handlers."""

    EXPECTED = {
        "detect_kicad",
        "create_project",
        "load_project",
        "lcsc_search",
        "lcsc_resolve_bom",
        "fetch_part_library",
        "part_pin_map",
        "sch_generate",
        "sch_run_erc",
        "pcb_generate",
        "easyeda_handoff",
        "package_for_jlcpcb",
        "session_resume",
        "session_confirm_bom",
    }

    def test_definitions_match(self):
        names = {t.name for t in _tool_definitions()}
        assert names == self.EXPECTED


class TestHandoffStatesTheChecks:
    """The handoff is the last thing the user reads before spending money,
    so the three things that can silently produce a wrong board belong in
    it rather than only in a warnings array they may have scrolled past."""

    @pytest.mark.asyncio
    async def test_lists_what_to_check_before_ordering(self, server, tmp_path):
        await server._handle_tool("create_project", {"parent_dir": str(tmp_path), "name": "demo"})
        pcb = tmp_path / "demo" / "demo.kicad_pcb"
        pcb.write_text("(kicad_pcb)")
        result = await server._handle_tool("easyeda_handoff", {})
        joined = " ".join(result["before_you_order"]).lower()
        assert "placeholder footprint" in joined
        assert "assembly preview" in joined
        assert "layer" in joined


class TestEasyedaHandoff:
    @pytest.mark.asyncio
    async def test_requires_pcb_file(self, server, tmp_path):
        await server._handle_tool("create_project", {"parent_dir": str(tmp_path), "name": "demo"})
        # No .kicad_pcb exists yet
        with pytest.raises(ValueError, match="No .kicad_pcb"):
            await server._handle_tool("easyeda_handoff", {})

    @pytest.mark.asyncio
    async def test_returns_instructions(self, server, tmp_path):
        await server._handle_tool("create_project", {"parent_dir": str(tmp_path), "name": "demo"})
        pcb_path = tmp_path / "demo" / "demo.kicad_pcb"
        pcb_path.write_text("(kicad_pcb)")
        result = await server._handle_tool("easyeda_handoff", {})
        assert result["pcb_path"] == str(pcb_path)
        assert result["size_bytes"] > 0
        assert isinstance(result["next_steps"], list)
        assert any("easyeda.com" in s for s in result["next_steps"])
        assert "why_easyeda" in result
        assert "alternative" in result


class TestPackageForJlcpcb:
    @pytest.mark.asyncio
    async def test_requires_active_project(self, server):
        with pytest.raises(ValueError, match="No active project"):
            await server._handle_tool("package_for_jlcpcb", {})

    @pytest.mark.asyncio
    async def test_requires_pcb_file_to_exist(self, server, tmp_path):
        await server._handle_tool("create_project", {"parent_dir": str(tmp_path), "name": "demo"})
        # No .kicad_pcb was created — create_project does not make one.
        with pytest.raises(ValueError, match="No PCB file"):
            await server._handle_tool("package_for_jlcpcb", {})

    @pytest.mark.asyncio
    async def test_full_pipeline_with_mocked_kicad_cli(self, server, tmp_path, monkeypatch):
        from kicad_jlcpcb_mcp import kicad_cli

        # Create the project + a fake .kicad_pcb + the stub .kicad_sch
        await server._handle_tool("create_project", {"parent_dir": str(tmp_path), "name": "demo"})
        pcb = tmp_path / "demo" / "demo.kicad_pcb"
        pcb.write_text("(kicad_pcb)")

        # Mock all four kicad-cli exports to plant fake outputs
        async def fake_export_gerbers(pcb_path, output_dir):
            from pathlib import Path

            out = Path(output_dir)
            out.mkdir(parents=True, exist_ok=True)
            for layer in (
                "F_Cu",
                "B_Cu",
                "F_Silkscreen",
                "B_Silkscreen",
                "F_Mask",
                "B_Mask",
                "F_Paste",
                "B_Paste",
                "Edge_Cuts",
            ):
                (out / f"demo-{layer}.gbr").write_text("G04*\nM02*\n")
            return {"output_dir": str(out), "files": []}

        async def fake_export_drill(pcb_path, output_dir):
            from pathlib import Path

            out = Path(output_dir)
            (out / "demo.drl").write_text("M48\nM30\n")
            return {"output_dir": str(out), "files": []}

        async def fake_export_pos(pcb_path, output_path):
            from pathlib import Path

            # Exactly what kicad-cli writes, including a bottom-side part.
            Path(output_path).write_text(
                "Ref,Val,Package,PosX,PosY,Rot,Side\n"
                '"R1","10k","R_0603",1.500000,-2.500000,90.000000,top\n'
                '"C1","100nF","C_0402",4.000000,-2.500000,0.000000,bottom\n'
            )
            return {"output_path": str(output_path)}

        async def fake_sch_export_bom(sch_path, output_path):
            from pathlib import Path

            Path(output_path).write_text(
                '"Reference","Value","Footprint","LCSC"\n'
                '"R1","10k","Resistor_SMD:R_0603","C25804"\n'
            )
            return {"output_path": str(output_path)}

        monkeypatch.setattr(kicad_cli, "pcb_export_gerbers", fake_export_gerbers)
        monkeypatch.setattr(kicad_cli, "pcb_export_drill", fake_export_drill)
        monkeypatch.setattr(kicad_cli, "pcb_export_pos", fake_export_pos)
        monkeypatch.setattr(kicad_cli, "sch_export_bom", fake_sch_export_bom)

        result = await server._handle_tool("package_for_jlcpcb", {})
        assert "zip_path" in result
        assert "demo" in result["zip_path"]
        # 9 gerbers + 1 drill + 1 cpl + 1 bom = 12
        assert result["file_count"] == 12
        # The rotation caveat is reported — JLCPCB's expected orientation
        # differs from KiCad's for some packages.
        assert any("assembly preview" in w for w in result["warnings"])

        # The CPL in the zip must be JLCPCB's format, not KiCad's.
        import zipfile

        with zipfile.ZipFile(result["zip_path"]) as z:
            cpl_name = next(n for n in z.namelist() if n.endswith("-cpl.csv"))
            cpl = z.read(cpl_name).decode()
        lines = cpl.strip().splitlines()
        assert lines[0] == "Designator,Mid X,Mid Y,Layer,Rotation"
        assert "R1,1.500000,-2.500000,Top,90.000000" in lines
        assert "C1,4.000000,-2.500000,Bottom,0.000000" in lines
        assert "PosX" not in cpl, "KiCad's own column names must not survive"

        # Same for the BOM: JLCPCB keys on Comment/Designator/LCSC Part #.
        with zipfile.ZipFile(result["zip_path"]) as z:
            bom_name = next(n for n in z.namelist() if n.endswith("-bom.csv"))
            bom = z.read(bom_name).decode()
        assert bom.splitlines()[0] == "Comment,Designator,Footprint,LCSC Part #"
        assert "C25804" in bom
        assert "Reference" not in bom
        # Steps recorded — BOM now comes from schematic via sch_export_bom
        step_names = [s["step"] for s in result["steps"]]
        assert step_names == [
            "export_gerbers",
            "export_drill",
            "export_pos",
            # kicad-cli writes Ref,Val,Package,PosX,PosY,Rot,Side; JLCPCB
            # needs Designator,Mid X,Mid Y,Layer,Rotation. Skipping this
            # step ships a CPL JLCPCB rejects.
            "convert_cpl_to_jlcpcb",
            "export_bom_from_sch",
            "convert_bom_to_jlcpcb",
        ]


class TestBomFallbackWhenThereIsNoSchematic:
    """JLCPCB assembly orders require a BOM.

    `package_for_jlcpcb` used to set `bom_out = None` whenever the project
    had no schematic, producing a zip the user could not actually order
    assembly with — while a comment claimed it built one from the board.
    `pcb.bom_from_components` existed for this and was never called.
    """

    def test_bom_is_built_from_the_session_spec(self, tmp_path):
        from kicad_jlcpcb_mcp.pcb import bom_from_components

        components = [
            {"ref": "R1", "value": "10k", "lcsc": "C25804", "lib": "Resistor_SMD", "fp": "R_0603"},
            {"ref": "R2", "value": "10k", "lcsc": "C25804", "lib": "Resistor_SMD", "fp": "R_0603"},
            {
                "ref": "C1",
                "value": "100nF",
                "lcsc": "C1525",
                "lib": "Capacitor_SMD",
                "fp": "C_0402",
            },
        ]
        out = tmp_path / "bom.csv"
        result = bom_from_components(components, out)

        assert result["rows"] == 2, "identical parts should be grouped onto one line"
        assert result["parts"] == 3
        text = out.read_text()
        assert "Qty,Value,LCSC,Footprint,References" in text
        assert '2,"10k","C25804"' in text
        assert '"R1,R2"' in text

    def test_handler_reaches_for_the_session_spec(self):
        """Guard the wiring itself: the fallback must consult the session."""
        import inspect

        from kicad_jlcpcb_mcp import server

        src = inspect.getsource(server.KicadJlcpcbServer._package_for_jlcpcb)
        assert "bom_from_components" in src
        assert "load_session" in src


class TestServerActuallyConstructs:
    """Guard the `mcp<2` bound with a test rather than a comment.

    mcp 2.x removed `Server.list_tools` and renamed `Tool.inputSchema`, so
    the server dies during construction with "'Server' object has no
    attribute 'list_tools'". Nothing in this suite noticed: every other
    test calls `_tool_definitions()` and `_handle_tool` directly, never
    building the server. A Dependabot PR widening the bound to `<3` passed
    CI against mcp 2.2.0 while the plugin was unstartable.

    Constructing the server is the cheapest thing that exercises the
    handler registration those versions broke.
    """

    def test_constructing_the_server_registers_handlers(self):
        from kicad_jlcpcb_mcp.server import KicadJlcpcbServer

        # Raises on an incompatible mcp: _setup_handlers() calls
        # @server.list_tools() and @server.call_tool() at construction time.
        srv = KicadJlcpcbServer()
        assert srv._server is not None

    def test_installed_mcp_exposes_the_low_level_api_we_build_on(self):
        from mcp.server import Server

        for attr in ("list_tools", "call_tool", "run"):
            assert hasattr(Server, attr), (
                f"installed mcp has no Server.{attr}; the server cannot start. "
                "The `mcp<2` bound in pyproject.toml exists for this — do not "
                "widen it without porting server.py to the newer API."
            )


class TestToolRoutingForEveryTool:
    """Argument unpacking for each tool, which was almost entirely untested.

    `_handle_tool` reads args with `.get()` defaults that no test exercised
    — `lcsc_search`'s `stock_min`, `pcb_generate`'s `lib_dir`, and so on.
    A typo in any of those is invisible until a user hits it, because the
    tool-definition tests only check that the *schema* exists.
    """

    @pytest.fixture
    def stub(self, monkeypatch):
        """Record what each module-level function was called with."""
        calls: dict[str, dict] = {}

        def record(name):
            async def fn(*args, **kwargs):
                calls[name] = {"args": args, "kwargs": kwargs}
                return {"ok": True}

            return fn

        return calls, record

    @pytest.mark.asyncio
    async def test_lcsc_search_passes_every_argument_through(self, server, monkeypatch):
        from kicad_jlcpcb_mcp import lcsc_client

        seen = {}

        async def fake_search(query, **kwargs):
            seen.update({"query": query, **kwargs})
            return []

        monkeypatch.setattr(lcsc_client, "search", fake_search)
        await server._handle_tool(
            "lcsc_search",
            {
                "query": "10k 0603",
                "package": "0603",
                "basic_only": False,
                "stock_min": 500,
                "limit": 7,
            },
        )
        assert seen == {
            "query": "10k 0603",
            "package": "0603",
            "basic_only": False,
            "stock_min": 500,
            "limit": 7,
        }

    @pytest.mark.asyncio
    async def test_lcsc_search_defaults(self, server, monkeypatch):
        from kicad_jlcpcb_mcp import lcsc_client

        seen = {}

        async def fake_search(query, **kwargs):
            seen.update(kwargs)
            return []

        monkeypatch.setattr(lcsc_client, "search", fake_search)
        await server._handle_tool("lcsc_search", {"query": "resistor"})
        assert seen["basic_only"] is True
        assert seen["stock_min"] == 1
        assert seen["limit"] == 20
        assert seen["package"] is None

    @pytest.mark.asyncio
    async def test_lcsc_resolve_bom_forwards_rows(self, server, monkeypatch):
        from kicad_jlcpcb_mcp import lcsc_client

        seen = {}

        async def fake_resolve(rows):
            seen["rows"] = rows
            return {"resolved": [], "unresolved": []}

        monkeypatch.setattr(lcsc_client, "resolve_bom", fake_resolve)
        rows = [{"lcsc": "C25804", "qty": 10}]
        await server._handle_tool("lcsc_resolve_bom", {"rows": rows})
        assert seen["rows"] == rows

    @pytest.mark.asyncio
    async def test_part_pin_map_forwards_force_refresh(self, server, monkeypatch):
        from kicad_jlcpcb_mcp import part_library

        seen = {}

        async def fake(lcsc, **kwargs):
            seen.update({"lcsc": lcsc, **kwargs})
            return {"lcsc": lcsc, "pinmap": {}}

        monkeypatch.setattr(part_library, "get_pin_map", fake)
        await server._handle_tool("part_pin_map", {"lcsc": "C82942", "force_refresh": True})
        assert seen["lcsc"] == "C82942"
        assert seen["force_refresh"] is True

    @pytest.mark.asyncio
    async def test_pcb_generate_forwards_lib_dir_and_flags(self, server, tmp_path, monkeypatch):
        from kicad_jlcpcb_mcp import pcb as pcb_mod

        seen = {}

        async def fake_generate(spec, output_path, **kwargs):
            seen.update({"spec": spec, "output_path": output_path, **kwargs})

            class R:
                def to_dict(self):
                    return {"success": True}

            return R()

        monkeypatch.setattr(pcb_mod, "generate_pcb", fake_generate)
        await server._handle_tool("create_project", {"parent_dir": str(tmp_path), "name": "demo"})
        await server._handle_tool(
            "pcb_generate",
            {
                "spec": {"components": [], "nets": {}},
                "auto_fetch_pinmaps": False,
                "force_refresh": True,
                "lib_dir": "/custom/footprints",
            },
        )
        assert seen["auto_fetch_pinmaps"] is False
        assert seen["force_refresh"] is True
        assert seen["lib_dir"] == "/custom/footprints"

    @pytest.mark.asyncio
    async def test_pcb_generate_lib_dir_defaults_to_none(self, server, tmp_path, monkeypatch):
        """None means 'resolve it per-platform', not 'use a hardcoded path'."""
        from kicad_jlcpcb_mcp import pcb as pcb_mod

        seen = {}

        async def fake_generate(spec, output_path, **kwargs):
            seen.update(kwargs)

            class R:
                def to_dict(self):
                    return {"success": True}

            return R()

        monkeypatch.setattr(pcb_mod, "generate_pcb", fake_generate)
        await server._handle_tool("create_project", {"parent_dir": str(tmp_path), "name": "demo"})
        await server._handle_tool("pcb_generate", {"spec": {"components": [], "nets": {}}})
        assert seen["lib_dir"] is None

    @pytest.mark.asyncio
    async def test_sch_generate_routes_to_the_schematic_module(self, server, tmp_path, monkeypatch):
        from kicad_jlcpcb_mcp import schematic

        seen = {}

        def fake(sch_path, name, netlist_spec):
            seen.update({"sch_path": sch_path, "name": name, "spec": netlist_spec})
            return {"sch_path": str(sch_path)}

        monkeypatch.setattr(schematic, "sch_generate", fake)
        await server._handle_tool("create_project", {"parent_dir": str(tmp_path), "name": "demo"})
        spec = {"components": [{"ref": "R1"}], "nets": []}
        await server._handle_tool("sch_generate", {"netlist_spec": spec})
        assert seen["spec"] == spec
        assert seen["name"] == "demo"

    @pytest.mark.asyncio
    async def test_fetch_part_library_uses_the_active_libs_dir(self, server, tmp_path, monkeypatch):
        from kicad_jlcpcb_mcp import part_library

        seen = {}

        async def fake(lcsc, libs_dir, **kwargs):
            seen.update({"lcsc": lcsc, "libs_dir": str(libs_dir)})

            class R:
                def to_dict(self):
                    return {"lcsc": lcsc}

            return R()

        monkeypatch.setattr(part_library, "fetch_part_library", fake)
        await server._handle_tool("create_project", {"parent_dir": str(tmp_path), "name": "demo"})
        await server._handle_tool("fetch_part_library", {"lcsc": "C25804"})
        assert seen["lcsc"] == "C25804"
        assert seen["libs_dir"].endswith("libs")

    @pytest.mark.asyncio
    async def test_unknown_tool_is_reported_not_raised(self, server):
        result = await server._handle_tool("no_such_tool", {})
        assert "error" in result
        assert "no_such_tool" in result["error"]


class TestErrorsReachTheModel:
    """`call_tool` wraps handler exceptions into the JSON the model reads.
    An exception type that falls through the wrong branch reaches the client
    as a bare string with no type, which is much harder to act on."""

    def _handler(self, server):
        # The registered call_tool closure is what the SDK actually invokes.
        import mcp.types as t

        assert t  # imported for the side effect of proving the SDK is present
        return server

    @pytest.mark.asyncio
    async def test_value_error_is_reported_as_invalid_argument(self, server, monkeypatch):
        async def boom(name, args):
            raise ValueError("spec.components must be a non-empty list")

        monkeypatch.setattr(server, "_handle_tool", boom)
        # Re-register handlers against the patched method.
        server._setup_handlers()
        with pytest.raises(ValueError, match="non-empty list"):
            await server._handle_tool("pcb_generate", {})

    @pytest.mark.asyncio
    async def test_missing_active_project_names_the_fix(self, server):
        with pytest.raises(ValueError, match="project_path or an active project"):
            await server._handle_tool("session_resume", {})

    @pytest.mark.asyncio
    async def test_tools_requiring_a_project_say_so(self, server):
        for tool in ("fetch_part_library", "sch_generate", "pcb_generate"):
            with pytest.raises(ValueError, match="[Nn]o active project"):
                await server._handle_tool(
                    tool,
                    {
                        "lcsc": "C1",
                        "netlist_spec": {"components": [{"ref": "R1"}]},
                        "spec": {"components": [], "nets": {}},
                    },
                )


class TestSessionConfirmBom:
    """The BOM checkpoint is the plugin's one guard against spending money on
    a wrong board, and its approval used to live only in the conversation.
    `bom_confirmed` was a declared stage with a resume hint that nothing ever
    set, so a restart mid-flow asked the user to approve the same BOM twice."""

    @pytest.mark.asyncio
    async def test_advances_the_stage(self, server, tmp_path):
        from kicad_jlcpcb_mcp import session as session_mod

        await server._handle_tool("create_project", {"parent_dir": str(tmp_path), "name": "demo"})
        result = await server._handle_tool("session_confirm_bom", {})
        assert result["confirmed"] is True
        assert result["stage"] == "bom_confirmed"

        sess = session_mod.load_session(tmp_path / "demo")
        assert sess["stage"] == "bom_confirmed"
        assert sess["checkpoints"]["bom_confirmed"] is True

    @pytest.mark.asyncio
    async def test_survives_a_reload(self, server, tmp_path):
        """The whole point: a restart must not re-ask for approval."""
        await server._handle_tool("create_project", {"parent_dir": str(tmp_path), "name": "demo"})
        await server._handle_tool("session_confirm_bom", {"notes": "user approved"})

        fresh = type(server)()
        await fresh._handle_tool("load_project", {"project_path": str(tmp_path / "demo")})
        resumed = await fresh._handle_tool("session_resume", {})
        assert resumed["stage"] == "bom_confirmed"
        assert "pcb_generate" in resumed["next_step"]

    @pytest.mark.asyncio
    async def test_notes_are_carried_into_the_session(self, server, tmp_path):
        from kicad_jlcpcb_mcp import session as session_mod

        await server._handle_tool("create_project", {"parent_dir": str(tmp_path), "name": "demo"})
        await server._handle_tool("session_confirm_bom", {"notes": "swapped C1 for a 25V part"})
        sess = session_mod.load_session(tmp_path / "demo")
        assert "25V" in sess["notes"]

    @pytest.mark.asyncio
    async def test_requires_a_project(self, server):
        with pytest.raises(ValueError, match="project_path or an active project"):
            await server._handle_tool("session_confirm_bom", {})

    @pytest.mark.asyncio
    async def test_explains_a_missing_session_file(self, server, tmp_path):
        d = tmp_path / "bare"
        d.mkdir()
        with pytest.raises(ValueError, match="create_project or load_project"):
            await server._handle_tool("session_confirm_bom", {"project_path": str(d)})


class TestCallToolErrorWrapping:
    """`call_tool` is the boundary where a handler exception becomes
    something the model reads. Getting the branch wrong turns a clear
    "invalid_argument" into a bare string with no type, which is much
    harder to act on."""

    async def _call(self, server, name, args):
        import json

        result = await server._call_tool(name, args)
        return json.loads(result[0].text)

    @pytest.mark.asyncio
    async def test_value_error_becomes_a_typed_json_error(self, server, monkeypatch):
        async def boom(name, args):
            raise ValueError("spec.components must be a non-empty list")

        monkeypatch.setattr(server, "_handle_tool", boom)
        body = await self._call(server, "pcb_generate", {})
        assert body["type"] == "invalid_argument"
        assert "non-empty list" in body["error"]

    @pytest.mark.asyncio
    async def test_missing_file_is_typed_too(self, server, monkeypatch):
        async def boom(name, args):
            raise FileNotFoundError("no .kicad_pcb at /tmp/x")

        monkeypatch.setattr(server, "_handle_tool", boom)
        body = await self._call(server, "package_for_jlcpcb", {})
        assert body["type"] == "file_not_found"

    @pytest.mark.asyncio
    async def test_a_successful_result_is_plain_json(self, server, tmp_path):
        body = await self._call(
            server, "create_project", {"parent_dir": str(tmp_path), "name": "demo"}
        )
        assert "active_project" in body
        assert "error" not in body

    @pytest.mark.asyncio
    async def test_an_unexpected_exception_is_re_raised_not_swallowed(self, server, monkeypatch):
        """A bug in a handler must reach the logs, not be dressed up as a
        successful result the model then acts on."""

        async def boom(name, args):
            raise RuntimeError("something genuinely unexpected")

        monkeypatch.setattr(server, "_handle_tool", boom)
        with pytest.raises(RuntimeError, match="genuinely unexpected"):
            await server._call_tool("pcb_generate", {})

    @pytest.mark.asyncio
    async def test_the_payload_is_json_the_model_can_parse(self, server, tmp_path):
        """Non-serialisable values must not blow up the response."""
        from pathlib import Path

        async def returns_a_path(name, args):
            return {"where": Path(tmp_path) / "x.kicad_pcb"}

        monkeypatch = pytest.MonkeyPatch()
        monkeypatch.setattr(server, "_handle_tool", returns_a_path)
        body = await self._call(server, "anything", {})
        monkeypatch.undo()
        assert "x.kicad_pcb" in body["where"]


class TestStartupBanner:
    """The banner is the first diagnostic anyone reads when a server won't
    start. A hardcoded tool list drifted to 13 names while the server
    exposed 14."""

    def test_the_banner_is_derived_from_the_real_tool_list(self, caplog):
        import logging

        from kicad_jlcpcb_mcp import server as server_mod

        with caplog.at_level(logging.INFO):
            with (
                patch.object(server_mod, "KicadJlcpcbServer"),
                patch.object(server_mod.asyncio, "run"),
            ):
                server_mod.main()

        banner = caplog.text
        expected = [t.name for t in _tool_definitions()]
        assert f"{len(expected)} tools" in banner
        for name in expected:
            assert name in banner
