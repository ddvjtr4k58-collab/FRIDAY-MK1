from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from typing import Optional


OLLAMA_URL = (os.getenv("FRIDAY_OLLAMA_URL") or os.getenv("JARVIS_OLLAMA_URL", "http://localhost:11434/api/generate"))
OLLAMA_MODEL = (os.getenv("FRIDAY_OLLAMA_MODEL") or os.getenv("JARVIS_OLLAMA_MODEL", "llama3.2:3b"))

try:
    OLLAMA_TIMEOUT_SECONDS = float((os.getenv("FRIDAY_OLLAMA_TIMEOUT") or os.getenv("JARVIS_OLLAMA_TIMEOUT", "1.0")))
except ValueError:
    OLLAMA_TIMEOUT_SECONDS = 1.0

ALLOWED_ACTIONS = {
    "open_music",
    "music_play",
    "music_pause",
    "music_resume",
    "music_next",
    "music_previous",
    "open_notes",
    "open_calendar",
    "open_weather",
    "open_map",
    "open_files",
    "open_system_health",
    "open_notifications",
    "open_settings",
    "clear_hud",
    "close_widget",
    "save_workshop_layout",
    "close_workshop_mode",
    "none"
}

WRITING_INTENTS = {
    "write me an email",
    "write an email",
    "write email",
    "write me email",
    "draft an email",
    "compose email",
    "compose an email",
    "write me a message",
    "email my teacher",
    "email my professor",
    "send email",
    "send a message",
    "send message",
    "help me email",
    "message my teacher",
    "message my professor",
    "write to my teacher",
    "write to my professor",
    "ask my teacher",
    "homework email",
    "what is my homework email",
    "write a message",
    "compose a message",
    "draft a message",
    "help me write",
    "write a letter",
    "write a response",
    "write an excuse",
    "write a note to",
    "draft a note",
    "make this sound better"
}

SIMPLE_ACTION_HINTS = {
    "play",
    "pause",
    "resume",
    "next",
    "previous",
    "back",
    "open",
    "show",
    "clear",
    "save",
    "close",
    "music",
    "calendar",
    "weather",
    "map",
    "notes",
    "files",
    "diagnostics",
    "system health",
    "notifications",
    "settings",
    "configuration",
    "hud",
    "workshop"
}

BLOCKED_COMPLEX_TERMS = {
    "delete",
    "remove file",
    "real desktop",
    "desktop file",
    "terminal",
    "shell",
    "command line",
    "api key",
    "password",
    "volume",
    "mute",
}


def _normalize(text: str) -> str:
    value = re.sub(r"\s+", " ", str(text or "").lower()).strip()
    value = re.sub(r"[,.!?]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def is_writing_or_message_intent(text: str) -> bool:
    value = _normalize(text)
    if any(intent in value for intent in WRITING_INTENTS):
        return True

    writing_patterns = (
        r"\bwrite\s+(?:me\s+)?(?:an?\s+)?email\b",
        r"\bwrite\s+(?:me\s+)?(?:a\s+)?message\b",
        r"\b(?:draft|compose)\s+(?:me\s+)?(?:an?\s+)?email\b",
        r"\b(?:email|message)\s+my\s+(?:teacher|professor)\b",
        r"\bwrite\s+to\s+my\s+(?:teacher|professor)\b",
        r"\bask\s+my\s+(?:teacher|professor)\b"
    )

    return any(re.search(pattern, value) for pattern in writing_patterns)


def _is_blocked_complex_intent(text: str) -> bool:
    value = _normalize(text)

    if any(term in value for term in BLOCKED_COMPLEX_TERMS):
        return True

    calendar_question = (
        "calendar" in value
        and any(term in value for term in ["what", "when", "today", "tomorrow", "week", "next event", "schedule", "agenda"])
    )
    news_question = any(term in value for term in [
        "what is on the news",
        "latest headlines",
        "current conflicts",
        "markets today",
        "news today"
    ])

    return calendar_question or news_question


def is_ollama_available() -> bool:
    try:
        request = urllib.request.Request(
            OLLAMA_URL,
            data=json.dumps({
                "model": OLLAMA_MODEL,
                "prompt": "Return JSON only: {\"action\":\"none\",\"target_workspace\":null,\"confidence\":1.0}",
                "stream": False
            }).encode("utf-8"),
            headers={"Content-Type": "application/json"}
        )

        with urllib.request.urlopen(request, timeout=OLLAMA_TIMEOUT_SECONDS) as response:
            return 200 <= response.status < 300
    except Exception:
        return False


def _extract_json(value: str) -> Optional[dict]:
    text = str(value or "").strip()

    if not text:
        return None

    try:
        parsed = json.loads(text)
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        pass

    match = re.search(r"\{.*\}", text, flags=re.DOTALL)

    if not match:
        return None

    try:
        parsed = json.loads(match.group(0))
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        return None


def classify_fast_action(text: str) -> Optional[dict]:
    if is_writing_or_message_intent(text) or _is_blocked_complex_intent(text):
        return None

    value = _normalize(text)

    if not value:
        return None

    if not any(hint in value for hint in SIMPLE_ACTION_HINTS):
        return None

    prompt = f"""
Classify this FRIDAY command into one safe local action.
Return strict JSON only.
Allowed actions: {", ".join(sorted(ALLOWED_ACTIONS))}.
Allowed target_workspace values: "main", "secondary", "intel", null.
Never classify writing, email, reasoning, calendar content questions, file deletion, real file access, shell commands, API access, audio level changes, or mute changes as an action.
Command: {value}
JSON:
""".strip()

    try:
        request = urllib.request.Request(
            OLLAMA_URL,
            data=json.dumps({
                "model": OLLAMA_MODEL,
                "prompt": prompt,
                "stream": False,
                "options": {
                    "temperature": 0,
                    "num_predict": 80
                }
            }).encode("utf-8"),
            headers={"Content-Type": "application/json"}
        )

        with urllib.request.urlopen(request, timeout=OLLAMA_TIMEOUT_SECONDS) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError):
        return None

    parsed = _extract_json(payload.get("response", ""))

    if not parsed:
        return None

    action = str(parsed.get("action") or "none").strip()
    workspace = parsed.get("target_workspace")

    if workspace not in {"main", "secondary", "intel", None}:
        workspace = None

    try:
        confidence = float(parsed.get("confidence", 0.0))
    except (TypeError, ValueError):
        confidence = 0.0

    return {
        "action": action,
        "target_workspace": workspace,
        "confidence": confidence
    }


def route_fast_action(text: str) -> Optional[dict]:
    result = classify_fast_action(text)

    if not result:
        return None

    action = result.get("action")

    if action not in ALLOWED_ACTIONS or action == "none":
        return None

    if float(result.get("confidence") or 0.0) < 0.75:
        return None

    return result
