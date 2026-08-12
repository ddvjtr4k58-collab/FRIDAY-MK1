"""Voice — how FRIDAY speaks.

Deliberately narrow. Listening, wake-word handling, barge-in and the live session
are owned by the audio engine and the brain loop and are not exposed here: the
voice path is the one thing in FRIDAY that must not change shape. What this tool
offers is the pair of things Jon actually asks for out loud — what voice is in use,
and turning speech on or off — both routed through the existing settings action so
there is only ever one way to flip that switch.
"""

from __future__ import annotations

from Core_Cognition.tools import (
    KIND_ACTION,
    KIND_DATA,
    Capability,
    Tool,
    ToolResult,
    register,
)


def _status(_slots) -> ToolResult:
    from Core_Cognition.settings_manager import get_setting

    enabled = bool(get_setting("voice_enabled", True))

    try:
        from Sensory_Array.audio_engine import describe_voice_provider

        provider = describe_voice_provider()
    except Exception:
        provider = str(get_setting("voice_provider", "friday_local") or "").replace("_", " ")

    if not enabled:
        return ToolResult(
            speech=f"Voice is switched off. When it is on I speak through {provider}.",
            data={"enabled": False, "provider": provider}
        )

    return ToolResult(speech=f"Voice is on, through {provider}.", data={"enabled": True, "provider": provider})


def _enable(_slots) -> ToolResult:
    return ToolResult(
        speech="Voice on.",
        action="toggle_voice",
        action_payload={"value": True},
        event="acknowledge"
    )


def _disable(_slots) -> ToolResult:
    return ToolResult(
        speech="Voice off.",
        action="toggle_voice",
        action_payload={"value": False},
        event="acknowledge"
    )


register(Tool(
    name="voice",
    description="FRIDAY's speech output: which voice is in use and whether she speaks at all.",
    category="system",
    anchors=("voice", "speech", "speaking", "talk out loud", "say things out loud"),
    inputs=("none",),
    outputs=("spoken voice status", "voice enabled or disabled"),
    capabilities=(
        Capability(
            name="voice_status",
            description="Whether voice is on, and which provider speaks.",
            handler=_status,
            kind=KIND_DATA,
            priority=1,
            phrases=("what voice are you using", "is your voice on", "what is your voice set to"),
            patterns=(r"^(?:what|which|is)\b.*\byour voice\b", r"^what voice\b"),
            cues=("what", "which", "is your", "status"),
            outputs=("enabled", "provider"),
        ),
        Capability(
            name="enable_voice",
            description="Start speaking replies again.",
            handler=_enable,
            kind=KIND_ACTION,
            priority=2,
            phrases=("turn your voice on", "start talking again", "speak out loud again"),
            patterns=(r"^turn (?:your )?voice (?:back )?on\b", r"^(?:start|begin) (?:talking|speaking)\b"),
            cues=("turn on", "back on", "start talking", "start speaking"),
            outputs=("voice enabled",),
        ),
        Capability(
            name="disable_voice",
            description="Stop speaking replies; keep answering on screen.",
            handler=_disable,
            kind=KIND_ACTION,
            priority=2,
            phrases=("turn your voice off", "stop speaking out loud", "answer on screen only"),
            patterns=(r"^turn (?:your )?voice off\b", r"^stop (?:talking|speaking) (?:out loud|entirely)\b"),
            cues=("turn off", "stop talking out loud", "on screen only", "silent"),
            outputs=("voice disabled",),
        ),
    ),
))
