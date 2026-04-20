"""Per-project session state, persisted to ``.kicad_jlcpcb_session.json``.

The session file lives at the project root and tracks where the user is in
the PCB-new / PCB-from-bom workflow. It survives Claude Code restarts and
lets ``/pcb-new`` on an existing project offer to *resume* mid-flow instead
of starting over.

The file is JSON, intentionally human-readable, and not required for any
core functionality — if it's missing or corrupt, we continue without it.
It is also listed in ``.gitignore`` because it's local state, not source.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, TypedDict

logger = logging.getLogger(__name__)

SESSION_FILENAME = ".kicad_jlcpcb_session.json"
_SCHEMA_VERSION = 1

Stage = Literal[
    "created",
    "parts_sourced",
    "bom_confirmed",
    "pcb_generated",
    "handoff_rendered",
]

_STAGE_ORDER: tuple[Stage, ...] = (
    "created",
    "parts_sourced",
    "bom_confirmed",
    "pcb_generated",
    "handoff_rendered",
)


class Session(TypedDict, total=False):
    """State snapshot for a single PCB project workflow."""

    schema_version: int
    project_path: str
    project_name: str
    stage: Stage
    created_at: str
    updated_at: str
    bom: list[dict[str, Any]]
    spec: dict[str, Any]
    checkpoints: dict[str, bool]
    last_tool: str
    notes: str


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _session_path(project_path: str | Path) -> Path:
    """Return the ``.kicad_jlcpcb_session.json`` path for a project dir."""
    root = Path(project_path).expanduser().resolve()
    if root.is_file():
        root = root.parent
    return root / SESSION_FILENAME


def new_session(project_path: str | Path, project_name: str) -> Session:
    """Create a fresh Session dict. Does not write to disk."""
    now = _now()
    return Session(
        schema_version=_SCHEMA_VERSION,
        project_path=str(Path(project_path).expanduser().resolve()),
        project_name=project_name,
        stage="created",
        created_at=now,
        updated_at=now,
        bom=[],
        spec={},
        checkpoints={
            "project_created": True,
            "parts_sourced": False,
            "bom_confirmed": False,
            "pcb_generated": False,
            "handoff_rendered": False,
        },
        last_tool="create_project",
        notes="",
    )


def load_session(project_path: str | Path) -> Session | None:
    """Read the session file for a project.

    Returns None if the file does not exist, is unreadable, or cannot be
    parsed. Corruption is logged as a warning — we never raise, because a
    bad session file shouldn't block the user from continuing.
    """
    path = _session_path(project_path)
    if not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as e:
        logger.warning("Ignoring corrupt session file at %s: %s", path, e)
        return None
    if not isinstance(raw, dict) or raw.get("schema_version") != _SCHEMA_VERSION:
        logger.warning(
            "Ignoring session file at %s: unsupported schema (got %r, expected %d)",
            path,
            raw.get("schema_version") if isinstance(raw, dict) else None,
            _SCHEMA_VERSION,
        )
        return None
    return raw  # type: ignore[return-value]


def save_session(session: Session) -> Path:
    """Write the session to its project's ``.kicad_jlcpcb_session.json``.

    The ``updated_at`` field is refreshed automatically. Returns the path
    that was written so callers can log or report it.
    """
    session["updated_at"] = _now()
    session.setdefault("schema_version", _SCHEMA_VERSION)
    path = _session_path(session["project_path"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(session, indent=2))
    return path


def update_stage(
    project_path: str | Path,
    stage: Stage,
    *,
    last_tool: str | None = None,
    bom: list[dict[str, Any]] | None = None,
    spec: dict[str, Any] | None = None,
    notes: str | None = None,
) -> Session | None:
    """Advance the session stage for a project.

    Loads the current session, updates the stage and any provided optional
    fields, marks all earlier checkpoints as complete, and writes it back.
    If no session file exists, returns None (caller chose not to track).
    """
    session = load_session(project_path)
    if session is None:
        return None

    session["stage"] = stage
    if last_tool is not None:
        session["last_tool"] = last_tool
    if bom is not None:
        session["bom"] = bom
    if spec is not None:
        session["spec"] = spec
    if notes is not None:
        session["notes"] = notes

    checkpoints = dict(session.get("checkpoints", {}))
    checkpoints.update(_checkpoints_up_to(stage))
    session["checkpoints"] = checkpoints

    save_session(session)
    return session


def _checkpoints_up_to(stage: Stage) -> dict[str, bool]:
    """Return checkpoint dict with every stage up to and including `stage` True."""
    keys = {
        "created": "project_created",
        "parts_sourced": "parts_sourced",
        "bom_confirmed": "bom_confirmed",
        "pcb_generated": "pcb_generated",
        "handoff_rendered": "handoff_rendered",
    }
    idx = _STAGE_ORDER.index(stage)
    return {keys[s]: True for s in _STAGE_ORDER[: idx + 1]}


def resume_summary(session: Session) -> dict[str, Any]:
    """Produce a human-friendly summary of session state.

    Intended to be returned from the ``session_resume`` MCP tool so the LLM
    can present the user with ``here is what you've done and here is what
    is next``.
    """
    stage = session.get("stage", "created")
    done = [k for k, v in session.get("checkpoints", {}).items() if v]
    todo = [k for k, v in session.get("checkpoints", {}).items() if not v]
    next_step = _next_step_for_stage(stage)

    return {
        "project_path": session.get("project_path"),
        "project_name": session.get("project_name"),
        "stage": stage,
        "completed_checkpoints": done,
        "pending_checkpoints": todo,
        "next_step": next_step,
        "bom_rows": len(session.get("bom", [])),
        "has_spec": bool(session.get("spec")),
        "last_tool": session.get("last_tool"),
        "created_at": session.get("created_at"),
        "updated_at": session.get("updated_at"),
    }


def _next_step_for_stage(stage: str) -> str:
    return {
        "created": "Source parts (lcsc_search or lcsc_resolve_bom), then confirm the BOM with the user.",
        "parts_sourced": "Review the BOM with the user, then call pcb_generate once they confirm.",
        "bom_confirmed": "Call pcb_generate to build the .kicad_pcb.",
        "pcb_generated": "Call easyeda_handoff to render the EasyEDA import instructions.",
        "handoff_rendered": "Project complete. Next run could iterate on the spec or package_for_jlcpcb for a Gerber zip.",
    }.get(stage, "Unknown stage — inspect the session file directly.")
