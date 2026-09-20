"""The plugin's declarative surface — commands, agents, skill, manifests.

None of this is Python, so none of it was covered by anything. It is also
where the plugin broke for everyone who installed it the documented way:
`allowed-tools` named `mcp__kicad-jlcpcb__<tool>`, which is the name a
*project* .mcp.json server gets. An installed plugin's server is
`mcp__plugin_<plugin>_<server>__<tool>`, so the allowlist matched nothing,
the command ran with no MCP tools, and the model wrote prose about PCBs
instead of building one.

These tests pin the two halves that can drift: both naming forms present,
and every named tool actually existing on the server.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from kicad_jlcpcb_mcp.server import _tool_definitions

PLUGIN_ROOT = Path(__file__).resolve().parent.parent

SHORT_PREFIX = "mcp__kicad-jlcpcb__"
PLUGIN_PREFIX = "mcp__plugin_kicad-jlcpcb_kicad-jlcpcb__"

SURFACE_FILES = [
    PLUGIN_ROOT / "commands" / "pcb-new.md",
    PLUGIN_ROOT / "commands" / "pcb-from-bom.md",
    PLUGIN_ROOT / "agents" / "part-sourcer.md",
]


def _frontmatter(path: Path) -> str:
    text = path.read_text()
    assert text.startswith("---\n"), f"{path.name} has no YAML frontmatter"
    end = text.index("\n---\n", 3)
    return text[4:end]


def _declared_tools(path: Path) -> tuple[set[str], set[str]]:
    """Return (short-form tools, plugin-namespaced tools) declared in frontmatter."""
    body = _frontmatter(path)
    short, namespaced = set(), set()
    for line in body.split("\n"):
        line = line.strip()
        if line.startswith("#"):
            continue
        if m := re.match(r"^-\s*" + re.escape(PLUGIN_PREFIX) + r"(\w+)$", line):
            namespaced.add(m.group(1))
        elif m := re.match(r"^-\s*" + re.escape(SHORT_PREFIX) + r"(\w+)$", line):
            short.add(m.group(1))
    return short, namespaced


@pytest.fixture(scope="module")
def real_tool_names() -> set[str]:
    return {t.name for t in _tool_definitions()}


@pytest.mark.parametrize("path", SURFACE_FILES, ids=lambda p: p.name)
class TestDeclaredToolsMatchTheServer:
    def test_every_short_name_has_a_plugin_namespaced_twin(self, path):
        short, namespaced = _declared_tools(path)
        assert short, f"{path.name} declares no MCP tools at all"
        missing = short - namespaced
        assert not missing, (
            f"{path.name} grants {sorted(missing)} only as {SHORT_PREFIX}*. "
            f"An installed plugin exposes them as {PLUGIN_PREFIX}*, so this "
            "allowlist blocks them for anyone who installed from the "
            "marketplace. List both forms."
        )

    def test_no_orphan_namespaced_name(self, path):
        short, namespaced = _declared_tools(path)
        orphans = namespaced - short
        assert not orphans, (
            f"{path.name} grants {sorted(orphans)} only as {PLUGIN_PREFIX}*. "
            "A project-scope .mcp.json server uses the short form; list both."
        )

    def test_every_declared_tool_exists_on_the_server(self, path, real_tool_names):
        short, namespaced = _declared_tools(path)
        unknown = (short | namespaced) - real_tool_names
        assert not unknown, (
            f"{path.name} grants tools the server does not define: "
            f"{sorted(unknown)}. Renamed or removed in _tool_definitions()?"
        )


class TestCommandsCoverTheWorkflow:
    def test_pcb_new_grants_every_tool_the_server_has(self, real_tool_names):
        """/pcb-new is the full workflow; a tool it cannot reach is a tool
        the primary entry point cannot use."""
        short, _ = _declared_tools(PLUGIN_ROOT / "commands" / "pcb-new.md")
        assert short == real_tool_names, f"missing from /pcb-new: {sorted(real_tool_names - short)}"
