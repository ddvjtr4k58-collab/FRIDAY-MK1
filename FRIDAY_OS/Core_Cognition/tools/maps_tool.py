"""Maps — the tactical map."""

from __future__ import annotations

from Core_Cognition.tools import (
    KIND_OPEN,
    Capability,
    Slot,
    Tool,
    ToolResult,
    register,
)

_DESTINATION = (
    r"\b(?:map|maps)\s+(?:of|for|to)\s+(?P<value>[\w .,'-]{2,60})$"
    r"|\b(?:where is|show me|find|locate|take me to)\s+(?P<alt>[\w .,'-]{2,60})\s+on the map\b"
)


def _show(slots) -> ToolResult:
    from Core_Cognition.state_manager import dock_orb_for_workspace
    from Sensory_Array.system_tools import open_map_widget

    destination = str(slots.get("destination") or "").strip()

    if not destination:
        return ToolResult(speech="", action="open_map", event="acknowledge")

    dock_orb_for_workspace(broadcast=False)
    result = open_map_widget(destination)

    return ToolResult(
        speech=str(result or f"Map centred on {destination}."),
        data={"destination": destination},
        event="acknowledge"
    )


def _open(_slots) -> ToolResult:
    return ToolResult(speech="", action="open_map", event="acknowledge")


register(Tool(
    name="maps",
    description="The tactical map, centred on the world or on a named place.",
    category="environment",
    anchors=("map", "maps", "tactical map", "globe", "atlas"),
    inputs=("destination (optional)",),
    outputs=("map widget",),
    capabilities=(
        Capability(
            name="open_map",
            description="Open the tactical map.",
            handler=_open,
            kind=KIND_OPEN,
            default=True,
            priority=1,
            phrases=("open the map", "show the map", "open maps", "pull up the map", "bring up the map"),
            patterns=(r"^(?:open|show|pull up|bring up|launch) (?:me )?(?:the )?(?:tactical )?maps?\b",),
            cues=("open", "show", "pull up", "bring up", "launch"),
            outputs=("map widget",),
        ),
        Capability(
            name="show_place",
            description="Centre the map on a named place.",
            handler=_show,
            kind=KIND_OPEN,
            priority=3,
            patterns=(
                r"\bmaps? (?:of|for|to) [\w .,'-]{2,}",
                r"\b(?:where is|show me|find|locate|take me to) [\w .,'-]{2,} on the map\b",
            ),
            cues=("of", "where is", "on the map", "take me to", "locate"),
            slots=(Slot("destination", _DESTINATION, description="Place to centre on"),),
            inputs=("destination",),
            outputs=("map widget centred on the destination",),
        ),
    ),
))
