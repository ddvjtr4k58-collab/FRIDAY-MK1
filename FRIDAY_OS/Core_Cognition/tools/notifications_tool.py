"""Notifications — alerts FRIDAY has raised."""

from __future__ import annotations

from Core_Cognition.tools import (
    KIND_ACTION,
    KIND_DATA,
    KIND_OPEN,
    Capability,
    Tool,
    ToolResult,
    register,
)


def _recent(_slots) -> ToolResult:
    from Core_Cognition.proactive_manager import get_notifications

    notifications = get_notifications(limit=10) or []

    if not notifications:
        return ToolResult(speech="No alerts waiting.", data={"count": 0})

    latest = notifications[0] if isinstance(notifications[0], dict) else {}
    message = str(latest.get("message") or latest.get("title") or "").strip()
    count = len(notifications)
    sentence = f"{count} alert{'s' if count != 1 else ''} waiting"

    if message:
        sentence += f". The most recent: {message.rstrip('.')}"

    return ToolResult(speech=sentence + ".", data={"count": count, "notifications": notifications})


def _clear(_slots) -> ToolResult:
    from Core_Cognition.proactive_manager import clear_notifications

    return ToolResult(speech=str(clear_notifications() or "Alerts cleared."), event="acknowledge")


def _open(_slots) -> ToolResult:
    return ToolResult(speech="", action="open_notifications", event="acknowledge")


register(Tool(
    name="notifications",
    description="Alerts and the notification centre.",
    category="system",
    anchors=("notification", "notifications", "alert", "alerts", "warnings"),
    inputs=("none",),
    outputs=("spoken alert summary", "notification centre widget"),
    capabilities=(
        Capability(
            name="recent_notifications",
            description="How many alerts are waiting, and the most recent one.",
            handler=_recent,
            kind=KIND_DATA,
            priority=1,
            phrases=(
                "do i have any notifications",
                "any alerts",
                "any new alerts",
                "what alerts do i have",
                "did i miss anything",
            ),
            patterns=(r"^(?:any|do i have any|what)\b.*\b(?:alerts?|notifications?)\b", r"^did i miss anything\b"),
            cues=("any", "new", "waiting", "missed", "what"),
            outputs=("alert count", "latest alert"),
        ),
        Capability(
            name="clear_notifications",
            description="Dismiss every alert.",
            handler=_clear,
            kind=KIND_ACTION,
            priority=2,
            # Dismisses every alert at once and cannot be undone. Unchanged when
            # Jon asks for it and nothing else; what the flag stops is a planner
            # slipping it into a list of other things.
            destructive=True,
            phrases=("clear my notifications", "dismiss all alerts", "clear all alerts"),
            patterns=(r"^(?:clear|dismiss|wipe)\b.*\b(?:alerts?|notifications?)\b",),
            cues=("clear", "dismiss", "wipe"),
            outputs=("cleared count",),
        ),
        Capability(
            name="open_notifications",
            description="Open the notification centre.",
            handler=_open,
            kind=KIND_OPEN,
            phrases=("open notifications", "show notifications", "open notification center", "open my alerts"),
            patterns=(r"^(?:open|show|pull up|bring up) (?:the |my )?(?:notifications?|notification cent(?:er|re)|alerts)\b",),
            cues=("open", "show", "pull up", "bring up"),
            outputs=("notification centre widget",),
        ),
    ),
))
