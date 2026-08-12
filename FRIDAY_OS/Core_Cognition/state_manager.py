import os
import time
import uuid
import copy
import json
import re
import datetime
import threading
import subprocess
import ipaddress
from pathlib import Path
from typing import Optional
from flask import Flask, request
from flask_socketio import SocketIO

from Core_Cognition.settings_manager import get_setting, load_settings, normalize_theme
# Memory v2 lives in its own module. The dependency is one-way: state_manager
# knows about memory_manager, and memory_manager reaches back for chat history
# only through a late import inside one function, so there is no import cycle.
from Core_Cognition import memory_manager

# ==========================================
# HUD / SOCKET CONFIGURATION
# ==========================================

PROJECT_ROOT = Path(__file__).resolve().parents[1]

VISUAL_INTERFACE_DIR = os.getenv(
    "FRIDAY_VISUAL_INTERFACE_DIR",
    str(PROJECT_ROOT / "Visual_Interface")
)

VALID_AI_STATUSES = {
    "IDLE",
    "LISTENING",
    "THINKING",
    "TOOLING",
    "SPEAKING",
    "ERROR"
}

VALID_HUD_MODES = {
    "OFFLINE",
    "ACTIVE",
    "SLEEP"
}

VALID_ORB_POSITIONS = {
    "CENTER",
    "DOCKED_BOTTOM_RIGHT"
}

MAX_TRANSCRIPT_ITEMS = 80
DATA_DIR = PROJECT_ROOT / "Data"
WORKSHOP_STATE_FILE = DATA_DIR / "workshop_state.json"
INITIAL_SETTINGS = load_settings()


def _default_workshop_state() -> dict:
    return {
        "active": False,
        "display_count": 0,
        "roles": {},
        "main_widgets": [],
        "secondary_widgets": [],
        "intel_active": True,
        "active_workspace": "main",
        "layout_saved_at": None,
        "displays": [],
        "windows": {},
        "active_panel": None,
        "chat_history": [],
        "chat_sessions": [],
        "active_chat_id": "",
        "project_memory": [],
        "memory_items": [],
        "file_manager_open": False,
        "desk_view_available": False,
        "desk_view_enabled": False,
        "last_opened_at": None
    }

state_lock = threading.RLock()
hud_process = None
pending_override = None
proactive_greeting_sent = False
PROACTIVE_GREETING_ENABLED = (os.getenv("FRIDAY_PROACTIVE_GREETING") or os.getenv("JARVIS_PROACTIVE_GREETING", "0")).strip().lower() in {
    "1",
    "true",
    "yes",
    "on"
}
FACE_ID_ENABLED = (os.getenv("FRIDAY_FACE_ID_ENABLED") or os.getenv("JARVIS_FACE_ID_ENABLED", "0")).strip().lower() in {
    "1",
    "true",
    "yes",
    "on"
}
DEFAULT_AUTHORIZED_USER = (os.getenv("FRIDAY_USER_NAME") or os.getenv("JARVIS_USER_NAME", "Jon"))
INITIAL_STARTUP_MODE = str(INITIAL_SETTINGS.get("startup_mode") or "sleep").strip().lower()

if INITIAL_STARTUP_MODE != "workstation":
    INITIAL_STARTUP_MODE = "sleep"

INITIAL_HUD_MODE = "ACTIVE" if INITIAL_STARTUP_MODE == "workstation" else "SLEEP"
INITIAL_ORB_POSITION = "DOCKED_BOTTOM_RIGHT" if INITIAL_STARTUP_MODE == "workstation" else "CENTER"

app = Flask(__name__)

# IMPORTANT:
# Keep async_mode="threading".
# Do not use eventlet/gevent on this Mac.
# Port 5050 only. Port 5000 conflicts with macOS AirPlay/ControlCenter.
socketio = SocketIO(
    app,
    cors_allowed_origins="*",
    async_mode="threading"
)

hud_state = {
    "state_version": 2,
    "hud_mode": INITIAL_HUD_MODE,
    "theme": normalize_theme(INITIAL_SETTINGS.get("theme")),
    "settings": INITIAL_SETTINGS,
    "orb_position": INITIAL_ORB_POSITION,
    "ai_status": "IDLE",
    "voice_phase": "IDLE",
    "live_transcript": [],
    "override_response": {
        "text": "",
        "timestamp": None,
        "source": None
    },
    "active_cards": [],
    "telemetry": {},
    "sleep_screen": {
        "time": "",
        "date": "",
        "prompt": "FRIDAY MK1 standing by.",
        "name": "FRIDAY"
    },
    "last_error": None,
    "presence_state": "SCANNING" if FACE_ID_ENABLED else "AUTHORIZED",
    "authorized_user": "" if FACE_ID_ENABLED else DEFAULT_AUTHORIZED_USER,
    "intruder_alert": False,
    "presence_confidence": 0.0 if FACE_ID_ENABLED else 1.0,
    "pre_presence_lock_mode": None,
    "pre_presence_lock_orb_position": None,
    "notification_history": [],
    "workshop_mode": _default_workshop_state(),
    "showcase_mode": {
        "active": False,
        "step": None,
        "started_at": None,
        "locked_input": False
    },
    "shutdown_requested": False,
    "last_updated": None
}


# ==========================================
# BASIC STATE HELPERS
# ==========================================

def _now_iso() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")


def _normalize_showcase_text(text: str) -> str:
    value = re.sub(r"[,.!?]+", " ", str(text or "").lower())
    return re.sub(r"\s+", " ", value).strip()


def _is_showcase_exit_text(text: str) -> bool:
    normalized = _normalize_showcase_text(text)
    return normalized in {"friday exit showcase mode", "jarvis exit showcase mode"}


SHUTDOWN_COMMAND_TEXTS = {
    "exit",
    "quit",
    "shutdown",
    "shut down",
    "power down",
    "terminate"
}


def _normalize_shutdown_text(text: str) -> str:
    value = re.sub(r"[^\w\s]", " ", str(text or "").lower().replace("_", " "))
    tokens = [
        token
        for token in re.split(r"\s+", value)
        if token and token not in {"friday", "jarvis", "please", "pls"}
    ]
    return re.sub(r"\s+", " ", " ".join(tokens)).strip()


def _is_shutdown_text(text: str) -> bool:
    return _normalize_shutdown_text(text) in SHUTDOWN_COMMAND_TEXTS


def _touch_state() -> None:
    hud_state["last_updated"] = _now_iso()


def get_hud_state_snapshot() -> dict:
    with state_lock:
        return copy.deepcopy(hud_state)


def sync_settings_state(settings: Optional[dict] = None, broadcast: bool = True) -> None:
    current_settings = settings if isinstance(settings, dict) else load_settings()

    with state_lock:
        hud_state["settings"] = copy.deepcopy(current_settings)
        hud_state["theme"] = normalize_theme(current_settings.get("theme"))
        _touch_state()

    if broadcast:
        broadcast_state()


def set_hud_theme(theme: str, broadcast: bool = True) -> None:
    normalized_theme = normalize_theme(theme)

    with state_lock:
        hud_state["theme"] = normalized_theme

        if isinstance(hud_state.get("settings"), dict):
            hud_state["settings"]["theme"] = normalized_theme

        _touch_state()

    if broadcast:
        broadcast_state()


# A single user action often touches several pieces of state, and each helper
# broadcast the whole ~22 KB snapshot, so one widget open pushed four identical
# payloads and forced four full re-renders in the HUD. state_update is an
# idempotent full snapshot, so collapsing a burst to its final value is
# equivalent and much cheaper.
BROADCAST_COALESCE_SECONDS = 0.04
_broadcast_timer = None
_broadcast_lock = threading.Lock()


def _memory_payload() -> dict:
    """The memory panel's data. Cached inside each module, so this is cheap.

    Two sources, merged here rather than inside either of them: memory_manager
    owns accepted facts, memory_learning owns pending observations, and neither
    should have to know about the other just to render a panel.
    """
    try:
        payload = memory_manager.memory_view()
    except Exception:
        payload = {"items": [], "projects": [], "active_project_id": "", "stats": {}}

    try:
        from Core_Cognition import memory_learning

        payload.update(memory_learning.candidate_view())
    except Exception:
        payload.setdefault("candidates", [])
        payload.setdefault("pending_count", 0)

    return payload


def _snapshot_with_memory() -> dict:
    """A broadcast snapshot with the memory view attached to workshop_mode.

    Memory is NOT stored inside hud_state, and therefore never lands in
    workshop_state.json: memory_manager owns the store, and duplicating it into
    the interface state file is how two copies start disagreeing. It is attached
    here, at the moment of broadcast, and nowhere else.
    """
    snapshot = get_hud_state_snapshot()

    if isinstance(snapshot.get("workshop_mode"), dict):
        snapshot["workshop_mode"]["memory"] = _memory_payload()

    return snapshot


def _flush_state_broadcast() -> None:
    global _broadcast_timer

    with _broadcast_lock:
        _broadcast_timer = None

    try:
        socketio.emit("state_update", _snapshot_with_memory())
    except Exception:
        pass


def broadcast_state(immediate: bool = False) -> None:
    global _broadcast_timer

    if immediate:
        with _broadcast_lock:
            timer = _broadcast_timer
            _broadcast_timer = None

        if timer is not None:
            try:
                timer.cancel()
            except Exception:
                pass

        _flush_state_broadcast()
        return

    with _broadcast_lock:
        if _broadcast_timer is not None:
            # A flush is already pending and will carry the latest state.
            return

        timer = threading.Timer(BROADCAST_COALESCE_SECONDS, _flush_state_broadcast)
        timer.daemon = True
        _broadcast_timer = timer

    timer.start()


def broadcast_to_hud(channel, data):
    if data is None:
        data = {}

    socketio.emit(channel, data)


def _broadcast_workshop_toggle(workshop_state: dict) -> None:
    """Workshop windows render their sidebar straight from this payload, so it
    carries the memory view for exactly the same reason state_update does."""
    payload = copy.deepcopy(workshop_state if isinstance(workshop_state, dict) else {})
    payload["memory"] = _memory_payload()
    broadcast_to_hud("workshop_mode_toggle", payload)


def broadcast_memory_state() -> None:
    """Push the memory panel, and the voice layer's identity block, after a change.

    Deliberately goes through the normal state broadcast: the UI must only ever
    show what the backend has actually stored, so there is no separate optimistic
    path that could show a pin or a deletion that did not happen.

    The live voice session is not reconnected — it picks the new block up at its
    next connect or rotation. Dropping a working session to deliver one memory
    sooner would cost the running conversation its context.
    """
    broadcast_state()

    try:
        broadcast_to_hud("voice_memory_context", {"block": memory_manager.identity_prompt_block()})
    except Exception:
        pass


def send_voice_system_event(text: str, mode: str = "announce") -> None:
    """Tell the live voice layer that something happened, in FRIDAY's own voice.

    This is deliberately NOT the same path as microphone audio. Text pushed onto a
    Live session arrives as user-turn content, so a bare "Calendar opened" would read
    as Boss having said it. Everything sent here is tagged as a system notice on the
    renderer side, and FRIDAY chooses her own words for it.

    mode "acknowledge" - an action she just performed; confirm briefly or stay silent.
    mode "announce"    - information she should relay (alerts, greetings, results).
    """
    message = str(text or "").strip()

    if not message:
        return

    broadcast_to_hud(
        "voice_system_event",
        {"text": message, "mode": "acknowledge" if mode == "acknowledge" else "announce"}
    )


def broadcast_virtual_desktop_payload_safely() -> None:
    try:
        from Sensory_Array.file_tools import broadcast_virtual_desktop_payload
        broadcast_virtual_desktop_payload()
    except Exception:
        pass


def broadcast_notification(notification: dict) -> None:
    if not bool(get_setting("show_notifications", True)):
        return

    socketio.emit("proactive_notification", notification or {})


def broadcast_notification_history(notifications: list) -> None:
    safe_notifications = notifications if isinstance(notifications, list) else []
    safe_notifications = safe_notifications[:50]

    with state_lock:
        hud_state["notification_history"] = safe_notifications
        _touch_state()

    socketio.emit(
        "notification_history_updated",
        {
            "notifications": safe_notifications,
            "count": len(safe_notifications)
        }
    )


def broadcast_notification_center(notifications: list) -> None:
    safe_notifications = notifications if isinstance(notifications, list) else []
    socketio.emit(
        "notification_center_toggle",
        {
            "notifications": safe_notifications,
            "count": len(safe_notifications)
        }
    )


# ==========================================
# PRESENCE / VISION STATE
# ==========================================

def set_presence_state(
    state: str,
    authorized_user: str = "",
    confidence: float = 0.0,
    broadcast: bool = True
) -> None:
    global proactive_greeting_sent
    normalized = (state or "SCANNING").upper()

    if normalized not in {"SCANNING", "AUTHORIZED", "VACANT", "UNAUTHORIZED"}:
        normalized = "SCANNING"

    if not FACE_ID_ENABLED:
        normalized = "AUTHORIZED"
        authorized_user = authorized_user or DEFAULT_AUTHORIZED_USER
        confidence = max(float(confidence or 0.0), 1.0)

    with state_lock:
        previous_presence = hud_state.get("presence_state", "SCANNING")

        hud_state["presence_state"] = normalized
        hud_state["authorized_user"] = authorized_user if normalized == "AUTHORIZED" else ""
        hud_state["presence_confidence"] = confidence
        hud_state["intruder_alert"] = normalized == "UNAUTHORIZED"

        if normalized in {"VACANT", "UNAUTHORIZED"}:
            if previous_presence not in {"VACANT", "UNAUTHORIZED"}:
                hud_state["pre_presence_lock_mode"] = hud_state.get("hud_mode", "ACTIVE")
                hud_state["pre_presence_lock_orb_position"] = hud_state.get("orb_position", "DOCKED_BOTTOM_RIGHT")

            hud_state["hud_mode"] = "SLEEP"
            hud_state["orb_position"] = "CENTER"
            hud_state["override_response"] = {
                "text": "",
                "timestamp": None,
                "source": None
            }
            hud_state["sleep_screen"]["prompt"] = (
                "Workstation locked. Presence required."
                if normalized == "VACANT"
                else "Unknown presence detected. Workstation locked."
            )
            hud_state["sleep_screen"]["time"] = datetime.datetime.now().strftime("%I:%M %p")
            hud_state["sleep_screen"]["date"] = datetime.datetime.now().strftime("%A, %B %d, %Y")

        if normalized == "AUTHORIZED" and previous_presence in {"VACANT", "UNAUTHORIZED"}:
            restore_mode = hud_state.get("pre_presence_lock_mode") or "ACTIVE"
            restore_orb_position = hud_state.get("pre_presence_lock_orb_position") or "DOCKED_BOTTOM_RIGHT"

            hud_state["hud_mode"] = restore_mode
            hud_state["orb_position"] = restore_orb_position
            hud_state["pre_presence_lock_mode"] = None
            hud_state["pre_presence_lock_orb_position"] = None
            hud_state["sleep_screen"]["prompt"] = "Access restored. Resuming previous workspace."
            hud_state["sleep_screen"]["time"] = datetime.datetime.now().strftime("%I:%M %p")
            hud_state["sleep_screen"]["date"] = datetime.datetime.now().strftime("%A, %B %d, %Y")

        should_send_proactive_greeting = (
            PROACTIVE_GREETING_ENABLED
            and normalized == "AUTHORIZED"
            and previous_presence != "AUTHORIZED"
            and not proactive_greeting_sent
        )

        if should_send_proactive_greeting:
            proactive_greeting_sent = True

        _touch_state()

    if should_send_proactive_greeting:
        def _proactive_greeting_worker():
            try:
                time.sleep(0.8)
                from Sensory_Array.audio_engine import speak
                speak("Welcome back, Boss. Systems are nominal. Financial markets are being monitored.")
            except Exception as error:
                print(f"[FRIDAY proactive greeting failed: {error}]")

        threading.Thread(
            target=_proactive_greeting_worker,
            daemon=True
        ).start()

    if broadcast:
        payload = {
            "presence_state": normalized,
            "authorized_user": authorized_user if normalized == "AUTHORIZED" else "",
            "presence_confidence": confidence,
            "intruder_alert": normalized == "UNAUTHORIZED"
        }
        socketio.emit("presence_update", payload)
        broadcast_state()


def get_presence_state() -> str:
    with state_lock:
        return hud_state.get("presence_state", "AUTHORIZED" if not FACE_ID_ENABLED else "SCANNING")


def is_presence_authorized() -> bool:
    return get_presence_state() == "AUTHORIZED"


# ==========================================
# AI / HUD STATUS
# ==========================================

def set_ai_status(status: str, broadcast: bool = True) -> None:
    normalized = (status or "IDLE").upper()

    if normalized not in VALID_AI_STATUSES:
        normalized = "ERROR"

    with state_lock:
        hud_state["ai_status"] = normalized
        hud_state["voice_phase"] = AI_STATUS_TO_VOICE_PHASE.get(
            normalized,
            hud_state.get("voice_phase", "IDLE")
        )
        _touch_state()

    if broadcast:
        broadcast_state()


# ==========================================
# VOICE PHASE (authoritative listening/speaking state)
# ==========================================
#
# One state machine owns the whole conversational turn. Every module reads and
# writes it through these helpers instead of keeping its own speaking/listening
# booleans, which is what previously allowed the orb and the microphone to
# disagree about what FRIDAY was doing.
#
# Phases map onto the existing ai_status values so the HUD, the settings widget,
# and every legacy caller keep working unchanged.

VOICE_PHASES = (
    "IDLE",
    "LISTENING",
    "USER_SPEAKING",
    "THINKING",
    "FRIDAY_SPEAKING"
)

VOICE_PHASE_TO_AI_STATUS = {
    "IDLE": "IDLE",
    "LISTENING": "LISTENING",
    "USER_SPEAKING": "LISTENING",
    "THINKING": "THINKING",
    "FRIDAY_SPEAKING": "SPEAKING"
}

AI_STATUS_TO_VOICE_PHASE = {
    "IDLE": "IDLE",
    "LISTENING": "LISTENING",
    "THINKING": "THINKING",
    "TOOLING": "THINKING",
    "SPEAKING": "FRIDAY_SPEAKING",
    "ERROR": "IDLE"
}

AUDIO_LEVEL_MIN_INTERVAL_SECONDS = 1.0 / 28.0

voice_phase_lock = threading.RLock()
_speech_playback_active = False
_speech_echo_guard_until = 0.0
_last_audio_level_emit_at = 0.0


def normalize_voice_phase(phase: str) -> str:
    normalized = str(phase or "IDLE").upper()
    return normalized if normalized in VOICE_PHASES else "IDLE"


def get_voice_phase() -> str:
    with state_lock:
        return normalize_voice_phase(hud_state.get("voice_phase", "IDLE"))


def set_voice_phase(phase: str, broadcast: bool = True, detail: Optional[dict] = None) -> str:
    """
    Authoritative phase transition.

    Emits the compact `voice_state` event rather than a full state snapshot so
    the orb can react immediately without paying for a whole HUD broadcast on
    every microphone transition.
    """
    normalized = normalize_voice_phase(phase)
    ai_status = VOICE_PHASE_TO_AI_STATUS.get(normalized, "IDLE")

    with state_lock:
        changed = hud_state.get("voice_phase") != normalized or hud_state.get("ai_status") != ai_status
        hud_state["voice_phase"] = normalized
        hud_state["ai_status"] = ai_status

        if changed:
            _touch_state()

    if not broadcast:
        return normalized

    payload = {"phase": normalized, "ai_status": ai_status}

    if isinstance(detail, dict):
        payload.update(detail)

    try:
        socketio.emit("voice_state", payload)
    except Exception:
        pass

    return normalized


def is_friday_speaking() -> bool:
    """
    True while FRIDAY audio is playing and through the short echo-settle window
    that follows it. This is the single guard the microphone consults so FRIDAY
    can never transcribe its own voice.
    """
    with voice_phase_lock:
        if _speech_playback_active:
            return True

        return time.time() < _speech_echo_guard_until


def speech_echo_guard_remaining() -> float:
    with voice_phase_lock:
        if _speech_playback_active:
            return -1.0

        return max(0.0, _speech_echo_guard_until - time.time())


def begin_speech_playback(text: str = "", detail: Optional[dict] = None) -> None:
    """Called at the moment real audio starts, by every TTS provider."""
    global _speech_playback_active

    with voice_phase_lock:
        _speech_playback_active = True

    set_voice_phase("FRIDAY_SPEAKING", detail=detail)

    try:
        socketio.emit("friday_speech_start", {"text": str(text or "")})
    except Exception:
        pass


# Callbacks that need to run once FRIDAY has genuinely stopped talking. Shutdown
# uses this so the process does not exit while she is still mid-sentence; both the
# live layer and the legacy engine converge on end_speech_playback, so registering
# here covers either pipeline.
_speech_end_listeners = []


def add_speech_end_listener(callback) -> None:
    if callable(callback) and callback not in _speech_end_listeners:
        _speech_end_listeners.append(callback)


def _notify_speech_end() -> None:
    for callback in list(_speech_end_listeners):
        try:
            callback()
        except Exception as error:
            print(f"[FRIDAY speech-end listener failed: {error}]")


def end_speech_playback(
    text: str = "",
    echo_settle_seconds: float = 0.22,
    next_phase: str = "IDLE"
) -> None:
    """Called when playback genuinely finished (or was stopped)."""
    global _speech_playback_active
    global _speech_echo_guard_until

    try:
        settle = max(0.0, min(float(echo_settle_seconds), 1.5))
    except (TypeError, ValueError):
        settle = 0.22

    with voice_phase_lock:
        _speech_playback_active = False
        _speech_echo_guard_until = time.time() + settle

    try:
        socketio.emit("friday_speech_end", {"text": str(text or "")})
    except Exception:
        pass

    set_voice_phase(next_phase)
    _notify_speech_end()


def emit_audio_level(level: float, state: str = "user_speaking", force: bool = False) -> bool:
    """
    Throttled amplitude broadcast for the audio-reactive orb.

    Capped at roughly 28 updates per second across all sources so a long
    utterance cannot flood Socket.IO.
    """
    global _last_audio_level_emit_at

    try:
        value = float(level)
    except (TypeError, ValueError):
        return False

    value = max(0.0, min(value, 1.0))
    now = time.time()

    with voice_phase_lock:
        if not force and now - _last_audio_level_emit_at < AUDIO_LEVEL_MIN_INTERVAL_SECONDS:
            return False

        _last_audio_level_emit_at = now

    channel = "friday_audio_level" if state == "friday_speaking" else "user_audio_level"

    try:
        socketio.emit(channel, {"level": round(value, 3), "state": state})
    except Exception:
        return False

    return True


def set_showcase_mode(active: bool, step: Optional[str] = None, broadcast: bool = True) -> None:
    with state_lock:
        hud_state["showcase_mode"] = {
            "active": bool(active),
            "step": step if active else None,
            "started_at": _now_iso() if active else None,
            "locked_input": bool(active)
        }
        _touch_state()

    if broadcast:
        broadcast_state()


def update_showcase_step(step: str, broadcast: bool = True) -> None:
    with state_lock:
        current = hud_state.get("showcase_mode") if isinstance(hud_state.get("showcase_mode"), dict) else {}

        if not current.get("active"):
            return

        current = {
            "active": True,
            "step": str(step or ""),
            "started_at": current.get("started_at") or _now_iso(),
            "locked_input": True
        }
        hud_state["showcase_mode"] = current
        _touch_state()

    if broadcast:
        broadcast_state()


def is_showcase_mode_active() -> bool:
    with state_lock:
        current = hud_state.get("showcase_mode")
        return bool(isinstance(current, dict) and current.get("active"))


def get_time_of_day_greeting() -> str:
    hour = datetime.datetime.now().hour

    if 5 <= hour < 12:
        return "Good morning, Boss."

    if 12 <= hour < 17:
        return "Good afternoon, Boss."

    if 17 <= hour < 22:
        return "Good evening, Boss."

    return "Working late again, Boss?"


def get_sleep_prompt() -> str:
    options = [
        "What will we be working on today, Boss?",
        "What should I open, Boss?",
        "Anything you wish me to open, Boss?",
        "Where shall we begin, Boss?"
    ]

    return options[int(time.time()) % len(options)]


def update_sleep_clock(broadcast: bool = False) -> None:
    with state_lock:
        hud_state["sleep_screen"]["time"] = datetime.datetime.now().strftime("%I:%M %p")
        hud_state["sleep_screen"]["date"] = datetime.datetime.now().strftime("%A, %B %d, %Y")
        _touch_state()

    if broadcast:
        broadcast_state()


def set_hud_mode(
    mode: str,
    orb_position: str = None,
    prompt: str = None,
    broadcast: bool = True
) -> None:
    normalized_mode = (mode or "ACTIVE").upper()

    if normalized_mode not in VALID_HUD_MODES:
        normalized_mode = "ACTIVE"

    next_orb_position = orb_position

    if next_orb_position is None:
        if normalized_mode in {"SLEEP", "OFFLINE"}:
            next_orb_position = "CENTER"
        else:
            next_orb_position = hud_state.get("orb_position", "CENTER")

    next_orb_position = (next_orb_position or "CENTER").upper()

    if next_orb_position not in VALID_ORB_POSITIONS:
        next_orb_position = "CENTER"

    with state_lock:
        hud_state["hud_mode"] = normalized_mode
        hud_state["orb_position"] = next_orb_position

        if normalized_mode == "SLEEP":
            hud_state["active_cards"] = []
            hud_state["override_response"] = {
                "text": "",
                "timestamp": None,
                "source": None
            }
            hud_state["sleep_screen"]["prompt"] = prompt or get_sleep_prompt()
            hud_state["sleep_screen"]["time"] = datetime.datetime.now().strftime("%I:%M %p")
            hud_state["sleep_screen"]["date"] = datetime.datetime.now().strftime("%A, %B %d, %Y")

        if normalized_mode == "OFFLINE":
            hud_state["active_cards"] = []
            hud_state["sleep_screen"]["prompt"] = "Passive listening enabled. Wake phrase required."
            hud_state["sleep_screen"]["time"] = datetime.datetime.now().strftime("%I:%M %p")
            hud_state["sleep_screen"]["date"] = datetime.datetime.now().strftime("%A, %B %d, %Y")

        _touch_state()

    if broadcast:
        broadcast_state()


def dock_orb_for_workspace(broadcast: bool = True) -> None:
    with state_lock:
        hud_state["orb_position"] = "DOCKED_BOTTOM_RIGHT"
        _touch_state()

    if broadcast:
        broadcast_state()


# ==========================================
# TRANSCRIPT / MANUAL OVERRIDE
# ==========================================

def append_transcript(
    speaker: str,
    text: str,
    source: str = "system",
    broadcast: bool = True
) -> None:
    if not text:
        return

    item = {
        "id": f"msg_{uuid.uuid4().hex[:10]}",
        "speaker": speaker,
        "text": text,
        "source": source,
        "timestamp": _now_iso()
    }

    with state_lock:
        hud_state["live_transcript"].append(item)
        hud_state["live_transcript"] = hud_state["live_transcript"][-MAX_TRANSCRIPT_ITEMS:]
        _touch_state()

    if broadcast:
        broadcast_state()


def set_override_response(
    text: str,
    source: str = "override",
    broadcast: bool = True
) -> None:
    with state_lock:
        hud_state["override_response"] = {
            "text": text or "",
            "source": source,
            "timestamp": _now_iso()
        }
        _touch_state()

    if broadcast:
        broadcast_state()


def pop_pending_override():
    global pending_override

    if not pending_override:
        return None

    payload = pending_override
    pending_override = None
    return payload


def has_pending_override() -> bool:
    """Read-only peek so the microphone can abandon a listen the instant a
    typed or button command arrives, instead of waiting out the listen timeout."""
    return bool(pending_override)


# ==========================================
# TELEMETRY / ERROR STATE
# ==========================================

def set_telemetry(payload: dict, broadcast: bool = True) -> None:
    with state_lock:
        hud_state["telemetry"] = payload or {}
        _touch_state()

    if broadcast:
        broadcast_state()


_pending_workshop_chat_id = ""


def set_pending_workshop_chat(chat_id: str) -> None:
    global _pending_workshop_chat_id
    _pending_workshop_chat_id = str(chat_id or "")


def take_pending_workshop_chat() -> str:
    """Consume the id of the chat awaiting a reply (one-shot)."""
    global _pending_workshop_chat_id
    chat_id = _pending_workshop_chat_id
    _pending_workshop_chat_id = ""
    return chat_id


def _new_workshop_chat_session(title: str = "New chat") -> dict:
    now = _now_iso()
    return {
        "id": f"workshop_chat_{uuid.uuid4().hex[:10]}",
        "title": str(title or "New chat"),
        "created_at": now,
        "updated_at": now,
        "messages": []
    }


def _safe_workshop_chat_sessions(value) -> list:
    sessions = []

    for entry in value if isinstance(value, list) else []:
        if not isinstance(entry, dict) or not entry.get("id"):
            continue

        messages = [item for item in entry.get("messages", []) if isinstance(item, dict)]
        sessions.append({
            "id": str(entry["id"]),
            "title": str(entry.get("title") or "New chat"),
            "created_at": entry.get("created_at") or _now_iso(),
            "updated_at": entry.get("updated_at") or entry.get("created_at") or _now_iso(),
            # Per-conversation cap; the old flat history capped at 80 overall.
            "messages": messages[-200:]
        })

    return sessions[-40:]


def _sync_workshop_chat_view(state: dict) -> dict:
    """Keep `chat_history` a mirror of the active session.

    Everything downstream (renderer, chat deltas, main.py) already reads
    chat_history, so sessions are layered underneath it rather than replacing it.
    """
    sessions = state.get("chat_sessions") or []

    if not sessions:
        # Adopt any pre-sessions history so upgrading never loses a conversation.
        legacy = [item for item in state.get("chat_history", []) if isinstance(item, dict)]
        session = _new_workshop_chat_session()

        if legacy:
            session["messages"] = legacy[-200:]
            session["title"] = _derive_workshop_chat_title(legacy) or session["title"]

        sessions = [session]
        state["chat_sessions"] = sessions

    active_id = str(state.get("active_chat_id") or "")
    known = {session["id"] for session in sessions}

    if active_id not in known:
        active_id = sessions[-1]["id"]

    state["active_chat_id"] = active_id
    active = next(session for session in sessions if session["id"] == active_id)
    state["chat_history"] = list(active["messages"])
    return state


def _derive_workshop_chat_title(messages: list) -> str:
    """Title a conversation from its first user message.

    Deterministic on purpose — no model call, so it cannot invent a topic the
    conversation does not have.
    """
    for item in messages if isinstance(messages, list) else []:
        if not isinstance(item, dict):
            continue

        role = str(item.get("role") or "").strip().lower()

        if role in {"friday", "jarvis", "assistant", "system"}:
            continue

        text = " ".join(str(item.get("text") or "").split())

        if not text:
            continue

        words = text.split(" ")[:6]
        title = " ".join(words).strip(" .,:;!?-—")

        if len(words) == 6 and len(text.split(" ")) > 6:
            title += "…"

        return title[:60] or "New chat"

    return ""


def _trim_workshop_memory(items: list, limit: int = 120) -> list:
    """Cap project memory at `limit` entries while never evicting pinned notes."""
    if len(items) <= limit:
        return items

    pinned = [item for item in items if item.get("pinned")]
    unpinned = [item for item in items if not item.get("pinned")]
    room = max(limit - len(pinned), 0)
    keep = set(id(item) for item in pinned)
    keep.update(id(item) for item in unpinned[-room:] if room)
    return [item for item in items if id(item) in keep]


def _safe_workshop_state(value=None) -> dict:
    state = _default_workshop_state()

    if isinstance(value, dict):
        for key in state:
            if key in value:
                state[key] = value[key]

    state["chat_history"] = [
        item for item in state.get("chat_history", [])
        if isinstance(item, dict)
    ][-80:]
    state["memory_items"] = [
        item for item in state.get("memory_items", [])
        if isinstance(item, dict)
    ][-120:]
    state["project_memory"] = _trim_workshop_memory([
        item for item in state.get("project_memory", state.get("memory_items", []))
        if isinstance(item, dict)
    ])
    state["memory_items"] = state["project_memory"]
    state["chat_sessions"] = _safe_workshop_chat_sessions(state.get("chat_sessions"))
    state = _sync_workshop_chat_view(state)
    state["main_widgets"] = [
        item for item in state.get("main_widgets", [])
        if isinstance(item, dict)
    ][-40:]
    state["secondary_widgets"] = [
        item for item in state.get("secondary_widgets", [])
        if isinstance(item, dict)
    ][-40:]
    state["displays"] = state.get("displays", []) if isinstance(state.get("displays"), list) else []
    state["windows"] = state.get("windows", {}) if isinstance(state.get("windows"), dict) else {}
    state["roles"] = state.get("roles", {}) if isinstance(state.get("roles"), dict) else {}
    state["display_count"] = int(state.get("display_count") or len(state["displays"]) or 0)
    state["intel_active"] = bool(state.get("intel_active", True))
    state["active_workspace"] = state.get("active_workspace") if state.get("active_workspace") in {"main", "secondary", "intel"} else "main"
    state["desk_view_available"] = False
    state["desk_view_enabled"] = False
    return state


def _write_workshop_state_file(snapshot: dict) -> None:
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)

        with open(WORKSHOP_STATE_FILE, "w") as workshop_file:
            json.dump(_safe_workshop_state(snapshot), workshop_file, indent=2)
    except Exception:
        pass


def set_workshop_mode(active: bool, broadcast: bool = True) -> None:
    with state_lock:
        current = _safe_workshop_state(hud_state.get("workshop_mode"))
        current["active"] = bool(active)
        current["desk_view_available"] = False
        current["desk_view_enabled"] = False

        if active:
            current["last_opened_at"] = _now_iso()

        hud_state["workshop_mode"] = current
        snapshot = copy.deepcopy(current)
        _touch_state()

    _write_workshop_state_file(snapshot)

    if broadcast:
        broadcast_state()
        _broadcast_workshop_toggle(current)
        if active:
            broadcast_virtual_desktop_payload_safely()


def update_workshop_display_count(count: int, broadcast: bool = True) -> None:
    safe_count = max(0, int(count or 0))

    with state_lock:
        current = _safe_workshop_state(hud_state.get("workshop_mode"))
        current["display_count"] = safe_count
        hud_state["workshop_mode"] = current
        snapshot = copy.deepcopy(current)
        _touch_state()

    _write_workshop_state_file(snapshot)

    if broadcast:
        broadcast_state()


def update_workshop_roles(roles: dict, broadcast: bool = True) -> None:
    safe_roles = roles if isinstance(roles, dict) else {}

    with state_lock:
        current = _safe_workshop_state(hud_state.get("workshop_mode"))
        current["roles"] = safe_roles
        current["intel_active"] = "workshop-intel" in safe_roles or "workshop-single" in safe_roles
        hud_state["workshop_mode"] = current
        snapshot = copy.deepcopy(current)
        _touch_state()

    _write_workshop_state_file(snapshot)

    if broadcast:
        broadcast_state()


def update_workshop_displays(displays: list, broadcast: bool = True) -> None:
    safe_displays = displays if isinstance(displays, list) else []

    with state_lock:
        current = _safe_workshop_state(hud_state.get("workshop_mode"))
        current["displays"] = safe_displays
        current["display_count"] = len(safe_displays)
        hud_state["workshop_mode"] = current
        snapshot = copy.deepcopy(current)
        _touch_state()

    _write_workshop_state_file(snapshot)

    if broadcast:
        broadcast_state()


def _workshop_has_secondary(current: dict) -> bool:
    roles = current.get("roles") if isinstance(current.get("roles"), dict) else {}
    return "workshop-secondary" in roles or current.get("display_count", 0) >= 3


def route_workshop_widget(widget_type: str, size_hint: str = "normal") -> str:
    return "main"


def add_workshop_widget(widget_type: str, payload=None, preferred_workspace=None):
    widget = str(widget_type or "widget").lower()
    payload = payload if isinstance(payload, dict) else {}
    size_hint = payload.get("size_hint") or (
        "large"
        if widget in {"calendar", "calendar_agenda", "map", "news", "intel", "virtual_finder"}
        else "small"
        if widget in {"music", "notification_center", "notifications", "tasks"}
        else "normal"
    )
    workspace = preferred_workspace if preferred_workspace in {"main", "secondary", "intel"} else route_workshop_widget(widget, size_hint)
    item = {
        "id": payload.get("id") or f"workshop_widget_{uuid.uuid4().hex[:10]}",
        "type": widget,
        "title": payload.get("title") or widget.replace("_", " ").upper(),
        "workspace": workspace,
        "size_hint": size_hint,
        "timestamp": _now_iso()
    }

    with state_lock:
        current = _safe_workshop_state(hud_state.get("workshop_mode"))

        if not current.get("active"):
            return item

        target_key = "secondary_widgets" if workspace == "secondary" else "main_widgets"

        for key in ("main_widgets", "secondary_widgets"):
            current[key] = [
                existing for existing in current.get(key, [])
                if isinstance(existing, dict) and existing.get("id") != item["id"]
            ]

        current[target_key].append(item)
        current["active_workspace"] = workspace
        hud_state["workshop_mode"] = current
        snapshot = copy.deepcopy(current)
        _touch_state()

    _write_workshop_state_file(snapshot)
    return item


def remove_workshop_widget(widget_id: str):
    target = str(widget_id or "").strip()

    if not target:
        return False

    removed = False

    with state_lock:
        current = _safe_workshop_state(hud_state.get("workshop_mode"))

        for key in ("main_widgets", "secondary_widgets"):
            before = len(current.get(key, []))
            current[key] = [
                item for item in current.get(key, [])
                if isinstance(item, dict) and item.get("id") != target
            ]
            removed = removed or len(current[key]) != before

        hud_state["workshop_mode"] = current
        snapshot = copy.deepcopy(current)
        _touch_state()

    if removed:
        _write_workshop_state_file(snapshot)

    return removed


def set_hud_card_workspace(card_id: str, workspace: str, broadcast: bool = True) -> bool:
    target_id = str(card_id or "").strip()
    target_workspace = "secondary" if workspace == "secondary" else "main"
    updated_card = None

    if not target_id:
        return False

    with state_lock:
        for card in hud_state["active_cards"]:
            if card.get("id") == target_id:
                card["workspace"] = target_workspace
                updated_card = copy.deepcopy(card)
                break

        if not updated_card:
            return False

        current = _safe_workshop_state(hud_state.get("workshop_mode"))

        for key in ("main_widgets", "secondary_widgets"):
            current[key] = [
                item for item in current.get(key, [])
                if isinstance(item, dict) and item.get("id") != target_id
            ]

        target_key = "secondary_widgets" if target_workspace == "secondary" else "main_widgets"
        current[target_key].append({
            "id": updated_card.get("id"),
            "type": updated_card.get("type"),
            "title": updated_card.get("title"),
            "workspace": target_workspace,
            "size_hint": updated_card.get("size_hint", "normal"),
            "timestamp": _now_iso()
        })
        current["active_workspace"] = target_workspace
        hud_state["workshop_mode"] = current
        snapshot = copy.deepcopy(current)
        _touch_state()

    _write_workshop_state_file(snapshot)

    if broadcast:
        broadcast_state()

    return True


def update_workshop_window_state(window_id: str, data: dict, broadcast: bool = True) -> None:
    safe_id = str(window_id or "").strip()

    if not safe_id:
        return

    with state_lock:
        current = _safe_workshop_state(hud_state.get("workshop_mode"))
        current["windows"][safe_id] = data if isinstance(data, dict) else {}
        hud_state["workshop_mode"] = current
        snapshot = copy.deepcopy(current)
        _touch_state()

    _write_workshop_state_file(snapshot)

    if broadcast:
        broadcast_state()


def append_workshop_chat(
    role: str,
    text: str,
    metadata=None,
    broadcast: bool = True,
    chat_id: str = ""
):
    clean_text = str(text or "").strip()

    if not clean_text:
        return None

    item = {
        "id": f"workshop_msg_{uuid.uuid4().hex[:10]}",
        "role": role or "user",
        "text": clean_text,
        "metadata": metadata if isinstance(metadata, dict) else {},
        "timestamp": _now_iso()
    }

    with state_lock:
        current = _safe_workshop_state(hud_state.get("workshop_mode"))
        # Route to the session the turn belongs to. A reply must land in the chat
        # the question was asked in, even if the user switched tabs while waiting.
        target_id = str(chat_id or "") or str(current.get("active_chat_id") or "")
        session = next(
            (entry for entry in current["chat_sessions"] if entry["id"] == target_id),
            None
        )

        if session is None:
            session = current["chat_sessions"][-1]

        # Stamp the owning conversation onto the message so the renderer can tell
        # whether an incoming delta belongs to the chat currently on screen.
        item["chat_id"] = session["id"]
        session["messages"].append(item)
        session["messages"] = session["messages"][-200:]
        session["updated_at"] = item["timestamp"]

        # First real exchange names the conversation.
        if session["title"] in ("", "New chat"):
            derived = _derive_workshop_chat_title(session["messages"])

            if derived:
                session["title"] = derived

        current = _sync_workshop_chat_view(current)
        hud_state["workshop_mode"] = current
        snapshot = copy.deepcopy(current)
        _touch_state()

    _write_workshop_state_file(snapshot)

    if broadcast:
        broadcast_state()

    return item


def get_chat_session_turns(chat_id: str, limit: int = 6) -> list:
    """The last few turns of ONE Silent Operator conversation.

    This is FRIDAY's conversation memory, and it is per-chat by construction:
    the caller names the conversation, and only that conversation's messages are
    returned. A different chat cannot see these turns, which is what stops the
    robot-arm conversation leaking into the calculus one.

    memory_manager calls this when building context; nothing else should need it.
    """
    target = str(chat_id or "").strip()

    if not target:
        return []

    with state_lock:
        current = _safe_workshop_state(hud_state.get("workshop_mode"))
        session = next(
            (entry for entry in current["chat_sessions"] if entry["id"] == target),
            None
        )

        if session is None:
            return []

        messages = session.get("messages", [])[-max(1, int(limit)):]

    return [
        {"role": str(item.get("role") or "user"), "text": str(item.get("text") or "")}
        for item in messages
        if isinstance(item, dict) and str(item.get("text") or "").strip()
    ]


def create_workshop_chat(broadcast: bool = True) -> dict:
    with state_lock:
        current = _safe_workshop_state(hud_state.get("workshop_mode"))
        session = _new_workshop_chat_session()
        current["chat_sessions"].append(session)
        current["chat_sessions"] = current["chat_sessions"][-40:]
        current["active_chat_id"] = session["id"]
        current = _sync_workshop_chat_view(current)
        hud_state["workshop_mode"] = current
        snapshot = copy.deepcopy(current)
        _touch_state()

    _write_workshop_state_file(snapshot)

    if broadcast:
        broadcast_state()

    return session


def select_workshop_chat(chat_id: str, broadcast: bool = True) -> bool:
    target = str(chat_id or "").strip()

    if not target:
        return False

    with state_lock:
        current = _safe_workshop_state(hud_state.get("workshop_mode"))

        if not any(entry["id"] == target for entry in current["chat_sessions"]):
            return False

        current["active_chat_id"] = target
        current = _sync_workshop_chat_view(current)
        hud_state["workshop_mode"] = current
        snapshot = copy.deepcopy(current)
        _touch_state()

    _write_workshop_state_file(snapshot)

    if broadcast:
        broadcast_state()

    return True


def rename_workshop_chat(chat_id: str, title: str, broadcast: bool = True) -> bool:
    target = str(chat_id or "").strip()
    clean_title = " ".join(str(title or "").split())[:60]

    if not target or not clean_title:
        return False

    with state_lock:
        current = _safe_workshop_state(hud_state.get("workshop_mode"))
        session = next((entry for entry in current["chat_sessions"] if entry["id"] == target), None)

        if session is None:
            return False

        session["title"] = clean_title
        current = _sync_workshop_chat_view(current)
        hud_state["workshop_mode"] = current
        snapshot = copy.deepcopy(current)
        _touch_state()

    _write_workshop_state_file(snapshot)

    if broadcast:
        broadcast_state()

    return True


def delete_workshop_chat(chat_id: str, broadcast: bool = True) -> bool:
    target = str(chat_id or "").strip()

    if not target:
        return False

    with state_lock:
        current = _safe_workshop_state(hud_state.get("workshop_mode"))
        remaining = [entry for entry in current["chat_sessions"] if entry["id"] != target]

        if len(remaining) == len(current["chat_sessions"]):
            return False

        # Never leave the operator with no conversation at all.
        current["chat_sessions"] = remaining or [_new_workshop_chat_session()]

        if current.get("active_chat_id") == target:
            current["active_chat_id"] = current["chat_sessions"][-1]["id"]

        current = _sync_workshop_chat_view(current)
        hud_state["workshop_mode"] = current
        snapshot = copy.deepcopy(current)
        _touch_state()

    _write_workshop_state_file(snapshot)

    if broadcast:
        broadcast_state()

    return True


# ==========================================
# MEMORY PANEL — thin wrappers over memory_manager
# ==========================================
# These used to keep their own list inside workshop_state.json. They now do
# nothing but call the real store and re-broadcast, so there is a single source
# of truth for what FRIDAY remembers. The names are kept because the older
# workshop_memory_* socket events still arrive from any renderer that has not
# reloaded yet.

def append_workshop_memory(text: str, metadata=None, broadcast: bool = True) -> Optional[dict]:
    """Pin a note from the Workshop panel. Project-scoped, like it always was."""
    meta = metadata if isinstance(metadata, dict) else {}
    record = memory_manager.remember(
        text,
        scope=memory_manager.SCOPE_PROJECT,
        project_id=meta.get("project_id"),
        importance=3,
        source="workshop_ui"
    )

    if record and broadcast:
        broadcast_memory_state()

    return record


def set_workshop_memory_pinned(memory_id: str, pinned: bool, broadcast: bool = True) -> bool:
    changed = memory_manager.set_pinned(memory_id, bool(pinned))

    if changed and broadcast:
        broadcast_memory_state()

    return changed


def remove_workshop_memory(memory_id: str, broadcast: bool = True) -> bool:
    removed = memory_manager.forget_by_id(memory_id)

    if removed and broadcast:
        broadcast_memory_state()

    return removed


def add_memory_entry(
    text: str,
    scope: str = memory_manager.SCOPE_USER,
    category: Optional[str] = None,
    project_id: Optional[str] = None,
    pinned: bool = False,
    source: str = "workshop_ui",
    broadcast: bool = True
) -> Optional[dict]:
    record = memory_manager.remember(
        text,
        scope=scope,
        category=category,
        project_id=project_id,
        importance=3,
        source=source,
        pinned=pinned
    )

    if record and broadcast:
        broadcast_memory_state()

    return record


def edit_memory_entry(memory_id: str, broadcast: bool = True, **fields) -> Optional[dict]:
    record = memory_manager.update_memory(memory_id, **fields)

    if record and broadcast:
        broadcast_memory_state()

    return record


def migrate_memory_stores() -> dict:
    """Bring memory v1 forward, once, at start-up.

    Two legacy sources: memory.txt (handled inside memory_manager) and the
    Workshop "Project Memory" list that lived in workshop_state.json. Both are
    backed up before anything is imported, and neither original is deleted —
    a migration that cannot be inspected afterwards is not one worth trusting.
    """
    legacy_items = []

    try:
        if WORKSHOP_STATE_FILE.exists():
            with open(WORKSHOP_STATE_FILE, "r") as workshop_file:
                snapshot = json.load(workshop_file)

            if isinstance(snapshot, dict):
                legacy_items = [
                    item for item in (
                        snapshot.get("project_memory")
                        or snapshot.get("memory_items")
                        or []
                    ) if isinstance(item, dict)
                ]

            if legacy_items:
                memory_manager._backup_file(WORKSHOP_STATE_FILE, reason="pre_memory_v2")
    except Exception:
        legacy_items = []

    try:
        return memory_manager.run_startup_migration(legacy_items)
    except Exception as error:
        return {"error": str(error)}


def set_workshop_file_manager_open(open_state: bool, broadcast: bool = True) -> None:
    with state_lock:
        current = _safe_workshop_state(hud_state.get("workshop_mode"))
        current["file_manager_open"] = bool(open_state)
        hud_state["workshop_mode"] = current
        snapshot = copy.deepcopy(current)
        _touch_state()

    _write_workshop_state_file(snapshot)

    if broadcast:
        broadcast_state()


def save_workshop_state_snapshot() -> str:
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    with state_lock:
        current = _safe_workshop_state(hud_state.get("workshop_mode"))
        current["layout_saved_at"] = _now_iso()
        hud_state["workshop_mode"] = current
        snapshot = copy.deepcopy(current)
        _touch_state()

    _write_workshop_state_file(snapshot)

    broadcast_state()
    return "Workshop layout saved"


def load_workshop_state_snapshot() -> dict:
    if not WORKSHOP_STATE_FILE.exists():
        return {}

    try:
        with open(WORKSHOP_STATE_FILE, "r") as workshop_file:
            snapshot = json.load(workshop_file)
    except Exception:
        return {}

    safe_snapshot = _safe_workshop_state(snapshot)

    with state_lock:
        hud_state["workshop_mode"] = safe_snapshot
        _touch_state()

    broadcast_state()
    _broadcast_workshop_toggle(safe_snapshot)
    return copy.deepcopy(safe_snapshot)


def reset_workshop_state_snapshot() -> str:
    try:
        if WORKSHOP_STATE_FILE.exists():
            WORKSHOP_STATE_FILE.unlink()
    except Exception as error:
        return f"Workshop layout reset failed, {error}"

    with state_lock:
        hud_state["workshop_mode"] = _default_workshop_state()
        _touch_state()

    broadcast_state()
    _broadcast_workshop_toggle(hud_state["workshop_mode"])
    return "Workshop layout reset"


def set_last_error(error_text: str, broadcast: bool = True) -> None:
    with state_lock:
        hud_state["last_error"] = {
            "text": error_text,
            "timestamp": _now_iso()
        }
        hud_state["ai_status"] = "ERROR"
        _touch_state()

    if broadcast:
        broadcast_state()


# ==========================================
# HUD PROCESS LIFECYCLE
# ==========================================

def request_hud_shutdown(broadcast: bool = True) -> None:
    with state_lock:
        hud_state["shutdown_requested"] = True
        _touch_state()

    if broadcast:
        broadcast_state()


def clear_hud_shutdown_flag(broadcast: bool = True) -> None:
    with state_lock:
        hud_state["shutdown_requested"] = False
        _touch_state()

    if broadcast:
        broadcast_state()


def _watch_hud_launch(process) -> None:
    """
    Report a HUD that dies immediately.

    The launcher previously discarded the child's output and always reported
    success, so a failed `npm start` looked identical to a working one: no
    window, no explanation.
    """
    def worker() -> None:
        try:
            process.wait(timeout=6)
        except Exception:
            return  # still running after the grace period, which is the good case

        if process.returncode in (0, None):
            return

        detail = ""

        try:
            if process.stderr is not None:
                detail = process.stderr.read().decode("utf-8", errors="ignore").strip()
                detail = detail.splitlines()[-1][:140] if detail else ""
        except Exception:
            detail = ""

        print("[HUD] Interface failed to start (exit {0}).{1}".format(
            process.returncode, " " + detail if detail else ""
        ))
        print("[HUD] The brain keeps running; voice and manual override are unaffected.")

    threading.Thread(target=worker, daemon=True).start()


# How many HUD surfaces are currently connected over Socket.IO.
#
# `hud_process` only knows about a HUD THIS process started. A HUD launched by
# `run_friday.sh`, by npm directly, or by a previous brain that has since been
# restarted is invisible to it — so the spawn guard passed and a SECOND Electron
# process was started, each with its own main window and its own workshop
# displays. That is where duplicate Proton/Workshop displays came from.
#
# A live socket connection is proof a HUD exists regardless of who started it.
_hud_client_count = 0
_hud_client_lock = threading.Lock()


def _note_hud_client(delta: int) -> None:
    global _hud_client_count

    with _hud_client_lock:
        _hud_client_count = max(0, _hud_client_count + int(delta))


def hud_client_connected() -> bool:
    with _hud_client_lock:
        return _hud_client_count > 0


def launch_hud_interface() -> str:
    global hud_process

    try:
        if hud_process and hud_process.poll() is None:
            return "HUD already online."

        if hud_client_connected():
            # Someone else's HUD is already attached; adopting it is correct, and
            # spawning here would produce a duplicate interface.
            return "HUD already connected."

        # ELECTRON_RUN_AS_NODE makes Electron boot as plain Node, so main.js
        # fails on the electron API. Some parent shells export it; clear it for
        # the child so the HUD always starts as a real Electron app.
        hud_env = os.environ.copy()
        hud_env.pop("ELECTRON_RUN_AS_NODE", None)

        hud_process = subprocess.Popen(
            ["npm", "start"],
            cwd=VISUAL_INTERFACE_DIR,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            start_new_session=True,
            env=hud_env
        )

        _watch_hud_launch(hud_process)
        return "HUD interface launched."

    except Exception as e:
        return f"HUD launch failed: {e}"


def show_hud_interface() -> str:
    result = launch_hud_interface()
    broadcast_to_hud("show_hud", {})
    return result


# Last voice health the renderer reported. Only ever written from an actual
# report — never optimistically set to "listening" because we asked for it.
_voice_health = {"ok": False, "reason": "not reported yet"}


def ensure_voice_ready() -> None:
    """Ask the renderer to make the microphone and Live session genuinely live.

    Fire and forget by design: the renderer owns the audio devices and is the
    only thing that can inspect a MediaStream track or an AudioContext. It
    answers on `voice_health` with what is actually true, which is what
    get_voice_health() then reports.
    """
    broadcast_to_hud("voice_ensure_ready", {})


def get_voice_health() -> dict:
    with state_lock:
        return dict(_voice_health)


def record_voice_health(payload: dict) -> None:
    global _voice_health

    if not isinstance(payload, dict):
        return

    with state_lock:
        _voice_health = dict(payload)

    listening = bool(payload.get("ok"))
    hud_state["voice_ready"] = listening
    hud_state["voice_status"] = "LISTENING" if listening else "VOICE OFFLINE"
    hud_state["voice_status_detail"] = str(payload.get("reason") or "")


@socketio.on("voice_health")
def handle_voice_health(data):
    previous = get_voice_health().get("ok")
    record_voice_health(data if isinstance(data, dict) else {})
    current = get_voice_health().get("ok")

    # Logged only on transition; a 15s heartbeat would otherwise spam the console.
    if previous != current:
        detail = get_voice_health().get("reason") or ""
        print(f"[VOICE] {'listening' if current else 'OFFLINE — ' + str(detail)}")


@socketio.on("workshop_displays_closed")
def handle_workshop_displays_closed(_data=None):
    """Every Workshop display has closed, so Workshop mode is over.

    Without this, closing a Workshop window directly left workshop_mode.active
    set forever and quietly broke the Workstation.
    """
    if get_hud_state_snapshot().get("workshop_mode", {}).get("active"):
        print("[WORKSHOP] all displays closed; leaving workshop mode")
        set_workshop_mode(False)


def workshop_mode_is_live() -> bool:
    """True only when Workshop is active AND still has displays behind it.

    The active flag alone is not trustworthy — it can outlive the windows it
    describes, and everything that trusted it then misbehaved on the Workstation.
    """
    workshop = get_hud_state_snapshot().get("workshop_mode", {})

    if not workshop.get("active"):
        return False

    return int(workshop.get("display_count") or 0) > 0


def ensure_workstation_visible(reason: str = "") -> str:
    """Bring the Workstation to the front, ready to receive a widget.

    THE single authority for "make the desktop visible". Widget creators used to
    each decide this for themselves — and disagreed. Some called
    activate_widget_surface(), some did nothing at all, and the ones that did
    only flipped the mode in Python WITHOUT broadcasting and WITHOUT showing the
    window. Opening Music from the sleep screen therefore built the card behind a
    sleep surface that was never told to step aside.

    Deliberately NOT for dedicated full-screen surfaces (Tactical Map) or for
    Workshop, which own their own presentation.

    Idempotent: safe to call on every widget open, including when the Workstation
    is already up. It never clears existing cards — set_hud_mode("SLEEP") does
    that, which is why this only ever moves toward ACTIVE.
    """
    snapshot = get_hud_state_snapshot()

    if workshop_mode_is_live():
        # Workshop genuinely owns the screen; widgets route to its displays.
        return "workshop active"

    if snapshot.get("workshop_mode", {}).get("active"):
        # Flag set with no displays behind it. Trusting it here is what stranded
        # the Workstation after leaving Workshop, so clear it and carry on.
        print("[WORKSHOP] stale workshop_mode with no displays; clearing")
        set_workshop_mode(False, broadcast=False)

    already_active = str(snapshot.get("hud_mode") or "").upper() == "ACTIVE"

    clear_hud_shutdown_flag(broadcast=False)
    set_hud_mode("ACTIVE", orb_position="DOCKED_BOTTOM_RIGHT", broadcast=False)

    # Launches only when no HUD exists; otherwise shows/focuses the one that does.
    launch_result = show_hud_interface()

    # Turning the interface on must also turn the ears on. Waking the desktop
    # without this produced the exact failure worth avoiding: a workstation that
    # looks online while nothing is being heard.
    ensure_voice_ready()

    # One broadcast for the whole transition, so the renderer sees a single
    # coherent change rather than a half-applied state.
    broadcast_state(immediate=True)

    if reason:
        print(f"[Workstation ready for {reason}: {launch_result}]")

    return "already active" if already_active else launch_result


def hide_hud_interface(reason: str = "interface_offline") -> None:
    broadcast_to_hud("hide_hud", {"reason": reason})


def shutdown_hud_interface(reason: str = "brain_shutdown", broadcast: bool = True) -> None:
    global hud_process

    request_hud_shutdown(broadcast=broadcast)
    broadcast_to_hud("shutdown_hud", {"reason": reason})
    time.sleep(0.8)

    # The HUD is launched as `npm start`, which spawns Electron as a child in a
    # new session. Signalling only the npm pid left the Electron window running
    # as an orphan after shutdown, so signal the whole process group.
    try:
        if hud_process and hud_process.poll() is None:
            _terminate_process_group(hud_process)

    except Exception:
        pass


def _terminate_process_group(process) -> None:
    import signal

    def signal_group(sig) -> bool:
        try:
            os.killpg(os.getpgid(process.pid), sig)
            return True
        except Exception:
            return False

    if not signal_group(signal.SIGTERM):
        try:
            process.terminate()
        except Exception:
            pass

    time.sleep(0.4)

    if process.poll() is None:
        if not signal_group(signal.SIGKILL):
            try:
                process.kill()
            except Exception:
                pass


# ==========================================
# HUD CARD / WIDGET MANAGEMENT
# ==========================================

def add_hud_card(
    url: str = "",
    title: str = "Untitled",
    card_type: str = "web",
    card_id: str = "",
    x: int = 80,
    y: int = 120,
    width: int = 420,
    height: int = 320,
    locked: bool = False,
    broadcast: bool = True,
    data: dict = None,
    preferred_workspace: str = None
) -> str:
    new_id = card_id or f"card_{uuid.uuid4().hex[:10]}"
    workspace = None
    size_hint = "large" if str(card_type or "").lower() in {
        "calendar",
        "calendar_agenda",
        "map",
        "news",
        "intel",
        "settings",
        "virtual_finder"
    } else "small" if str(card_type or "").lower() in {
        "music",
        "notification_center",
        "tasks"
    } else "normal"

    with state_lock:
        workshop_active = bool(_safe_workshop_state(hud_state.get("workshop_mode")).get("active"))

    if workshop_active:
        widget_record = add_workshop_widget(
            card_type,
            {
                "id": new_id,
                "title": title,
                "size_hint": size_hint
            },
            preferred_workspace=preferred_workspace
        )
        workspace = widget_record.get("workspace", "main")

    card = {
        "id": new_id,
        "type": card_type,
        "title": title or "Untitled",
        "url": url or "",
        "x": x,
        "y": y,
        "width": width,
        "height": height,
        "locked": locked,
        "timestamp": _now_iso(),
        "workspace": workspace,
        "size_hint": size_hint,
        "data": data or {}
    }

    with state_lock:
        for existing_card in hud_state["active_cards"]:
            if existing_card.get("id") == new_id:
                existing_card.update(card)
                _touch_state()

                if broadcast:
                    broadcast_state()

                return new_id

        hud_state["active_cards"].append(card)
        _touch_state()

    if broadcast:
        broadcast_state()

    return new_id


def remove_hud_card(card_id: str, broadcast: bool = True) -> bool:
    with state_lock:
        original_count = len(hud_state["active_cards"])

        hud_state["active_cards"] = [
            card for card in hud_state["active_cards"]
            if card.get("id") != card_id
        ]

        removed = len(hud_state["active_cards"]) != original_count
        if removed:
            remove_workshop_widget(card_id)
        _touch_state()

    if removed and broadcast:
        broadcast_state()

    return removed


def update_hud_card(card_id: str, broadcast: bool = True, **updates) -> bool:
    changed = False

    with state_lock:
        for card in hud_state["active_cards"]:
            if card.get("id") == card_id:
                for key, value in updates.items():
                    if value is not None and value != "":
                        card[key] = value

                card["timestamp"] = _now_iso()
                changed = True
                break

        if changed:
            _touch_state()

    if changed and broadcast:
        broadcast_state()

    return changed


def clear_hud_cards(broadcast: bool = True) -> None:
    with state_lock:
        hud_state["active_cards"] = []
        current = _safe_workshop_state(hud_state.get("workshop_mode"))
        current["main_widgets"] = []
        current["secondary_widgets"] = []
        current["file_manager_open"] = False
        hud_state["workshop_mode"] = current
        snapshot = copy.deepcopy(current)
        _touch_state()

    _write_workshop_state_file(snapshot)

    if broadcast:
        broadcast_state()


def clear_workshop_workspace(workspace: str, broadcast: bool = True) -> None:
    target = "secondary" if workspace == "secondary" else "main"

    with state_lock:
        hud_state["active_cards"] = [
            card for card in hud_state["active_cards"]
            if str(card.get("workspace") or "").lower() != target
        ]
        current = _safe_workshop_state(hud_state.get("workshop_mode"))
        if target == "secondary":
            current["secondary_widgets"] = []
        else:
            current["main_widgets"] = []
        current["file_manager_open"] = False
        hud_state["workshop_mode"] = current
        snapshot = copy.deepcopy(current)
        _touch_state()

    _write_workshop_state_file(snapshot)

    if broadcast:
        broadcast_state()


def close_named_hud_widget(command_text: str) -> str:
    """
    Closes native widgets by natural language before weather/news/map handlers can reopen them.
    """
    text = (command_text or "").lower()
    close_words = ["close", "remove", "hide", "dismiss", "clear", "delete"]

    if not any(word in text for word in close_words):
        return ""

    targets = []

    if any(word in text for word in ["weather", "forecast", "temperature"]):
        targets.append(("weather_current", "Weather widget closed."))

    if any(word in text for word in ["news", "headlines", "briefing", "current events"]):
        targets.append(("news_briefing", "News widget closed."))

    if any(word in text for word in ["map", "tactical map", "travel"]):
        targets.append(("map_fullscreen", "Tactical map closed."))

    if any(word in text for word in ["notification", "notifications", "alert", "alerts"]):
        targets.append(("notification_center", "Notification center closed."))

    if any(word in text for word in ["task", "tasks", "reminder", "reminders"]):
        targets.append(("tasks_widget", "Tasks closed."))

    if any(word in text for word in ["notes", "note", "sticky"]):
        targets.append(("sticky_notes", "Notes closed."))

    if any(word in text for word in ["diagnostics", "diagnostic", "system health", "system status", "hardware"]):
        targets.append(("system_health", "Diagnostics closed."))

    if not targets:
        return ""

    closed_messages = []

    for card_id, message in targets:
        if remove_hud_card(card_id, broadcast=False):
            closed_messages.append(message)

    if closed_messages:
        broadcast_state()
        return " ".join(closed_messages)

    return "No matching widget is currently open."


def update_hud_display(
    action: str = "add",
    url: str = "",
    title: str = "Untitled",
    card_id: str = "",
    card_type: str = "web",
    replace_existing: bool = True
) -> str:
    if action and isinstance(action, str) and action.startswith(("http://", "https://")) and not url:
        url = action
        action = "add"

    normalized = (action or "add").lower().strip()

    if normalized == "add":
        if replace_existing and not card_id:
            clear_hud_cards(broadcast=False)

        set_hud_mode("ACTIVE", orb_position="DOCKED_BOTTOM_RIGHT", broadcast=False)

        new_id = add_hud_card(
            url=url,
            title=title,
            card_type=card_type,
            card_id=card_id
        )

        return f"SUCCESS: Added HUD card {new_id}."

    if normalized == "update":
        if not card_id:
            return "ERROR: update requires card_id."

        changed = update_hud_card(
            card_id,
            title=title,
            url=url,
            type=card_type
        )

        if changed:
            return f"SUCCESS: Updated HUD card {card_id}."

        return f"ERROR: HUD card {card_id} not found."

    if normalized == "remove":
        if not card_id:
            return "ERROR: remove requires card_id."

        removed = remove_hud_card(card_id)

        if removed:
            return f"SUCCESS: Removed HUD card {card_id}."

        return f"ERROR: HUD card {card_id} not found."

    if normalized == "clear":
        clear_hud_cards()
        return "SUCCESS: Cleared all HUD cards."

    return f"ERROR: Unknown HUD action '{action}'. Use add, update, remove, or clear."


# ==========================================
# SOCKET.IO EVENT HANDLERS
# ==========================================

@socketio.on("connect")
def handle_hud_connect(auth=None):
    # All Socket.IO commands share this connection. Rejecting non-loopback
    # peers here prevents both Finder metadata broadcasts and indirect command
    # routes (such as manual_override) from reaching the local filesystem.
    if _virtual_finder_loopback_denial():
        return False

    _note_hud_client(1)
    broadcast_state()
    broadcast_virtual_desktop_payload_safely()


@socketio.on("disconnect")
def handle_hud_disconnect(*_args):
    _note_hud_client(-1)


@socketio.on("manual_override")
def handle_manual_input(data):
    global pending_override

    text = data.get("text", "") if isinstance(data, dict) else str(data)

    if is_showcase_mode_active() and not (_is_showcase_exit_text(text) or _is_shutdown_text(text)):
        return

    pending_override = {
        "text": text,
        "source": "override",
        "timestamp": _now_iso()
    }

    append_transcript("Jon", text, source="override")
    set_ai_status("THINKING")


# ---------------------------------------------------------------------------
# Live voice layer (Electron + Gemini Live)
#
# The renderer owns the microphone, endpointing, interruption and speech; these
# handlers translate what it reports into the SAME voice-phase and orb calls the
# Python audio engine used, so the orb, transcript and echo guard behave identically
# regardless of which pipeline is active.
# ---------------------------------------------------------------------------

@socketio.on("voice_phase")
def handle_voice_phase(data):
    payload = data if isinstance(data, dict) else {}
    phase = str(payload.get("phase") or "").strip().upper()
    if phase in VOICE_PHASES:
        set_voice_phase(phase)


@socketio.on("voice_transcript")
def handle_voice_transcript(data):
    payload = data if isinstance(data, dict) else {}
    text = str(payload.get("text") or "").strip()
    if not text:
        return
    role = str(payload.get("role") or "user").lower()
    speaker = "Jon" if role == "user" else "FRIDAY"
    append_transcript(speaker, text, source="voice_live")


@socketio.on("voice_audio_level")
def handle_voice_audio_level(data):
    payload = data if isinstance(data, dict) else {}
    try:
        level = float(payload.get("level") or 0.0)
    except (TypeError, ValueError):
        return
    state = str(payload.get("state") or "user_speaking")
    # Routed through emit_audio_level rather than straight to the renderer so the
    # existing 28/sec throttle and channel selection stay the single authority.
    emit_audio_level(level, state)


@socketio.on("voice_playback_start")
def handle_voice_playback_start(data):
    payload = data if isinstance(data, dict) else {}
    # Raised from real audio onset in the playback worklet, preserving the contract
    # that speaking state follows actual audio rather than intent to speak.
    begin_speech_playback(str(payload.get("text") or ""))


@socketio.on("voice_playback_end")
def handle_voice_playback_end(data):
    payload = data if isinstance(data, dict) else {}
    # Read the setting directly: audio_engine.echo_settle_seconds() would be a circular
    # import, since audio_engine imports this module.
    try:
        settle = int(get_setting("voice_echo_settle_ms", 220)) / 1000.0
    except (TypeError, ValueError):
        settle = 0.22
    end_speech_playback(str(payload.get("text") or ""), max(0.0, min(settle, 1.5)))


@socketio.on("voice_session_state")
def handle_voice_session_state(data):
    payload = data if isinstance(data, dict) else {}
    with voice_phase_lock:
        hud_state["voice_session"] = {
            "state": str(payload.get("state") or ""),
            "model": str(payload.get("model") or ""),
            "voice": str(payload.get("voice") or ""),
        }


@socketio.on("voice_diagnostic")
def handle_voice_diagnostic(data):
    payload = data if isinstance(data, dict) else {}
    message = str(payload.get("message") or "").strip()
    if message:
        print(f"[VOICE LIVE] {message}")


@socketio.on("launcher_action")
def handle_launcher_action(data):
    global pending_override

    text = (
        data.get("action")
        or data.get("text")
        or ""
    ) if isinstance(data, dict) else str(data)
    source = data.get("source") if isinstance(data, dict) else ""

    if is_showcase_mode_active() and not (_is_showcase_exit_text(text) or _is_shutdown_text(text)):
        return

    pending_override = {
        "text": text,
        "source": source or "launcher",
        "silent": True,
        "action": data.get("action") if isinstance(data, dict) else "",
        "workshop_role": data.get("workshop_role") if isinstance(data, dict) else "",
        "timestamp": _now_iso()
    }

    set_ai_status("THINKING", broadcast=False)


@socketio.on("workshop_displays_detected")
def handle_workshop_displays_detected(data):
    displays = data.get("displays", []) if isinstance(data, dict) else []
    roles = data.get("roles", {}) if isinstance(data, dict) else {}
    display_count = data.get("display_count") if isinstance(data, dict) else None

    update_workshop_displays(displays, broadcast=False)

    if display_count is not None:
        update_workshop_display_count(display_count, broadcast=False)

    if roles:
        update_workshop_roles(roles, broadcast=False)

    broadcast_state()


@socketio.on("workshop_unavailable")
def handle_workshop_unavailable(data):
    set_workshop_mode(False, broadcast=False)
    set_override_response("Workshop mode unavailable, no display detected.", source="system", broadcast=False)
    append_transcript("FRIDAY", "Workshop mode unavailable, no display detected.", source="system", broadcast=False)
    broadcast_state()


@socketio.on("workshop_window_state")
def handle_workshop_window_state(data):
    if not isinstance(data, dict):
        return

    update_workshop_window_state(
        data.get("window_id") or data.get("id") or "",
        data.get("data") or {}
    )


@socketio.on("workshop_chat_append")
def handle_workshop_chat_append(data):
    if not isinstance(data, dict):
        return

    append_workshop_chat(
        data.get("role") or "Jon",
        data.get("text") or "",
        data.get("metadata") if isinstance(data.get("metadata"), dict) else {}
    )


@socketio.on("workshop_chat_submit")
def handle_workshop_chat_submit(data):
    global pending_override

    if not isinstance(data, dict):
        return

    text = str(data.get("text") or "").strip()

    if not text:
        return

    if is_showcase_mode_active() and not (_is_showcase_exit_text(text) or _is_shutdown_text(text)):
        return

    requested_chat = str(data.get("chat_id") or "").strip()
    item = append_workshop_chat(
        data.get("role") or "Jon",
        text,
        data.get("metadata") if isinstance(data.get("metadata"), dict) else {},
        broadcast=False,
        chat_id=requested_chat
    )

    # Remember which conversation asked, so FRIDAY's reply is filed against it
    # even if the operator switches chats while the answer is being produced.
    set_pending_workshop_chat(requested_chat or (item or {}).get("chat_id") or "")

    pending_override = {
        "text": text,
        "source": "workshop_chat",
        "silent": False,
        "timestamp": _now_iso()
    }

    set_ai_status("THINKING", broadcast=False)

    if item:
        broadcast_to_hud("workshop_chat_delta", {"message": item})


@socketio.on("workshop_chat_new")
def handle_workshop_chat_new(_data=None):
    create_workshop_chat()


@socketio.on("workshop_chat_select")
def handle_workshop_chat_select(data):
    if isinstance(data, dict):
        select_workshop_chat(data.get("id") or "")


@socketio.on("workshop_chat_rename")
def handle_workshop_chat_rename(data):
    if isinstance(data, dict):
        rename_workshop_chat(data.get("id") or "", data.get("title") or "")


@socketio.on("workshop_chat_delete")
def handle_workshop_chat_delete(data):
    if isinstance(data, dict):
        delete_workshop_chat(data.get("id") or "")


@socketio.on("workshop_memory_append")
def handle_workshop_memory_append(data):
    if not isinstance(data, dict):
        return

    append_workshop_memory(
        data.get("text") or "",
        data.get("metadata") if isinstance(data.get("metadata"), dict) else {}
    )


@socketio.on("workshop_memory_pin")
def handle_workshop_memory_pin(data):
    if not isinstance(data, dict):
        return

    set_workshop_memory_pinned(
        data.get("id") or "",
        bool(data.get("pinned", True))
    )


@socketio.on("workshop_memory_remove")
def handle_workshop_memory_remove(data):
    if not isinstance(data, dict):
        return

    remove_workshop_memory(data.get("id") or "")


# ==========================================
# MEMORY PANEL SOCKET API
# ==========================================
# Every one of these mutates the store first and re-broadcasts afterwards. The
# renderer applies nothing optimistically, so what is on screen is always what
# is on disk.

@socketio.on("memory_add")
def handle_memory_add(data):
    if not isinstance(data, dict):
        return

    scope = str(data.get("scope") or memory_manager.SCOPE_USER).strip().lower()

    add_memory_entry(
        data.get("text") or "",
        scope=memory_manager.SCOPE_PROJECT if scope == memory_manager.SCOPE_PROJECT else memory_manager.SCOPE_USER,
        category=data.get("category") or None,
        project_id=data.get("project_id") or None,
        pinned=bool(data.get("pinned", False))
    )


@socketio.on("memory_edit")
def handle_memory_edit(data):
    if not isinstance(data, dict):
        return

    fields = {}

    if data.get("text") is not None:
        fields["text"] = data.get("text")

    if data.get("category") is not None:
        fields["category"] = data.get("category")

    if data.get("importance") is not None:
        fields["importance"] = data.get("importance")

    if data.get("pinned") is not None:
        fields["pinned"] = bool(data.get("pinned"))

    if not fields:
        return

    edit_memory_entry(data.get("id") or "", **fields)


@socketio.on("memory_delete")
def handle_memory_delete(data):
    if not isinstance(data, dict):
        return

    remove_workshop_memory(data.get("id") or "")


@socketio.on("memory_pin")
def handle_memory_pin(data):
    if not isinstance(data, dict):
        return

    set_workshop_memory_pinned(data.get("id") or "", bool(data.get("pinned", True)))


@socketio.on("memory_set_project")
def handle_memory_set_project(data):
    if not isinstance(data, dict):
        return

    name = str(data.get("name") or data.get("id") or "").strip()

    if not name:
        return

    memory_manager.set_active_project(name)
    broadcast_memory_state()


@socketio.on("memory_request")
def handle_memory_request(_data=None):
    """Explicit refresh, used when a Workshop window opens."""
    broadcast_memory_state()


# ==========================================
# LEARNING CANDIDATES (Memory v2.5)
# ==========================================
# A pending candidate is something FRIDAY noticed but has not accepted. These
# two handlers are Jon's final say over that: confidence scoring decides what
# happens automatically, and these decide what happens when he disagrees with it.

@socketio.on("memory_candidate_promote")
def handle_memory_candidate_promote(data):
    if not isinstance(data, dict):
        return

    try:
        from Core_Cognition import memory_learning

        promoted = memory_learning.promote_candidate(data.get("id") or "")
    except Exception:
        promoted = None

    if promoted:
        broadcast_memory_state()
        memory = promoted.get("memory") or {}
        broadcast_to_hud("memory_learned", {
            "title": "Memory saved",
            "category": str(memory.get("category") or "").title(),
            "text": memory.get("text") or "",
            "previous": promoted.get("previous") or "",
            "memory_id": memory.get("id") or ""
        })


@socketio.on("memory_candidate_reject")
def handle_memory_candidate_reject(data):
    if not isinstance(data, dict):
        return

    try:
        from Core_Cognition import memory_learning

        rejected = memory_learning.reject_candidate(data.get("id") or "")
    except Exception:
        rejected = None

    if rejected:
        broadcast_memory_state()


def _virtual_finder_result(ok: bool, code: str, message: str, data=None) -> dict:
    return {
        "ok": bool(ok),
        "code": str(code or ("ok" if ok else "operation_failed")),
        "message": str(message or ""),
        "data": data if isinstance(data, dict) else {}
    }


def _virtual_finder_loopback_denial():
    remote_address = str(request.remote_addr or "").strip()

    try:
        address = ipaddress.ip_address(remote_address)
        loopback = address.is_loopback or bool(
            getattr(address, "ipv4_mapped", None)
            and address.ipv4_mapped.is_loopback
        )
    except Exception:
        loopback = False

    if loopback:
        return None

    return _virtual_finder_result(
        False,
        "local_access_required",
        "Virtual Finder is available only from this Mac."
    )


def _virtual_finder_workspace(data) -> Optional[str]:
    workspace = data.get("workspace") if isinstance(data, dict) else None
    return workspace if workspace in {"main", "secondary"} else None


def _virtual_finder_safe_exception(context: str, error: Exception) -> dict:
    if hasattr(error, "as_result") and callable(error.as_result):
        return error.as_result()

    print(f"[VirtualFinder] {context} failed ({type(error).__name__})")
    set_last_error("Virtual Finder request failed safely.", broadcast=False)
    return _virtual_finder_result(
        False,
        "operation_failed",
        "Virtual Finder request failed safely."
    )


def _refresh_virtual_finder_after_operation(result: dict, data: dict) -> dict:
    if result.get("ok") is not True:
        return result

    result_data = result.get("data") if isinstance(result.get("data"), dict) else {}
    refresh_path = result_data.get("refresh_path")

    if refresh_path is None:
        refresh_path = data.get("current_path") or data.get("parent") or ""

    query = str(data.get("query") or "").strip()

    try:
        from Sensory_Array.file_tools import create_virtual_finder_widget
        create_virtual_finder_widget(
            refresh_path,
            search_query=query or None,
            preferred_workspace=_virtual_finder_workspace(data)
        )
    except Exception as error:
        safe_error = _virtual_finder_safe_exception("refresh", error)
        operation_code = result.get("code", "operation_complete")
        result_data["refresh_failed"] = True
        result_data["refresh_error"] = safe_error.get("code", "operation_failed")
        result_data["operation_code"] = operation_code
        result["data"] = result_data
        result["code"] = "operation_completed_refresh_failed"
        result["message"] = "Operation completed, but the file browser could not refresh. Use Refresh."

    return result


def _handle_virtual_finder_operation(operation: str, data, refresh: bool = False) -> dict:
    denied = _virtual_finder_loopback_denial()

    if denied:
        return denied

    if not isinstance(data, dict):
        return _virtual_finder_result(
            False,
            "invalid_request",
            "A valid Virtual Finder request is required."
        )

    try:
        from Sensory_Array.file_tools import perform_virtual_finder_operation
        result = perform_virtual_finder_operation(operation, data)
    except Exception as error:
        return _virtual_finder_safe_exception(operation, error)

    return _refresh_virtual_finder_after_operation(result, data) if refresh else result


@socketio.on("virtual_finder_open_path")
def handle_virtual_finder_open_path(data):
    denied = _virtual_finder_loopback_denial()

    if denied:
        return denied

    request_data = data if isinstance(data, dict) else {"path": str(data or "")}
    path = request_data.get("path") or ""

    try:
        from Sensory_Array.file_tools import create_virtual_finder_widget
        payload = create_virtual_finder_widget(
            path,
            preferred_workspace=_virtual_finder_workspace(request_data)
        )
        current_path = payload.get("current_path", "")
        result = _virtual_finder_result(
            True,
            "folder_loaded",
            "Folder loaded.",
            {"current_path": current_path}
        )
        result["current_path"] = current_path
        return result
    except Exception as error:
        return _virtual_finder_safe_exception("open_path", error)


@socketio.on("virtual_finder_search")
def handle_virtual_finder_search(data):
    denied = _virtual_finder_loopback_denial()

    if denied:
        return denied

    request_data = data if isinstance(data, dict) else {"query": str(data or "")}
    query = str(request_data.get("query") or "").strip()
    path = request_data.get("path") or ""

    try:
        from Sensory_Array.file_tools import create_virtual_finder_widget
        payload = create_virtual_finder_widget(
            path,
            search_query=query or None,
            preferred_workspace=_virtual_finder_workspace(request_data)
        )
        current_path = payload.get("current_path", "")
        applied_query = payload.get("search_query", "")
        result = _virtual_finder_result(
            True,
            "search_complete",
            "Search complete.",
            {
                "current_path": current_path,
                "search_query": applied_query
            }
        )
        result["current_path"] = current_path
        result["search_query"] = applied_query
        return result
    except Exception as error:
        return _virtual_finder_safe_exception("search", error)


@socketio.on("virtual_finder_create_folder")
def handle_virtual_finder_create_folder(data):
    return _handle_virtual_finder_operation("create_folder", data, refresh=True)


@socketio.on("virtual_finder_create_file")
def handle_virtual_finder_create_file(data):
    return _handle_virtual_finder_operation("create_file", data, refresh=True)


@socketio.on("virtual_finder_rename")
def handle_virtual_finder_rename(data):
    return _handle_virtual_finder_operation("rename", data, refresh=True)


@socketio.on("virtual_finder_delete")
def handle_virtual_finder_delete(data):
    return _handle_virtual_finder_operation("delete", data, refresh=True)


@socketio.on("virtual_finder_transfer")
def handle_virtual_finder_transfer(data):
    return _handle_virtual_finder_operation("transfer", data, refresh=True)


@socketio.on("virtual_finder_preview")
def handle_virtual_finder_preview(data):
    return _handle_virtual_finder_operation("preview", data, refresh=False)


# ------------------------------------------------------------------
# READ-ONLY BROWSING
# ------------------------------------------------------------------
# `virtual_finder_open_path` above is navigation for the WORKSTATION widget: it
# calls create_virtual_finder_widget, so asking it for a folder listing also puts
# that widget on screen and moves it to the folder.
#
# Workshop's Files panel needs the listing and nothing else. Browsing a folder in
# the sidebar must not summon the Workstation Finder or drag it along behind you.
# These two are the same file_tools operations with none of the side effects —
# one filesystem, one validator, one set of protections, two ways to look at it.

@socketio.on("virtual_finder_list")
def handle_virtual_finder_list(data):
    return _handle_virtual_finder_operation("list", data, refresh=False)


@socketio.on("virtual_finder_metadata")
def handle_virtual_finder_metadata(data):
    return _handle_virtual_finder_operation("metadata", data, refresh=False)


@socketio.on("workshop_analytics_request")
def handle_workshop_analytics_request():
    try:
        from Sensory_Array.system_tools import get_workshop_analytics_payload
        socketio.emit("workshop_analytics_update", get_workshop_analytics_payload())
    except Exception as error:
        socketio.emit(
            "workshop_analytics_update",
            {
                "generated_at": _now_iso(),
                "error": str(error),
                "metrics": {},
                "components": [],
                "weather": {}
            }
        )


@socketio.on("close_hud_card")
def handle_close_hud_card(data):
    card_id = (
        data.get("card_id")
        or data.get("id")
        or ""
    ) if isinstance(data, dict) else str(data)

    if not card_id:
        return

    remove_hud_card(card_id)


# ==========================================
# WEBSOCKET SERVER STARTUP
# ==========================================

def start_bridge():
    try:
        import werkzeug.serving
        werkzeug.serving._log = lambda *args, **kwargs: None
    except Exception:
        pass

    # IMPORTANT: Port 5050 only.
    # Port 5000 is reserved/conflicts on macOS because of AirPlay/ControlCenter.
    socketio.run(
        app,
        host="0.0.0.0",
        port=5050,
        log_output=False,
        allow_unsafe_werkzeug=True
    )


def start_bridge_thread():
    threading.Thread(target=start_bridge, daemon=True).start()
