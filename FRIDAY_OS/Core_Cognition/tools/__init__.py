"""FRIDAY's tools.

Every module in this package describes one tool and registers it. Discovery is
automatic: drop a new module in here that calls `register(Tool(...))` and the
intent layer routes to it on the next start. Nothing else is edited — no router,
no phrase table, no allowlist.

Import order between tool modules is irrelevant; each one only touches the
registry. Handlers import their data sources lazily, inside the call, so loading
the whole package costs almost nothing at start-up and a tool whose backend is
unavailable still registers (and simply reports that it cannot answer).
"""

from __future__ import annotations

import importlib
import pkgutil
from typing import Any, Dict, List, Optional

from Core_Cognition.tool_registry import (
    INTENT_APPLICATION,
    INTENT_CONVERSATION,
    INTENT_TOOL,
    KIND_ACTION,
    KIND_DATA,
    KIND_OPEN,
    Capability,
    Resolution,
    Slot,
    Tool,
    ToolRegistry,
    ToolResult,
    normalize,
)

__all__ = [
    "Capability",
    "INTENT_APPLICATION",
    "INTENT_CONVERSATION",
    "INTENT_TOOL",
    "KIND_ACTION",
    "KIND_DATA",
    "KIND_OPEN",
    "REGISTRY",
    "Resolution",
    "Slot",
    "Tool",
    "ToolResult",
    "execute",
    "explain",
    "get",
    "load_tools",
    "manifest",
    "normalize",
    "register",
    "resolve",
    "set_action_bridge",
    "tools",
]


REGISTRY = ToolRegistry()


def register(tool: Tool) -> Tool:
    """Add a tool to FRIDAY. The one call a new tool module has to make."""
    return REGISTRY.register(tool)


# ==========================================================
# CONVERSATION GUARDS
# ==========================================================
# Requests that must never be routed, however many tool words they happen to
# contain. Checked before any tool is scored.

# Plain social speech. Short, complete, and about nothing.
_SOCIAL = {
    "hello",
    "hi",
    "hey",
    "yo",
    "hey there",
    "good morning",
    "good afternoon",
    "good evening",
    "good night",
    "goodnight",
    "thanks",
    "thank you",
    "thanks a lot",
    "thank you so much",
    "much appreciated",
    "appreciate it",
    "cheers",
    "nice",
    "cool",
    "great",
    "awesome",
    "perfect",
    "nice one",
    "well done",
    "good job",
    "sorry",
    "my bad",
    "never mind",
    "nevermind",
    "forget it",
    "ok",
    "okay",
    "sure",
    "yes",
    "no",
    "yeah",
    "yep",
    "nope",
    "how are you",
    "how are you doing",
    "how is it going",
    "what is up",
    "you good",
    "are you ok",
    "bye",
    "goodbye",
    "see you",
    "see you later",
    "talk later",
    "love you",
    "you are funny",
    "that is funny",
}

# Openers that mean "produce prose about this", not "operate this". Without them
# "explain how the weather forecast works" would open the weather widget.
_DISCURSIVE_STARTS = (
    "explain ",
    "explain how ",
    "explain why ",
    "summarize ",
    "summarise ",
    "translate ",
    "define ",
    "describe how ",
    "tell me about ",
    "tell me a ",
    "teach me ",
    "brainstorm ",
    "compare ",
    "what do you think",
    "what is your opinion",
    "give me ideas",
    "help me understand",
    "how does ",
    "why is ",
    "why are ",
    "why do ",
    "why does ",
)


# macOS audio levels and mute state are off limits to FRIDAY by standing rule, so
# "turn the music down" is conversation, not a Music capability. Without this the
# Music tool's anchor would claim the sentence and answer something adjacent —
# which is worse than not answering, because it looks like the request landed.
_AUDIO_LEVEL = (
    "volume",
    "louder",
    "quieter",
    "mute",
    "unmute",
    "turn it up",
    "turn it down",
    "turn up the",
    "turn down the",
)


def _social_guard(normalized: str, _original: str) -> bool:
    return normalized in _SOCIAL


def _audio_level_guard(normalized: str, _original: str) -> bool:
    return any(term in normalized for term in _AUDIO_LEVEL)


def _discursive_guard(normalized: str, _original: str) -> bool:
    return normalized.startswith(_DISCURSIVE_STARTS)


def _writing_guard(normalized: str, original: str) -> bool:
    """Drafting stays drafting.

    Reuses the existing detector rather than restating it, so the writing guard
    on this path and the one in the brain loop can never disagree about what
    counts as a drafting request.
    """
    try:
        from Core_Cognition.fast_router import is_writing_or_message_intent
    except Exception:
        return False

    return bool(is_writing_or_message_intent(original) or is_writing_or_message_intent(normalized))


_LOADED = False


def load_tools(force: bool = False) -> ToolRegistry:
    """Import every tool module in this package and install the guards."""
    global _LOADED

    if _LOADED and not force:
        return REGISTRY

    for module_info in pkgutil.iter_modules(__path__):
        if module_info.name.startswith("_"):
            continue

        importlib.import_module(f"{__name__}.{module_info.name}")

    REGISTRY.add_conversation_guard(_social_guard)
    REGISTRY.add_conversation_guard(_discursive_guard)
    REGISTRY.add_conversation_guard(_writing_guard)
    REGISTRY.add_conversation_guard(_audio_level_guard)

    _LOADED = True
    return REGISTRY


def resolve(text: str) -> Resolution:
    """What is this request? Conversation, a tool, or an application."""
    return REGISTRY.resolve(text)


def execute(resolution: Resolution) -> ToolResult:
    """Run a resolved capability. Never raises."""
    return REGISTRY.execute(resolution)


def explain(text: str) -> Dict[str, Any]:
    return REGISTRY.explain(text)


def manifest() -> List[Dict[str, Any]]:
    return REGISTRY.manifest()


def set_action_bridge(bridge) -> None:
    REGISTRY.set_action_bridge(bridge)


def get(name: str) -> Optional[Tool]:
    return REGISTRY.get(name)


def tools() -> List[Tool]:
    return REGISTRY.tools()


load_tools()
