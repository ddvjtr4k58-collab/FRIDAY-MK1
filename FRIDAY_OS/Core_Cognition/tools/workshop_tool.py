"""Workshop — the multi-workspace engineering mode."""

from __future__ import annotations

from Core_Cognition.tools import (
    KIND_ACTION,
    KIND_DATA,
    Capability,
    Tool,
    ToolResult,
    register,
)


def _open(_slots) -> ToolResult:
    return ToolResult(speech="", action="open_workshop_mode", event="acknowledge")


def _close(_slots) -> ToolResult:
    return ToolResult(speech="", action="close_workshop_mode", event="acknowledge")


def _save(_slots) -> ToolResult:
    return ToolResult(speech="", action="save_workshop_layout", event="acknowledge")


def _status(_slots) -> ToolResult:
    from Core_Cognition.state_manager import get_hud_state_snapshot

    snapshot = get_hud_state_snapshot() or {}
    workshop = snapshot.get("workshop_mode") or {}
    active = bool(workshop.get("active"))
    cards = len(snapshot.get("active_cards") or [])

    if not active:
        return ToolResult(speech="Workshop mode is closed.", data={"active": False, "cards": cards})

    return ToolResult(
        speech=f"Workshop mode is engaged with {cards} widget{'s' if cards != 1 else ''} open.",
        data={"active": True, "cards": cards}
    )


register(Tool(
    name="workshop",
    description="Workshop mode: multiple workspaces, saved layouts and the Silent Operator chat.",
    category="workspace",
    anchors=("workshop", "workshop mode", "engineering mode", "layout", "workspaces"),
    inputs=("none",),
    outputs=("workshop mode state", "saved layout"),
    capabilities=(
        Capability(
            name="workshop_status",
            description="Whether Workshop mode is engaged.",
            handler=_status,
            kind=KIND_DATA,
            priority=1,
            phrases=("workshop status", "is workshop mode on", "am i in workshop mode"),
            patterns=(r"^(?:is|am i in)\b.*\bworkshop\b", r"^workshop status\b"),
            cues=("status", "is", "am i in"),
            outputs=("active", "open widget count"),
        ),
        Capability(
            name="open_workshop",
            description="Engage Workshop mode.",
            handler=_open,
            kind=KIND_ACTION,
            priority=2,
            phrases=("engage workshop mode", "start workshop mode", "get into workshop mode"),
            patterns=(r"^(?:open|start|engage|enter|launch|get into)\b.*\bworkshop\b",),
            cues=("open", "start", "engage", "enter", "launch"),
            outputs=("Workshop mode engaged",),
        ),
        Capability(
            name="close_workshop",
            description="Leave Workshop mode.",
            handler=_close,
            kind=KIND_ACTION,
            priority=3,
            phrases=("get out of workshop mode", "shut workshop mode", "drop out of workshop"),
            patterns=(r"^(?:close|exit|leave|shut|drop out of|get out of)\b.*\bworkshop\b",),
            cues=("close", "exit", "leave", "shut", "get out"),
            outputs=("Workshop mode closed",),
        ),
        Capability(
            name="save_layout",
            description="Save the current Workshop layout.",
            handler=_save,
            kind=KIND_ACTION,
            priority=3,
            phrases=("save this layout", "save my workshop layout", "remember this layout"),
            patterns=(r"^(?:save|keep|store)\b.*\blayout\b",),
            cues=("save", "keep", "store"),
            outputs=("saved layout",),
        ),
    ),
))
