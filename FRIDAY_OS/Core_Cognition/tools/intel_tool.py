"""Intel — the news, markets and conflict briefing."""

from __future__ import annotations

from Core_Cognition.tools import (
    KIND_DATA,
    KIND_OPEN,
    Capability,
    Tool,
    ToolResult,
    register,
)


def _headlines(tab: str, label: str) -> ToolResult:
    from Sensory_Array.system_tools import get_news_payload

    payload = get_news_payload(active_tab=tab) or {}
    tabs = payload.get("tabs") or {}
    items = (tabs.get(tab) or {}).get("items") or []
    headlines = [
        str(item.get("title") or "").strip()
        for item in items[:2]
        if isinstance(item, dict) and item.get("title")
    ]

    if not headlines:
        return ToolResult(ok=False, speech=f"No {label} available right now.", data=payload)

    return ToolResult(
        speech=f"Top of the {label}: " + " ".join(item.rstrip(".") + "." for item in headlines),
        data={"tab": tab, "headlines": headlines}
    )


def _briefing(_slots) -> ToolResult:
    return _headlines("briefing", "briefing")


def _markets(_slots) -> ToolResult:
    return _headlines("markets", "market news")


def _open(_slots) -> ToolResult:
    return ToolResult(speech="", action="open_intel", event="acknowledge")


register(Tool(
    name="intel",
    description="The Intel briefing: headlines, markets and world events.",
    category="information",
    anchors=("news", "headline", "headlines", "briefing", "intel", "markets", "stocks"),
    inputs=("none",),
    outputs=("spoken headline summary", "Intel widget"),
    # News feeds over the network.
    timeout=8.0,
    capabilities=(
        Capability(
            name="briefing",
            description="The top headlines.",
            handler=_briefing,
            kind=KIND_DATA,
            priority=1,
            phrases=("what is in the news", "give me the headlines", "what is happening today"),
            patterns=(r"^what is (?:in|on) the news\b", r"\b(?:give me|read me) the (?:news|headlines)\b"),
            cues=("news", "headlines", "briefing", "happening"),
            outputs=("headlines",),
        ),
        Capability(
            name="markets",
            description="Market headlines.",
            handler=_markets,
            kind=KIND_DATA,
            priority=2,
            phrases=("how are the markets", "what are the markets doing"),
            patterns=(r"\b(?:markets?|stocks)\b.*\b(?:doing|today|look|looking)\b", r"^how are the markets\b"),
            cues=("markets", "stocks"),
            outputs=("market headlines",),
        ),
        Capability(
            name="open_intel",
            description="Open the Intel widget.",
            handler=_open,
            kind=KIND_OPEN,
            phrases=("open intel", "show intel", "open the briefing", "pull up the news"),
            patterns=(r"^(?:open|show|pull up|bring up|launch) (?:the |my )?(?:intel|news|briefing)\b",),
            cues=("open", "show", "pull up", "bring up", "launch"),
            outputs=("Intel widget",),
        ),
    ),
))
