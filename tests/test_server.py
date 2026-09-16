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
        # Phase 1 ships these three tools wired through the server.
        # Other Phase 1 tools (lcsc_*, sch_*, package_*) wire in below.
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
    """Phase 1 contract: every tool the server can route must appear in
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
    }

    def test_definitions_match(self):
        names = {t.name for t in _tool_definitions()}
        assert names == self.EXPECTED


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
        # No .kicad_pcb was created (Phase 1 doesn't auto-generate one)
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
