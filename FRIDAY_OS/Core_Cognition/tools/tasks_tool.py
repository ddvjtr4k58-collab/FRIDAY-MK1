"""Tasks — what is still outstanding."""

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

_ADD = r"(?:remind me to|add (?:a )?task|create (?:a )?task|put on my (?:task ?list|list))\s+(?P<value>.+)$"
_COMPLETE = r"(?:mark|tick)\s+(?P<value>.+?)\s+(?:as )?(?:done|complete|completed|off)\b"


def _plural(count: int, singular: str) -> str:
    return f"{count} {singular}" if count == 1 else f"{count} {singular}s"


def _list(_slots) -> ToolResult:
    from Core_Cognition.tasks_manager import build_tasks_payload

    payload = build_tasks_payload()
    counts = payload.get("counts", {})
    active = int(counts.get("total_active") or 0)
    overdue = int(counts.get("overdue") or 0)
    today = int(counts.get("today") or 0)
    next_task = payload.get("next_task") or {}

    if not active:
        return ToolResult(speech="Your task list is clear.", data=payload)

    sentence = f"You still have {_plural(active, 'task')}"

    if overdue:
        sentence += f", {overdue} of them overdue"
    elif today:
        sentence += f", {today} due today"

    title = str(next_task.get("title") or "").strip()

    if title:
        sentence += f". Next up is {title}"

    return ToolResult(speech=sentence + ".", data=payload)


def _today(_slots) -> ToolResult:
    from Core_Cognition.tasks_manager import build_tasks_payload

    payload = build_tasks_payload()
    today = payload.get("today") or []

    if not today:
        return ToolResult(speech="Nothing is due today.", data=payload)

    first = str((today[0] or {}).get("title") or "").strip()
    sentence = f"{_plural(len(today), 'task')} due today"

    if first:
        sentence += f", starting with {first}"

    return ToolResult(speech=sentence + ".", data=payload)


def _overdue(_slots) -> ToolResult:
    from Core_Cognition.tasks_manager import build_tasks_payload

    payload = build_tasks_payload()
    overdue = payload.get("overdue") or []

    if not overdue:
        return ToolResult(speech="Nothing is overdue.", data=payload)

    first = str((overdue[0] or {}).get("title") or "").strip()
    sentence = f"{_plural(len(overdue), 'task')} overdue"

    if first:
        sentence += f", the oldest being {first}"

    return ToolResult(speech=sentence + ".", data=payload)


def _add(slots) -> ToolResult:
    from Core_Cognition.tasks_manager import add_task, parse_due_datetime_from_text

    title = str(slots.get("title") or "").strip()

    if len(title) < 2:
        return ToolResult(ok=False, speech="")

    due_at, cleaned = parse_due_datetime_from_text(title)
    task = add_task(cleaned or title, due_at=due_at, source="tool_intelligence")
    speech = f"Added {task.get('title') or cleaned or title}."

    return ToolResult(
        speech=speech,
        action="refresh_tasks",
        data={"task": task},
        event="acknowledge"
    )


def _complete(slots) -> ToolResult:
    from Core_Cognition.tasks_manager import complete_task

    query = str(slots.get("title") or "").strip()

    if len(query) < 2:
        return ToolResult(ok=False, speech="")

    task = complete_task(query)

    if not task:
        return ToolResult(speech="I could not find a task matching that.")

    return ToolResult(
        speech=f"Marked {task.get('title')} done.",
        action="refresh_tasks",
        data={"task": task},
        event="acknowledge"
    )


def _open(_slots) -> ToolResult:
    return ToolResult(speech="", action="open_tasks", event="acknowledge")


register(Tool(
    name="tasks",
    description="The task and reminder list: what is outstanding, due, or overdue.",
    category="productivity",
    anchors=(
        "task",
        "tasks",
        "reminder",
        "reminders",
        "to do",
        "todo",
        "to dos",
        "todos",
        "due",
        "overdue",
        "list",
    ),
    inputs=("task title (for adding or completing)",),
    outputs=("spoken task summary", "tasks page when explicitly opened"),
    capabilities=(
        Capability(
            name="list_tasks",
            description="Everything still outstanding.",
            handler=_list,
            kind=KIND_DATA,
            default=True,
            priority=1,
            phrases=(
                "what tasks do i still have",
                "what tasks are left",
                "what do i still have to do",
                "what is left to do",
                "what do i have to do",
                "anything left on my list",
                "how many tasks do i have",
            ),
            patterns=(
                r"^what (?:tasks|reminders|to ?dos)\b",
                r"^what (?:do i|have i) (?:still )?(?:have|got) (?:left )?to do\b",
                r"^what is (?:still )?left to do\b",
                r"^anything (?:left|else) (?:on my list|to do)\b",
            ),
            cues=("still", "left", "outstanding", "remaining", "how many"),
            outputs=("active count", "overdue count", "next task"),
        ),
        Capability(
            name="due_today",
            description="Tasks due today.",
            handler=_today,
            kind=KIND_DATA,
            priority=2,
            phrases=("what is due today", "what do i need to do today", "anything due today"),
            patterns=(r"\b(?:due|need to do|to do)\b.*\btoday\b", r"\btoday\b.*\b(?:due|tasks?)\b"),
            cues=("today", "due today"),
            outputs=("today count", "first task"),
        ),
        Capability(
            name="overdue",
            description="Tasks past their due date.",
            handler=_overdue,
            kind=KIND_DATA,
            priority=3,
            phrases=("what is overdue", "anything overdue", "am i behind on anything"),
            patterns=(r"\boverdue\b", r"\bam i behind\b", r"\bbehind on anything\b"),
            cues=("overdue", "behind", "late"),
            outputs=("overdue count", "oldest task"),
        ),
        Capability(
            name="add_task",
            description="Add something to the list.",
            handler=_add,
            kind=KIND_ACTION,
            priority=4,
            patterns=(
                r"\bput (?:it |that )?on my (?:task ?list|list)\b",
                r"^add (?:a )?task\b",
                r"^remind me to \S",
            ),
            cues=("add", "put on my list", "new task"),
            slots=(Slot("title", _ADD, description="What to add"),),
            inputs=("task title", "optional due time"),
            outputs=("stored task",),
        ),
        Capability(
            name="complete_task",
            description="Mark something done.",
            handler=_complete,
            kind=KIND_ACTION,
            priority=4,
            patterns=(r"\b(?:mark|tick)\b.+\b(?:as )?(?:done|complete|completed|off)\b",),
            cues=("done", "complete", "finished", "tick off"),
            slots=(Slot("title", _COMPLETE, description="Which task"),),
            inputs=("task title",),
            outputs=("completed task",),
        ),
        Capability(
            name="open_tasks",
            description="Open the tasks page.",
            handler=_open,
            kind=KIND_OPEN,
            phrases=(
                "open tasks",
                "show tasks",
                "open my tasks",
                "show me my tasks",
                "pull up my tasks",
                "open my reminders",
                "open my to do list",
            ),
            patterns=(r"^(?:open|show|pull up|bring up|launch) (?:me )?(?:the |my )?(?:tasks?|reminders?|to ?do list)\b",),
            cues=("open", "show", "pull up", "bring up", "launch"),
            outputs=("tasks page",),
        ),
    ),
))
