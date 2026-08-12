"""Memory — recall, preferences and the current project.

READ-ONLY over Memory v2's public surface. Nothing here touches the store's
architecture: retrieval, ranking and phrasing all stay inside memory_manager, and
this module only asks it questions. Saving is delegated to the same
parse_memory_command / run_memory_command pair the brain loop uses, so precedence
("remember to ..." is a task, not a memory) is decided in exactly one place.
"""

from __future__ import annotations

from Core_Cognition.tools import (
    KIND_ACTION,
    KIND_DATA,
    Capability,
    Slot,
    Tool,
    ToolResult,
    register,
)

_TOPIC = r"(?:remember|know|stored?|saved?)\s+(?:anything\s+)?about\s+(?P<value>.+)$"


def _recall(slots) -> ToolResult:
    from Core_Cognition import memory_manager

    topic = str(slots.get("topic") or "").strip()
    answer = memory_manager.describe_memories(topic).strip()

    if not answer or answer.lower().startswith("nothing stored"):
        return ToolResult(
            ok=False,
            speech="",
            data={"topic": topic, "found": False}
        )

    return ToolResult(speech=answer, data={"topic": topic, "found": True})


def _project(_slots) -> ToolResult:
    from Core_Cognition import memory_manager

    project = memory_manager.get_active_project() or {}
    name = str(project.get("name") or "").strip()

    if not name:
        return ToolResult(ok=False, speech="", data={"project": None})

    records = memory_manager.all_memories(
        scope=memory_manager.SCOPE_PROJECT,
        project_id=project.get("id"),
        include_all_projects=False
    )
    sentence = f"You are working on {name}."

    if records:
        memory_manager.touch([records[0]["id"]])
        sentence += f" Latest on it: {records[0]['text'].rstrip('.')}."

    return ToolResult(
        speech=sentence,
        data={"project": name, "notes": [record["text"] for record in records[:3]]}
    )


def _preferences(_slots) -> ToolResult:
    from Core_Cognition import memory_manager

    records = [
        record for record in memory_manager.all_memories(scope=memory_manager.SCOPE_USER)
        if record.get("category") == "preference"
    ]

    if not records:
        return ToolResult(ok=False, speech="", data={"found": False})

    selected = records[:3]
    memory_manager.touch([record["id"] for record in selected])

    return ToolResult(
        speech=" ".join(record["text"].rstrip(".") + "." for record in selected),
        data={"preferences": [record["text"] for record in selected]}
    )


def _save(slots) -> ToolResult:
    from Core_Cognition import memory_manager

    text = str(slots.get("_text") or "")
    intent = memory_manager.parse_memory_command(text)

    if not intent:
        # memory_manager declined it, which means it belongs to another system —
        # a task, or a note. Hand the turn straight back.
        return ToolResult(ok=False, speech="")

    reply = memory_manager.run_memory_command(intent, conversation_id="voice_main")

    if not reply:
        return ToolResult(ok=False, speech="")

    return ToolResult(speech=reply, data={"intent": intent.get("action")}, event="acknowledge")


register(Tool(
    name="memory",
    description="What FRIDAY remembers about Jon, his preferences and his projects.",
    category="memory",
    # "remember" and "forget" are deliberately NOT anchors. "Remember to buy milk"
    # is a task and "forget it" is small talk, and both contain the word — every
    # genuine memory request either matches a pattern below or says "memory",
    # "project" or "preference" outright.
    anchors=(
        "memory",
        "memories",
        "recall",
        "project",
        "working on",
        "prefer",
        "preference",
        "preferences",
    ),
    inputs=("topic (optional)",),
    outputs=("spoken recall", "stored memory"),
    capabilities=(
        Capability(
            name="search_memory",
            description="Recall what is stored about a topic.",
            handler=_recall,
            kind=KIND_DATA,
            priority=1,
            phrases=(
                "what do you remember",
                "what do you know about me",
                "what have you stored",
                "what do you remember about me",
            ),
            patterns=(
                r"^what (?:do|did) you (?:remember|know)\b",
                r"^do you remember\b",
                r"^what (?:have|did) you (?:stored?|saved?)\b",
            ),
            cues=("recall", "memory", "memories", "stored"),
            slots=(Slot("topic", _TOPIC, default="", description="What to recall"),),
            outputs=("recalled memories",),
        ),
        Capability(
            name="project_memory",
            description="Which project is active, and the latest note on it.",
            handler=_project,
            kind=KIND_DATA,
            priority=3,
            phrases=(
                "what project am i working on",
                "i forgot what project i am working on",
                "which project am i on",
                "what am i working on",
                "remind me what i am working on",
                "what is my current project",
            ),
            patterns=(
                r"\bwhat project\b",
                r"\bwhich project\b",
                r"\bcurrent project\b",
                r"\bwhat (?:am i|was i) working on\b",
                r"\bforgot what (?:project|i am working on)\b",
            ),
            cues=("project", "working on"),
            outputs=("project name", "recent project notes"),
        ),
        Capability(
            name="retrieve_preferences",
            description="Stored preferences.",
            handler=_preferences,
            kind=KIND_DATA,
            priority=2,
            phrases=("what are my preferences", "what do i prefer", "what are my settings preferences"),
            patterns=(r"^what (?:are my|do i) (?:preferences|prefer)\b", r"\bmy preferences\b"),
            cues=("preference", "preferences", "prefer"),
            outputs=("preference memories",),
        ),
        Capability(
            name="save_memory",
            description="Store something durable.",
            handler=_save,
            kind=KIND_ACTION,
            priority=4,
            patterns=(r"^(?:remember|keep in mind|do not forget) that\b", r"^make a note that\b"),
            cues=("remember that", "keep in mind"),
            inputs=("the fact to store",),
            outputs=("stored memory",),
        ),
    ),
))
