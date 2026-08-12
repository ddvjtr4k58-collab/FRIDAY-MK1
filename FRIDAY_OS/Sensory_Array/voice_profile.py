"""
Speech-text preparation and delivery-style classification for the FRIDAY voice.

Two responsibilities, both purely local (no extra model calls):

1. sanitize_for_speech() turns displayed text into text that is pleasant to hear:
   no markdown symbols, no spelled-out URLs, no code blocks, sentence punctuation
   preserved so the synthesizer can actually phrase the line.

2. classify_delivery_style() picks a delivery style from local rules, and the
   style tables below turn that into rate and pause choices. Style only affects
   *delivery* - it never changes the words, the facts, or any safety behaviour.

The displayed transcript keeps the original text; only the audio path goes
through here.
"""

from __future__ import annotations

import re
from typing import Dict, Optional


NEUTRAL = "neutral"
CONCISE = "concise"
INFORMATIVE = "informative"
WARNING = "warning"
CONVERSATIONAL = "conversational"
DRY_HUMOR = "dry_humor"

DELIVERY_STYLES = (NEUTRAL, CONCISE, INFORMATIVE, WARNING, CONVERSATIONAL, DRY_HUMOR)

# rate_multiplier, sentence pause (ms), clause pause (ms)
STYLE_DELIVERY = {
    NEUTRAL: (1.04, 140, 0),
    CONCISE: (1.10, 90, 0),
    INFORMATIVE: (1.00, 170, 0),
    WARNING: (0.92, 280, 110),
    CONVERSATIONAL: (1.04, 160, 0),
    DRY_HUMOR: (1.00, 240, 80),
}

# Styles that represent a real exchange rather than a one-shot acknowledgement.
FOLLOW_UP_STYLES = {INFORMATIVE, CONVERSATIONAL, DRY_HUMOR}

WARNING_MARKERS = (
    "error",
    "failed",
    "failure",
    "unable to",
    "cannot ",
    "can not ",
    "could not",
    "denied",
    "unavailable",
    "not connected",
    "not available",
    "requires attention",
    "requires action",
    "disconnected",
    "expired",
    "offline",
    "warning",
    "critical",
    "alert",
    "invalid",
    "missing",
    "timed out",
    "no response",
)

DRY_HUMOR_MARKERS = (
    "of course",
    "naturally",
    "as always",
    "predictably",
    "unsurprisingly",
    "apparently",
    "somehow",
    "as one does",
    "shockingly",
)

ACK_MARKERS = (
    "opened",
    "closed",
    "engaged",
    "disengaged",
    "saved",
    "cleared",
    "removed",
    "restored",
    "activated",
    "deactivated",
    "enabled",
    "disabled",
    "done",
    "acknowledged",
    "understood",
    "confirmed",
    "online",
    "standing by",
)

# Enthusiasm FRIDAY does not use. Stripped only when it opens a sentence, so
# content words like "Great Lakes" or "a perfect fit" survive intact.
OPENING_FILLERS = (
    "awesome",
    "fantastic",
    "wonderful",
    "absolutely",
    "delighted",
    "cheers",
)

CODE_BLOCK_PATTERN = re.compile(r"```.*?```", re.DOTALL)
INLINE_CODE_PATTERN = re.compile(r"`([^`]*)`")
MARKDOWN_LINK_PATTERN = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
URL_PATTERN = re.compile(r"\b(?:https?://|www\.)\S+", re.IGNORECASE)
EMAIL_PATTERN = re.compile(r"\b([A-Za-z0-9._%+-]+)@([A-Za-z0-9.-]+\.[A-Za-z]{2,})\b")
HEADING_PATTERN = re.compile(r"^\s{0,3}#{1,6}\s*", re.MULTILINE)
LIST_BULLET_PATTERN = re.compile(r"^\s{0,4}[-*+]\s+", re.MULTILINE)
NUMBERED_LIST_PATTERN = re.compile(r"^\s{0,4}(\d{1,2})[.)]\s+", re.MULTILINE)

CLAUSE_SPLIT_WORDS = (
    " and ",
    " but ",
    " because ",
    " which ",
    " although ",
    " however ",
    " so that ",
    " while ",
)

MAX_UNBROKEN_CLAUSE_CHARS = 140


def _spoken_host(url: str) -> str:
    host = re.sub(r"^https?://", "", str(url or ""), flags=re.IGNORECASE)
    host = host.split("/")[0].split("?")[0].strip()
    host = re.sub(r"^www\.", "", host, flags=re.IGNORECASE)

    if not host:
        return "a link"

    return "a link to {0}".format(host.replace(".", " dot "))


def sanitize_for_speech(text: str) -> str:
    """
    Convert display text into speakable text.

    Sentence punctuation is deliberately preserved: the previous implementation
    replaced every period with a comma, which removed every sentence boundary the
    synthesizer uses for phrasing and made long answers sound like one rushed run-on.
    """
    value = str(text or "").strip()

    if not value:
        return ""

    # Code blocks are never read aloud.
    value = CODE_BLOCK_PATTERN.sub(" ", value)
    value = INLINE_CODE_PATTERN.sub(r"\1", value)

    # Links read as their label or their host, never character by character.
    value = MARKDOWN_LINK_PATTERN.sub(lambda m: m.group(1), value)
    value = URL_PATTERN.sub(lambda m: _spoken_host(m.group(0)), value)
    value = EMAIL_PATTERN.sub(
        lambda m: "{0} at {1}".format(m.group(1), m.group(2).replace(".", " dot ")),
        value,
    )

    # Markdown structure becomes plain prose with sentence breaks.
    value = HEADING_PATTERN.sub("", value)
    value = LIST_BULLET_PATTERN.sub("", value)
    value = NUMBERED_LIST_PATTERN.sub("", value)
    value = re.sub(r"\*\*([^*]+)\*\*", r"\1", value)
    value = re.sub(r"\*([^*]+)\*", r"\1", value)
    value = re.sub(r"__([^_]+)__", r"\1", value)

    # Symbols that should be spoken rather than pronounced literally.
    value = value.replace("°", " degrees ")
    value = value.replace("%", " percent ")
    value = value.replace("&", " and ")
    value = re.sub(r"(?<=\d)\s*-\s*(?=\d)", " to ", value)

    # Remaining symbol noise.
    value = re.sub(r"[\[\]{}<>|~^*#`_\\]", " ", value)
    value = value.replace("(", ", ").replace(")", ", ")
    value = re.sub(r"\.{2,}", ", ", value)
    value = value.replace("!", ".").replace("¡", "")
    value = value.replace(";", ",")
    # Clock times keep their colon so "9:00" is spoken as a time, not "9, 00".
    value = re.sub(r"(?<!\d):(?!\d)", ",", value)
    value = re.sub(r"\bJARVI\b", "FRIDAY", value, flags=re.IGNORECASE)

    # Newlines become sentence boundaries so list items do not run together.
    value = re.sub(r"\s*\n+\s*", ". ", value)

    # Opening enthusiasm FRIDAY does not use.
    for filler in OPENING_FILLERS:
        value = re.sub(
            r"(^|(?<=[.?]\s))" + re.escape(filler) + r"\b[,!]?\s*",
            "",
            value,
            flags=re.IGNORECASE,
        )

    # Punctuation and whitespace tidy-up. Digit-adjacent commas are left alone so
    # thousands separators are not split into separate numbers.
    value = re.sub(r"\s+([,.?])", r"\1", value)
    value = re.sub(r",{2,}", ",", value)
    value = re.sub(r"\.{2,}", ".", value)
    value = re.sub(r"(?:,\s*)+([.?])", r"\1", value)
    value = re.sub(r",(?=[^\s\d])", ", ", value)
    value = re.sub(r"\s+", " ", value)
    # Removing an opening filler can leave orphaned punctuation behind.
    value = re.sub(r"^[\s,.?]+", "", value)
    value = re.sub(r"([.?])\s*\.", r"\1", value)
    value = value.strip(" \t\n\r,")

    if not value:
        return ""

    if value[-1] not in ".?":
        value = "{0}.".format(value.rstrip(" ,"))

    return value


def classify_delivery_style(text: str, context: Optional[str] = None) -> str:
    """
    Local delivery-style classification. Order matters: anything that reads as a
    warning is never delivered as humour.
    """
    value = str(text or "").strip()

    if not value:
        return NEUTRAL

    lowered = value.lower()
    context_value = str(context or "").strip().lower()

    if context_value in DELIVERY_STYLES:
        # An explicit marker from the caller wins, except that a warning-shaped
        # message can never be downgraded into humour.
        if context_value == DRY_HUMOR and any(marker in lowered for marker in WARNING_MARKERS):
            return WARNING

        return context_value

    if any(marker in lowered for marker in WARNING_MARKERS):
        return WARNING

    words = re.findall(r"[\w']+", value)
    sentences = [item for item in re.split(r"(?<=[.?])\s+", value) if item.strip()]

    if any(marker in lowered for marker in DRY_HUMOR_MARKERS) and len(words) <= 30:
        return DRY_HUMOR

    if len(words) <= 7 and any(marker in lowered for marker in ACK_MARKERS):
        return CONCISE

    if "?" in value:
        return CONVERSATIONAL

    has_data = bool(re.search(r"\d", value))

    if has_data and (len(words) > 8 or len(sentences) > 1):
        return INFORMATIVE

    if len(sentences) > 1 or len(words) > 22:
        return CONVERSATIONAL

    if len(words) <= 7:
        return CONCISE

    return NEUTRAL


def opens_conversation_window(style: str) -> bool:
    return str(style or NEUTRAL) in FOLLOW_UP_STYLES


def speech_rate_for_style(style: str, base_rate: int) -> int:
    multiplier = STYLE_DELIVERY.get(str(style or NEUTRAL), STYLE_DELIVERY[NEUTRAL])[0]

    try:
        rate = int(round(float(base_rate) * multiplier))
    except (TypeError, ValueError):
        rate = 175

    return max(90, min(rate, 300))


def voice_speed_for_style(style: str, base_speed: float) -> float:
    multiplier = STYLE_DELIVERY.get(str(style or NEUTRAL), STYLE_DELIVERY[NEUTRAL])[0]

    try:
        speed = float(base_speed) * multiplier
    except (TypeError, ValueError):
        speed = 0.9

    return max(0.65, min(speed, 1.1))


def _break_long_clauses(sentence: str) -> str:
    """Give very long generated sentences somewhere to breathe."""
    if len(sentence) <= MAX_UNBROKEN_CLAUSE_CHARS:
        return sentence

    result = sentence

    for word in CLAUSE_SPLIT_WORDS:
        if word in result and "," not in result:
            result = result.replace(word, ",{0}".format(word), 1)

    return result


def prepare_speech_text(text: str, style: str = NEUTRAL, provider: str = "local") -> str:
    """
    Produce the exact string handed to the synthesizer.

    For the macOS `say` provider, pacing is expressed with native embedded speech
    commands ([[slnc ms]]), which cost nothing to generate and are honoured by the
    synthesizer directly. Other providers get clean punctuation only.
    """
    cleaned = sanitize_for_speech(text)

    if not cleaned:
        return ""

    style_key = str(style or NEUTRAL)

    if style_key not in STYLE_DELIVERY:
        style_key = NEUTRAL

    sentences = [item.strip() for item in re.split(r"(?<=[.?])\s+", cleaned) if item.strip()]
    sentences = [_break_long_clauses(item) for item in sentences]

    if str(provider or "local").lower() != "local":
        return " ".join(sentences)

    _, sentence_pause, clause_pause = STYLE_DELIVERY[style_key]
    parts = []

    for index, sentence in enumerate(sentences):
        spoken = sentence

        if clause_pause > 0 and "," in spoken:
            spoken = spoken.replace(", ", ", [[slnc {0}]] ".format(clause_pause))

        parts.append(spoken)

        is_last = index == len(sentences) - 1

        if is_last:
            continue

        pause = sentence_pause

        # Dry delivery lands better with a beat before the closing line.
        if style_key == DRY_HUMOR and index == len(sentences) - 2:
            pause = int(sentence_pause * 1.6)

        parts.append("[[slnc {0}]]".format(pause))

    return " ".join(parts).strip()


def describe_style(style: str) -> str:
    return str(style or NEUTRAL)


def style_summary() -> Dict[str, float]:
    return {name: values[0] for name, values in STYLE_DELIVERY.items()}
