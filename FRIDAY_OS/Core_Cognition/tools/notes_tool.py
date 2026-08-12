"""Notes — the sticky note board."""

from __future__ import annotations

from Core_Cognition.tools import (
    KIND_ACTION,
    KIND_DATA,
    KIND_OPEN,
    Capability,
    Slot,
    Tool,
    ToolResult,
    register,
)

_NOTE_TEXT = r"(?:note|write down|jot down)\s*(?:that|this)?[:,]?\s+(?P<value>.+)$"


def _list(_slots) -> ToolResult:
    from Sensory_Array.system_tools import load_notes

    notes = load_notes() or []

    if not notes:
        return ToolResult(speech="You have no notes saved.", data={"count": 0})

    latest = str((notes[-1] or {}).get("text") or "").strip()
    count = len(notes)
    sentence = f"You have {count} note{'s' if count != 1 else ''}"

    if latest:
        sentence += f". The most recent one says: {latest.rstrip('.')}"

    return ToolResult(speech=sentence + ".", data={"count": count, "notes": notes})


def _add(slots) -> ToolResult:
    from Sensory_Array.system_tools import add_note, create_notes_widget

    text = str(slots.get("text") or "").strip()

    if len(text) < 2:
        return ToolResult(ok=False, speech="")

    note = add_note(text, source="tool_intelligence")
    create_notes_widget()

    return ToolResult(speech="Note saved.", data={"note": note}, event="acknowledge")


def _open(_slots) -> ToolResult:
    return ToolResult(speech="", action="open_notes", event="acknowledge")


register(Tool(
    name="notes",
    description="Sticky notes: short things worth keeping on screen.",
    category="productivity",
    # No default capability. "Notes" on its own already opens the widget through
    # the brain loop's fast action table, and the verbs that surround the word
    # here — delete, clear, read, write down — mean genuinely different things.
    # Guessing between them is worse than handing the turn back.
    anchors=("note", "notes", "sticky note", "sticky notes", "jot", "scratchpad"),
    inputs=("note text (for adding)",),
    outputs=("spoken note summary", "notes widget"),
    capabilities=(
        Capability(
            name="list_notes",
            description="How many notes there are and what the latest one says.",
            handler=_list,
            kind=KIND_DATA,
            priority=1,
            phrases=(
                "what notes do i have",
                "read my notes",
                "what is on my notes",
                "what did i note down",
                "how many notes do i have",
            ),
            patterns=(r"^(?:what|read|how many)\b.*\bnotes?\b",),
            cues=("what", "read", "how many", "latest", "last"),
            outputs=("note count", "latest note"),
        ),
        Capability(
            name="add_note",
            description="Write a note down.",
            handler=_add,
            kind=KIND_ACTION,
            priority=3,
            patterns=(r"^(?:write down|jot down)\b", r"^note that\b"),
            cues=("write down", "jot down", "note that"),
            slots=(Slot("text", _NOTE_TEXT, description="What to write down"),),
            inputs=("note text",),
            outputs=("saved note",),
        ),
        Capability(
            name="open_notes",
            description="Put the notes widget on the Workstation.",
            handler=_open,
            kind=KIND_OPEN,
            phrases=("open notes", "show notes", "open my notes", "pull up my notes", "bring up notes"),
            patterns=(r"^(?:open|show|pull up|bring up|launch) (?:the |my )?(?:sticky )?notes\b",),
            cues=("open", "show", "pull up", "bring up", "launch"),
            outputs=("notes widget",),
        ),
    ),
))
