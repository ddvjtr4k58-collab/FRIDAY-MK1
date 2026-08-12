"""Calendar — what is on the schedule."""

from __future__ import annotations

from Core_Cognition.tools import (
    KIND_DATA,
    KIND_OPEN,
    Capability,
    Tool,
    ToolResult,
    register,
)

_NOT_CONNECTED = "Calendar is not connected yet."


def _agenda(fetch, label: str) -> ToolResult:
    from Sensory_Array.calendar_tools import (
        get_last_calendar_error,
        is_calendar_connected,
        summarize_events,
    )

    if not is_calendar_connected():
        return ToolResult(ok=False, speech=_NOT_CONNECTED)

    events = fetch()

    if get_last_calendar_error():
        return ToolResult(ok=False, speech="The calendar query failed.")

    return ToolResult(
        speech=summarize_events(events, label).rstrip(".") + ".",
        data={"label": label, "events": events, "count": len(events)}
    )


def _today(_slots) -> ToolResult:
    from Sensory_Array.calendar_tools import get_today_agenda

    return _agenda(get_today_agenda, "today")


def _tomorrow(_slots) -> ToolResult:
    from Sensory_Array.calendar_tools import get_tomorrow_agenda

    return _agenda(get_tomorrow_agenda, "tomorrow")


def _this_week(_slots) -> ToolResult:
    from Sensory_Array.calendar_tools import get_week_agenda

    return _agenda(get_week_agenda, "this week")


def _next_week(_slots) -> ToolResult:
    from Sensory_Array.calendar_tools import get_next_week_agenda

    return _agenda(get_next_week_agenda, "next week")


def _next_event(_slots) -> ToolResult:
    from Sensory_Array.calendar_tools import (
        format_event_start,
        get_last_calendar_error,
        get_next_event,
        is_calendar_connected,
    )

    if not is_calendar_connected():
        return ToolResult(ok=False, speech=_NOT_CONNECTED)

    event = get_next_event()

    if get_last_calendar_error():
        return ToolResult(ok=False, speech="The calendar query failed.")

    if not event:
        return ToolResult(speech="Nothing upcoming on the calendar.", data={"event": None})

    title = event.get("title") or "an untitled event"
    return ToolResult(
        speech=f"Next up is {title} at {format_event_start(event)}.",
        data={"event": event}
    )


def _status(_slots) -> ToolResult:
    from Sensory_Array.calendar_tools import is_calendar_connected

    connected = is_calendar_connected()

    return ToolResult(
        speech="Calendar is connected." if connected else _NOT_CONNECTED,
        data={"connected": connected}
    )


def _open(_slots) -> ToolResult:
    return ToolResult(speech="", action="open_calendar", event="acknowledge")


register(Tool(
    name="calendar",
    description="Events, meetings and the schedule ahead.",
    category="schedule",
    anchors=(
        "calendar",
        "agenda",
        "schedule",
        "event",
        "events",
        "meeting",
        "meetings",
        "appointment",
        "appointments",
    ),
    inputs=("day range (today, tomorrow, this week, next week)",),
    outputs=("spoken agenda summary", "calendar page when explicitly opened"),
    # Google Calendar is a network call, and a slow one on a cold token.
    timeout=8.0,
    capabilities=(
        Capability(
            name="todays_events",
            description="Everything on the calendar today.",
            handler=_today,
            kind=KIND_DATA,
            default=True,
            priority=1,
            phrases=(
                "what is on my calendar today",
                "what do i have on today",
                "what is my day look like",
                "what does my day look like",
                "am i busy today",
                "what meetings do i have today",
            ),
            patterns=(
                r"^what (?:is|does) my day\b",
                r"^am i busy\b",
                r"\b(?:calendar|agenda|schedule|meetings?|events?) (?:for )?today\b",
            ),
            cues=("today", "this morning", "this afternoon", "rest of the day"),
            outputs=("event count", "first event"),
        ),
        Capability(
            name="tomorrow",
            description="Everything on the calendar tomorrow.",
            handler=_tomorrow,
            kind=KIND_DATA,
            priority=2,
            phrases=("what do i have tomorrow", "what is on tomorrow", "am i busy tomorrow"),
            patterns=(r"\b(?:calendar|agenda|schedule|meetings?|events?|do i have|busy).*\btomorrow\b",),
            cues=("tomorrow",),
            outputs=("event count", "first event"),
        ),
        Capability(
            name="next_event",
            description="The single next thing coming up.",
            handler=_next_event,
            kind=KIND_DATA,
            priority=3,
            phrases=(
                "what is my next event",
                "what is next",
                "what is coming up",
                "when is my next meeting",
                "what is my next meeting",
            ),
            patterns=(
                r"\bnext (?:event|meeting|appointment)\b",
                r"^what (?:is|s) (?:coming up|next)\b",
                r"^when is my next\b",
                r"^what time is my next\b",
            ),
            cues=("next", "coming up", "after this"),
            outputs=("title", "start time", "location"),
            # What a later step may read out of this one. An event carries a
            # location often enough to be worth publishing, and publishing it is
            # what lets "the weather where my next meeting is" become a plan
            # rather than a guess. Absent on most events, which the planner
            # handles by skipping the dependent step rather than inventing one.
            provides=("event.location", "event.title"),
        ),
        Capability(
            name="upcoming_week",
            description="The week ahead.",
            handler=_this_week,
            kind=KIND_DATA,
            priority=2,
            phrases=(
                "what do i have this week",
                "what is on my calendar this week",
                "how busy is my week",
                "how is my week looking",
                "how does my week look",
                "what does my week look like",
            ),
            patterns=(
                r"\b(?:calendar|agenda|schedule|week).*\bthis week\b",
                r"\bhow (?:busy )?(?:is|does) my week\b",
                r"\bwhat does my week look like\b",
            ),
            cues=("this week", "the week", "rest of the week"),
            outputs=("event count", "next event"),
        ),
        Capability(
            name="next_week",
            description="The week after this one.",
            handler=_next_week,
            kind=KIND_DATA,
            priority=3,
            phrases=("what do i have next week", "what is on my calendar next week"),
            patterns=(r"\bnext week\b",),
            cues=("next week", "week after"),
            outputs=("event count", "next event"),
        ),
        Capability(
            name="calendar_status",
            description="Whether Google Calendar is connected.",
            handler=_status,
            kind=KIND_DATA,
            priority=4,
            phrases=("is my calendar connected", "is the calendar working", "calendar connection"),
            patterns=(
                r"\bis (?:my |the )?calendar (?:connected|working|linked|hooked up)\b",
                r"^(?:reconnect|reauthorize|reauthorise|connect|fix) (?:the |my )?calendar\b",
            ),
            cues=("connected", "connection", "working", "linked", "reconnect"),
            outputs=("connected",),
        ),
        Capability(
            name="open_calendar",
            description="Open the calendar page.",
            handler=_open,
            kind=KIND_OPEN,
            phrases=(
                "open calendar",
                "show calendar",
                "open my calendar",
                "show me my calendar",
                "pull up my calendar",
                "bring up the calendar",
                "open my agenda",
                "open my schedule",
            ),
            patterns=(r"^(?:open|show|pull up|bring up|launch) (?:me )?(?:the |my )?(?:calendar|agenda|schedule)\b",),
            cues=("open", "show", "pull up", "bring up", "launch"),
            outputs=("calendar page",),
        ),
    ),
))
