"""Settings — the configuration surface."""

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

def _set_theme(slots) -> ToolResult:
    """Pick the theme out of the sentence by name.

    Matched against settings_manager's own alias table rather than a regex, for
    two reasons: the aliases are the real vocabulary ("charcoal" and "steel" both
    mean Graphite), and a request naming no known theme must fail closed.
    normalize_theme() answers "blue" for anything it does not recognise, so
    handing it an unchecked word would quietly repaint the interface.
    """
    from Core_Cognition.settings_manager import (
        THEME_ALIASES,
        THEME_LABELS,
        normalize_theme,
        theme_label,
    )

    text = f" {str(slots.get('_normalized') or '')} "
    known = sorted(set(THEME_ALIASES) | set(THEME_LABELS), key=len, reverse=True)
    requested = next((name for name in known if f" {name} " in text), "")

    if not requested:
        return ToolResult(ok=False, speech="")

    normalized = normalize_theme(requested)

    return ToolResult(
        speech=f"{theme_label(normalized)} theme.",
        action="set_theme",
        action_payload={"value": normalized},
        data={"theme": normalized},
        event="acknowledge"
    )


def _status(_slots) -> ToolResult:
    from Core_Cognition.settings_manager import get_setting, theme_label

    theme = theme_label(get_setting("theme", "blue"))
    voice_on = bool(get_setting("voice_enabled", True))
    location = str(get_setting("default_location", "") or "").split(",")[0].strip()

    parts = [f"the {theme} theme", "voice on" if voice_on else "voice off"]

    if location:
        parts.append(f"weather set to {location}")

    return ToolResult(
        speech="You are running " + ", ".join(parts) + ".",
        data={"theme": theme, "voice_enabled": voice_on, "location": location}
    )


def _open(_slots) -> ToolResult:
    return ToolResult(speech="", action="open_settings", event="acknowledge")


register(Tool(
    name="settings",
    description="FRIDAY's configuration: theme, voice, location, presence and startup behaviour.",
    category="system",
    anchors=("setting", "settings", "configuration", "config", "theme", "options"),
    inputs=("none",),
    outputs=("spoken configuration summary", "settings page"),
    capabilities=(
        Capability(
            name="settings_status",
            description="The settings that are currently in force.",
            handler=_status,
            kind=KIND_DATA,
            priority=1,
            phrases=("what are my settings", "what settings am i using", "what theme am i on"),
            patterns=(r"^what\b.*\b(?:settings|theme) (?:am i|do i|are my)\b", r"^what are my settings\b"),
            cues=("what", "current", "am i using", "am i on"),
            outputs=("theme", "voice state", "default location"),
        ),
        Capability(
            name="set_theme",
            description="Switch the interface theme.",
            handler=_set_theme,
            kind=KIND_ACTION,
            priority=3,
            phrases=("switch to the blue theme", "put it in graphite"),
            patterns=(
                r"^(?:set|change|switch|make it|use|go|put it in)\b.*\btheme\b",
                r"^(?:switch|change) to (?:the )?[\w-]+(?: theme)?$",
                r"^(?:make it|put it in) [\w-]+$",
            ),
            cues=("set", "change", "switch", "use", "make it"),
            inputs=("theme name",),
            outputs=("applied theme",),
        ),
        Capability(
            name="open_settings",
            description="Open the settings page.",
            handler=_open,
            kind=KIND_OPEN,
            priority=2,
            phrases=(
                "open settings",
                "show settings",
                "open my settings",
                "open the settings page",
                "pull up settings",
                "open configuration",
            ),
            patterns=(r"^(?:open|show|pull up|bring up|launch) (?:the |my )?(?:settings|configuration|config|options)\b",),
            cues=("open", "show", "pull up", "bring up", "launch"),
            outputs=("settings page",),
        ),
    ),
))
