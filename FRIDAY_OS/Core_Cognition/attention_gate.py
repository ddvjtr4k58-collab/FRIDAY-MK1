import re
from typing import Dict, Optional


BYPASS_SOURCES = {"override", "manual", "button", "launcher", "workshop_chat", "workshop_dock", "hotbar"}
SHUTDOWN_COMMANDS = {"exit", "quit", "shutdown", "shut down", "power down", "terminate"}

COMMAND_VERBS = (
    "open",
    "close",
    "show",
    "hide",
    "play",
    "pause",
    "resume",
    "next",
    "previous",
    "clear",
    "turn on",
    "turn off",
    "switch",
    "change",
    "set",
    "enable",
    "disable",
    "launch",
    "enter",
    "exit",
    "shutdown",
    "quit",
    "remind",
    "add",
    "delete",
    "mark",
    "test",
    "identify",
    "list",
    "status",
    "check",
)

COMMAND_TARGETS = (
    "settings",
    "calendar",
    "tasks",
    "task widget",
    "reminders",
    "music",
    "notes",
    "files",
    "weather",
    "map",
    "notifications",
    "workstation",
    "sleep screen",
    "hud",
    "theme",
    "voice",
    "presence",
    "camera",
    "cameras",
    "camera presence",
    "door camera",
    "desk camera",
    "workshop",
    "showcase",
)

SPECIAL_DIRECT_COMMANDS = {
    "camera presence status",
    "camera status",
    "check camera presence",
    "test door camera",
    "test desk camera",
    "enable camera presence",
    "turn on camera presence",
    "disable camera presence",
    "turn off camera presence",
    "identify cameras",
    "list cameras",
}

TALKING_ABOUT_PREDICATES = (
    "can",
    "could",
    "would",
    "will",
    "is",
    "has",
    "does",
    "did",
    "knows",
    "uses",
    "connects",
    "connected",
    "handles",
    "opens",
    "plays",
    "shows",
    "runs",
)

ALBANIAN_HINTS = (
    # Definite form of the assistant's name. "jarvisi" stays alongside it for
    # the same reason "jarvis" stays in WAKE_NAMES below.
    "fridayi",
    "jarvisi",
    "mundet",
    "mundet me",
    "me hap",
    "kalendarin",
    "vllaut",
    "vllau",
    "tregoj",
    "qka",
    "bon",
)


# FRIDAY is the assistant identity. "jarvis" stays accepted during migration so
# nothing that already worked suddenly stops.
WAKE_NAMES = ("friday", "jarvis")
WAKE_PATTERN = r"(?:friday|jarvis)"
# Politeness prefixes people actually use before the name.
WAKE_PREFIX_PATTERN = r"^(?:hey|ok|okay|yo)\s+" + WAKE_PATTERN + r"\b"


def strip_wake_prefix(value: str) -> str:
    """Remove a leading wake name, with or without hey/ok, plus trailing please."""
    cleaned = re.sub(WAKE_PREFIX_PATTERN, "", value).strip()
    cleaned = re.sub(r"^" + WAKE_PATTERN + r"\b", "", cleaned).strip()
    return re.sub(r"\s+please$", "", cleaned).strip()


def _normalize(text: str) -> str:
    value = str(text or "").lower().replace("_", " ")
    value = re.sub(r"[^\w\s]", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def _strip_wake_and_polite_words(value: str) -> str:
    tokens = [
        token
        for token in re.split(r"\s+", _normalize(value))
        if token and token not in {"friday", "jarvis", "please", "pls", "hey", "ok", "okay"}
    ]
    return re.sub(r"\s+", " ", " ".join(tokens)).strip()


def _result(should_respond: bool, reason: str, confidence: float, normalized: str) -> Dict[str, object]:
    return {
        "should_respond": bool(should_respond),
        "reason": reason,
        "confidence": float(confidence),
        "normalized": normalized,
    }


def normalize_shutdown_command_text(text: str) -> str:
    return _strip_wake_and_polite_words(text)


def is_shutdown_command(text: str) -> bool:
    return normalize_shutdown_command_text(text) in SHUTDOWN_COMMANDS


def _has_known_command_tail(value: str) -> bool:
    normalized = _normalize(value)

    if not normalized:
        return False

    return any(
        normalized == verb
        or normalized.startswith(f"{verb} ")
        or f" {verb} " in f" {normalized} "
        for verb in COMMAND_VERBS
    ) and any(
        normalized == target
        or normalized.startswith(f"{target} ")
        or normalized.endswith(f" {target}")
        or f" {target} " in f" {normalized} "
        for target in COMMAND_TARGETS
    )


def _clean_polite_request_tail(value: str) -> str:
    normalized = _normalize(value)
    normalized = re.sub(r"\b(please|pls)\b", " ", normalized)
    normalized = re.sub(r"\s+", " ", normalized).strip()
    return normalized


def detect_polite_direct_request(text: str) -> bool:
    return _polite_direct_normalized(text) is not None


def _polite_direct_normalized(text: str) -> Optional[str]:
    value = _normalize(text)

    if not value:
        return None

    value = strip_wake_prefix(value)

    patterns = (
        r"^(?:can|could|would|will)\s+you\s+(.+)$",
        r"^please\s+(.+)$",
    )

    for pattern in patterns:
        match = re.match(pattern, value)

        if not match:
            continue

        tail = _clean_polite_request_tail(match.group(1))

        if _has_known_command_tail(tail) or is_shutdown_command(tail):
            return tail

    return None


def detect_clear_english_direct_command(text: str) -> bool:
    value = _normalize(text)

    if not value:
        return False

    value = strip_wake_prefix(value)

    if is_shutdown_command(value):
        return True

    if _clean_polite_request_tail(value) in SPECIAL_DIRECT_COMMANDS:
        return True

    return any(value == verb or value.startswith(f"{verb} ") for verb in COMMAND_VERBS)


def _clear_direct_normalized(text: str) -> str:
    value = _normalize(text)
    value = strip_wake_prefix(value)
    return _clean_polite_request_tail(value)


def detect_albanian_geg_casual(text: str) -> bool:
    value = _normalize(text)

    if not value:
        return False

    return any(hint in value for hint in ALBANIAN_HINTS)


def detect_talking_about_assistant(text: str) -> bool:
    value = _normalize(text)

    if not value or not any(name in value for name in WAKE_NAMES):
        return False

    if detect_polite_direct_request(value):
        return False

    if re.search(r"\b" + WAKE_PATTERN + r"\s+(?:can|could|would|will)\s+you\b", value):
        return False

    if re.search(r"\b(?:can|could|would|will)\s+you\b", value):
        return False

    about_context = re.search(
        r"\b(?:told|telling|explaining|explained|said|saying|talking|about|that)\b.*\b" + WAKE_PATTERN + r"\b",
        value,
    )

    if about_context:
        return True

    predicate_pattern = r"\b" + WAKE_PATTERN + r"\s+(?:" + "|".join(re.escape(item) for item in TALKING_ABOUT_PREDICATES) + r")\b"
    return re.search(predicate_pattern, value) is not None


def _looks_unclear_casual(value: str) -> bool:
    normalized = _normalize(value)

    if not normalized:
        return True

    if any(name in normalized for name in WAKE_NAMES):
        return False

    if detect_polite_direct_request(normalized) or detect_clear_english_direct_command(normalized):
        return False

    if _has_known_command_tail(normalized):
        return False

    return len(normalized.split()) <= 6


def should_friday_respond(text: str, input_source: str = "voice") -> Dict[str, object]:
    source = str(input_source or "").strip().lower()
    value = _normalize(text)

    if source in BYPASS_SOURCES:
        return _result(True, "bypass_source", 1.0, value)

    if is_shutdown_command(value):
        return _result(True, "shutdown_command", 1.0, normalize_shutdown_command_text(value))

    if detect_clear_english_direct_command(value):
        return _result(True, "clear_direct_command", 0.9, _clear_direct_normalized(value))

    polite_normalized = _polite_direct_normalized(value)

    if polite_normalized:
        return _result(True, "polite_direct_command", 0.85, polite_normalized)

    if detect_albanian_geg_casual(value):
        return _result(False, "albanian_geg_casual", 0.85, value)

    if detect_talking_about_assistant(value):
        return _result(False, "talking_about_assistant", 0.9, value)

    if source == "voice" and _looks_unclear_casual(value):
        return _result(False, "unclear_casual_speech", 0.55, value)

    return _result(True, "default_allowed", 0.5, value)
