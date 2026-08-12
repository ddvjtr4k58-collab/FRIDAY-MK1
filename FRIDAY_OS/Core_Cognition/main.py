import os
import sys

os.environ["GRPC_VERBOSITY"] = "NONE"
os.environ["GRPC_TRACE"] = ""
os.environ["GRPC_POLL_STRATEGY"] = "poll"

import re
import time
import queue
import select
import logging
import warnings
import datetime
import threading
import subprocess
import concurrent.futures
from typing import Dict, List, Optional, Tuple

import google.generativeai as genai
from dotenv import load_dotenv

from Core_Cognition.state_manager import (
    socketio,
    start_bridge_thread,
    pop_pending_override,
    get_sleep_prompt,
    update_sleep_clock,
    set_ai_status,
    set_hud_mode,
    set_showcase_mode,
    set_hud_theme,
    sync_settings_state,
    update_showcase_step,
    is_showcase_mode_active,
    set_workshop_mode,
    save_workshop_state_snapshot,
    load_workshop_state_snapshot,
    reset_workshop_state_snapshot,
    append_workshop_chat,
    take_pending_workshop_chat,
    migrate_memory_stores,
    broadcast_memory_state,
    get_hud_state_snapshot,
    dock_orb_for_workspace,
    append_transcript,
    set_override_response,
    set_last_error,
    clear_hud_shutdown_flag,
    launch_hud_interface,
    show_hud_interface,
    ensure_workstation_visible,
    hide_hud_interface,
    shutdown_hud_interface,
    add_hud_card,
    clear_hud_cards,
    clear_workshop_workspace,
    remove_hud_card,
    broadcast_state,
    set_hud_card_workspace,
    close_named_hud_widget,
    broadcast_to_hud,
    set_voice_phase,
    get_voice_phase,
    is_friday_speaking,
    add_speech_end_listener
)

from Core_Cognition import voice_metrics

# Memory v2. Every read, write and prompt-context decision goes through this one
# module — see Core_Cognition/memory_manager.py for the architecture.
from Core_Cognition import memory_manager

from Core_Cognition.proactive_manager import (
    start_proactive_monitor,
    should_show_daily_briefing,
    mark_daily_briefing_shown,
    build_daily_briefing,
    build_short_return_greeting,
    register_watch_topic,
    disable_watch_topic,
    reset_daily_briefing,
    get_watch_status,
    check_proactive_alerts,
    clear_notifications,
    create_notification_center_card,
    get_notifications,
    consume_away_alerts,
    build_away_summary,
    create_proactive_hud_alert
)

from Core_Cognition.presence_manager import (
    build_presence_payload,
    format_return_greeting,
    get_idle_seconds,
    load_presence_state,
    mark_away,
    mark_returned,
    save_presence_state,
    should_mark_away,
    should_mark_returned
)

from Core_Cognition.camera_presence import (
    MODE_AWAY as CAMERA_MODE_AWAY,
    MODE_DESK_PRESENT as CAMERA_MODE_DESK_PRESENT,
    MODE_DESK_WATCH as CAMERA_MODE_DESK_WATCH,
    MODE_DOOR_WATCH as CAMERA_MODE_DOOR_WATCH,
    MODE_ENTRY_DETECTED as CAMERA_MODE_ENTRY_DETECTED,
    ROLE_DESK as CAMERA_ROLE_DESK,
    ROLE_DOOR as CAMERA_ROLE_DOOR,
    build_camera_presence_payload,
    camera_presence_available,
    detect_presence_from_camera,
    force_camera_presence_mode,
    get_available_cameras,
    load_camera_presence_state,
    reset_camera_presence_state,
    save_camera_presence_state,
    transition_camera_presence_state
)

from Core_Cognition.attention_gate import should_friday_respond

from Core_Cognition.fast_router import (
    is_writing_or_message_intent,
    route_fast_action
)

# Tool Intelligence. Every tool FRIDAY has describes itself to this registry, and
# the intent layer routes to it from what a request MEANS rather than from the
# exact words it used. Importing the package registers every tool in it — see
# Core_Cognition/tool_registry.py for the architecture.
from Core_Cognition import tools as tool_intelligence

# Multi-Step Planning. The layer above Tool Intelligence: when one request needs
# more than one capability, this builds a small validated plan from the same
# registry, runs it, and hands the collected results back for FRIDAY to answer
# from — see Core_Cognition/tool_planner.py.
from Core_Cognition import tool_planner

from Core_Cognition.settings_manager import (
    get_setting,
    load_settings,
    normalize_theme,
    set_setting,
    theme_label,
    update_settings
)

from Core_Cognition.tasks_manager import (
    add_task,
    build_tasks_payload,
    clear_completed_tasks,
    complete_task,
    delete_task,
    format_tasks_summary,
    get_today_tasks,
    get_overdue_tasks,
    load_tasks,
    parse_due_datetime_from_text,
    save_tasks
)

from Sensory_Array.audio_engine import (
    create_recognizer_and_mic,
    calibrate_microphone,
    describe_voice_provider,
    fish_configured,
    fish_status,
    listen_with_gemini_audio,
    prewarm_voice_backend,
    resolve_local_voice,
    speak,
    stop_speaking
)

from Sensory_Array import voice_profile

from Sensory_Array.vision_core import start_vision_core_thread

from Sensory_Array.system_tools import (
    store_information,
    get_hardware_telemetry,
    web_search,
    get_weather,
    get_time,
    read_file,
    execute_macos_command,
    update_hud_display,
    add_note,
    clear_notes,
    delete_note,
    create_notes_widget,
    create_weather_widget,
    create_music_widget,
    create_intel_widget,
    create_system_health_widget,
    get_workshop_analytics_payload,
    seed_context_cards,
    get_news_payload,
    extract_map_destination,
    open_map_widget,
    open_calendar_page,
    capture_optical_feed,
    capture_webcam_feed
)

from Sensory_Array.file_tools import (
    create_virtual_finder_widget,
    create_virtual_folder,
    broadcast_virtual_desktop_payload
)

from Sensory_Array.calendar_tools import (
    debug_calendar_probe,
    format_event_date,
    format_event_start,
    get_last_calendar_error,
    get_next_event,
    get_next_two_weeks_agenda,
    get_next_week_agenda,
    get_today_agenda,
    get_today_priority_reminders,
    get_tomorrow_agenda,
    get_week_agenda,
    is_calendar_connected,
    reconnect_calendar,
    summarize_next_birthday,
    summarize_events
)

warnings.filterwarnings("ignore")
logging.getLogger("google").setLevel(logging.ERROR)
logging.getLogger("werkzeug").disabled = True

load_dotenv()

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

ACTIVE_SETTINGS = load_settings()
DEFAULT_LOCATION = str(
    ACTIVE_SETTINGS.get("default_location")
    or (os.getenv("FRIDAY_DEFAULT_LOCATION") or os.getenv("JARVIS_DEFAULT_LOCATION", "Warrenville, IL"))
).strip() or "Warrenville, IL"
SILENT_MODEL_NAME = str(
    ACTIVE_SETTINGS.get("silent_operator_model")
    or (os.getenv("FRIDAY_SILENT_MODEL") or os.getenv("JARVIS_SILENT_MODEL", "gemini-2.5-flash"))
).strip() or "gemini-2.5-flash"
FACE_ID_ENABLED = (os.getenv("FRIDAY_FACE_ID_ENABLED") or os.getenv("JARVIS_FACE_ID_ENABLED", "0")).strip().lower() in {
    "1",
    "true",
    "yes",
    "on"
}


def performance_logs_enabled() -> bool:
    env_enabled = (os.getenv("FRIDAY_PERF_LOG") or os.getenv("JARVIS_PERF_LOG", "1")).strip().lower() in {"1", "true", "yes", "on"}
    return bool(get_setting("performance_logs", True)) and env_enabled


def perf_log(label: str, started_at: float):
    if performance_logs_enabled():
        elapsed = (time.time() - started_at) * 1000
        print(f"[PERF] {label}: {elapsed:.0f} ms")


def turn_on_greeting_allowed() -> bool:
    global LAST_TURN_ON_GREETING_AT

    now = time.time()

    if now - LAST_TURN_ON_GREETING_AT < TURN_ON_GREETING_COOLDOWN_SECONDS:
        return False

    LAST_TURN_ON_GREETING_AT = now
    return True


# ==========================================
# ACTION ACKNOWLEDGEMENTS
# ==========================================
# There is no phrase table here on purpose. Rotating a fixed list of canned lines
# is what made every action sound like a recording, and it competed with the
# persona instead of expressing it. Python now reports only WHAT happened; FRIDAY
# chooses the words.
#
# ACTION_EVENTS is data, not phrasing: a plain past-tense statement of fact. It is
# what gets handed to the live session, and what appears in the HUD transcript when
# the voice layer is not running.

ACTION_EVENTS = {
    "open_settings": "Settings opened",
    "close_settings": "Settings closed",
    "clear_hud": "Workspace cleared",
    "open_workstation": "Workstation opened",
    "open_music": "Music opened",
    "open_calendar": "Calendar opened",
    "open_notes": "Notes opened",
    "close_notes": "Notes closed",
    "open_weather": "Weather opened",
    "open_map": "Map opened",
    "open_files": "Virtual Finder opened",
    "open_intel": "Intel opened",
    "open_news": "Intel opened",
    "open_system_health": "Diagnostics opened",
    "open_notifications": "Notification center opened",
    "open_tasks": "Tasks opened",
    "open_task_widget": "Tasks widget opened",
    "close_tasks": "Tasks closed",
    "close_task_widget": "Tasks widget closed",
    "music_play": "Music started",
    "music_resume": "Music resumed",
    "music_toggle": "Music toggled",
    "music_pause": "Music paused",
    "music_next": "Skipped to the next track",
    "music_previous": "Went back a track",
    "save_workshop_layout": "Workshop layout saved",
    "close_workshop_mode": "Workshop mode closed",
    "open_workshop_mode": "Workshop mode engaged",
    "sleep_screen": "Sleep screen active",
    "open_sleep_screen": "Sleep screen active",
    "interface_on": "Interface online",
    "interface_off": "Interface hidden",
    "remember_fact": "Stored in memory",
    "forget_fact": "Removed from memory",
    "action_complete": "Done",
}


def action_event_text(action_key, detail: str = "") -> str:
    """Factual description of a completed action, with no personality applied."""
    key = str(action_key or "action_complete").strip().lower() or "action_complete"
    event = ACTION_EVENTS.get(key)

    if not event:
        # Unknown keys still read sensibly: "open_map" -> "Open map".
        event = key.replace("_", " ").strip().capitalize() or "Done"

    extra = str(detail or "").strip()
    return f"{event}, {extra}" if extra else event


def acknowledge_action(
    action_key,
    input_source: str,
    is_voice: bool,
    detail: str = ""
) -> None:
    """Report a completed action and let FRIDAY phrase the acknowledgement.

    With the live voice layer running this goes out as a system event, so she can
    confirm in her own words or say nothing at all when nothing needs saying. The
    HUD transcript still gets the factual line either way, which is also what the
    legacy engine speaks when the live layer is off.
    """
    respond_to_user(action_event_text(action_key, detail), input_source, is_voice, event="acknowledge")


def current_default_location() -> str:
    return str(get_setting("default_location", DEFAULT_LOCATION) or DEFAULT_LOCATION).strip() or DEFAULT_LOCATION

STATE_NAME_MAP = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas", "CA": "California",
    "CO": "Colorado", "CT": "Connecticut", "DE": "Delaware", "FL": "Florida", "GA": "Georgia",
    "HI": "Hawaii", "ID": "Idaho", "IL": "Illinois", "IN": "Indiana", "IA": "Iowa",
    "KS": "Kansas", "KY": "Kentucky", "LA": "Louisiana", "ME": "Maine", "MD": "Maryland",
    "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota", "MS": "Mississippi", "MO": "Missouri",
    "MT": "Montana", "NE": "Nebraska", "NV": "Nevada", "NH": "New Hampshire", "NJ": "New Jersey",
    "NM": "New Mexico", "NY": "New York", "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio",
    "OK": "Oklahoma", "OR": "Oregon", "PA": "Pennsylvania", "RI": "Rhode Island", "SC": "South Carolina",
    "SD": "South Dakota", "TN": "Tennessee", "TX": "Texas", "UT": "Utah", "VT": "Vermont",
    "VA": "Virginia", "WA": "Washington", "WV": "West Virginia", "WI": "Wisconsin", "WY": "Wyoming"
}

CYAN = "\033[96m"
GRAY = "\033[90m"
RED = "\033[91m"
RESET = "\033[0m"

start_time = datetime.datetime.now()
llm_lock = threading.Lock()
LAST_TURN_ON_GREETING_AT = 0.0
TURN_ON_GREETING_COOLDOWN_SECONDS = 8
LAST_TURN_ON_GREETING_VARIANT = -1
LAST_TURN_ON_CONTEXT_VARIANT = -1
TURN_ON_GREETING_HISTORY = []
LAST_HEALTH_CHECK = {
    "status": "Not run",
    "summary": "Health check has not run in this session"
}

WORKSHOP_OPEN_COMMANDS = {
    "workshop mode",
    "open workshop mode",
    "enter workshop mode",
    "initiate workshop mode",
    "start workshop mode",
    "engage workshop mode",
    "engineering mode",
    "open engineering mode",
    "enter engineering mode",
    "initiate engineering mode"
}

WORKSHOP_CLOSE_COMMANDS = {
    "close workshop mode",
    "exit workshop mode",
    "leave workshop mode",
    "close workshop",
    "exit workshop",
    "close engineering mode",
    "exit engineering mode"
}

tools = [
    store_information,
    get_hardware_telemetry,
    web_search,
    get_weather,
    get_time,
    read_file,
    execute_macos_command,
    update_hud_display
]


def background_watchdog():
    battery_warned = False
    fatigue_warned = False

    while True:
        time.sleep(60)

        try:
            import subprocess

            batt_info = subprocess.check_output(["pmset", "-g", "batt"]).decode("utf-8")
            match = re.search(r"(\d+)%", batt_info)

            if match and "discharging" in batt_info.lower():
                level = int(match.group(1))

                if level <= 20 and not battery_warned:
                    battery_warned = True
                    trigger_alert(f"[SYSTEM ALERT: Battery is at {level}%. Warn Boss.]")
                elif level > 20:
                    battery_warned = False

            uptime = (datetime.datetime.now() - start_time).total_seconds() / 3600

            if uptime >= 16 and not fatigue_warned:
                fatigue_warned = True
                trigger_alert(
                    f"[SYSTEM ALERT: Boss has been active for {int(uptime)} hours. Suggest rest.]"
                )

        except Exception:
            pass


def check_task_reminders() -> None:
    now = datetime.datetime.now().replace(microsecond=0)
    data = load_tasks()
    due_notifications = []
    changed = False

    for task in data.get("tasks", []):
        if task.get("completed") or task.get("reminder_sent"):
            continue

        due_at_value = task.get("due_at")

        if not due_at_value:
            continue

        try:
            due_at = datetime.datetime.fromisoformat(str(due_at_value))
        except Exception:
            continue

        if due_at <= now:
            task["reminder_sent"] = True
            changed = True
            priority = str(task.get("priority") or "normal").lower()
            alert_priority = priority if priority in {"low", "normal", "high", "urgent"} else "normal"
            due_notifications.append({
                "id": f"task_due_{task.get('id')}",
                "topic": "tasks",
                "title": "Task due",
                "message": f"Reminder, {task.get('title')}.",
                "items": [task.get("title")],
                "priority": "high" if alert_priority == "urgent" else alert_priority,
                "timestamp": now.isoformat(timespec="seconds"),
                "target": "tasks"
            })

    if changed:
        save_tasks(data)
        refresh_tasks_surfaces()

    for alert in due_notifications:
        create_proactive_hud_alert(alert)


def start_task_reminder_monitor(interval_seconds: int = 45) -> None:
    def run():
        while True:
            try:
                check_task_reminders()
            except Exception as error:
                set_last_error(f"Task reminder monitor failed: {error}", broadcast=False)

            time.sleep(max(30, int(interval_seconds or 45)))

    thread = threading.Thread(target=run, daemon=True)
    thread.start()


PRESENCE_GREETING_COOLDOWN_SECONDS = 300
CAMERA_PRESENCE_SPEECH_SILENCE_SECONDS = 5
presence_monitor_thread = None
presence_monitor_lock = threading.Lock()
camera_presence_monitor_thread = None
camera_presence_monitor_lock = threading.Lock()
camera_presence_previous_frames = {
    CAMERA_ROLE_DOOR: None,
    CAMERA_ROLE_DESK: None
}
last_friday_speech_finished_at = 0.0


def _presence_now_iso() -> str:
    return datetime.datetime.now().replace(microsecond=0).isoformat()


def _presence_parse_iso(value) -> Optional[datetime.datetime]:
    if not value:
        return None

    try:
        return datetime.datetime.fromisoformat(str(value))
    except Exception:
        return None


def _presence_elapsed_seconds(value) -> Optional[int]:
    timestamp = _presence_parse_iso(value)

    if not timestamp:
        return None

    return max(0, int((datetime.datetime.now().replace(microsecond=0) - timestamp).total_seconds()))


def _presence_state_with_idle(state: dict, idle_seconds: int) -> dict:
    next_state = dict(state if isinstance(state, dict) else {})
    safe_idle = max(0, int(idle_seconds))
    next_state["last_idle_seconds"] = safe_idle
    next_state["source"] = "mac_idle"

    if bool(next_state.get("user_present", True)):
        last_seen_at = datetime.datetime.now().replace(microsecond=0) - datetime.timedelta(seconds=safe_idle)
        next_state["last_seen_at"] = last_seen_at.isoformat()

    return next_state


def _presence_greeting_allowed(state: dict) -> bool:
    last_greeting_at = _presence_parse_iso(state.get("last_return_greeting_at"))

    if not last_greeting_at:
        return True

    elapsed = (datetime.datetime.now().replace(microsecond=0) - last_greeting_at).total_seconds()
    return elapsed >= PRESENCE_GREETING_COOLDOWN_SECONDS


def _presence_notify(event_key: str, message: str) -> None:
    if not bool(get_setting("show_notifications", True)):
        return

    now_bucket = int(time.time() // PRESENCE_GREETING_COOLDOWN_SECONDS)
    create_proactive_hud_alert({
        "id": f"presence_{event_key}_{now_bucket}",
        "topic": "presence",
        "title": "Presence",
        "message": message,
        "items": [],
        "priority": "low",
        "timestamp": _presence_now_iso(),
        "speak": False
    })


def _broadcast_presence_status() -> None:
    payload = build_presence_payload()
    broadcast_to_hud("presence_status_update", payload)
    broadcast_settings_payload()


def start_presence_monitor(interval_seconds: int = 30) -> None:
    global presence_monitor_thread

    with presence_monitor_lock:
        if presence_monitor_thread and presence_monitor_thread.is_alive():
            return

        def run():
            while True:
                try:
                    settings = load_settings()

                    if not bool(settings.get("presence_mode_enabled", False)):
                        time.sleep(max(30, int(interval_seconds or 30)))
                        continue

                    idle_seconds = get_idle_seconds()

                    if idle_seconds is None:
                        time.sleep(max(30, int(interval_seconds or 30)))
                        continue

                    state = load_presence_state()
                    state = _presence_state_with_idle(state, idle_seconds)

                    if should_mark_away(settings, state):
                        state = mark_away(state)
                        save_presence_state(state)

                        if bool(settings.get("presence_auto_sleep_enabled", True)):
                            activate_sleep_standby()

                        _presence_notify("away", "Presence changed: away.")
                        _broadcast_presence_status()
                    elif should_mark_returned(settings, state):
                        away_seconds = _presence_elapsed_seconds(state.get("away_since"))
                        greeting_allowed = _presence_greeting_allowed(state)
                        state = mark_returned(state)

                        if greeting_allowed and bool(settings.get("presence_return_greeting_enabled", True)):
                            state["last_return_greeting_at"] = _presence_now_iso()

                        save_presence_state(state)

                        if bool(settings.get("presence_auto_launch_ui", False)):
                            launch_hud_interface()

                        if bool(settings.get("presence_auto_open_workstation", False)):
                            activate_workstation_home()

                        if greeting_allowed and bool(settings.get("presence_return_greeting_enabled", True)):
                            respond_to_user(format_return_greeting(away_seconds), "presence", True)

                        _presence_notify("returned", "Presence confirmed.")
                        _broadcast_presence_status()
                    else:
                        save_presence_state(state)

                except Exception as error:
                    set_last_error(f"Presence monitor failed: {error}", broadcast=False)

                time.sleep(max(30, int(interval_seconds or 30)))

        presence_monitor_thread = threading.Thread(target=run, daemon=True)
        presence_monitor_thread.start()


def _camera_presence_parse_iso(value) -> Optional[datetime.datetime]:
    if not value:
        return None

    try:
        return datetime.datetime.fromisoformat(str(value))
    except Exception:
        return None


def _camera_presence_greeting_allowed(state: dict, settings: dict) -> bool:
    last_greeting_at = _camera_presence_parse_iso(state.get("last_greeting_at"))

    if not last_greeting_at:
        return True

    try:
        cooldown = int(settings.get("camera_presence_greeting_cooldown_seconds", 120))
    except (TypeError, ValueError):
        cooldown = 120

    cooldown = max(10, min(cooldown, 86400))
    elapsed = (datetime.datetime.now().replace(microsecond=0) - last_greeting_at).total_seconds()
    return elapsed >= cooldown


def _camera_presence_check_interval_seconds(settings: dict, fallback: int = 10) -> int:
    try:
        interval = int(settings.get("camera_presence_check_interval_seconds", fallback) or fallback)
    except (TypeError, ValueError):
        interval = fallback

    return max(5, min(interval, 60))


def _camera_presence_speech_allowed(settings: dict, setting_key: str) -> bool:
    if not bool(settings.get(setting_key, False)):
        return False

    ai_status = str(get_hud_state_snapshot().get("ai_status") or "").upper()

    if ai_status == "SPEAKING":
        return False

    if time.time() - last_friday_speech_finished_at < CAMERA_PRESENCE_SPEECH_SILENCE_SECONDS:
        return False

    return True


def _camera_presence_now_iso() -> str:
    return datetime.datetime.now().replace(microsecond=0).isoformat()


def _broadcast_camera_presence_status() -> dict:
    payload = build_camera_presence_payload()
    broadcast_to_hud("camera_presence_status_update", payload)
    broadcast_settings_payload()
    return payload


def _camera_presence_role_for_mode(mode: str) -> Optional[str]:
    if mode == CAMERA_MODE_DOOR_WATCH:
        return CAMERA_ROLE_DOOR

    if mode in {CAMERA_MODE_DESK_WATCH, CAMERA_MODE_DESK_PRESENT}:
        return CAMERA_ROLE_DESK

    return None


def _camera_presence_device_index(role: str, settings: dict) -> int:
    key = "camera_presence_door_device_index" if role == CAMERA_ROLE_DOOR else "camera_presence_desk_device_index"
    default = 1 if role == CAMERA_ROLE_DOOR else 0

    try:
        return max(0, min(int(float(settings.get(key, default))), 20))
    except (TypeError, ValueError):
        return default


def _camera_presence_motion_threshold(settings: dict) -> float:
    try:
        return max(0.0, min(float(settings.get("camera_presence_motion_threshold", 18)), 255.0))
    except (TypeError, ValueError):
        return 18.0


def _camera_presence_set_mode(mode: str) -> dict:
    state = force_camera_presence_mode(mode, load_settings())
    _broadcast_camera_presence_status()
    return state


def _camera_presence_test_role(role: str) -> dict:
    settings = load_settings()

    if not camera_presence_available():
        payload = build_camera_presence_payload()
        broadcast_to_hud("camera_presence_status_update", payload)
        return {
            "available": False,
            "detected": False,
            "motion_score": 0.0,
            "message": "Camera presence unavailable."
        }

    index = _camera_presence_device_index(role, settings)
    detection = detect_presence_from_camera(index, None, _camera_presence_motion_threshold(settings))
    detection["role"] = role

    state = load_camera_presence_state()
    state["last_motion_score"] = detection.get("motion_score", 0)
    state["status"] = "available" if detection.get("available") else "unavailable"
    save_camera_presence_state(state)
    _broadcast_camera_presence_status()
    return detection


def start_camera_presence_monitor(interval_seconds: int = 10) -> None:
    global camera_presence_monitor_thread

    with camera_presence_monitor_lock:
        if camera_presence_monitor_thread and camera_presence_monitor_thread.is_alive():
            return

        def run():
            while True:
                try:
                    settings = load_settings()
                    sleep_interval = _camera_presence_check_interval_seconds(settings, interval_seconds)

                    if not bool(settings.get("camera_presence_enabled", False)):
                        time.sleep(sleep_interval)
                        continue

                    state = load_camera_presence_state()
                    previous_mode = str(state.get("mode") or "")
                    state = transition_camera_presence_state(state, settings, {})
                    mode = str(state.get("mode") or "")

                    if mode == CAMERA_MODE_AWAY:
                        if bool(settings.get("camera_presence_auto_handoff_enabled", False)):
                            state = force_camera_presence_mode(CAMERA_MODE_DOOR_WATCH, settings)
                            save_camera_presence_state(state)

                        _broadcast_camera_presence_status()
                        time.sleep(sleep_interval)
                        continue

                    role = _camera_presence_role_for_mode(mode)

                    if not role:
                        save_camera_presence_state(state)
                        _broadcast_camera_presence_status()
                        time.sleep(sleep_interval)
                        continue

                    index = _camera_presence_device_index(role, settings)
                    detection = detect_presence_from_camera(
                        index,
                        camera_presence_previous_frames.get(role),
                        _camera_presence_motion_threshold(settings)
                    )
                    detection["role"] = role
                    detection_frame = detection.get("frame")

                    if detection_frame is not None:
                        camera_presence_previous_frames[role] = detection_frame

                    next_state = transition_camera_presence_state(state, settings, detection)
                    next_mode = str(next_state.get("mode") or "")

                    if next_mode == CAMERA_MODE_ENTRY_DETECTED:
                        if (
                            _camera_presence_speech_allowed(settings, "camera_presence_unknown_greeting_enabled")
                            and _camera_presence_greeting_allowed(next_state, settings)
                        ):
                            next_state["last_greeting_at"] = _camera_presence_now_iso()
                            save_camera_presence_state(next_state)
                            respond_to_user(
                                "Hello, you are not my primary user, how can I help you?",
                                "camera_presence",
                                True
                            )

                        if bool(settings.get("camera_presence_auto_handoff_enabled", False)):
                            next_state = force_camera_presence_mode(CAMERA_MODE_DESK_WATCH, settings)

                    elif next_mode == CAMERA_MODE_DESK_PRESENT and previous_mode == CAMERA_MODE_DESK_WATCH:
                        if (
                            _camera_presence_speech_allowed(settings, "camera_presence_primary_user_greeting_enabled")
                            and _camera_presence_greeting_allowed(next_state, settings)
                        ):
                            idle_seconds = get_idle_seconds()

                            if idle_seconds is not None and idle_seconds < 30:
                                next_state["last_greeting_at"] = _camera_presence_now_iso()
                                save_camera_presence_state(next_state)
                                respond_to_user("Welcome back.", "camera_presence", True)

                    elif next_mode == CAMERA_MODE_AWAY:
                        if bool(settings.get("camera_presence_auto_handoff_enabled", False)):
                            next_state = force_camera_presence_mode(CAMERA_MODE_DOOR_WATCH, settings)

                    save_camera_presence_state(next_state)
                    _broadcast_camera_presence_status()

                except Exception as error:
                    set_last_error(f"Camera presence monitor failed: {error}", broadcast=False)

                try:
                    sleep_seconds = _camera_presence_check_interval_seconds(load_settings(), interval_seconds)
                except Exception:
                    sleep_seconds = _camera_presence_check_interval_seconds({}, interval_seconds)

                time.sleep(sleep_seconds)

        camera_presence_monitor_thread = threading.Thread(target=run, daemon=True)
        camera_presence_monitor_thread.start()


def trigger_alert(alert_text):
    if is_showcase_mode_active():
        return

    with llm_lock:
        chat = genai.GenerativeModel("gemini-3-flash-preview").start_chat()

        set_ai_status("THINKING")
        response = chat.send_message(alert_text)
        final_text = clean_friday_output(response.text.strip())

        append_transcript("SYSTEM", alert_text, source="watchdog")
        append_transcript("FRIDAY", final_text, source="watchdog")

        print(f"\n\n{CYAN}FRIDAY: {final_text}{RESET}")
        set_ai_status("IDLE")
        print("Jon: ", end="", flush=True)


def make_model():
    # No memory dump in the system prompt any more.
    #
    # Memory v1 pasted the entire memory.txt in here at start-up: it grew without
    # bound, it could not be corrected, and it was equally present whether or not
    # the request had anything to do with it. Relevant memories now arrive per
    # turn from memory_manager.build_memory_context(), which is why the rule
    # below tells her to treat them as recall rather than as something to recite.
    return genai.GenerativeModel(
        model_name="gemini-3-flash-preview",
        tools=tools,
        system_instruction=f"""
You are FRIDAY, the operating-system AI Jon talks to out loud.

MEMORY:
0. Some turns arrive with a short block of context FRIDAY remembers about Jon and his projects.
   Use it when it is relevant and ignore it when it is not. Never read it out as a list, never
   mention that it came from memory, and never claim to remember something that is not in it.

VOICE AND MANNER:
1. You are warm, concise, natural, and confident. Speak like a capable colleague who is glad to
   help, not a manual and not a machine.
2. Genuinely friendly without being bubbly. Observant. Light wit is welcome when it fits; never
   forced. Never cold, distant, or clipped.
3. Vary your phrasing. Do not reuse the same acknowledgement every time.
4. Address Jon by name or not at all. "Boss" is occasional at most, never every reply.
5. Match response length to the task. Prefer concise answers for simple questions and commands,
   but give fuller explanations when Jon asks for detail, reasoning, comparison, planning, design,
   or technical explanation. Never truncate useful information merely to stay short, and never pad
   a short answer to seem thorough. There is no fixed sentence count.
6. Write normal prose with normal punctuation, including full stops inside a reply.
7. Skip preamble and process narration. Lead with the answer.
8. Never sound theatrical, over-eager, or servile.

OPERATING RULES:
9. Native handlers control music, news, maps, weather, notifications, sleep, and HUD actions before you are consulted.
10. Use tools immediately when useful, then report the result at whatever length the result
    actually warrants.
11. No bullet lists in spoken replies.
12. Never adjust Mac system audio levels or audio mute state.
	"""
    )


def make_workshop_text_model():
    return genai.GenerativeModel(
        model_name=SILENT_MODEL_NAME,
        system_instruction=f"""
You are FRIDAY in Silent Operator text mode for Jon.

Each message may be preceded by a short block of context FRIDAY remembers, and by the recent turns
of THIS conversation only. Use them when relevant. Never read the context back as a list, never
mention that it came from memory, and never assume anything that is not in front of you — other
conversations are not visible to you and must not be guessed at.

You are a normal helpful assistant in text only.

Match response length to the task. Simple questions and commands get short answers; requests for
detail, reasoning, comparison, planning, design, or technical explanation get as much depth as they
genuinely need. Do not truncate useful information to stay short, and do not pad a simple answer.
There is no fixed sentence count.

Markdown is welcome here — headings, lists and fenced code blocks all render.
You may draft emails, messages, explanations, and plans.
Do not use voice.
Do not open widgets unless explicitly requested.
Address Jon as Boss only when natural.
"""
    )


def ordinal_day(day: int) -> str:
    if 10 <= day % 100 <= 20:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(day % 10, "th")

    return f"{day}{suffix}"


def expand_state_abbreviations(text: str) -> str:
    result = str(text or "")

    for abbreviation, full_name in STATE_NAME_MAP.items():
        result = re.sub(rf"\b{abbreviation}\b", full_name, result)

    return result


def normalize_asr_command_text(text: str) -> str:
    value = str(text or "").strip()
    value = re.sub(r"\s+", " ", value)
    # Wake-name misrecognitions observed in practice. Kept deliberately narrow:
    # only tokens that are close phonetic neighbours of the name, anchored at
    # the start of the phrase.
    value = re.sub(
        r"^(fry\s*day|fri\s*day|freida|frida|friyay|friday)\b",
        "Friday",
        value,
        flags=re.IGNORECASE
    )
    # The legacy name and its own mishears still resolve to the current
    # identity, so anyone still saying "Jarvis" is understood.
    value = re.sub(
        r"^(joe|george|jordan|jar\s*vess|jervis|jarvi|service|jarvis)\b",
        "FRIDAY",
        value,
        flags=re.IGNORECASE
    )

    replacements = {
        "nixt": "next",
        "brthday": "birthday",
        "calender": "calendar",
        "wether": "weather",
        "conflicks": "conflicts",
        "muzik": "music"
    }

    for wrong, right in replacements.items():
        value = re.sub(rf"\b{wrong}\b", right, value, flags=re.IGNORECASE)

    return value


def clean_friday_output(message: str) -> str:
    """
    Tidy an assistant reply for display and speech.

    The FRIDAY version forced a single-sentence shape: every period became a
    comma, every reply gained a trailing "Boss", and a 135-character truncation
    clipped real answers mid-word. FRIDAY speaks in normal prose, so sentence
    punctuation is preserved and the reply is left at its natural length.
    """
    text = str(message or "").strip()

    if not text:
        return "Done."

    text = re.sub(r"\s+", " ", text).strip()
    text = text.replace("!", ".").replace("¡", "")
    text = text.replace(";", ",")
    # Keep clock times intact while turning prose colons into commas.
    text = re.sub(r"(?<!\d):(?!\d)", ",", text)
    text = re.sub(r"\s+([,.?])", r"\1", text)
    text = re.sub(r",{2,}", ",", text)
    text = re.sub(r"\.{2,}", ".", text)
    text = re.sub(r",(?=[^\s\d])", ", ", text)
    text = re.sub(r"\bJARVI\b", "FRIDAY", text, flags=re.IGNORECASE)

    # Any leftover "Sir" from older copy becomes "Boss". Note this only rewrites an
    # address that is already present — it never inserts one, so FRIDAY stays free to
    # answer with no form of address at all, which is the common case.
    text = re.sub(r"\bsir\b", "Boss", text, flags=re.IGNORECASE)

    # "Boss" is occasional, so collapse repeats rather than enforcing one per reply.
    address_matches = list(re.finditer(r"\bBoss\b", text))

    if len(address_matches) > 1:
        _, first_end = address_matches[0].span()
        before = text[:first_end]
        after = re.sub(r"\bBoss\b", "", text[first_end:]).strip()
        text = re.sub(r"\s+", " ", f"{before} {after}".strip())
        text = re.sub(r"\s*,\s*", ", ", text)

    # Only genuinely over-eager openers are trimmed, and only at a sentence
    # start, so ordinary words like "great" inside a sentence survive.
    for phrase in ("awesome", "fantastic", "wonderful", "absolutely", "delighted", "cheers"):
        text = re.sub(
            r"(^|(?<=[.?]\s))" + re.escape(phrase) + r"\b[,!]?\s*",
            "",
            text,
            flags=re.IGNORECASE,
        )

    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(r"^[\s,.?]+", "", text)
    # Removing a repeated "Boss" or an opening filler can leave a gap before the
    # punctuation that followed it.
    text = re.sub(r"\s+([,.?])", r"\1", text)
    text = re.sub(r",\s*([.?])", r"\1", text)
    text = text.strip(" \t\n\r,")

    if not text:
        return "Done."

    if text[-1] not in ".?":
        text = f"{text}."

    return text


def clean_workshop_chat_output(message: str) -> str:
    text = str(message or "").strip()
    text = text.replace("!", ".").replace("¡", ".")
    text = re.sub(r"\bJARVI\b", "FRIDAY", text, flags=re.IGNORECASE)
    return text


# ==========================================
# CONVERSATION FOLLOW-UP WINDOW
# ==========================================
# After a genuine conversational answer FRIDAY keeps listening for a short while
# so a natural follow-up ("what time is the first one?") is accepted without the
# wake word. This narrows the Attention Gate only inside that window, and only
# for the "unclear casual speech" verdict - every other protection, including the
# Albanian/Geg filter, keeps working exactly as before.

CONVERSATION_WINDOW_UNTIL = 0.0
CONVERSATION_WINDOW_RELAXABLE_REASONS = {"unclear_casual_speech"}


def conversation_window_seconds() -> int:
    try:
        value = int(float(get_setting("voice_follow_up_window_seconds", 10)))
    except (TypeError, ValueError):
        value = 10

    return max(0, min(value, 60))


def open_conversation_window() -> None:
    global CONVERSATION_WINDOW_UNTIL

    window = conversation_window_seconds()

    if window <= 0:
        CONVERSATION_WINDOW_UNTIL = 0.0
        return

    CONVERSATION_WINDOW_UNTIL = time.time() + window


def close_conversation_window() -> None:
    global CONVERSATION_WINDOW_UNTIL
    CONVERSATION_WINDOW_UNTIL = 0.0


def conversation_window_active() -> bool:
    return time.time() < CONVERSATION_WINDOW_UNTIL


def respond_to_user(
    message: str,
    input_source: str,
    is_voice: bool,
    style: str = None,
    event: str = "announce"
) -> None:
    """
    Deliver one response.

    The transcript and the on-screen text go out before any audio work starts,
    so the interface updates immediately while speech is still being produced.
    Speaking state, the speech-start/speech-end events, and the orb animation are
    owned entirely by the audio engine, which raises them against real playback
    rather than against the moment we decided to speak.

    `event` describes what this text IS, which is what lets the live voice layer
    treat it correctly: "announce" is information to relay, "acknowledge" is a
    completed action she may confirm briefly or pass over in silence.
    """
    global last_friday_speech_finished_at

    response_started_at = time.time()
    # Routing is over the moment we know what to say.
    voice_metrics.mark("routing_done")
    preserve_note_prompt = str(message or "").strip() == "What would you like me to note, Boss?"

    if input_source == "workshop_chat":
        message = clean_workshop_chat_output(message)
        append_workshop_chat(
            "FRIDAY",
            message,
            {"source": "workshop_chat_response"},
            broadcast=True
        )
        set_voice_phase("IDLE", broadcast=False)
        perf_log("respond_to_user", response_started_at)
        return

    message = clean_friday_output(message)

    if preserve_note_prompt:
        message = f"{message.rstrip('.')}?"

    append_transcript("FRIDAY", message, source=input_source)

    if input_source == "override":
        set_override_response(message, source=input_source)

    broadcast_to_hud("friday_speech", {"text": message})

    if is_voice and bool(get_setting("voice_enabled", True)):
        try:
            delivered_style = speak(message, style=style, event=event)
        except Exception as error:
            delivered_style = voice_profile.NEUTRAL
            print(f"{GRAY}[Speech failed: {error}]{RESET}")
            set_voice_phase("IDLE")
        finally:
            last_friday_speech_finished_at = time.time()

        if voice_profile.opens_conversation_window(delivered_style):
            open_conversation_window()
    else:
        print(f"{CYAN}FRIDAY: {message}{RESET}")
        set_voice_phase("IDLE")

    perf_log("respond_to_user", response_started_at)


SHUTDOWN_COMMANDS = {
    "exit",
    "quit",
    "shutdown",
    "shut down",
    "power down",
    "terminate"
}

# Explicit stop-speaking commands. Also recognised by the barge-in listener in
# the audio engine while FRIDAY is mid-sentence.
VOICE_INTERRUPT_COMMANDS = {
    "friday stop",
    "friday stop talking",
    "friday stop speaking",
    "friday quiet",
    "friday be quiet",
    "friday cancel",
    "friday enough",
    "friday silence",
    "stop talking",
    "jarvis stop talking",
    "stop speaking",
    "jarvis stop speaking",
    "jarvis stop",
    "be quiet",
    "jarvis be quiet",
    "quiet",
    "jarvis quiet",
    "cancel",
    "jarvis cancel",
    "enough",
    "jarvis enough",
    "that's enough",
    "thats enough",
    "silence",
    "jarvis silence"
}


def normalize_shutdown_command_text(text: str) -> str:
    value = str(text or "").strip().lower().replace("_", " ")
    value = re.sub(r"[^\w\s]", " ", value)
    # "friday" is dropped alongside "jarvis" so the caller never has to strip the
    # wake word: "friday shut down" must reach the same place "shut down" does.
    tokens = [
        token
        for token in re.split(r"\s+", value)
        if token and token not in {"friday", "jarvis", "please", "pls"}
    ]
    return re.sub(r"\s+", " ", " ".join(tokens)).strip()


def is_full_shutdown_command(text: str) -> bool:
    return normalize_shutdown_command_text(text) in SHUTDOWN_COMMANDS


# Shutting down used to be a straight os._exit after a 0.3s sleep. That was already
# tight for the legacy engine and is simply wrong for the live layer, where speech is
# produced in the renderer a beat AFTER Python has said its piece — the process died
# before FRIDAY made a sound. The exit now waits for speech to genuinely finish, with
# a hard ceiling so a silent failure can never leave the app running.
SHUTDOWN_SPEECH_GRACE_SECONDS = 8.0
SHUTDOWN_SILENT_GRACE_SECONDS = 1.2
# She needs a moment to start; a speech-end raised immediately after arming belongs to
# whatever she was saying before, not to the goodbye.
SHUTDOWN_MIN_SPEAKING_SECONDS = 1.0

_shutdown_armed_at = 0.0
_shutdown_lock = threading.Lock()


def _finalize_full_shutdown() -> None:
    with _shutdown_lock:
        global _shutdown_armed_at

        if _shutdown_armed_at < 0:
            return  # already finalising

        _shutdown_armed_at = -1.0

    try:
        set_ai_status("IDLE", broadcast=False)
    except Exception:
        pass

    try:
        broadcast_state(immediate=True)
        broadcast_to_hud("friday_full_shutdown", {})
    except Exception:
        pass

    try:
        shutdown_hud_interface(reason="brain_shutdown")
    except TypeError:
        try:
            shutdown_hud_interface()
        except Exception:
            pass
    except Exception:
        pass

    time.sleep(0.3)
    os._exit(0)


def _shutdown_on_speech_end() -> None:
    """Speech-end listener: finish the exit once the goodbye has actually played."""
    with _shutdown_lock:
        armed_at = _shutdown_armed_at

    if armed_at <= 0:
        return

    if time.time() - armed_at < SHUTDOWN_MIN_SPEAKING_SECONDS:
        return

    _finalize_full_shutdown()


def perform_full_shutdown(
    input_source: str = "system",
    is_voice: bool = False,
    announce: bool = True
) -> None:
    """Say goodbye, then exit once that has been heard.

    Returns immediately so the caller can still deliver a tool result — a voice
    shutdown that exits inside the handler leaves FRIDAY waiting on a response that
    is never sent, and she goes quiet instead of signing off.

    `announce=False` is for exactly that case: when the request came from her own
    tool call she is already signing off from the tool result, and a system note on
    top of it just makes her say goodbye twice.
    """
    with _shutdown_lock:
        global _shutdown_armed_at

        if _shutdown_armed_at != 0.0:
            return  # already shutting down

        _shutdown_armed_at = time.time()

    if announce:
        try:
            respond_to_user("Shutting down", input_source, is_voice)
        except Exception:
            pass

    grace = SHUTDOWN_SPEECH_GRACE_SECONDS if is_voice else SHUTDOWN_SILENT_GRACE_SECONDS
    threading.Timer(grace, _finalize_full_shutdown).start()


add_speech_end_listener(_shutdown_on_speech_end)


def append_workshop_chat_fast(role: str, message: str, metadata=None, chat_id: str = "") -> None:
    text = clean_workshop_chat_output(message)
    # File the reply against the conversation that asked, not merely whichever
    # one happens to be selected when the answer arrives. The caller normally
    # claims that id up front and passes it in; falling back to the pending id
    # keeps any other caller working unchanged.
    item = append_workshop_chat(
        role,
        text,
        metadata if isinstance(metadata, dict) else {},
        broadcast=False,
        chat_id=chat_id or take_pending_workshop_chat()
    )

    if item:
        broadcast_to_hud("workshop_chat_delta", {"message": item})

def build_daily_startup_briefing() -> str:
    now = datetime.datetime.now()
    today = f"{now.strftime('%A %B')} {ordinal_day(now.day)} {now.year}"
    weather_summary = "weather is unavailable"

    try:
        weather_result = str(get_weather(current_default_location())).strip()

        if weather_result:
            weather_summary = expand_state_abbreviations(weather_result)
    except Exception:
        pass

    return clean_friday_output(
        f"Welcome Boss, today is {today}, {weather_summary}, calendar is not connected yet, markets are being monitored"
    )


def _turn_on_daypart(now: Optional[datetime.datetime] = None) -> str:
    current = now or datetime.datetime.now()
    hour = current.hour

    if hour < 12:
        return "Good morning"

    if hour < 18:
        return "Good afternoon"

    return "Good evening"


def _plural(count: int, singular: str, plural: Optional[str] = None) -> str:
    return singular if count == 1 else (plural or f"{singular}s")


def _trim_context_text(value: object, max_length: int = 44) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip(" \t\n\r,.;:!?")

    if len(text) <= max_length:
        return text

    return text[: max(1, max_length - 3)].rstrip(" ,.;:!?") + "..."


def _run_fast_context_probe(callback, timeout_seconds: float = 1.2):
    result = {"ready": False, "value": None}

    def runner() -> None:
        try:
            result["value"] = callback()
        except Exception:
            result["value"] = None
        finally:
            result["ready"] = True

    worker = threading.Thread(target=runner, daemon=True)
    worker.start()
    worker.join(max(0.1, float(timeout_seconds or 1.2)))

    if not result["ready"]:
        return None

    return result["value"]


def _format_context_duration(seconds: int) -> str:
    safe_seconds = max(0, int(seconds or 0))

    if safe_seconds < 60:
        return "less than a minute"

    minutes = max(1, int(round(float(safe_seconds) / 60.0)))

    if minutes < 60:
        return f"{minutes} {_plural(minutes, 'minute')}"

    hours = minutes // 60
    remaining_minutes = minutes % 60

    if remaining_minutes:
        return f"{hours} {_plural(hours, 'hour')} {remaining_minutes} {_plural(remaining_minutes, 'minute')}"

    return f"{hours} {_plural(hours, 'hour')}"


def _turn_on_presence_fragment() -> str:
    try:
        state = load_presence_state()
        away_since = state.get("away_since")

        if not away_since:
            return ""

        parsed = datetime.datetime.fromisoformat(str(away_since))
        now = datetime.datetime.now(parsed.tzinfo) if parsed.tzinfo else datetime.datetime.now()
        away_seconds = max(0, int((now - parsed).total_seconds()))

        if away_seconds < 60:
            return ""

        return f"you were away for {_format_context_duration(away_seconds)}"
    except Exception:
        return ""


def _turn_on_calendar_fragment() -> str:
    try:
        cards = get_hud_state_snapshot().get("active_cards", [])
        calendar_card = next(
            (
                card for card in cards
                if isinstance(card, dict)
                and str(card.get("id") or "") == "calendar_agenda"
            ),
            None
        )

        if not calendar_card:
            return ""

        data = calendar_card.get("data", {}) if isinstance(calendar_card.get("data"), dict) else {}
        events = data.get("today_events") or data.get("upcoming_events") or []

        if events and isinstance(events[0], dict):
            event = events[0]
            title = _trim_context_text(event.get("title") or "next event")
            time_text = str(event.get("time") or "").strip()

            if time_text:
                return f"next event is {title} at {time_text}"

            return f"next event is {title}"

        if data.get("connected") is False:
            return "calendar is not connected"

        return "calendar is synced"
    except Exception:
        return ""


def _turn_on_tasks_fragment() -> str:
    try:
        payload = build_tasks_payload()
        counts = payload.get("counts", {}) if isinstance(payload, dict) else {}
        overdue_count = int(counts.get("overdue") or 0)
        today_count = int(counts.get("today") or 0)
        active_count = int(counts.get("total_active") or 0)

        if overdue_count:
            return f"{overdue_count} overdue {_plural(overdue_count, 'task')}"

        if today_count:
            return f"{today_count} {_plural(today_count, 'task')} due today"

        if active_count:
            return f"{active_count} active {_plural(active_count, 'task')}"

        return "no tasks due today"
    except Exception:
        return ""


def _turn_on_workshop_fragment() -> str:
    try:
        workshop = get_hud_state_snapshot().get("workshop_mode", {})
        return "Workshop mode is active" if workshop.get("active") else ""
    except Exception:
        return ""


def _turn_on_cached_weather_fragment() -> str:
    try:
        cards = get_hud_state_snapshot().get("active_cards", [])
        weather_card = next(
            (
                card for card in cards
                if isinstance(card, dict)
                and str(card.get("id") or "") == "weather_current"
            ),
            None
        )

        if not weather_card:
            return ""

        data = weather_card.get("data", {}) if isinstance(weather_card.get("data"), dict) else {}
        temperature = data.get("temperature")
        condition = str(data.get("condition") or "").strip()
        location = _trim_context_text(data.get("location") or "")

        if temperature is None or condition.lower() in {"", "unavailable", "loading"}:
            return ""

        location_text = f" in {location}" if location else ""
        return f"weather is {condition.lower()} and {temperature} degrees{location_text}"
    except Exception:
        return ""


def _turn_on_alerts_fragment() -> str:
    try:
        notifications = get_notifications(limit=20)
    except Exception:
        return ""

    urgent_count = 0

    for notification in notifications:
        priority = str(notification.get("priority") or "").strip().lower()

        if priority in {"high", "urgent", "critical"}:
            urgent_count += 1

    if urgent_count:
        return f"{urgent_count} urgent {_plural(urgent_count, 'alert')} logged"

    return ""


def build_turn_on_greeting() -> str:
    global LAST_TURN_ON_CONTEXT_VARIANT, LAST_TURN_ON_GREETING_VARIANT

    now = datetime.datetime.now()
    daypart = _turn_on_daypart(now)
    system_phrases = [
        "systems are online",
        "the workstation is ready",
        "monitoring is active",
        "all core systems are operational",
        "FRIDAY is standing by",
        "the interface initialized successfully"
    ]
    context_facts = [
        fragment
        for fragment in (
            _turn_on_presence_fragment(),
            _turn_on_tasks_fragment(),
            _turn_on_workshop_fragment(),
            _turn_on_cached_weather_fragment(),
            _turn_on_calendar_fragment()
        )
        if fragment
    ]
    context_parts = []

    if context_facts:
        LAST_TURN_ON_CONTEXT_VARIANT = (LAST_TURN_ON_CONTEXT_VARIANT + 1) % len(context_facts)
        fact_count = 2 if len(context_facts) >= 4 else 1
        context_parts = [
            context_facts[(LAST_TURN_ON_CONTEXT_VARIANT + offset) % len(context_facts)]
            for offset in range(fact_count)
        ]

    context = ", ".join(context_parts)
    selected_variant = (LAST_TURN_ON_GREETING_VARIANT + 1) % len(system_phrases)
    greeting = ""

    for offset in range(len(system_phrases)):
        variant = (selected_variant + offset) % len(system_phrases)
        system_phrase = system_phrases[variant]
        candidate = clean_friday_output(
            f"{daypart} Boss, {system_phrase}, {context}"
            if context
            else f"{daypart} Boss, {system_phrase}"
        )

        if candidate not in TURN_ON_GREETING_HISTORY:
            selected_variant = variant
            greeting = candidate
            break

    if not greeting:
        greeting = clean_friday_output(f"{daypart} Boss, {system_phrases[selected_variant]}")

    LAST_TURN_ON_GREETING_VARIANT = selected_variant
    TURN_ON_GREETING_HISTORY.append(greeting)
    del TURN_ON_GREETING_HISTORY[:-5]
    return greeting


def classify_intel_request(text: str):
    if is_writing_or_message_intent(text):
        return None

    value = f" {text.lower().strip()} "
    tokens = set(re.findall(r"[a-z0-9]+", value))

    monitor_value = normalize_control_text(text.lower())

    if re.search(r"\b(stop\s+)?(monitoring|watching|monitor|watch)\b", monitor_value):
        return None

    if any(term in value for term in ["music", "song", "track", "playlist", "album", "pause", "resume", "play ", "skip", "next", "shuffle", "loop", "acdc", "ac/dc"]):
        return None

    kosovo_terms = [
        "kosovo", "kosova", "pristina", "prishtina", "kurti", "balkans", "serbia",
        "serbian", "mitrovica", "albin", "vucic"
    ]
    market_terms = [
        "market", "markets", "stock", "stocks", "finance", "financial", "economy",
        "economic", "nasdaq", "dow", "s&p", "sp500", "crypto", "bitcoin", "btc",
        "inflation", "fed", "federal reserve", "rates", "treasury", "investors",
        "wall street", "trading"
    ]
    conflict_terms = [
        "war", "wars", "conflict", "conflicts", "active conflict", "active conflicts",
        "current conflict", "current conflicts", "battle", "battles", "fighting",
        "military", "ukraine", "russia", "gaza", "israel", "iran", "red sea",
        "nato", "ceasefire", "missile", "attack", "attacks", "invasion", "security"
    ]
    technology_terms = [
        "technology", "tech", "ai", "artificial intelligence", "cyber", "cybersecurity",
        "openai", "apple", "google", "microsoft", "nvidia", "chips", "semiconductor"
    ]
    general_news_terms = [
        "news", "headlines", "current events", "briefing", "what's going on",
        "whats going on", "what is going on", "what happened", "what is happening",
        "what's happening", "latest news", "world news", "u.s.",
        "america", "united states", "current situation", "updates", "update me"
    ]
    general_news_tokens = {"news", "headlines", "briefing", "usa"}

    if any(term in value for term in kosovo_terms):
        return "kosovo"

    if any(term in value for term in market_terms):
        return "markets"

    if any(term in value for term in conflict_terms):
        return "conflict"

    if any(term in value for term in technology_terms):
        return "technology"

    if any(term in value for term in general_news_terms) or tokens.intersection(general_news_tokens):
        return "briefing"

    return None


def build_intel_topic(tab_name: str) -> str:
    if tab_name == "kosovo":
        return "kosovo news"
    if tab_name == "markets":
        return "financial markets news"
    if tab_name == "conflict":
        return "active wars conflict news"
    if tab_name == "technology":
        return "technology ai news"
    return "news briefing"


def extract_music_target(raw_lower: str) -> str:
    value = re.sub(r"\b(friday|jarvis|please)\b", "", raw_lower).strip()
    value = re.sub(r"\s+", " ", value)

    if any(term in value for term in ["random", "anything", "whatever"]):
        return "random"

    value = re.sub(r"^(play|start|resume)\s+", "", value).strip()
    value = re.sub(r"^(some|a|the)\s+", "", value).strip()
    value = re.sub(r"\b(song|track|music|playlist|album)\b", "", value).strip()
    value = re.sub(r"\s+", " ", value)

    if value in {"", "some", "a", "the"}:
        return "play"

    return value


def normalize_control_text(raw_lower: str) -> str:
    # "friday" is the wake name; "jarvis" stays accepted as a compatibility
    # alias so phrasing that already worked keeps working. Both are stripped
    # here so every command table below only has to spell the bare phrase.
    value = re.sub(r"\s+", " ", str(raw_lower or "").lower()).strip()
    value = re.sub(r"[,.!?]+", " ", value)
    value = re.sub(r"\s+", " ", value).strip()

    while True:
        updated = re.sub(r"^(friday|jarvis|please)\s+", "", value).strip()

        if updated == value:
            break

        value = updated

    return value


def normalize_command_text(text: str) -> str:
    value = str(text or "").lower()
    value = re.sub(r"\bshow\s+case\b", "showcase", value)
    value = re.sub(r"[.!?]+", " ", value)
    value = re.sub(r"\s*,\s*", ", ", value)
    value = re.sub(r"\s+", " ", value).strip()

    while True:
        updated = re.sub(r"^(friday|jarvis|please)\s+", "", value).strip()

        if updated == value:
            break

        value = updated

    return value


def hud_card_is_open(card_id: str) -> bool:
    target = str(card_id or "").strip()

    if not target:
        return False

    return any(
        card.get("id") == target
        for card in get_hud_state_snapshot().get("active_cards", [])
        if isinstance(card, dict)
    )


def handle_music_request(
    raw_lower: str,
    input_source: str,
    is_voice: bool,
    music_context_active: bool
) -> Tuple[bool, bool]:
    open_music_phrases = {
        "open music",
        "open music widget",
        "open the music widget",
        "show music",
        "show music widget",
        "show the music widget",
        "music widget",
        "launch music",
        "launch music widget",
        "friday open music",
        "friday open music widget",
        "friday show music",
        "friday show music widget",
        "jarvis open music",
        "jarvis open music widget",
        "jarvis show music",
        "jarvis show music widget"
    }

    if raw_lower in open_music_phrases:
        dock_orb_for_workspace(broadcast=False)
        if not hud_card_is_open("music_controls"):
            seed_context_cards("music", replace_existing=False)
        acknowledge_action("open_music", input_source, is_voice)
        return True, True

    control_map = {
        "resume": ("play", "Music resumed"),
        "play": ("play", "Music online"),
        "start": ("play", "Music online"),
        "pause": ("pause", "Music paused"),
        "stop": ("pause", "Music paused"),
        "next": ("next", "Skipping track"),
        "skip": ("next", "Skipping track"),
        "previous": ("previous", "Previous track"),
        "back": ("previous", "Previous track"),
        "loop": ("repeat", "Track loop enabled"),
        "repeat": ("repeat", "Track loop enabled"),
        "loop off": ("repeat off", "Track loop disabled"),
        "repeat off": ("repeat off", "Track loop disabled"),
        "shuffle": ("shuffle", "Shuffle enabled"),
        "shuffle off": ("shuffle off", "Shuffle disabled"),
        "unshuffle": ("shuffle off", "Shuffle disabled")
    }

    mentions_music = any(
        word in raw_lower
        for word in ["music", "song", "track", "playlist", "album", "acdc", "ac/dc"]
    )

    direct_control = raw_lower in control_map and (music_context_active or raw_lower not in {"play", "start"})
    play_music_request = mentions_music and any(word in raw_lower for word in ["play", "start", "resume"])

    if direct_control or play_music_request:
        started_at = time.time()

        if play_music_request and raw_lower not in control_map:
            target = extract_music_target(raw_lower)
            message = "Music online" if target == "play" else f"Playing {target}"
        else:
            target, message = control_map.get(raw_lower, ("play", "Music online"))

        execute_macos_command("music", target)
        perf_log("music routing", started_at)
        respond_to_user(message, input_source, is_voice)
        return True, True

    if mentions_music:
        return False, True

    return False, music_context_active


def extract_proactive_topic(raw_lower: str, prefixes: Tuple[str, ...]):
    value = normalize_control_text(raw_lower)
    aliases = {
        "kosovo": "kosovo",
        "kosova": "kosovo",
        "market": "markets",
        "markets": "markets",
        "financial markets": "markets",
        "stock": "markets",
        "stocks": "markets",
        "conflict": "conflict",
        "conflicts": "conflict",
        "active wars": "conflict",
        "wars": "conflict",
        "war": "conflict",
        "weather": "weather",
        "calendar": "calendar"
    }

    for prefix in prefixes:
        if value.startswith(prefix):
            topic = value[len(prefix):].strip()
            topic = re.sub(r"^the\s+", "", topic).strip()

            return aliases.get(topic)

    return None


def summarize_proactive_scan(alerts: List[Dict]) -> str:
    if not alerts:
        return "No new proactive alerts"

    topics = []

    for alert in alerts:
        topic = str(alert.get("topic", "")).replace("conflict", "active wars")

        if topic and topic not in topics:
            topics.append(topic)

    if len(topics) == 1:
        topic_text = topics[0]
    else:
        topic_text = ", ".join(topics[:-1]) + f" and {topics[-1]}"

    return f"{len(alerts)} proactive alerts found, {topic_text}"


def calendar_summary_response(summary: str) -> str:
    """Calendar facts, unadorned.

    This used to splice an address into the summary at whichever comma looked
    right. FRIDAY adds any address herself, so the data is returned as-is.
    """
    text = str(summary or "").strip().rstrip(".")
    return text or "Calendar is clear today"


def calendar_priority_response(reminders: List[Dict], wanted_types=None) -> str:
    selected = []

    for reminder in reminders:
        reminder_type = str(reminder.get("type", ""))

        if wanted_types is None or reminder_type in wanted_types:
            selected.append(reminder)

    if selected:
        return str(selected[0].get("message") or "Calendar reminder")

    if wanted_types and "birthday" in wanted_types:
        return "No birthdays on calendar today"

    return "Nothing critical on calendar today"


def extract_note_text(control_lower: str):
    source = re.sub(r"\s+", " ", str(control_lower or "")).strip()

    while True:
        updated = re.sub(r"^(friday|jarvis|please)\s+", "", source, flags=re.IGNORECASE).strip()

        if updated == source:
            break

        source = updated

    normalized = normalize_control_text(source)
    note_prefixes = (
        "create a note",
        "remember this",
        "write this down",
        "take a note",
        "add a note",
        "create note",
        "make a note",
        "take note",
        "note this",
        "add note",
        "note",
        "remember"
    )

    for prefix in note_prefixes:
        if normalized == prefix:
            return ""

        prefix_pattern = re.escape(prefix).replace(r"\ ", r"\s+")
        pattern = rf"^{prefix_pattern}\b\s*[,;:]?\s*(.*)$"
        match = re.match(pattern, source, flags=re.IGNORECASE)

        if match:
            return match.group(1).strip(" \t\n\r,.;:!?")

    return None


def join_calendar_event_titles(events: List[Dict], limit: int = 2) -> str:
    titles = []

    for event in events[:limit]:
        title = str(event.get("title") or "Untitled event").strip()

        if title:
            titles.append(title)

    if not titles:
        return "nothing scheduled"

    if len(titles) == 1:
        return titles[0]

    return " and ".join(titles)


def build_calendar_opening_summary(action: str = "opened") -> str:
    prefix = "Calendar synced" if action == "synced" else "Calendar opened"

    if not is_calendar_connected():
        error = get_last_calendar_error()

        if error == "Reauthorization required":
            return f"{prefix}, calendar reauthorization required"

        return f"{prefix}, calendar is not connected yet"

    today_events = get_today_agenda()

    if get_last_calendar_error():
        return f"{prefix}, calendar query failed"

    if today_events:
        first_event = today_events[0]
        count = len(today_events)
        noun = "event" if count == 1 else "events"
        return (
            f"{prefix}, you have {count} {noun} today, next is "
            f"{first_event.get('title', 'Untitled event')} at {format_event_start(first_event)}"
        )

    tomorrow_events = get_tomorrow_agenda()

    if get_last_calendar_error():
        return f"{prefix}, calendar query failed"

    if tomorrow_events:
        return f"{prefix}, tomorrow has {join_calendar_event_titles(tomorrow_events)}"

    next_event = get_next_event()

    if get_last_calendar_error():
        return f"{prefix}, calendar query failed"

    if next_event:
        title = next_event.get("title", "Untitled event")
        when = format_event_date(next_event)
        time_text = format_event_start(next_event)
        time_phrase = "" if str(time_text).lower() == "all day" else f" at {time_text}"
        return f"{prefix}, next event is {title} on {when}{time_phrase}"

    return f"{prefix}, no critical events ahead"


def workshop_status_response() -> str:
    state = get_hud_state_snapshot().get("workshop_mode", {})
    active_text = "active" if state.get("active") else "inactive"
    display_count = int(state.get("display_count") or len(state.get("displays") or []) or 0)
    saved_text = "layout memory is online" if state.get("layout_saved_at") else "layout memory is empty"

    if display_count:
        return f"Workshop mode {active_text}, {display_count} displays detected, {saved_text}"

    return f"Workshop mode {active_text}, {saved_text}"


def has_writing_intent(control_lower: str) -> bool:
    return is_writing_or_message_intent(control_lower)


LOCAL_FAST_ACTIONS = {
    "play": "music_play",
    "play music": "music_play",
    "resume": "music_resume",
    "resume music": "music_resume",
    "toggle music": "music_toggle",
    "pause": "music_pause",
    "pause music": "music_pause",
    "stop music": "music_pause",
    "next": "music_next",
    "next song": "music_next",
    "skip": "music_next",
    "skip song": "music_next",
    "previous": "music_previous",
    "previous song": "music_previous",
    "open music": "open_music",
    "show music": "open_music",
    "music": "open_music",
    "open notes": "open_notes",
    "show notes": "open_notes",
    "notes": "open_notes",
    "open calendar": "open_calendar",
    "show calendar": "open_calendar",
    "calendar": "open_calendar",
    "open files": "open_files",
    "open virtual finder": "open_files",
    "open file manager": "open_files",
    "files": "open_files",
    "open weather": "open_weather",
    "show weather": "open_weather",
    "weather": "open_weather",
    "open map": "open_map",
    "show map": "open_map",
    "tactical map": "open_map",
    "open intel": "open_intel",
    "open news": "open_intel",
    "intel briefing": "open_intel",
    "open system health": "open_system_health",
    "system health": "open_system_health",
    "system status": "open_system_health",
    "diagnostics": "open_system_health",
    "open notifications": "open_notifications",
    "show notifications": "open_notifications",
    "notifications": "open_notifications",
    "open tasks": "open_tasks",
    "show tasks": "open_tasks",
    "tasks": "open_tasks",
    "open reminders": "open_tasks",
    "show reminders": "open_tasks",
    "reminders": "open_tasks",
    "open task widget": "open_task_widget",
    "show task widget": "open_task_widget",
    "open reminders widget": "open_task_widget",
    "show reminders widget": "open_task_widget",
    "hide tasks": "close_task_widget",
    "close tasks": "close_task_widget",
    "close task widget": "close_task_widget",
    "hide reminders": "close_task_widget",
    "open settings": "open_settings",
    "show settings": "open_settings",
    "settings": "open_settings",
    "open configuration": "open_settings",
    "open system settings": "open_settings",
    "open friday settings": "open_settings",
    "open jarvis settings": "open_settings",
    "clear hud": "clear_hud",
    "clear workspace": "clear_hud",
    "clear screen": "clear_hud",
    "save workshop layout": "save_workshop_layout",
    "close workshop mode": "close_workshop_mode"
}

FAST_MUSIC_COMMANDS = {
    "play": ("play", "Music online"),
    "play music": ("play", "Music online"),
    "resume": ("play", "Music resumed"),
    "resume music": ("play", "Music resumed"),
    "pause": ("pause", "Music paused"),
    "pause music": ("pause", "Music paused"),
    "stop music": ("pause", "Music paused"),
    "next": ("next", "Skipping track"),
    "next song": ("next", "Skipping track"),
    "skip": ("next", "Skipping track"),
    "skip song": ("next", "Skipping track"),
    "previous": ("previous", "Previous track"),
    "previous song": ("previous", "Previous track"),
    "back": ("previous", "Previous track")
}

DIRECT_LAUNCHER_SOURCES = {"button", "launcher", "workshop_dock", "hotbar"}
WORKSHOP_WIDGET_ACTIONS = {
    "open_calendar": "calendar",
    "open_music": "music",
    "open_notes": "notes",
    "open_tasks": "tasks",
    "open_task_widget": "tasks",
    "open_files": "virtual_finder",
    "open_virtual_finder": "virtual_finder",
    "open_file_manager": "virtual_finder",
    "open_weather": "weather",
    "open_settings": "settings",
    "open_notifications": "notifications",
    "open_system_health": "system_health",
    "open_intel": "news",
    "open_news": "news",
    "open_map": "map"
}
DIRECT_ACTION_ALLOWLIST = {
    "open_music",
    "music_toggle",
    "music_play",
    "music_pause",
    "music_next",
    "music_previous",
    "music_shuffle",
    "music_shuffle_off",
    "music_repeat",
    "music_repeat_off",
    "open_calendar",
    "open_notes",
    "open_files",
    "open_weather",
    "open_map",
    "open_intel",
    "open_system_health",
    "open_notifications",
    "open_tasks",
    "open_task_widget",
    "close_tasks",
    "close_task_widget",
    "add_task",
    "complete_task",
    "delete_task",
    "clear_completed_tasks",
    "refresh_tasks",
    "open_settings",
    "close_settings",
    "set_theme",
    "toggle_voice",
    "toggle_performance_logs",
    "set_voice_provider",
    "set_voice_rate",
    "set_voice_local_name",
    "set_voice_pause_threshold",
    "set_voice_follow_up_window",
    "toggle_voice_audio_reactive_orb",
    "toggle_voice_performance_logs",
    "toggle_voice_interrupt",
    "toggle_notifications",
    "toggle_hotbar",
    "toggle_task_widget_on_wake",
    "toggle_silent_operator",
    "toggle_workshop_restore_layout",
    "toggle_showcase_return_sleep",
    "toggle_privacy_mode",
    "toggle_presence_mode",
    "toggle_presence_return_greeting",
    "toggle_presence_auto_sleep",
    "toggle_presence_auto_launch_ui",
    "toggle_presence_auto_open_workstation",
    "set_presence_idle_timeout",
    "refresh_presence_status",
    "toggle_camera_presence",
    "test_door_camera",
    "test_desk_camera",
    "set_camera_presence_door_index",
    "set_camera_presence_desk_index",
    "refresh_camera_presence_status",
    "reset_camera_presence",
    "camera_presence_door_watch",
    "camera_presence_desk_watch",
    "set_startup_mode",
    "set_default_location",
    "run_health_check",
    "clear_hud",
    "sleep_screen",
    "open_sleep_screen",
    "workstation_home",
    "open_workstation",
    "save_workshop_layout",
    "close_workshop_mode",
    "open_workshop_mode",
    "open_proactive_monitor",
    # Interface power state. These existed only as phrase matches inside the brain
    # loop, which the live voice layer never reaches — so "turn the interface off"
    # had nothing to call. They are actions like any other, so they live here.
    "interface_on",
    "interface_off",
    "full_shutdown",
    # Memory. The live voice layer never reaches the Python router, so "remember
    # that I prefer Graphite" spoken to Gemini Live has to arrive as an action.
    "remember_fact",
    "forget_fact"
}

# Actions that manage their own surface and must NOT be preceded by a Workstation
# wake-up. Everything else in the allowlist opens something on the Workstation and
# is routed through ensure_workstation_visible() by handle_direct_action.
SURFACE_INDEPENDENT_ACTIONS = {
    "interface_on",          # already IS the wake-up
    "interface_off",
    "full_shutdown",
    # Remembering something changes no surface. Waking the Workstation because
    # Jon said "remember that I prefer Graphite" would be a surprise.
    "remember_fact",
    "forget_fact",
    "sleep_screen",
    "open_sleep_screen",
    "open_workshop_mode",    # Workshop owns its own displays
    "close_workshop_mode",
    "save_workshop_layout",
    # Settings toggles change state without putting anything on screen; waking the
    # desktop for "toggle performance logs" would be a surprise.
    "toggle_voice",
    "toggle_performance_logs",
    "set_voice_provider",
    "set_voice_rate",
    "set_voice_local_name",
    "set_voice_pause_threshold",
    "set_voice_follow_up_window",
    "toggle_voice_audio_reactive_orb",
    "toggle_voice_performance_logs",
    "toggle_voice_interrupt",
    "toggle_notifications",
    "toggle_hotbar",
    "toggle_task_widget_on_wake",
    "toggle_silent_operator",
    "toggle_workshop_restore_layout",
    "toggle_showcase_return_sleep",
    "toggle_privacy_mode",
    "toggle_presence_mode",
    "toggle_presence_return_greeting",
    "toggle_presence_auto_sleep",
    "toggle_presence_auto_launch_ui",
    "toggle_presence_auto_open_workstation",
    "set_presence_idle_timeout",
    "refresh_presence_status",
    "toggle_camera_presence",
    "test_door_camera",
    "test_desk_camera",
    "set_camera_presence_door_index",
    "set_camera_presence_desk_index",
    "refresh_camera_presence_status",
    "reset_camera_presence",
    "camera_presence_door_watch",
    "camera_presence_desk_watch",
    "set_startup_mode",
    "set_default_location",
    "set_theme",
    "refresh_tasks",
    "complete_task",
    "delete_task",
    "clear_completed_tasks",
    "close_tasks",
    "close_task_widget",
    "close_settings",
    "clear_hud",
    # Transport controls act on Music.app, not on the desktop. "Pause the music"
    # while asleep should pause the music, not light up the screen. open_music is
    # deliberately NOT here — that one really is a request to see something.
    "music_toggle",
    "music_play",
    "music_pause",
    "music_next",
    "music_previous",
    "music_shuffle",
    "music_shuffle_off",
    "music_repeat",
    "music_repeat_off"
}
SHOWCASE_START_COMMANDS = {
    "showcase mode",
    "initiate showcase mode",
    "start showcase mode",
    "open showcase mode",
    "enter showcase mode",
    "engage showcase mode",
    "launch showcase mode",
    "run showcase mode",
    "activate showcase mode",
    "begin showcase mode",
    "demo mode",
    "start demo mode",
    "run demo mode",
    "launch demo mode",
    "open demo mode",
    "enter demo mode",
    "start demo",
    "run demo",
    "show demo",
    "show me the demo",
    "present friday",
    "show off friday",
    "present jarvis",
    "show off jarvis",
    "presentation",
    "presentation mode",
    "jarvis presentation",
    "start presentation mode",
    "initiate presentation mode"
}
SHOWCASE_EXIT_COMMANDS = {
    "friday exit showcase mode",
    "jarvis exit showcase mode",
}

FAST_ACTION_STARTERS = ("open ", "show ", "launch ")
FAST_ACTION_TARGET_TERMS = {
    "music",
    "notes",
    "calendar",
    "files",
    "file manager",
    "virtual finder",
    "weather",
    "map",
    "intel",
    "news",
    "system health",
    "diagnostics",
    "notifications",
    "settings",
    "configuration",
    "tasks",
    "reminders",
    "task widget"
}


def perf_enabled() -> bool:
    return performance_logs_enabled()


def local_fast_action_payload(control_lower: str):
    value = normalize_control_text(control_lower)
    action = LOCAL_FAST_ACTIONS.get(value)

    if not action:
        return None

    return {
        "action": action,
        "target_workspace": None,
        "confidence": 1.0
    }


def direct_action_for_text(raw_input: str, control_lower: str) -> str:
    candidates = [
        str(raw_input or "").strip(),
        str(control_lower or "").strip(),
        normalize_control_text(str(raw_input or "").lower()),
        normalize_control_text(str(control_lower or "").lower())
    ]

    for candidate in candidates:
        if not candidate:
            continue

        normalized = normalize_control_text(candidate.replace("_", " "))
        normalized_key = normalized.replace(" ", "_")

        if normalized in LOCAL_FAST_ACTIONS:
            return LOCAL_FAST_ACTIONS[normalized]

        if normalized_key in {
            "music_play",
            "music_pause",
            "music_resume",
            "music_toggle",
            "music_next",
            "music_previous",
            "music_shuffle",
            "music_shuffle_off",
            "music_repeat",
            "music_repeat_off",
            "open_music",
            "open_calendar",
            "open_notes",
            "open_weather",
            "open_map",
            "open_files",
            "open_system_health",
            "open_notifications",
            "open_tasks",
            "open_task_widget",
            "close_tasks",
            "close_task_widget",
            "add_task",
            "complete_task",
            "delete_task",
            "clear_completed_tasks",
            "refresh_tasks",
            "open_settings",
            "close_settings",
            "set_theme",
            "toggle_voice",
            "toggle_performance_logs",
            "set_voice_provider",
            "set_voice_rate",
            "set_voice_local_name",
            "set_voice_pause_threshold",
            "set_voice_follow_up_window",
            "toggle_voice_audio_reactive_orb",
            "toggle_voice_performance_logs",
            "toggle_voice_interrupt",
            "toggle_notifications",
            "toggle_hotbar",
            "toggle_task_widget_on_wake",
            "toggle_silent_operator",
            "toggle_workshop_restore_layout",
            "toggle_showcase_return_sleep",
            "toggle_privacy_mode",
            "toggle_presence_mode",
            "toggle_presence_return_greeting",
            "toggle_presence_auto_sleep",
            "toggle_presence_auto_launch_ui",
            "toggle_presence_auto_open_workstation",
            "set_presence_idle_timeout",
            "refresh_presence_status",
            "toggle_camera_presence",
            "test_door_camera",
            "test_desk_camera",
            "set_camera_presence_door_index",
            "set_camera_presence_desk_index",
            "refresh_camera_presence_status",
            "reset_camera_presence",
            "camera_presence_door_watch",
            "camera_presence_desk_watch",
            "set_startup_mode",
            "set_default_location",
            "run_health_check",
            "clear_hud",
            "save_workshop_layout",
            "close_workshop_mode",
            "open_intel",
            "open_news",
            "open_sleep_screen",
            "sleep_screen",
            "open_workstation",
            "workstation_home",
            "open_workshop_mode",
            "open_proactive_monitor"
        }:
            return normalized_key

    return ""


def handle_direct_launcher_action(raw_input: str, control_lower: str, workshop_role: str = "") -> bool:
    started_at = time.time()
    action = direct_action_for_text(raw_input, control_lower)

    if not action:
        perf_log("direct action miss", started_at)
        return False

    handled = handle_direct_action(action, {"workshop_role": workshop_role})
    perf_log("direct launcher action", started_at)
    return handled


def handle_fast_music_command(control_lower: str, input_source: str, is_voice: bool, silent: bool = False) -> bool:
    value = normalize_control_text(control_lower)

    if value not in FAST_MUSIC_COMMANDS:
        return False

    started_at = time.time()
    target, message = FAST_MUSIC_COMMANDS[value]
    execute_macos_command("music", target)
    perf_log("music routing", started_at)

    if not silent:
        respond_to_user(message, input_source, is_voice)

    return True


MULTI_ACTION_VERBS = {
    "open": "open",
    "show": "open",
    "launch": "open",
    "start": "open",
    "begin": "open",
    "enter": "open",
    "engage": "open",
    "activate": "open",
    "initiate": "open",
    "close": "close",
    "hide": "close",
    "remove": "close",
    "dismiss": "close",
    "exit": "close",
    "clear": "clear"
}

MULTI_TARGET_ALIASES = (
    ("system_health", ("system health", "system status", "diagnostics", "diagnostic", "hardware status")),
    ("workshop_mode", ("workshop mode", "workshop")),
    ("showcase_mode", ("showcase mode", "showcase", "demo mode", "presentation mode")),
    ("sleep_screen", ("sleep screen", "sleep", "standby mode", "stand by mode")),
    ("files", ("virtual finder", "file manager", "finder", "files", "file")),
    ("notifications", ("notifications", "notification center", "alerts", "alert")),
    ("tasks", ("tasks", "task widget", "reminders", "mission queue")),
    ("intel", ("intel briefing", "news briefing", "briefing", "news", "intel")),
    ("calendar", ("calendar", "agenda")),
    ("weather", ("weather", "forecast")),
    ("music", ("music controls", "music widget", "music")),
    ("notes", ("sticky notes", "notes", "note")),
    ("map", ("tactical map", "maps", "map")),
    ("hud", ("hud", "workspace", "screen"))
)

MULTI_OPEN_ACTIONS = {
    "music": "open_music",
    "calendar": "open_calendar",
    "notes": "open_notes",
    "files": "open_files",
    "weather": "open_weather",
    "map": "open_map",
    "intel": "open_intel",
    "system_health": "open_system_health",
    "notifications": "open_notifications",
    "tasks": "open_tasks",
    "workshop_mode": "open_workshop_mode",
    "sleep_screen": "sleep_screen"
}

MULTI_TARGET_LABELS = {
    "music": "music",
    "calendar": "calendar",
    "notes": "notes",
    "files": "files",
    "weather": "weather",
    "map": "map",
    "intel": "news",
    "system_health": "system health",
    "notifications": "notifications",
    "tasks": "tasks",
    "workshop_mode": "workshop mode",
    "showcase_mode": "showcase mode",
    "sleep_screen": "sleep screen",
    "hud": "HUD"
}


def normalize_multi_target(target_text: str):
    value = normalize_command_text(target_text)
    value = re.sub(r"^(the|a|an|my|your)\s+", "", value).strip()
    value = re.sub(r"\b(widget|page|panel|module|app)\b", "", value).strip()
    value = re.sub(r"\s+", " ", value)

    if value in {"go to sleep", "go sleep"}:
        return "sleep_screen"

    for target, aliases in MULTI_TARGET_ALIASES:
        if any(value == alias or alias in value for alias in aliases):
            return target

    return None


def parse_multi_action_command(text: str) -> list:
    value = normalize_command_text(text)

    if not value or has_writing_intent(value):
        return []

    chained = bool(re.search(r"\b(and|then|after that|also)\b|,", value))
    value = re.sub(r"\s*,\s*and\s+", ", ", value)
    parts = [
        part.strip()
        for part in re.split(r"\s*(?:,|\band\b|\bthen\b|\bafter that\b|\balso\b)\s*", value)
        if part.strip()
    ]

    if not chained or len(parts) < 2:
        return []

    actions = []
    current_verb = None

    for part in parts:
        segment = re.sub(r"^(the|a|an)\s+", "", part).strip()
        verb = None
        target_text = segment

        if segment.startswith("go to sleep"):
            verb = "open"
            target_text = "sleep screen"
        else:
            for candidate, normalized_verb in MULTI_ACTION_VERBS.items():
                if segment == candidate or segment.startswith(f"{candidate} "):
                    verb = normalized_verb
                    target_text = segment[len(candidate):].strip()
                    break

        if verb:
            current_verb = verb
        else:
            verb = current_verb

        if not verb:
            continue

        target = normalize_multi_target(target_text)

        if not target:
            continue

        if verb == "clear" and target != "hud":
            verb = "close"

        actions.append({
            "verb": verb,
            "target": target
        })

    return actions if len(actions) >= 2 else []


def join_action_labels(labels: list) -> str:
    clean_labels = [str(label or "").strip() for label in labels if str(label or "").strip()]

    if not clean_labels:
        return ""

    if len(clean_labels) == 1:
        return clean_labels[0]

    if len(clean_labels) == 2:
        return f"{clean_labels[0]} and {clean_labels[1]}"

    return f"{', '.join(clean_labels[:-1])}, and {clean_labels[-1]}"


def summarize_multi_actions(results: list) -> str:
    if not results:
        return "Action complete"

    phrases = []
    current_status = None
    current_labels = []

    def flush_group():
        if not current_status or not current_labels:
            return

        labels = join_action_labels(current_labels)

        if current_status == "cleared":
            phrases.append(f"{labels} cleared")
        elif current_status == "opened":
            phrases.append(f"{labels} opened")
        elif current_status == "closed":
            phrases.append(f"{labels} closed")
        elif current_status == "engaged":
            phrases.append(f"{labels} engaged")
        else:
            phrases.append(f"{labels} {current_status}")

    for result in results:
        status = result.get("status")
        label = result.get("label")

        if status != current_status:
            flush_group()
            current_status = status
            current_labels = []

        current_labels.append(label)

    flush_group()

    if not phrases:
        return "Action complete"

    def sentence_start(text: str) -> str:
        if text.startswith("HUD"):
            return text

        return f"{text[:1].upper()}{text[1:]}" if text else text

    if len(phrases) == 1:
        return f"{sentence_start(phrases[0])}"

    if phrases[0].lower() == "hud cleared":
        return f"HUD cleared, {' and '.join(phrases[1:])}"

    return f"{sentence_start(' and '.join(phrases))}"


def close_multi_action_target(target: str) -> bool:
    if target == "hud":
        clear_hud_cards()
        return True

    if target == "calendar":
        remove_hud_card("calendar_agenda", broadcast=False)
        broadcast_to_hud("close_calendar_page", {})
        broadcast_state()
        return True

    if target == "music":
        remove_hud_card("music_controls", broadcast=False)
        broadcast_state()
        return True

    if target == "notes":
        remove_hud_card("sticky_notes", broadcast=False)
        broadcast_state()
        return True

    if target == "weather":
        remove_hud_card("weather_current", broadcast=False)
        broadcast_state()
        return True

    if target == "map":
        remove_hud_card("map_fullscreen", broadcast=False)
        broadcast_state()
        return True

    if target == "intel":
        remove_hud_card("news_briefing", broadcast=False)
        broadcast_state()
        return True

    if target == "files":
        remove_hud_card("virtual_finder", broadcast=False)
        broadcast_state()
        return True

    if target == "system_health":
        remove_hud_card("system_health", broadcast=False)
        broadcast_state()
        return True

    if target == "notifications":
        remove_hud_card("notification_center", broadcast=False)
        broadcast_to_hud("notification_center_close", {})
        broadcast_state()
        return True

    if target == "tasks":
        remove_hud_card("tasks_widget", broadcast=False)
        broadcast_to_hud("close_tasks_page", {})
        broadcast_state()
        return True

    if target == "workshop_mode":
        set_workshop_mode(False)
        return True

    if target == "showcase_mode":
        set_showcase_mode(False, broadcast=True)
        broadcast_to_hud("showcase_stop", {"return_sleep": False})
        return True

    return False


def execute_multi_actions(actions: list, input_source: str, is_voice: bool, silent: bool = False) -> bool:
    if not actions:
        return False

    results = []
    failures = 0

    for action in actions:
        verb = action.get("verb")
        target = action.get("target")
        label = MULTI_TARGET_LABELS.get(target, target or "module")
        handled = False
        status = "handled"

        if verb == "open":
            if target == "showcase_mode":
                set_showcase_mode(True, step="activation", broadcast=True)
                broadcast_to_hud("showcase_start", {"step": "activation"})
                handled = True
                status = "engaged"
            else:
                direct_action = MULTI_OPEN_ACTIONS.get(target)
                handled = perform_direct_action(direct_action) if direct_action else False
                status = "opened"
        elif verb == "close":
            handled = close_multi_action_target(target)
            status = "closed"
        elif verb == "clear" and target == "hud":
            handled = perform_direct_action("clear_hud")
            status = "cleared"

        if handled:
            results.append({
                "label": label,
                "status": status
            })
        else:
            failures += 1

    if not silent:
        if failures:
            respond_to_user("Some actions failed", input_source, is_voice)
        else:
            respond_to_user(summarize_multi_actions(results), input_source, is_voice)

    return bool(results)


def asks_complex_question(control_lower: str) -> bool:
    value = normalize_control_text(control_lower)

    if not value:
        return False

    if "?" in str(control_lower or ""):
        return True

    question_starts = (
        "why ",
        "how ",
        "what ",
        "what's ",
        "whats ",
        "when ",
        "where ",
        "who ",
        "which ",
        "can you explain ",
        "could you explain "
    )

    return value.startswith(question_starts)


def should_try_ollama_fast_router(control_lower: str, input_source: str) -> bool:
    if input_source in DIRECT_LAUNCHER_SOURCES or input_source == "workshop_chat":
        return False

    value = normalize_control_text(control_lower)

    if not value:
        return False

    if has_writing_intent(value):
        return False

    if len(value.split()) > 8:
        return False

    if value.startswith(("write", "draft", "compose", "explain", "summarize", "why", "how")):
        return False

    if asks_complex_question(value):
        return False

    action_hints = FAST_ACTION_TARGET_TERMS.union({
        "play",
        "pause",
        "resume",
        "next",
        "skip",
        "previous",
        "clear",
        "save",
        "close"
    })

    if not any(hint in value for hint in action_hints):
        return False

    return True


WORKSHOP_WRITING_INSTRUCTIONS = (
    "You are FRIDAY in Silent Operator text mode. Draft the requested message clearly and professionally. "
    "Do not be overly short. Do not mention system routing. Address the user as Boss only if natural. "
    "Preserve normal email formatting."
)

WORKSHOP_CHAT_INSTRUCTIONS = (
    "You are FRIDAY in Silent Operator text mode. Answer clearly in useful text. "
    "Use normal paragraphs and punctuation. Do not use voice, do not mention system routing, "
    "and do not force a one-sentence answer."
)


def workshop_writing_prompt(raw_input: str) -> str:
    return f"{WORKSHOP_WRITING_INSTRUCTIONS}\n\nUser request:\n{raw_input}"


def workshop_chat_prompt(raw_input: str) -> str:
    return f"{WORKSHOP_CHAT_INSTRUCTIONS}\n\nUser message:\n{raw_input}"


# ==========================================
# MEMORY — the only places main.py touches it
# ==========================================
# Three small functions, and nothing else in this file knows how memory works.
# Storage, ranking and phrasing all live in Core_Cognition/memory_manager.py.

# Spoken and typed turns are not persisted as chat sessions, so they share one
# conversation id. Workshop chats pass their own.
VOICE_CONVERSATION_ID = "voice_main"


def handle_memory_command(raw_input: str, conversation_id: str = "") -> str:
    """Answer an explicit memory instruction, or return "" to keep routing.

    Deliberately returns the reply instead of speaking it, so the caller decides
    how it is delivered — spoken, written into a chat, or handed to the voice
    layer as a tool result.

    Precedence is important and is enforced inside memory_manager:
      "remember to call Sam"      -> a task, left alone here
      "remember this" (bare)      -> the existing notes flow, unless the chat has
                                     a previous turn worth storing
      "remember that I use Vim"   -> a memory
    """
    intent = memory_manager.parse_memory_command(raw_input)

    if not intent:
        return ""

    try:
        return memory_manager.run_memory_command(intent, conversation_id=conversation_id)
    except Exception as error:
        print(f"{GRAY}[Memory command failed: {error}]{RESET}")
        return ""


# ------------------------------------------------------------------
# Background learning worker
# ------------------------------------------------------------------
# Memory v2.5 analyses every conversational turn, which is more work than v2's
# single regex sweep. None of it belongs on the response path, so turns are
# queued and processed by ONE daemon thread:
#
#   * the brain loop returns to listening the instant it has queued the turn
#   * one thread, so observations are processed in the order they were said and
#     no burst of speech can spawn a pile of threads
#   * a bounded queue, so if learning ever fell behind it would drop
#     observations rather than grow without limit
#
# Losing an observation costs nothing — the same fact said again will be picked
# up next time. Delaying a reply costs everything.
MEMORY_LEARNING_QUEUE: "queue.Queue" = queue.Queue(maxsize=64)


def _memory_learning_worker() -> None:
    while True:
        job = MEMORY_LEARNING_QUEUE.get()

        try:
            raw_input, conversation_id = job
            report = memory_manager.capture_candidates(
                raw_input,
                conversation_id=conversation_id
            )
            announce_memory_learning(report)
        except Exception as error:
            print(f"{GRAY}[Memory capture failed: {error}]{RESET}")
        finally:
            MEMORY_LEARNING_QUEUE.task_done()


threading.Thread(target=_memory_learning_worker, daemon=True).start()


def announce_memory_learning(report) -> None:
    """Tell the interface when something was ACTUALLY learned.

    Nothing is spoken. A memory forming in the background is a side effect of
    the conversation, not a turn in it, so it surfaces as a quiet toast and
    never interrupts. A report that stored nothing produces no toast at all —
    there are no notifications for internal state changing.
    """
    try:
        from Core_Cognition import memory_learning

        summary = memory_learning.learning_summary(report)
    except Exception:
        summary = None

    if not summary:
        return

    broadcast_memory_state()
    broadcast_to_hud("memory_learned", summary)

    if perf_enabled():
        print(f"[MEMORY] {summary['title']}: {summary['text'][:70]}")


def capture_memory_candidates(raw_input: str, conversation_id: str = "") -> None:
    """Queue a completed turn for the learning pipeline.

    Returns immediately. Everything expensive happens on the worker above,
    after the reply has already gone out.
    """
    text = str(raw_input or "").strip()

    if not text:
        return

    try:
        MEMORY_LEARNING_QUEUE.put_nowait((text, conversation_id))
    except queue.Full:
        # Learning is best-effort by design; a dropped observation is invisible
        # and self-correcting, whereas blocking here would not be.
        if perf_enabled():
            print("[MEMORY] learning queue full, observation dropped")


def memory_context_prompt(raw_input: str, conversation_id: str = "") -> str:
    """The spoken/typed brain's prompt for one turn, with memory attached.

    That model keeps its own short rolling history, so conversation turns are
    left out here — only long-term, project and working memory are added, and
    only when the request could actually benefit from them.
    """
    try:
        context = memory_manager.build_memory_context(
            raw_input,
            conversation_id=conversation_id,
            include_conversation=False
        )
    except Exception:
        return raw_input

    if not context.get("block"):
        return raw_input

    return (
        "Context FRIDAY remembers (use only what is relevant, never read it aloud as a list, "
        "never mention that it came from memory):\n"
        f"{context['block']}\n\n"
        f"Jon: {raw_input}"
    )


def is_explicit_intel_request(control_lower: str) -> bool:
    value = normalize_control_text(control_lower)
    explicit_terms = (
        "news",
        "headline",
        "briefing",
        "current event",
        "latest",
        "market",
        "markets",
        "stocks",
        "conflict",
        "war",
        "technology news",
        "kosovo news",
        "update me on",
        "what is happening in",
        "whats happening in"
    )

    return any(term in value for term in explicit_terms)


def handle_writing_intent(raw_input: str, input_source: str, is_voice: bool, chat) -> bool:
    control_lower = normalize_control_text(str(raw_input or "").lower())

    if not has_writing_intent(control_lower):
        return False

    started_at = time.time()
    response = chat.send_message(
        f"{raw_input}\n\nRoute note: this is a writing or message drafting request. "
        "Do not open news, widgets, tools, or routes. Draft the requested text directly."
    )
    perf_log("writing guard", started_at)
    final_text = response.text.strip() or "Draft unavailable"
    respond_to_user(final_text, input_source, is_voice)
    return True


def handle_workshop_chat_fast(raw_input: str, silent_model, silent_chat_state: dict, silent_chat_lock: threading.Lock):
    started_at = time.time()
    # Claim the conversation this turn belongs to ONCE, at the top.
    #
    # It is needed twice — to build that chat's own context, and to file the
    # reply against it — and take_pending_workshop_chat() is a one-shot read, so
    # taking it here and passing it down is what keeps a reply in the chat that
    # asked for it even if Jon switches tabs while waiting.
    chat_id = take_pending_workshop_chat()
    control_lower = normalize_control_text(str(raw_input or "").lower())
    exact_chat_actions = {
        "open calendar": "open_calendar",
        "open files": "open_files",
        "open notes": "open_notes",
        "clear hud": "clear_hud",
        "open music": "open_music",
        "open weather": "open_weather"
    }

    action = exact_chat_actions.get(control_lower)

    if action:
        action_started_at = time.time()
        perform_direct_action(action)
        perf_log("workshop chat direct action", action_started_at)
        append_workshop_chat_fast(
            "FRIDAY",
            action_event_text(action),
            {"source": "workshop_chat_response", "mode": "direct"},
            chat_id=chat_id
        )
        perf_log("workshop chat total", started_at)
        return

    # Explicit memory commands never reach here: the brain loop answers them
    # before any other routing, for both voice and Silent Operator.
    multi_actions = parse_multi_action_command(raw_input)

    if multi_actions:
        multi_started_at = time.time()
        execute_multi_actions(multi_actions, "workshop_chat", False)
        perf_log("workshop chat multi-action", multi_started_at)
        perf_log("workshop chat total", started_at)
        return

    perf_log("workshop chat queued", started_at)

    def run_silent_gemini() -> str:
        """One stateless request carrying THIS chat's own context.

        The old implementation kept a single google.generativeai chat object and
        reused it for every Silent Operator conversation, so switching chats did
        not switch history — chat B could see chat A's messages. History now
        comes from the named conversation via memory_manager, and the model call
        itself is stateless, which makes leakage between chats impossible rather
        than merely unlikely.
        """
        context_started_at = time.time()
        instructions = (
            WORKSHOP_WRITING_INSTRUCTIONS if has_writing_intent(control_lower) else WORKSHOP_CHAT_INSTRUCTIONS
        )
        prompt = memory_manager.build_prompt(
            raw_input,
            conversation_id=chat_id,
            instructions=instructions
        )
        perf_log("workshop chat memory context", context_started_at)

        # The lock now only serialises calls to one model object, not a shared
        # conversation, so two windows cannot interleave into one history.
        with silent_chat_lock:
            response = silent_model.generate_content(prompt)

        return response.text.strip() or "Action complete."

    def worker() -> None:
        gemini_started_at = time.time()
        executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        future = executor.submit(run_silent_gemini)

        try:
            final_text = future.result(timeout=20)
            perf_log("workshop chat gemini", gemini_started_at)
        except concurrent.futures.TimeoutError:
            final_text = "Request timed out, Gemini is slow."
            perf_log("workshop chat gemini timeout", gemini_started_at)
        except Exception as error:
            final_text = f"Silent Operator error, {error}"
            perf_log("workshop chat gemini error", gemini_started_at)
        finally:
            executor.shutdown(wait=False, cancel_futures=True)

        append_workshop_chat_fast(
            "FRIDAY",
            final_text,
            {"source": "workshop_chat_response", "mode": "gemini"},
            chat_id=chat_id
        )
        # Memory capture runs AFTER the reply is on screen, never before it, so
        # it can never add latency to an answer.
        capture_memory_candidates(raw_input, chat_id)
        perf_log("workshop chat total", started_at)

    threading.Thread(target=worker, daemon=True).start()


SETTINGS_OPEN_COMMANDS = {
    "open settings",
    "show settings",
    "settings",
    "open configuration",
    "open system settings",
    "open friday settings",
    "open jarvis settings"
}

SETTINGS_CLOSE_COMMANDS = {
    "close settings",
    "return to workstation",
    "close friday settings",
    "close jarvis settings",
    "exit settings"
}


def extract_theme_setting_command(control_lower: str):
    value = normalize_control_text(control_lower)
    value = re.sub(r"^(?:friday|jarvis)\s+", "", value).strip()

    def recognized_theme(candidate: str):
        theme_text = normalize_control_text(candidate)

        if "high contrast" in theme_text:
            return "high-contrast"

        if "tactical green" in theme_text or theme_text == "green":
            return "green"

        if "white mode" in theme_text or theme_text in {"white", "light", "light mode"}:
            return "white"

        if "midnight" in theme_text:
            return "midnight"

        if "graphite" in theme_text or theme_text in {"charcoal", "steel", "monochrome"}:
            return "graphite"

        if (
            "dark blue" in theme_text
            or "friday blue" in theme_text
            or "jarvis blue" in theme_text  # legacy name for the same theme
            or theme_text == "blue"
        ):
            return "blue"

        return None

    if value in {"enable high contrast", "turn on high contrast", "use high contrast"}:
        return "high-contrast"

    if value in {"switch to white mode", "use white mode"}:
        return "white"

    if value in {"use tactical green", "switch to tactical green"}:
        return "green"

    if value in {"switch to midnight", "use midnight"}:
        return "midnight"

    if value in {"switch to graphite", "use graphite", "graphite mode"}:
        return "graphite"

    patterns = (
        r"^(?:switch|change|set)\s+(?:the\s+)?theme\s+to\s+(.+)$",
        r"^(?:switch|change|set)\s+to\s+(.+?)\s+theme$",
        r"^use\s+(.+?)\s+theme$",
        r"^use\s+(.+)$",
        r"^go\s+back\s+to\s+(.+?)\s+theme$"
    )

    for pattern in patterns:
        match = re.match(pattern, value)

        if match:
            return recognized_theme(match.group(1).strip())

    return None


def extract_default_location_command(raw_input: str):
    value = re.sub(r"^\s*(?:friday|jarvis)\s+", "", str(raw_input or "").strip(), flags=re.IGNORECASE)
    value = re.sub(r"\s+", " ", value).strip()
    patterns = (
        r"^set default location to (.+)$",
        r"^change default location to (.+)$",
        r"^set weather location to (.+)$",
        r"^change weather location to (.+)$"
    )

    for pattern in patterns:
        match = re.match(pattern, value, flags=re.IGNORECASE)

        if match:
            return match.group(1).strip()

    return None


def settings_toggle_label(key: str) -> str:
    labels = {
        "show_notifications": "Notifications",
        "show_hotbar": "Hotbar",
        "show_task_widget_on_wake": "Task widget on wake",
        "silent_operator_enabled": "Silent Operator",
        "workshop_restore_layout": "Workshop restore layout",
        "showcase_return_to_sleep": "Showcase return to sleep",
        "privacy_mode": "Privacy mode",
        "presence_mode_enabled": "Presence mode",
        "presence_return_greeting_enabled": "Return greeting",
        "presence_auto_sleep_enabled": "Auto sleep when away",
        "presence_auto_launch_ui": "Presence auto launch UI",
        "presence_auto_open_workstation": "Presence auto open workstation"
    }
    return labels.get(key, key.replace("_", " ").title())


PRESENCE_ENABLE_COMMANDS = {
    "enable presence mode",
    "turn on presence mode"
}

PRESENCE_DISABLE_COMMANDS = {
    "disable presence mode",
    "turn off presence mode"
}

PRESENCE_STATUS_COMMANDS = {
    "am i detected",
    "presence status",
    "check presence",
    "are you detecting me",
    "refresh presence status"
}


def extract_presence_timeout_minutes(control_lower: str) -> Optional[int]:
    patterns = (
        r"^set away timeout to (\d+) minutes?$",
        r"^set presence timeout to (\d+) minutes?$",
        r"^wait (\d+) minutes? before sleeping$"
    )

    for pattern in patterns:
        match = re.match(pattern, control_lower)

        if match:
            try:
                return max(1, int(match.group(1)))
            except (TypeError, ValueError):
                return None

    return None


def presence_status_response() -> str:
    payload = refresh_presence_status_action()
    status = str(payload.get("user_presence") or "Unknown")

    if status == "Present":
        return "You are currently detected."

    if status == "Away":
        return "You are currently marked away."

    return "Presence status unknown."


def handle_presence_command(raw_input: str, input_source: str, is_voice: bool) -> bool:
    control_lower = normalize_control_text(str(raw_input or "").lower())
    control_lower = re.sub(r"^(?:friday|jarvis)\s+", "", control_lower).strip()

    if control_lower in PRESENCE_ENABLE_COMMANDS:
        set_boolean_setting("presence_mode_enabled", True)
        respond_to_user("Presence mode enabled", input_source, is_voice)
        return True

    if control_lower in PRESENCE_DISABLE_COMMANDS:
        set_boolean_setting("presence_mode_enabled", False)
        respond_to_user("Presence mode disabled", input_source, is_voice)
        return True

    if control_lower in PRESENCE_STATUS_COMMANDS:
        respond_to_user(presence_status_response(), input_source, is_voice)
        return True

    timeout_minutes = extract_presence_timeout_minutes(control_lower)

    if timeout_minutes is not None:
        saved_minutes = set_presence_idle_timeout_setting(timeout_minutes)
        respond_to_user(f"Away timeout set to {saved_minutes} minutes", input_source, is_voice)
        return True

    return_greeting_toggles = {
        "turn on return greeting": (True, "Return greeting enabled"),
        "turn off return greeting": (False, "Return greeting disabled"),
        "disable welcome back greeting": (False, "Return greeting disabled"),
        "enable welcome back greeting": (True, "Return greeting enabled")
    }

    if control_lower in return_greeting_toggles:
        enabled, message = return_greeting_toggles[control_lower]
        set_boolean_setting("presence_return_greeting_enabled", enabled)
        respond_to_user(message, input_source, is_voice)
        return True

    auto_sleep_toggles = {
        "enable auto sleep when away": (True, "Auto sleep when away enabled"),
        "disable auto sleep when away": (False, "Auto sleep when away disabled")
    }

    if control_lower in auto_sleep_toggles:
        enabled, message = auto_sleep_toggles[control_lower]
        set_boolean_setting("presence_auto_sleep_enabled", enabled)
        respond_to_user(message, input_source, is_voice)
        return True

    auto_workstation_toggles = {
        "enable auto open workstation": (True, "Auto open workstation enabled"),
        "disable auto open workstation": (False, "Auto open workstation disabled")
    }

    if control_lower in auto_workstation_toggles:
        enabled, message = auto_workstation_toggles[control_lower]
        set_boolean_setting("presence_auto_open_workstation", enabled)
        respond_to_user(message, input_source, is_voice)
        return True

    return False


def _camera_presence_timestamp_label(value) -> str:
    timestamp = _camera_presence_parse_iso(value)

    if not timestamp:
        return "none"

    return timestamp.strftime("%Y-%m-%d %H %M %S")


def _camera_presence_motion_label(value) -> str:
    try:
        score = float(value)
    except (TypeError, ValueError):
        score = 0.0

    if score.is_integer():
        return str(int(score))

    return f"{score:.2f}".replace(".", " point ")


def _camera_presence_greetings_label(payload: dict) -> str:
    greetings_enabled = (
        bool(payload.get("unknown_greeting_enabled", False))
        or bool(payload.get("primary_user_greeting_enabled", False))
    )
    return "enabled" if greetings_enabled else "disabled"


def _camera_presence_available_cameras_response() -> str:
    if not camera_presence_available():
        return "Camera presence unavailable."

    cameras = get_available_cameras(5)

    if not cameras:
        return "No available cameras found."

    labels = [f"index {index}" for index in cameras]

    if len(labels) == 1:
        camera_text = labels[0]
    else:
        camera_text = ", ".join(labels[:-1]) + f" and {labels[-1]}"

    return f"Available cameras, {camera_text}."


def camera_presence_status_response() -> str:
    payload = build_camera_presence_payload()
    enabled = "enabled" if payload.get("enabled") else "disabled"
    mode = str(payload.get("mode") or "IDLE").replace("_", " ").title()
    active = str(payload.get("active_camera_label") or "None")
    door_index = payload.get("door_camera_index", 1)
    desk_index = payload.get("desk_camera_index", 0)
    motion_score = _camera_presence_motion_label(payload.get("last_motion_score", 0))
    availability = str(payload.get("camera_status") or payload.get("status") or "Unavailable").lower()

    return (
        f"Camera presence is {enabled}, mode is {mode}, active camera is {active}, "
        f"door index {door_index}, desk index {desk_index}, "
        f"last door detection {_camera_presence_timestamp_label(payload.get('last_door_seen_at'))}, "
        f"last desk detection {_camera_presence_timestamp_label(payload.get('last_desk_seen_at'))}, "
        f"last motion score {motion_score}, greetings are {_camera_presence_greetings_label(payload)}, "
        f"camera status is {availability}."
    )


def _camera_presence_set_index(key: str, value) -> int:
    try:
        index = int(float(value))
    except (TypeError, ValueError):
        index = 0

    index = max(0, min(index, 20))
    settings = set_setting(key, index)
    sync_settings_state(settings, broadcast=True)
    state = load_camera_presence_state()

    if key == "camera_presence_door_device_index":
        state["door_camera_index"] = index
    else:
        state["desk_camera_index"] = index

    save_camera_presence_state(state)
    _broadcast_camera_presence_status()
    return index


def _camera_presence_test_response(role: str) -> str:
    detection = _camera_presence_test_role(role)

    if not detection.get("available"):
        return "Camera presence unavailable."

    if role == CAMERA_ROLE_DOOR:
        return "Door camera detected motion." if detection.get("detected") else "Door camera did not detect motion."

    return "Desk camera detected presence." if detection.get("detected") else "Desk camera did not detect presence."


def handle_camera_presence_command(raw_input: str, input_source: str, is_voice: bool) -> bool:
    control_lower = normalize_control_text(str(raw_input or "").lower())
    control_lower = re.sub(r"^(?:friday|jarvis)\s+", "", control_lower).strip()

    if control_lower in {"enable camera presence", "turn on camera presence"}:
        if not camera_presence_available():
            respond_to_user("Camera presence unavailable.", input_source, is_voice)
            return True

        set_boolean_setting("camera_presence_enabled", True)
        force_camera_presence_mode(CAMERA_MODE_DOOR_WATCH, load_settings())
        _broadcast_camera_presence_status()
        respond_to_user("Camera presence enabled.", input_source, is_voice)
        return True

    if control_lower in {"disable camera presence", "turn off camera presence"}:
        set_boolean_setting("camera_presence_enabled", False)
        force_camera_presence_mode("IDLE", load_settings())
        _broadcast_camera_presence_status()
        respond_to_user("Camera presence disabled.", input_source, is_voice)
        return True

    if control_lower in {"camera presence status", "check camera presence", "camera status"}:
        respond_to_user(camera_presence_status_response(), input_source, is_voice)
        return True

    if control_lower in {"identify cameras", "list cameras", "identify camera", "list camera indexes"}:
        respond_to_user(_camera_presence_available_cameras_response(), input_source, is_voice)
        return True

    if control_lower in {"test door camera", "camera presence test door", "test room camera"}:
        respond_to_user(_camera_presence_test_response(CAMERA_ROLE_DOOR), input_source, is_voice)
        return True

    if control_lower in {"test desk camera", "camera presence test desk", "test workstation camera"}:
        respond_to_user(_camera_presence_test_response(CAMERA_ROLE_DESK), input_source, is_voice)
        return True

    door_match = re.match(r"^(?:set\s+)?door camera (?:to|index to)?\s*(\d+)$", control_lower)

    if door_match:
        index = _camera_presence_set_index("camera_presence_door_device_index", door_match.group(1))
        respond_to_user(f"Door camera set to {index}.", input_source, is_voice)
        return True

    desk_match = re.match(r"^(?:set\s+)?desk camera (?:to|index to)?\s*(\d+)$", control_lower)

    if desk_match:
        index = _camera_presence_set_index("camera_presence_desk_device_index", desk_match.group(1))
        respond_to_user(f"Desk camera set to {index}.", input_source, is_voice)
        return True

    if control_lower == "reset camera presence":
        reset_camera_presence_state(load_settings())
        _broadcast_camera_presence_status()
        respond_to_user("Camera presence reset.", input_source, is_voice)
        return True

    if control_lower == "switch camera presence to door watch":
        _camera_presence_set_mode(CAMERA_MODE_DOOR_WATCH)
        respond_to_user("Camera presence is in Door Watch.", input_source, is_voice)
        return True

    if control_lower == "switch camera presence to desk watch":
        _camera_presence_set_mode(CAMERA_MODE_DESK_WATCH)
        respond_to_user("Camera presence is in Desk Watch.", input_source, is_voice)
        return True

    return False


def handle_settings_command(raw_input: str, input_source: str, is_voice: bool) -> bool:
    control_lower = normalize_control_text(str(raw_input or "").lower())
    control_lower = re.sub(r"^(?:friday|jarvis)\s+", "", control_lower).strip()

    if handle_camera_presence_command(raw_input, input_source, is_voice):
        return True

    if handle_presence_command(raw_input, input_source, is_voice):
        return True

    if control_lower in SETTINGS_OPEN_COMMANDS:
        open_settings_page()
        acknowledge_action("open_settings", input_source, is_voice)
        return True

    if control_lower in SETTINGS_CLOSE_COMMANDS:
        close_settings_page()
        acknowledge_action("close_settings", input_source, is_voice)
        return True

    theme = extract_theme_setting_command(control_lower)

    if theme:
        label = set_theme_setting(theme)
        respond_to_user(f"Theme set to {label}", input_source, is_voice)
        return True

    if control_lower in {"disable voice", "turn off voice", "turn voice off"}:
        voice_was_enabled = bool(get_setting("voice_enabled", True))

        if is_voice and voice_was_enabled:
            respond_to_user("Voice disabled", input_source, is_voice)
            set_voice_enabled(False)
        else:
            set_voice_enabled(False)
            respond_to_user("Voice disabled", input_source, is_voice)

        return True

    if control_lower in {"enable voice", "turn voice back on", "turn on voice", "turn voice on"}:
        set_voice_enabled(True)
        respond_to_user("Voice enabled", input_source, is_voice)
        return True

    if control_lower in {"turn off performance logs", "disable performance logs"}:
        set_performance_logs_enabled(False)
        respond_to_user("Performance logs disabled", input_source, is_voice)
        return True

    if control_lower in {"turn on performance logs", "enable performance logs"}:
        set_performance_logs_enabled(True)
        respond_to_user("Performance logs enabled", input_source, is_voice)
        return True

    setting_toggles = {
        "turn off notifications": ("show_notifications", False, "Notifications disabled"),
        "disable notifications": ("show_notifications", False, "Notifications disabled"),
        "turn on notifications": ("show_notifications", True, "Notifications enabled"),
        "enable notifications": ("show_notifications", True, "Notifications enabled"),
        "hide hotbar": ("show_hotbar", False, "Hotbar hidden"),
        "turn off hotbar": ("show_hotbar", False, "Hotbar hidden"),
        "show hotbar": ("show_hotbar", True, "Hotbar shown"),
        "turn on hotbar": ("show_hotbar", True, "Hotbar shown"),
        "hide tasks on wake": ("show_task_widget_on_wake", False, "Task widget on wake disabled"),
        "turn off task widget on wake": ("show_task_widget_on_wake", False, "Task widget on wake disabled"),
        "show tasks on wake": ("show_task_widget_on_wake", True, "Task widget on wake enabled"),
        "turn on task widget on wake": ("show_task_widget_on_wake", True, "Task widget on wake enabled")
    }

    if control_lower in setting_toggles:
        key, enabled, message = setting_toggles[control_lower]
        set_boolean_setting(key, enabled)
        respond_to_user(message, input_source, is_voice)
        return True

    if control_lower in {"start in sleep mode", "startup in sleep mode", "start on sleep screen"}:
        set_startup_mode_setting("sleep")
        respond_to_user("Startup mode set to Sleep", input_source, is_voice)
        return True

    if control_lower in {"start in workstation mode", "startup in workstation mode", "start on workstation", "start in workstation"}:
        set_startup_mode_setting("workstation")
        respond_to_user("Startup mode set to Workstation", input_source, is_voice)
        return True

    location = extract_default_location_command(raw_input)

    if location:
        saved_location = set_default_location_setting(location)
        respond_to_user(f"Default location set to {saved_location}", input_source, is_voice)
        return True

    return False


def extract_workspace_widget_request(control_lower: str):
    value = normalize_control_text(control_lower)

    if has_writing_intent(value):
        return None

    workspace = None
    workspace_patterns = {
        "secondary": (
            "in the secondary",
            "in secondary",
            "secondary workspace",
            "secondary work",
            "secondary workstation",
            "left workspace",
            "left workstation",
            "left monitor",
            "on the left",
            "on left"
        ),
        "main": (
            "center monitor",
            "on the center",
            "main workspace",
            "main workstation",
            "center workspace",
            "center workstation",
            "primary workspace"
        )
    }

    for target, phrases in workspace_patterns.items():
        if any(phrase in value for phrase in phrases):
            workspace = target
            break

    if not workspace and " in workshop" not in value and " workshop" not in value:
        return None

    if not any(prefix in value for prefix in ("open ", "show ", "launch ")):
        return None

    widget_aliases = (
        ("system_health", ("system health", "diagnostics", "diagnostic", "hardware status")),
        ("virtual_finder", ("virtual finder", "file manager", "files")),
        ("notifications", ("notifications", "notification center", "alerts")),
        ("tasks", ("tasks", "task widget", "reminders", "mission queue")),
        ("calendar", ("calendar", "agenda")),
        ("weather", ("weather", "forecast")),
        ("music", ("music", "song", "track")),
        ("notes", ("notes", "sticky notes")),
        ("intel", ("intel", "news", "briefing")),
        ("map", ("map", "tactical map"))
    )

    for widget, aliases in widget_aliases:
        if any(alias in value for alias in aliases):
            return widget, workspace

    return None


def open_widget_in_workspace(widget_type: str, workspace: Optional[str] = None) -> str:
    widget = str(widget_type or "").lower()
    target = workspace if workspace in {"main", "secondary"} else None
    workshop_active = bool(get_hud_state_snapshot().get("workshop_mode", {}).get("active"))

    if widget in {"tasks", "task_widget", "reminders"}:
        create_tasks_widget(preferred_workspace=target, activate_workstation=not workshop_active)
        return "Tasks widget opened"

    if widget == "weather":
        create_weather_widget(current_default_location(), use_cache=True, refresh_async=True)
        if target:
            set_hud_card_workspace("weather_current", target)
        return action_event_text("open_weather")

    if widget == "music":
        if not hud_card_is_open("music_controls"):
            create_music_widget(use_cache=True, refresh_async=True)
        if target:
            set_hud_card_workspace("music_controls", target)
        return action_event_text("open_music")

    if widget == "calendar":
        if workshop_active or target:
            seed_context_cards("calendar", replace_existing=False)

            if target:
                set_hud_card_workspace("calendar_agenda", target)
        else:
            open_calendar_page(preferred_workspace=target, use_cache=True, refresh_async=True)

        return action_event_text("open_calendar")

    if widget == "notes":
        create_notes_widget()
        if target:
            set_hud_card_workspace("sticky_notes", target)
        return action_event_text("open_notes")

    if widget == "map":
        open_map_widget("Planet Earth", preferred_workspace=target)
        return action_event_text("open_map")

    if widget == "intel":
        create_intel_widget(
            "briefing",
            use_cache=True,
            refresh_async=True,
            preferred_workspace=target
        )
        return "Intel opened"

    if widget == "system_health":
        create_system_health_widget(use_cache=True, refresh_async=True)
        if target:
            set_hud_card_workspace("system_health", target)
        return action_event_text("open_system_health")

    if widget in {"virtual_finder", "files"}:
        create_virtual_finder_widget(preferred_workspace=target)
        return action_event_text("open_files")

    if widget == "notifications":
        create_notification_center_card()
        return "Notification center opened"

    if widget in {"news", "intel"}:
        create_intel_widget(
            "briefing",
            use_cache=True,
            refresh_async=True,
            preferred_workspace=target
        )
        return "Intel opened"

    if widget == "settings":
        create_settings_widget(preferred_workspace=target, activate_workstation=not workshop_active)
        return action_event_text("open_settings")

    return "Widget unavailable"


def active_mode_label() -> str:
    state = get_hud_state_snapshot()

    if state.get("showcase_mode", {}).get("active"):
        return "Showcase"

    if state.get("workshop_mode", {}).get("active"):
        return "Workshop"

    mode = str(state.get("hud_mode") or "ACTIVE").upper()

    if mode == "SLEEP":
        return "Sleep"

    if mode == "OFFLINE":
        return "Offline"

    return "Workstation"


# The next-event lookup is a live Google Calendar API call with no timeout of
# its own. It used to run inline while building the Settings payload, so a slow
# or unreachable network blocked opening Settings for as long as the request
# took (measured at 60 s here). The value is now served from a short-lived cache
# and refreshed on a background thread, so Settings always renders immediately.
CALENDAR_SUMMARY_TTL_SECONDS = 120.0
_calendar_summary_cache = {"at": 0.0, "value": "", "refreshing": False}
_calendar_summary_lock = threading.Lock()


def _compute_next_event_summary() -> str:
    if not is_calendar_connected():
        if get_last_calendar_error() == "Reauthorization required":
            return "Reauthorization required"

        return "Calendar not connected"

    try:
        event = get_next_event()
    except Exception:
        return "Unavailable"

    if get_last_calendar_error():
        return "Unavailable"

    if not event:
        return "No upcoming events"

    title = str(event.get("title") or "Untitled event").strip()
    when = format_event_date(event)
    start = format_event_start(event)

    if str(start).lower() == "all day":
        return f"{title} on {when}"

    return f"{title} on {when} at {start}"


def _refresh_calendar_summary_async() -> None:
    with _calendar_summary_lock:
        if _calendar_summary_cache["refreshing"]:
            return

        _calendar_summary_cache["refreshing"] = True

    def worker() -> None:
        try:
            value = _compute_next_event_summary()
        except Exception:
            value = "Unavailable"

        with _calendar_summary_lock:
            _calendar_summary_cache["value"] = value
            _calendar_summary_cache["at"] = time.time()
            _calendar_summary_cache["refreshing"] = False

        try:
            broadcast_settings_payload()
        except Exception:
            pass

    threading.Thread(target=worker, daemon=True).start()


def next_event_settings_summary() -> str:
    """Never blocks. Returns the cached summary and refreshes it in the background."""
    with _calendar_summary_lock:
        value = _calendar_summary_cache["value"]
        age = time.time() - _calendar_summary_cache["at"]

    if not value or age > CALENDAR_SUMMARY_TTL_SECONDS:
        _refresh_calendar_summary_async()

    return value or "Checking..."


def calendar_connected_for_display() -> bool:
    """
    Cached connectivity for the Settings widget only.

    When the calendar is disconnected, get_calendar_service() retries the auth
    path on every call, which can include a network token refresh. Voice and
    command routes still call is_calendar_connected() directly so they always
    see live state.
    """
    with _calendar_summary_lock:
        cached = _calendar_summary_cache.get("connected")
        age = time.time() - _calendar_summary_cache.get("connected_at", 0.0)

    if cached is not None and age <= CALENDAR_SUMMARY_TTL_SECONDS:
        return bool(cached)

    def worker() -> None:
        try:
            connected = bool(is_calendar_connected())
        except Exception:
            connected = False

        with _calendar_summary_lock:
            _calendar_summary_cache["connected"] = connected
            _calendar_summary_cache["connected_at"] = time.time()

    threading.Thread(target=worker, daemon=True).start()
    return bool(cached)


def get_settings_status_payload() -> dict:
    settings = load_settings()
    presence_payload = build_presence_payload()
    camera_presence_payload = build_camera_presence_payload()
    state = get_hud_state_snapshot()
    workshop_state = state.get("workshop_mode", {}) if isinstance(state.get("workshop_mode"), dict) else {}
    showcase_state = state.get("showcase_mode", {}) if isinstance(state.get("showcase_mode"), dict) else {}
    credentials_path = os.path.join(os.path.dirname(__file__), "calendar_credentials.json")
    token_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "Data", "calendar_token.json")
    explicit_ollama = bool((os.getenv("FRIDAY_OLLAMA_URL") or os.getenv("JARVIS_OLLAMA_URL", "")).strip())
    # Live health from the audio engine rather than a guess from which env keys
    # happen to be set, so Settings shows whether Fish is actually working.
    fish_health = fish_status()
    fish_label = fish_health.get("label", "Unknown")

    if fish_health.get("state") == "backoff" and fish_health.get("retry_in_seconds"):
        fish_label = "{0} (retry in {1}s)".format(fish_label, fish_health["retry_in_seconds"])

    return {
        "settings": settings,
        "theme": settings.get("theme", "blue"),
        "theme_label": theme_label(settings.get("theme")),
        "port": 5050,
        "python_version": sys.version.split()[0],
        "electron_ui": "Online",
        "active_mode": active_mode_label(),
        "performance_logs": bool(settings.get("performance_logs", True)),
        "show_notifications": bool(settings.get("show_notifications", True)),
        "show_hotbar": bool(settings.get("show_hotbar", True)),
        "show_task_widget_on_wake": bool(settings.get("show_task_widget_on_wake", False)),
        "silent_operator_enabled": bool(settings.get("silent_operator_enabled", True)),
        "workshop_restore_layout": bool(settings.get("workshop_restore_layout", True)),
        "showcase_return_to_sleep": bool(settings.get("showcase_return_to_sleep", True)),
        "privacy_mode": bool(settings.get("privacy_mode", False)),
        "startup_mode": settings.get("startup_mode") or "sleep",
        "presence": presence_payload,
        "presence_mode": presence_payload.get("presence_mode", "Disabled"),
        "presence_mode_enabled": bool(settings.get("presence_mode_enabled", False)),
        "presence_source": presence_payload.get("presence_source", "Mac idle"),
        "presence_user": presence_payload.get("user_presence", "Unknown"),
        "presence_idle_seconds": presence_payload.get("idle_seconds"),
        "presence_away_timeout_minutes": presence_payload.get("away_timeout_minutes", 10),
        "presence_last_seen": presence_payload.get("last_seen_at"),
        "presence_away_since": presence_payload.get("away_since"),
        "presence_return_greeting_enabled": bool(settings.get("presence_return_greeting_enabled", True)),
        "presence_auto_sleep_enabled": bool(settings.get("presence_auto_sleep_enabled", True)),
        "presence_auto_launch_ui": bool(settings.get("presence_auto_launch_ui", False)),
        "presence_auto_open_workstation": bool(settings.get("presence_auto_open_workstation", False)),
        "presence_quiet_hours_enabled": bool(settings.get("presence_quiet_hours_enabled", False)),
        "camera_presence": camera_presence_payload,
        "camera_presence_enabled": bool(settings.get("camera_presence_enabled", False)),
        "camera_presence_mode": camera_presence_payload.get("mode", "IDLE"),
        "camera_presence_active_camera": camera_presence_payload.get("active_camera_label", "None"),
        "camera_presence_door_device_index": camera_presence_payload.get("door_camera_index", 1),
        "camera_presence_desk_device_index": camera_presence_payload.get("desk_camera_index", 0),
        "camera_presence_last_door_seen_at": camera_presence_payload.get("last_door_seen_at"),
        "camera_presence_last_desk_seen_at": camera_presence_payload.get("last_desk_seen_at"),
        "camera_presence_last_camera_switch_at": camera_presence_payload.get("last_camera_switch_at"),
        "camera_presence_last_motion_score": camera_presence_payload.get("last_motion_score", 0),
        "camera_presence_status": camera_presence_payload.get("camera_status", "Unavailable"),
        "camera_presence_opencv_available": bool(camera_presence_payload.get("opencv_available", False)),
        "camera_presence_check_interval_seconds": camera_presence_payload.get("check_interval_seconds", 10),
        "camera_presence_unknown_greeting_enabled": bool(camera_presence_payload.get("unknown_greeting_enabled", False)),
        "camera_presence_primary_user_greeting_enabled": bool(camera_presence_payload.get("primary_user_greeting_enabled", False)),
        "camera_presence_auto_handoff_enabled": bool(camera_presence_payload.get("auto_handoff_enabled", False)),
        "gemini_api": "Connected" if GEMINI_API_KEY else "Missing",
        "main_model": "gemini-3-flash-preview",
        "silent_operator_model": settings.get("silent_operator_model") or SILENT_MODEL_NAME,
        "ollama": "Offline" if explicit_ollama else "Not configured",
        "fast_router": "Enabled" if explicit_ollama else "Disabled",
        "voice_enabled": bool(settings.get("voice_enabled", True)),
        "fish_audio": fish_label,
        "fish_state": fish_health.get("state", "unknown"),
        "fish_retry_in_seconds": fish_health.get("retry_in_seconds", 0),
        "macos_fallback_voice": resolve_local_voice() or "System default",
        "microphone": "Listening" if str(state.get("voice_phase") or state.get("ai_status") or "").upper() in {"LISTENING", "USER_SPEAKING"} else "Idle",
        "system_volume_control": "Disabled",
        "voice_provider": settings.get("voice_provider", "local"),
        "voice_provider_label": describe_voice_provider(),
        "assistant_name": settings.get("assistant_name", "FRIDAY"),
        "voice_local_name": settings.get("voice_local_name", ""),
        "voice_local_name_resolved": resolve_local_voice() or "System default",
        "voice_rate": settings.get("voice_rate", 178),
        "voice_pause_threshold": settings.get("voice_pause_threshold", 0.6),
        "voice_listen_timeout": settings.get("voice_listen_timeout", 8),
        "voice_follow_up_window_seconds": settings.get("voice_follow_up_window_seconds", 10),
        "voice_echo_settle_ms": settings.get("voice_echo_settle_ms", 220),
        "voice_audio_reactive_orb": bool(settings.get("voice_audio_reactive_orb", True)),
        "voice_performance_logs": bool(settings.get("voice_performance_logs", True)),
        "voice_interrupt_enabled": bool(settings.get("voice_interrupt_enabled", True)),
        "voice_phase": str(state.get("voice_phase") or "IDLE"),
        "fish_configured": bool(fish_configured()),
        "calendar_connected": bool(calendar_connected_for_display()),
        "calendar_credentials": "Found" if os.path.exists(credentials_path) else "Missing",
        "calendar_token": "Present" if os.path.exists(token_path) else "Missing",
        "next_event_summary": next_event_settings_summary(),
        "default_location": settings.get("default_location") or current_default_location(),
        "face_id_enabled": bool(FACE_ID_ENABLED),
        "workshop_active": bool(workshop_state.get("active")),
        "workshop_available": True,
        "showcase_active": bool(showcase_state.get("active")),
        "showcase_available": True,
        "layout_saved": bool(workshop_state.get("layout_saved_at")),
        "display_count": int(workshop_state.get("display_count") or len(workshop_state.get("displays") or []) or 0),
        "guest_mode": "Disabled",
        "desk_view": "Disabled",
        "onshape": "Disabled",
        "api_keys": "Hidden",
        "local_files": "Safe root only",
        "health_check_status": LAST_HEALTH_CHECK.get("status", "Not run"),
        "health_check_summary": LAST_HEALTH_CHECK.get("summary", "Health check has not run in this session"),
        "build": "FRIDAY MK1"
    }


def broadcast_settings_payload() -> dict:
    payload = get_settings_status_payload()
    broadcast_to_hud("settings_payload_update", payload)
    return payload


def open_settings_page() -> None:
    workshop_active = bool(get_hud_state_snapshot().get("workshop_mode", {}).get("active"))
    create_settings_widget(activate_workstation=not workshop_active)


def create_settings_widget(
    preferred_workspace: Optional[str] = None,
    activate_workstation: bool = True
) -> None:
    if activate_workstation:
        set_hud_mode("ACTIVE", orb_position="DOCKED_BOTTOM_RIGHT", broadcast=False)

    sync_settings_state(load_settings(), broadcast=False)
    payload = get_settings_status_payload()
    add_hud_card(
        card_id="settings_widget",
        title="SETTINGS",
        card_type="settings",
        x=760,
        y=78,
        width=620,
        height=560,
        broadcast=False,
        data=payload,
        preferred_workspace=preferred_workspace
    )

    broadcast_state()
    broadcast_settings_payload()


def close_settings_page() -> None:
    set_hud_mode("ACTIVE", orb_position="DOCKED_BOTTOM_RIGHT", broadcast=False)
    remove_hud_card("settings_widget", broadcast=False)
    broadcast_state()
    broadcast_to_hud("close_settings_page", {})


def broadcast_tasks_payload() -> dict:
    payload = build_tasks_payload()
    broadcast_to_hud("tasks_payload_update", payload)
    return payload


def create_tasks_widget(
    preferred_workspace: Optional[str] = None,
    activate_workstation: bool = True
) -> None:
    payload = build_tasks_payload()

    if activate_workstation:
        set_hud_mode("ACTIVE", orb_position="DOCKED_BOTTOM_RIGHT", broadcast=False)

    add_hud_card(
        card_id="tasks_widget",
        title="TASKS",
        card_type="tasks",
        x=1180,
        y=92,
        width=380,
        height=360,
        broadcast=False,
        data=payload
    )

    if preferred_workspace in {"main", "secondary"}:
        set_hud_card_workspace("tasks_widget", preferred_workspace, broadcast=False)

    broadcast_state()
    broadcast_tasks_payload()


def refresh_tasks_surfaces() -> None:
    if hud_card_is_open("tasks_widget"):
        add_hud_card(
            card_id="tasks_widget",
            title="TASKS",
            card_type="tasks",
            x=1180,
            y=92,
            width=380,
            height=360,
            broadcast=False,
            data=build_tasks_payload()
        )
        broadcast_state()

    broadcast_tasks_payload()


def open_tasks_page() -> None:
    set_hud_mode("ACTIVE", orb_position="DOCKED_BOTTOM_RIGHT", broadcast=False)
    broadcast_state()
    broadcast_to_hud("open_tasks_page", build_tasks_payload())


def close_tasks_page() -> None:
    set_hud_mode("ACTIVE", orb_position="DOCKED_BOTTOM_RIGHT", broadcast=False)
    broadcast_state()
    broadcast_to_hud("close_tasks_page", {})


def close_tasks_widget() -> None:
    remove_hud_card("tasks_widget", broadcast=False)
    broadcast_state()
    broadcast_tasks_payload()


def apply_task_widget_on_workstation() -> None:
    if bool(get_setting("show_task_widget_on_wake", False)):
        create_tasks_widget(activate_workstation=False)
        return

    if hud_card_is_open("tasks_widget"):
        remove_hud_card("tasks_widget", broadcast=False)

    broadcast_state()


def activate_workstation_home(prompt: str = "") -> None:
    set_hud_mode("ACTIVE", orb_position="DOCKED_BOTTOM_RIGHT", prompt=prompt, broadcast=False)
    apply_task_widget_on_workstation()


def activate_sleep_standby(prompt: str = "") -> None:
    set_hud_mode("SLEEP", orb_position="CENTER", prompt=prompt or get_sleep_prompt())


def apply_startup_mode(startup_prompt: str) -> None:
    settings = load_settings()
    sync_settings_state(settings, broadcast=False)

    if str(settings.get("startup_mode") or "sleep").lower() == "workstation":
        set_hud_mode("ACTIVE", orb_position="DOCKED_BOTTOM_RIGHT", prompt=startup_prompt, broadcast=False)
        apply_task_widget_on_workstation()
        return

    set_hud_mode("SLEEP", orb_position="CENTER", prompt=startup_prompt, broadcast=False)

    if hud_card_is_open("tasks_widget"):
        remove_hud_card("tasks_widget", broadcast=False)

    broadcast_state()


def task_due_response(due_at: str) -> str:
    if not due_at:
        return ""

    try:
        due = datetime.datetime.fromisoformat(str(due_at))
    except Exception:
        return ""

    now = datetime.datetime.now()
    tomorrow = (now + datetime.timedelta(days=1)).date()
    date_label = due.strftime("%A")

    if due.date() == now.date():
        date_label = "today"
    elif due.date() == tomorrow:
        date_label = "tomorrow"

    return f"{date_label} at {due.strftime('%-I:%M %p').replace(':00 ', ' ')}"


def clean_task_title_candidate(text: str) -> str:
    value = str(text or "").strip()
    value = re.sub(r"^\s*(?:friday|jarvis)\s+", "", value, flags=re.IGNORECASE).strip()
    value = re.sub(r"^(?:please\s+)?", "", value, flags=re.IGNORECASE).strip()
    patterns = (
        r"^task\s+remind\s+me\s+to\s+",
        r"^remind\s+me\s+to\s+",
        r"^add\s+(?:a\s+)?task\s+",
        r"^create\s+(?:a\s+)?task\s+",
        r"^remember\s+to\s+",
        r"^don['’]?t\s+let\s+me\s+forget\s+to\s+",
        r"^dont\s+let\s+me\s+forget\s+to\s+"
    )

    for pattern in patterns:
        value = re.sub(pattern, "", value, flags=re.IGNORECASE).strip()

    return re.sub(r"\s+", " ", value).strip(" .,")


def split_task_priority(text: str):
    value = str(text or "").strip()
    priority = "normal"
    patterns = (
        ("urgent", r"\burgent\s+(?:priority\s+)?"),
        ("high", r"\bhigh\s+priority\s+"),
        ("low", r"\blow\s+priority\s+")
    )

    for next_priority, pattern in patterns:
        if re.search(pattern, value, flags=re.IGNORECASE):
            priority = next_priority
            value = re.sub(pattern, "", value, flags=re.IGNORECASE).strip()
            break

    return priority, value


def add_task_from_text(raw_input: str, input_source: str):
    candidate = clean_task_title_candidate(raw_input)
    priority, candidate = split_task_priority(candidate)
    due_at, title = parse_due_datetime_from_text(candidate)
    title = clean_task_title_candidate(title)

    if not title:
        return False, "Task title missing."

    result = add_task(title, due_at=due_at, priority=priority, source=input_source)

    if not result.get("ok"):
        return False, "Task title missing."

    refresh_tasks_surfaces()

    if due_at and any(phrase in normalize_control_text(raw_input) for phrase in ["remind me", "remember to", "forget"]):
        due_text = task_due_response(due_at)
        return True, f"Reminder set for {due_text}" if due_text else "Reminder set"

    return True, "Task added"


TASKS_OPEN_COMMANDS = {
    "open tasks",
    "show tasks",
    "tasks",
    "open reminders",
    "show reminders",
    "reminders"
}

TASK_WIDGET_OPEN_COMMANDS = {
    "open task widget",
    "show task widget",
    "open tasks widget",
    "show tasks widget",
    "open reminder widget",
    "open reminders widget",
    "show reminders widget"
}

TASK_WIDGET_CLOSE_COMMANDS = {
    "hide tasks",
    "close tasks",
    "close task widget",
    "hide reminders",
    "close reminders",
    "close reminder widget"
}


def handle_tasks_command(raw_input: str, input_source: str, is_voice: bool) -> bool:
    control_lower = normalize_control_text(str(raw_input or "").lower())

    if control_lower in TASKS_OPEN_COMMANDS:
        open_tasks_page()
        acknowledge_action("open_tasks", input_source, is_voice)
        return True

    if control_lower in TASK_WIDGET_OPEN_COMMANDS:
        create_tasks_widget()
        acknowledge_action("open_task_widget", input_source, is_voice)
        return True

    if control_lower in TASK_WIDGET_CLOSE_COMMANDS:
        close_tasks_widget()
        close_tasks_page()
        respond_to_user("Tasks hidden", input_source, is_voice)
        return True

    if control_lower in {"clear completed tasks", "remove completed tasks"}:
        count = clear_completed_tasks()
        refresh_tasks_surfaces()
        respond_to_user(f"Cleared {count} completed task{'s' if count != 1 else ''}", input_source, is_voice)
        return True

    view_commands = {
        "what tasks do i have",
        "what reminders do i have",
        "what do i need to do today",
        "what is due today",
        "what's due today",
        "what is overdue",
        "what's overdue"
    }

    if control_lower in view_commands:
        payload = build_tasks_payload()
        counts = payload.get("counts", {})

        if "overdue" in control_lower:
            count = int(counts.get("overdue") or 0)
            message = f"{count} overdue task{'s' if count != 1 else ''}" if count else "No overdue tasks"
        elif "today" in control_lower:
            count = int(counts.get("today") or 0)
            message = f"You have {count} task{'s' if count != 1 else ''} today" if count else "No tasks due today"
        else:
            message = format_tasks_summary()

        respond_to_user(message, input_source, is_voice)
        return True

    complete_match = re.match(r"^(?:mark\s+(.+?)\s+(?:complete|completed|done)|complete\s+(.+)|finish\s+(.+)|task\s+complete\s+(.+))$", control_lower)

    if complete_match:
        query = next((group for group in complete_match.groups() if group), "")
        task = complete_task(query)

        if task:
            refresh_tasks_surfaces()
            respond_to_user("Task completed", input_source, is_voice)
        else:
            respond_to_user("No matching task found", input_source, is_voice)

        return True

    delete_match = re.match(r"^(?:delete|remove)\s+(?:task|reminder)\s+(.+)$", control_lower)

    if delete_match:
        task = delete_task(delete_match.group(1))

        if task:
            refresh_tasks_surfaces()
            respond_to_user("Task deleted", input_source, is_voice)
        else:
            respond_to_user("No matching task found", input_source, is_voice)

        return True

    add_prefixes = (
        "remind me to ",
        "task remind me to ",
        "add task ",
        "create task ",
        "remember to ",
        "don't let me forget to ",
        "dont let me forget to "
    )

    if control_lower.startswith(add_prefixes):
        _, message = add_task_from_text(raw_input, input_source)
        respond_to_user(message, input_source, is_voice)
        return True

    return False


def set_theme_setting(theme: str) -> str:
    normalized = normalize_theme(theme)
    settings = set_setting("theme", normalized)
    sync_settings_state(settings, broadcast=False)
    set_hud_theme(normalized, broadcast=True)
    broadcast_settings_payload()
    return theme_label(normalized)


def set_voice_enabled(enabled: bool) -> None:
    settings = set_setting("voice_enabled", bool(enabled))
    sync_settings_state(settings, broadcast=True)
    broadcast_settings_payload()


def set_performance_logs_enabled(enabled: bool) -> None:
    settings = set_setting("performance_logs", bool(enabled))
    sync_settings_state(settings, broadcast=True)
    broadcast_settings_payload()


def set_voice_provider_setting(provider: str) -> str:
    from Core_Cognition.settings_manager import normalize_voice_provider

    normalized = normalize_voice_provider(provider)

    if normalized in {"fish", "auto"} and not fish_configured():
        print(f"{GRAY}[Fish Audio is not configured, keeping the local voice.]{RESET}")
        normalized = "local"

    settings = set_setting("voice_provider", normalized)
    sync_settings_state(settings, broadcast=True)
    broadcast_settings_payload()
    return normalized


def set_voice_rate_setting(rate) -> int:
    try:
        safe_rate = int(float(rate))
    except (TypeError, ValueError):
        safe_rate = 178

    safe_rate = max(90, min(safe_rate, 300))
    settings = set_setting("voice_rate", safe_rate)
    sync_settings_state(settings, broadcast=True)
    broadcast_settings_payload()
    return safe_rate


def set_voice_local_name_setting(name: str) -> str:
    cleaned = re.sub(r"\s+", " ", str(name or "")).strip()
    settings = set_setting("voice_local_name", cleaned)
    sync_settings_state(settings, broadcast=True)
    broadcast_settings_payload()
    return cleaned


def set_voice_pause_threshold_setting(value) -> float:
    try:
        safe_value = float(value)
    except (TypeError, ValueError):
        safe_value = 0.6

    safe_value = max(0.3, min(safe_value, 2.0))
    settings = set_setting("voice_pause_threshold", safe_value)
    sync_settings_state(settings, broadcast=True)
    broadcast_settings_payload()
    return safe_value


def set_voice_follow_up_window_setting(value) -> int:
    try:
        safe_value = int(float(value))
    except (TypeError, ValueError):
        safe_value = 10

    safe_value = max(0, min(safe_value, 60))
    settings = set_setting("voice_follow_up_window_seconds", safe_value)
    sync_settings_state(settings, broadcast=True)
    broadcast_settings_payload()
    return safe_value


def set_boolean_setting(key: str, enabled: bool) -> bool:
    settings = set_setting(key, bool(enabled))
    sync_settings_state(settings, broadcast=True)
    broadcast_settings_payload()
    return bool(settings.get(key))


def set_startup_mode_setting(mode: str) -> str:
    normalized = normalize_control_text(mode).replace("active", "workstation")
    normalized = "workstation" if "workstation" in normalized or "work" in normalized else "sleep"
    settings = set_setting("startup_mode", normalized)
    sync_settings_state(settings, broadcast=True)
    broadcast_settings_payload()
    return normalized


def set_default_location_setting(location: str) -> str:
    cleaned = re.sub(r"\s+", " ", str(location or "")).strip(" \t\n\r,.;:!?")

    if not cleaned:
        cleaned = DEFAULT_LOCATION

    if cleaned == cleaned.lower():
        cleaned = cleaned.title()

    settings = set_setting("default_location", cleaned)
    sync_settings_state(settings, broadcast=True)
    broadcast_settings_payload()
    return cleaned


def set_presence_idle_timeout_setting(minutes) -> int:
    try:
        safe_minutes = int(float(minutes))
    except (TypeError, ValueError):
        safe_minutes = 10

    safe_minutes = max(1, min(safe_minutes, 1440))
    settings = set_setting("presence_idle_timeout_minutes", safe_minutes)
    sync_settings_state(settings, broadcast=True)
    broadcast_settings_payload()
    return safe_minutes


def refresh_presence_status_action() -> dict:
    settings = load_settings()
    idle_seconds = get_idle_seconds()

    if idle_seconds is not None:
        state = load_presence_state()
        state = _presence_state_with_idle(state, idle_seconds)

        if bool(settings.get("presence_mode_enabled", False)) and should_mark_away(settings, state):
            state = mark_away(state)
        elif bool(settings.get("presence_mode_enabled", False)) and should_mark_returned(settings, state):
            state = mark_returned(state)

        save_presence_state(state)

    payload = build_presence_payload()
    broadcast_to_hud("presence_status_update", payload)
    broadcast_settings_payload()
    return payload


def run_health_check_action() -> None:
    global LAST_HEALTH_CHECK
    root_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    script_path = os.path.join(root_dir, "check_friday.sh")

    if not os.path.exists(script_path):
        LAST_HEALTH_CHECK = {
            "status": "Missing",
            "summary": "check_friday.sh was not found"
        }
        broadcast_settings_payload()
        return

    try:
        result = subprocess.run(
            [script_path],
            cwd=root_dir,
            capture_output=True,
            text=True,
            timeout=45,
            check=False
        )
        output = (result.stdout or result.stderr or "").strip().splitlines()
        summary = output[-1] if output else "Health check completed"
        LAST_HEALTH_CHECK = {
            "status": "Passed" if result.returncode == 0 else "Failed",
            "summary": summary
        }
    except subprocess.TimeoutExpired:
        LAST_HEALTH_CHECK = {
            "status": "Timed out",
            "summary": "Health check exceeded 45 seconds"
        }
    except Exception as error:
        LAST_HEALTH_CHECK = {
            "status": "Failed",
            "summary": str(error)
        }

    broadcast_settings_payload()


def direct_action_bool(payload: dict, setting_key: str, default: bool = True) -> bool:
    current = bool(get_setting(setting_key, default))
    raw_value = payload.get("value") if isinstance(payload, dict) and "value" in payload else None

    if raw_value is None:
        return not current

    if isinstance(raw_value, str):
        normalized = normalize_control_text(raw_value)
        return normalized in {"1", "true", "yes", "on", "enabled", "enable", "show", "visible"}

    return bool(raw_value)


def workshop_widget_alias(widget_type: str) -> str:
    widget = str(widget_type or "").lower()

    if widget == "virtual_finder":
        return "files"

    if widget == "intel":
        return "news"

    if widget == "notification_center":
        return "notifications"

    return widget


def route_direct_workshop_widget(
    action: str,
    workspace: Optional[str] = None,
    workshop_role: str = "",
    payload: Optional[dict] = None
) -> bool:
    state = get_hud_state_snapshot()
    workshop_state = state.get("workshop_mode", {}) if isinstance(state.get("workshop_mode"), dict) else {}

    if not workshop_state.get("active"):
        return False

    widget = WORKSHOP_WIDGET_ACTIONS.get(action)

    if not widget:
        return False

    target_workspace = workspace if workspace in {"main", "secondary"} else "main"

    print(f"[Workshop] opening widget: {workshop_widget_alias(widget)}")
    open_widget_in_workspace(widget, target_workspace)
    broadcast_to_hud(
        "workshop_open_widget",
        {
            "widget": workshop_widget_alias(widget),
            "workspace": target_workspace,
            "source": "backend",
            "action": action
        }
    )
    return True


def perform_direct_action(
    action: str,
    workspace: Optional[str] = None,
    workshop_role: str = "",
    payload: Optional[dict] = None
) -> bool:
    payload = payload if isinstance(payload, dict) else {}
    value = normalize_control_text(str(action or "").replace("_", " "))
    value = value.replace(" ", "_")
    target_workspace = workspace if workspace in {"main", "secondary"} else None

    if route_direct_workshop_widget(value, target_workspace, workshop_role, payload):
        return True

    if value == "open_tasks":
        open_tasks_page()
        return True

    if value == "open_task_widget":
        create_tasks_widget(preferred_workspace=target_workspace)
        return True

    if value == "close_tasks":
        close_tasks_page()
        return True

    if value == "close_task_widget":
        close_tasks_widget()
        return True

    if value == "add_task":
        title = payload.get("title") or payload.get("value") or payload.get("text") or ""
        due_at = payload.get("due_at")
        priority = payload.get("priority") or "normal"
        source = payload.get("source") or "tasks_ui"

        if not due_at:
            due_at, title = parse_due_datetime_from_text(str(title))

        add_task(str(title or "").strip(), due_at=due_at, priority=priority, source=source)
        refresh_tasks_surfaces()
        return True

    if value == "complete_task":
        complete_task(payload.get("query") or payload.get("title") or payload.get("value") or "")
        refresh_tasks_surfaces()
        return True

    if value == "delete_task":
        delete_task(payload.get("query") or payload.get("title") or payload.get("value") or "")
        refresh_tasks_surfaces()
        return True

    if value == "clear_completed_tasks":
        clear_completed_tasks()
        refresh_tasks_surfaces()
        return True

    if value == "refresh_tasks":
        refresh_tasks_surfaces()
        return True

    if value == "open_settings":
        open_settings_page()
        return True

    if value == "close_settings":
        close_settings_page()
        return True

    if value == "set_theme":
        set_theme_setting(payload.get("value") or payload.get("theme"))
        return True

    if value == "toggle_voice":
        next_value = payload.get("value")

        if next_value is None:
            next_value = not bool(get_setting("voice_enabled", True))

        set_voice_enabled(bool(next_value))
        return True

    if value == "toggle_performance_logs":
        next_value = payload.get("value")

        if next_value is None:
            next_value = not bool(get_setting("performance_logs", True))

        set_performance_logs_enabled(bool(next_value))
        return True

    if value == "set_voice_provider":
        set_voice_provider_setting(payload.get("value") or payload.get("voice_provider") or "local")
        return True

    if value == "set_voice_rate":
        set_voice_rate_setting(payload.get("value", 178))
        return True

    if value == "set_voice_local_name":
        set_voice_local_name_setting(payload.get("value") or "")
        return True

    if value == "set_voice_pause_threshold":
        set_voice_pause_threshold_setting(payload.get("value", 0.6))
        return True

    if value == "set_voice_follow_up_window":
        set_voice_follow_up_window_setting(payload.get("value", 10))
        return True

    if value == "toggle_voice_audio_reactive_orb":
        set_boolean_setting(
            "voice_audio_reactive_orb",
            direct_action_bool(payload, "voice_audio_reactive_orb", True)
        )
        return True

    if value == "toggle_voice_performance_logs":
        set_boolean_setting(
            "voice_performance_logs",
            direct_action_bool(payload, "voice_performance_logs", True)
        )
        return True

    if value == "toggle_voice_interrupt":
        set_boolean_setting(
            "voice_interrupt_enabled",
            direct_action_bool(payload, "voice_interrupt_enabled", True)
        )
        return True

    if value == "toggle_notifications":
        set_boolean_setting("show_notifications", direct_action_bool(payload, "show_notifications", True))
        return True

    if value == "toggle_hotbar":
        set_boolean_setting("show_hotbar", direct_action_bool(payload, "show_hotbar", True))
        return True

    if value == "toggle_task_widget_on_wake":
        set_boolean_setting(
            "show_task_widget_on_wake",
            direct_action_bool(payload, "show_task_widget_on_wake", False)
        )
        return True

    if value == "toggle_silent_operator":
        set_boolean_setting("silent_operator_enabled", direct_action_bool(payload, "silent_operator_enabled", True))
        return True

    if value == "toggle_workshop_restore_layout":
        set_boolean_setting(
            "workshop_restore_layout",
            direct_action_bool(payload, "workshop_restore_layout", True)
        )
        return True

    if value == "toggle_showcase_return_sleep":
        set_boolean_setting(
            "showcase_return_to_sleep",
            direct_action_bool(payload, "showcase_return_to_sleep", True)
        )
        return True

    if value == "toggle_privacy_mode":
        set_boolean_setting("privacy_mode", direct_action_bool(payload, "privacy_mode", False))
        return True

    if value == "toggle_presence_mode":
        set_boolean_setting("presence_mode_enabled", direct_action_bool(payload, "presence_mode_enabled", False))
        return True

    if value == "toggle_presence_return_greeting":
        set_boolean_setting(
            "presence_return_greeting_enabled",
            direct_action_bool(payload, "presence_return_greeting_enabled", True)
        )
        return True

    if value == "toggle_presence_auto_sleep":
        set_boolean_setting(
            "presence_auto_sleep_enabled",
            direct_action_bool(payload, "presence_auto_sleep_enabled", True)
        )
        return True

    if value == "toggle_presence_auto_launch_ui":
        set_boolean_setting(
            "presence_auto_launch_ui",
            direct_action_bool(payload, "presence_auto_launch_ui", False)
        )
        return True

    if value == "toggle_presence_auto_open_workstation":
        set_boolean_setting(
            "presence_auto_open_workstation",
            direct_action_bool(payload, "presence_auto_open_workstation", False)
        )
        return True

    if value == "set_presence_idle_timeout":
        if "value" in payload:
            timeout_value = payload.get("value")
        elif "minutes" in payload:
            timeout_value = payload.get("minutes")
        else:
            timeout_value = payload.get("presence_idle_timeout_minutes", 10)

        set_presence_idle_timeout_setting(timeout_value)
        return True

    if value == "refresh_presence_status":
        refresh_presence_status_action()
        return True

    if value == "toggle_camera_presence":
        if direct_action_bool(payload, "camera_presence_enabled", False) and not camera_presence_available():
            _broadcast_camera_presence_status()
            return True

        enabled = set_boolean_setting(
            "camera_presence_enabled",
            direct_action_bool(payload, "camera_presence_enabled", False)
        )

        if enabled:
            force_camera_presence_mode(CAMERA_MODE_DOOR_WATCH, load_settings())
        else:
            force_camera_presence_mode("IDLE", load_settings())

        _broadcast_camera_presence_status()
        return True

    if value == "test_door_camera":
        _camera_presence_test_role(CAMERA_ROLE_DOOR)
        return True

    if value == "test_desk_camera":
        _camera_presence_test_role(CAMERA_ROLE_DESK)
        return True

    if value == "set_camera_presence_door_index":
        _camera_presence_set_index("camera_presence_door_device_index", payload.get("value", 1))
        return True

    if value == "set_camera_presence_desk_index":
        _camera_presence_set_index("camera_presence_desk_device_index", payload.get("value", 0))
        return True

    if value == "refresh_camera_presence_status":
        _broadcast_camera_presence_status()
        return True

    if value == "reset_camera_presence":
        reset_camera_presence_state(load_settings())
        _broadcast_camera_presence_status()
        return True

    if value == "camera_presence_door_watch":
        _camera_presence_set_mode(CAMERA_MODE_DOOR_WATCH)
        return True

    if value == "camera_presence_desk_watch":
        _camera_presence_set_mode(CAMERA_MODE_DESK_WATCH)
        return True

    if value == "set_startup_mode":
        set_startup_mode_setting(payload.get("value") or payload.get("startup_mode") or "sleep")
        return True

    if value == "set_default_location":
        set_default_location_setting(payload.get("value") or payload.get("default_location") or "")
        return True

    if value == "run_health_check":
        run_health_check_action()
        return True

    if value in {"open_music", "music"}:
        dock_orb_for_workspace(broadcast=False)
        if not hud_card_is_open("music_controls"):
            create_music_widget(use_cache=True, refresh_async=True)
        return True

    music_targets = {
        "play": "play",
        "music_play": "play",
        "resume": "play",
        "music_resume": "play",
        "music_toggle": "toggle",
        "pause": "pause",
        "music_pause": "pause",
        "stop_music": "pause",
        "next": "next",
        "skip": "next",
        "music_next": "next",
        "previous": "previous",
        "music_previous": "previous",
        "music_shuffle": "shuffle",
        "music_shuffle_off": "shuffle off",
        "music_repeat": "repeat",
        "music_repeat_off": "repeat off"
    }

    if value in music_targets:
        execute_macos_command("music", music_targets[value])
        return True

    widget_actions = {
        "open_calendar": "calendar",
        "open_notes": "notes",
        "open_weather": "weather",
        "open_map": "map",
        "open_files": "virtual_finder",
        "open_virtual_finder": "virtual_finder",
        "open_file_manager": "virtual_finder",
        "open_system_health": "system_health",
        "open_notifications": "notifications",
        "open_task_widget": "tasks",
        "open_intel": "intel",
        "open_news": "intel"
    }

    if value in widget_actions:
        open_widget_in_workspace(widget_actions[value], target_workspace)
        return True

    if value == "clear_hud":
        if workshop_role in {"main", "secondary", "single"} and get_hud_state_snapshot().get("workshop_mode", {}).get("active"):
            clear_workshop_workspace("main" if workshop_role == "single" else workshop_role)
        else:
            clear_hud_cards()
        return True

    if value == "save_workshop_layout":
        save_workshop_state_snapshot()
        return True

    if value == "close_workshop_mode":
        set_workshop_mode(False)
        return True

    if value in {"open_sleep_screen", "sleep_screen", "go_to_sleep"}:
        set_hud_mode("SLEEP", orb_position="CENTER", prompt=get_sleep_prompt())
        return True

    if value in {"interface_on", "turn_on_interface", "show_interface"}:
        # Turning on lands on the Workstation, not the sleep screen.
        #
        # It used to call set_hud_mode("SLEEP"), which is why "turn on" felt
        # unreliable: it showed the window and then put the sleep surface back up,
        # and — because SLEEP clears active_cards — it also wiped whatever widgets
        # were already open. ensure_workstation_visible only ever moves toward
        # ACTIVE, so repeating the command is harmless.
        ensure_workstation_visible("turn on")
        return True

    if value in {"interface_off", "turn_off_interface", "hide_interface"}:
        # Hides the window only. The brain, the microphone and the live session all
        # keep running, which is why this is not the same thing as full_shutdown.
        hide_hud_interface(reason="interface_offline")
        return True

    if value in {"full_shutdown", "shutdown"}:
        # Returns immediately; the process exits once the goodbye has been spoken.
        # Voice callers sign off from the tool result, so no separate announcement.
        source = str(payload.get("source") or "direct_action")
        perform_full_shutdown(source, is_voice=True, announce=source != "voice_live")
        return True

    if value in {"open_workstation", "open_workspace", "workstation_home", "home"}:
        activate_workstation_home()
        return True

    if value == "open_workshop_mode":
        set_workshop_mode(True)
        return True

    if value == "remember_fact":
        text = payload.get("value") or payload.get("text") or ""
        # Routed through the same command layer the typed path uses, so "for this
        # project, ..." scopes identically whether it was spoken or written.
        intent = memory_manager.parse_memory_command(text) or {"action": "store", "text": text}
        reply = memory_manager.run_memory_command(intent, conversation_id=VOICE_CONVERSATION_ID)

        if reply:
            broadcast_memory_state()

        return bool(reply)

    if value == "forget_fact":
        topic = payload.get("value") or payload.get("text") or ""
        removed = memory_manager.forget(topic)

        if removed:
            broadcast_memory_state()

        return bool(removed)

    if value in {"proactive_status", "open_proactive_monitor"}:
        create_notification_center_card()
        return True

    if value == "close_widget":
        return True

    return False


def handle_direct_action(action: str, payload: Optional[dict] = None) -> bool:
    payload = payload if isinstance(payload, dict) else {}
    started_at = time.time()
    normalized = normalize_control_text(str(action or payload.get("action") or "").replace("_", " "))
    normalized = normalized.replace(" ", "_")
    source = str(payload.get("source") or "").strip().lower()

    if is_showcase_mode_active() and source != "showcase":
        perf_log("direct action ignored during showcase", started_at)
        return False

    if normalized not in DIRECT_ACTION_ALLOWLIST:
        set_last_error(f"Unknown direct action: {normalized}", broadcast=False)
        perf_log(f"direct action {normalized or 'unknown'}", started_at)
        return False

    # Any action that puts something on the Workstation gets the Workstation
    # brought up first, in one place, rather than each handler remembering to.
    # Excluded: actions that own their own presentation (Workshop, the sleep
    # screen itself, interface power) — waking the desktop for those would fight
    # the very transition being requested.
    if normalized not in SURFACE_INDEPENDENT_ACTIONS:
        ensure_workstation_visible(normalized)

    workspace = payload.get("target_workspace") or payload.get("workspace")
    workshop_role = payload.get("workshop_role") or ""
    handled = perform_direct_action(
        normalized,
        workspace=workspace,
        workshop_role=workshop_role,
        payload=payload
    )
    set_ai_status("IDLE", broadcast=False)

    if perf_enabled():
        status = "handled" if handled else "miss"
        print(f"[PERF] direct action {normalized}: {status}")

    perf_log(f"direct action {normalized}", started_at)
    return handled


@socketio.on("direct_action")
def handle_direct_action_event(data):
    payload = data if isinstance(data, dict) else {"action": str(data or "")}
    action = payload.get("action") or payload.get("text") or ""
    text = payload.get("text") if isinstance(payload, dict) else ""

    if is_full_shutdown_command(action) or is_full_shutdown_command(text):
        perform_full_shutdown(str(payload.get("source") or "direct_action"), False)
        return

    handle_direct_action(action, payload)


@socketio.on("notification_action")
def handle_notification_action(data):
    """Notification Center actions, applied to the real notification store.

    Every one of these mutates proactive_manager's persisted records and then
    rebroadcasts the history, so the panel always redraws from the source of
    truth rather than from its own local copy.
    """
    payload = data if isinstance(data, dict) else {}
    action = str(payload.get("action") or "").strip().lower()
    ids = payload.get("ids")

    if isinstance(ids, str):
        ids = [ids]
    elif not isinstance(ids, list):
        ids = []

    from Core_Cognition.proactive_manager import (
        clear_notifications,
        dismiss_notification,
        mark_notifications_read
    )

    if action == "dismiss":
        for notification_id in ids:
            dismiss_notification(str(notification_id))
        return

    if action == "clear_all":
        clear_notifications()
        return

    if action == "mark_read":
        # No ids means "everything", which is what the Mark all read control sends.
        mark_notifications_read(ids or None)
        return

    print(f"[NOTIFICATIONS] unknown action {action!r}")


@socketio.on("voice_request_manifest")
def handle_voice_request_manifest(_data=None):
    """Hand the live voice layer the action vocabulary.

    Sourced from DIRECT_ACTION_ALLOWLIST so the model's tool enum and
    perform_direct_action can never drift into two separate vocabularies, which is
    exactly what went wrong with the old string-matching router.
    """
    actions = sorted(DIRECT_ACTION_ALLOWLIST)
    socketio.emit("voice_tool_manifest", {"actions": actions})
    send_voice_memory_identity()

    if perf_enabled():
        print(f"[VOICE] sent tool manifest ({len(actions)} actions)")


def send_voice_memory_identity() -> None:
    """Give the live session the handful of memories relevant to every reply.

    A Live session has one system instruction fixed at connect and no per-turn
    prompt to attach context to, so the always-relevant core — preferred name,
    core preferences — rides along there. It is capped at three lines inside
    memory_manager; anything else the model wants it asks for with get_status
    ('memory'), which runs the same ranked retrieval Silent Operator uses.

    Also re-sent by state_manager.broadcast_memory_state() whenever the store
    changes, so this is only the start-up delivery.
    """
    try:
        block = memory_manager.identity_prompt_block()
    except Exception:
        block = ""

    socketio.emit("voice_memory_context", {"block": block})


@socketio.on("voice_data_call")
def handle_voice_data_call(data):
    """Answer an information request with REAL data.

    This is deliberately separate from voice_tool_call. Actions change the interface;
    these only read. Keeping them apart is what stops "what's my battery?" from being
    answered by opening a widget — previously the model had no data tool to reach for,
    so opening System Health was its only available move.

    Everything here reads live sources so the model never invents a value.
    """
    payload = data if isinstance(data, dict) else {}
    request_id = str(payload.get("request_id") or "")
    query = str(payload.get("query") or "").strip().lower()
    argument = str(payload.get("argument") or "").strip()

    def reply(ok: bool, detail):
        socketio.emit(
            "voice_data_result",
            {"request_id": request_id, "ok": bool(ok), "detail": detail},
        )

    try:
        if query == "battery":
            # Read the battery on its own rather than through the full health sweep.
            # That sweep polls presence, cameras and the calendar, which cost about
            # half a second — half a second of dead air, since a function call blocks
            # her mid-turn. This is the most common question she gets asked.
            #
            # It also used to be read from the wrong level of the payload and came
            # back null, which is worse than an error here: with no value to give,
            # opening the widget was the only move left to her.
            from Sensory_Array.system_tools import _pmset_battery_status

            raw = str(_pmset_battery_status() or "").strip()
            match = re.search(r"(\d+)\s*percent", raw)

            reply(
                bool(raw) and raw.lower() != "unavailable",
                {
                    "percent": int(match.group(1)) if match else None,
                    "charging": "charging" in raw.lower(),
                    "summary": raw or "unavailable",
                },
            )

        elif query == "system_health":
            from Sensory_Array.system_tools import get_system_health_payload

            reply(True, get_system_health_payload() or {})

        elif query == "weather":
            from Sensory_Array.system_tools import get_weather_payload

            reply(True, get_weather_payload(argument or None))

        elif query == "music":
            from Sensory_Array.system_tools import get_music_payload

            reply(True, get_music_payload())

        elif query == "calendar":
            from Sensory_Array.calendar_tools import get_today_agenda

            reply(True, {"today": get_today_agenda()})

        elif query == "tasks":
            from Core_Cognition.tasks_manager import load_tasks

            reply(True, load_tasks())

        elif query == "notes":
            from Sensory_Array.system_tools import load_notes

            reply(True, {"notes": load_notes()})

        elif query == "notifications":
            from Core_Cognition.proactive_manager import get_notifications

            reply(True, {"notifications": get_notifications(limit=10)})

        elif query == "memory":
            # Recall, not a dump. The model asks about a topic and gets back the
            # few memories that rank for it, exactly as Silent Operator would —
            # the same retrieval layer, so both halves of FRIDAY remember the
            # same things in the same order.
            context = memory_manager.build_memory_context(
                argument or "what do you remember about me",
                conversation_id=VOICE_CONVERSATION_ID,
                include_conversation=False
            )
            reply(
                True,
                {
                    "recalled": [record["text"] for record in context["long_term"]],
                    "project": [record["text"] for record in context["project"]],
                    "project_name": context["project_name"]
                }
            )

        elif query == "time":
            reply(True, {"local_time": datetime.datetime.now().strftime("%A %d %B %Y, %H:%M")})

        else:
            reply(False, f"unknown query {query}")

    except Exception as exc:  # noqa: BLE001 - never leave the model waiting
        reply(False, f"error reading {query}: {exc}")


@socketio.on("voice_tool_call")
def handle_voice_tool_call(data):
    """Execute a model-requested system action against the existing allowlist.

    Function calling on gemini-3.1-flash-live BLOCKS the model mid-turn, so this must
    answer quickly — perform_direct_action is synchronous and local, which is why the
    model is given HUD actions rather than anything that touches the network.
    """
    payload = data if isinstance(data, dict) else {}
    request_id = str(payload.get("request_id") or "")
    action = str(payload.get("action") or "").strip()
    value = str(payload.get("value") or "").strip()

    if not action:
        socketio.emit(
            "voice_tool_result",
            {"request_id": request_id, "ok": False, "detail": "no action supplied"},
        )
        return

    if action not in DIRECT_ACTION_ALLOWLIST:
        socketio.emit(
            "voice_tool_result",
            {"request_id": request_id, "ok": False, "detail": f"unknown action {action}"},
        )
        return

    action_payload = {"action": action, "source": "voice_live"}
    if value:
        # perform_direct_action reads argument-bearing actions from these keys.
        action_payload["value"] = value
        action_payload["text"] = value

    try:
        handled = handle_direct_action(action, action_payload)
        detail = "done" if handled else f"{action} was not available right now"
        socketio.emit(
            "voice_tool_result",
            {"request_id": request_id, "ok": bool(handled), "detail": detail},
        )
    except Exception as exc:  # noqa: BLE001 - never leave the model waiting
        socketio.emit(
            "voice_tool_result",
            {"request_id": request_id, "ok": False, "detail": f"error: {exc}"},
        )


def is_showcase_exit_command(raw_lower: str, control_lower: str) -> bool:
    normalized = re.sub(r"[,.!?]+", " ", str(raw_lower or "").lower())
    normalized = re.sub(r"\s+", " ", normalized).strip()
    return normalized in SHOWCASE_EXIT_COMMANDS


def is_showcase_start_command(control_lower: str) -> bool:
    return normalize_command_text(control_lower) in SHOWCASE_START_COMMANDS


def start_showcase_mode(input_source: str, is_voice: bool) -> None:
    if is_showcase_mode_active():
        broadcast_to_hud("showcase_start", {"step": "activation", "restart": True})
        return

    set_showcase_mode(True, step="activation", broadcast=True)
    broadcast_to_hud("showcase_start", {"step": "activation"})
    respond_to_user("Showcase mode engaged", input_source, is_voice)


def stop_showcase_mode(input_source: str = "system", is_voice: bool = False, emergency: bool = False) -> None:
    set_showcase_mode(False, broadcast=True)
    broadcast_to_hud("showcase_stop", {"return_sleep": True})

    if emergency:
        broadcast_to_hud("showcase_return_sleep", {})
        set_hud_mode("SLEEP", orb_position="CENTER", prompt="Showcase mode terminated.")
        respond_to_user("Showcase mode terminated", input_source, is_voice)
    else:
        broadcast_to_hud("showcase_return_sleep", {})
        set_hud_mode("SLEEP", orb_position="CENTER", prompt="Core systems demonstrated.")
        respond_to_user("Core systems demonstrated", input_source, False)


@socketio.on("showcase_step")
def handle_showcase_step_event(data):
    if not is_showcase_mode_active():
        return

    step = data.get("step") if isinstance(data, dict) else str(data or "")
    update_showcase_step(step or "", broadcast=False)


@socketio.on("showcase_complete")
def handle_showcase_complete_event(_data=None):
    if is_showcase_mode_active():
        stop_showcase_mode("system", False, emergency=False)


@socketio.on("showcase_aborted")
def handle_showcase_aborted_event(_data=None):
    if is_showcase_mode_active():
        stop_showcase_mode("system", False, emergency=True)


def execute_fast_action(action_payload: dict, input_source: str, is_voice: bool) -> bool:
    if not isinstance(action_payload, dict):
        return False

    action = str(action_payload.get("action") or "")
    workspace = action_payload.get("target_workspace")

    if perform_direct_action(action, workspace=workspace):
        acknowledge_action(action, input_source, is_voice)
        return True

    return False


# ==========================================================
# TOOL INTELLIGENCE
# ==========================================================
# The layer that understands what was ASKED FOR rather than what was said.
#
# It sits between the deterministic phrase tables above and the model below, which
# is the only placement that adds understanding without taking any away: every
# command that already worked still matches its exact phrase first and behaves
# exactly as it did, and everything the tables miss now gets one local, sub-
# millisecond attempt at a real answer before Gemini is asked to guess.
#
# Nothing here is tool-specific. The tools describe themselves in
# Core_Cognition/tools/; this is only the wiring between that description, the
# existing action dispatcher, and the voice.


def tool_intelligence_bridge(action: str, payload: Optional[dict] = None) -> bool:
    """The one route by which a tool changes the interface.

    Tools name an action; handle_direct_action decides whether it is permitted and
    carries it out. Keeping it that way means the registry cannot grow a second,
    divergent copy of the action allowlist — which is the exact failure the old
    string-matching router had.
    """
    return handle_direct_action(action, payload if isinstance(payload, dict) else {})


tool_intelligence.set_action_bridge(tool_intelligence_bridge)


def handle_tool_intelligence(raw_input: str, input_source: str, is_voice: bool) -> bool:
    """Answer from a registered tool, or return False and let Gemini have the turn.

    False is a normal outcome, not an error. It is returned when no tool claims the
    request, and also when a tool claims it and then finds it cannot actually
    answer — a weather lookup that failed, a theme name that is not a theme. In
    both cases the turn continues down the existing path, so a tool going wrong
    can never cost Jon an answer.
    """
    started_at = time.time()
    resolution = tool_intelligence.resolve(raw_input)

    if not resolution.handled:
        perf_log(f"tool intelligence: conversation ({resolution.reason})", started_at)
        return False

    result = tool_intelligence.execute(resolution)

    if not result.ok and not result.speech:
        if result.error and perf_enabled():
            print(f"{GRAY}[Tool Intelligence: {result.error}]{RESET}")

        perf_log(f"tool intelligence declined {resolution.label}", started_at)
        return False

    if result.speech:
        respond_to_user(result.speech, input_source, is_voice, event=result.event)
    else:
        # An open action with nothing to report. ACTION_EVENTS phrases it, exactly
        # as it does for the same action arriving from a button or the fast path.
        acknowledge_action(result.action, input_source, is_voice)

    perf_log(f"tool intelligence {resolution.label} via {resolution.route}", started_at)
    return True


# ==========================================================
# MULTI-STEP PLANNING
# ==========================================================
# One layer up from Tool Intelligence, and the only thing above it that is still
# local. Tool Intelligence answers "which ONE capability is this?"; the planner
# answers "which capabilities, in what order?" for the requests where the answer
# is more than one — "what do I have tomorrow and will it rain?".
#
# It sits BEFORE handle_tool_intelligence rather than after, because after would
# be too late: Tool Intelligence would have already picked the calendar and
# thrown the weather question away. That placement is only affordable because
# plan() answers None after a single regex for anything that is not a list of
# things, which is nearly every request — "open music" pays one regex and nothing
# else. See tool_planner.plan() for that gate.


def multi_step_plan_log(message: str) -> None:
    """[PLAN] lines, on the same switch as every other performance log."""
    if performance_logs_enabled():
        print(f"{GRAY}{message}{RESET}")


tool_planner.set_logger(multi_step_plan_log)


def handle_multi_step_plan(raw_input: str, input_source: str, is_voice: bool, chat) -> bool:
    """Answer a request that needs several tools, or return False.

    False is a normal outcome: the request was not multi-step, no plan validated,
    or nothing the plan ran produced anything to say. In every case the turn
    carries on to Tool Intelligence and then to Gemini exactly as it did before,
    so a planner that declines can never cost Jon an answer.

    Partial success is still success. If the calendar answered and the weather
    API did not, FRIDAY is given the calendar result and told plainly that the
    weather is missing — which is the whole difference between a useful reply and
    "task failed".
    """
    started_at = time.time()
    built = tool_planner.plan(raw_input)

    if built is None:
        return False

    # The orb reports work, not reasoning. Nothing about which tools were chosen
    # or why reaches the interface.
    set_voice_phase("THINKING")
    tool_planner.execute(built)

    if not built.usable:
        perf_log(f"plan produced nothing ({len(built.steps)} steps)", started_at)
        return False

    # FUSION. The tools returned finished sentences and structured readings; the
    # ORIGINAL question and those results go to FRIDAY, who decides the wording.
    # There is deliberately no per-combination formatter here — that is a table
    # that grows as the square of the tool count.
    answer = ""

    try:
        voice_metrics.mark("ai_request_start")
        gemini_started_at = time.time()
        response = chat.send_message(tool_planner.fusion_prompt(built))
        perf_log("Gemini call", gemini_started_at)
        voice_metrics.mark("ai_response_done")
        answer = str(getattr(response, "text", "") or "").strip()
    except Exception as error:  # noqa: BLE001 - fusion failing must not lose the results
        if perf_enabled():
            print(f"{GRAY}[Plan fusion failed: {error}]{RESET}")

    if not answer:
        # The plan ran and the readings are in hand; only the phrasing failed.
        # Saying them plainly is better than throwing them away.
        answer = tool_planner.summarize(built)

    if not answer:
        perf_log("plan had nothing to say", started_at)
        return False

    respond_to_user(answer, input_source, is_voice)
    memory_manager.record_turn(VOICE_CONVERSATION_ID, "Jon", raw_input)
    memory_manager.record_turn(VOICE_CONVERSATION_ID, "FRIDAY", answer)
    perf_log(f"multi-step plan {built.status} ({len(built.steps)} steps)", started_at)
    return True


def extract_virtual_folder_request(control_lower: str):
    value = normalize_control_text(control_lower)
    patterns = (
        r"^create a folder for (.+)$",
        r"^create folder for (.+)$",
        r"^create folder (.+)$",
        r"^make a folder for (.+)$",
        r"^make folder (.+)$"
    )

    for pattern in patterns:
        match = re.match(pattern, value, flags=re.IGNORECASE)

        if match:
            return match.group(1).strip()

    return None


def extract_birthday_lookup_name(control_lower: str):
    value = re.sub(r"[’']", "", str(control_lower or "").lower())
    value = re.sub(r"\s+", " ", value).strip()

    if value in {
        "when is my birthday",
        "when is my next birthday",
        "next birthday",
        "when is the next birthday"
    }:
        return ""

    match = re.match(r"^when is (.+?)s birthday$", value)

    if match:
        return match.group(1).strip()

    match = re.match(r"^when is (.+?) birthday$", value)

    if match:
        return match.group(1).strip()

    return None


def extract_birthday_lookup(control_lower: str):
    value = re.sub(r"[’']", "", str(control_lower or "").lower())
    value = re.sub(r"\s+", " ", value).strip()

    if value == "when is my birthday":
        return {
            "name": "Jon",
            "allow_nearest": False,
            "personal": True
        }

    if value == "when is my next birthday":
        return {
            "name": "Jon",
            "allow_nearest": True,
            "personal": True
        }

    if value in {"next birthday", "when is the next birthday"}:
        return {
            "name": None,
            "allow_nearest": True,
            "personal": False
        }

    match = re.match(r"^when is (.+?)s birthday$", value)

    if match:
        return {
            "name": match.group(1).strip(),
            "allow_nearest": False,
            "personal": False
        }

    match = re.match(r"^when is (.+?) birthday$", value)

    if match:
        return {
            "name": match.group(1).strip(),
            "allow_nearest": False,
            "personal": False
        }

    return None


def classify_calendar_request(control_lower: str, calendar_context_active: bool = False):
    value = f" {control_lower} "

    if control_lower in {"calendar status", "is calendar connected"}:
        return "status"

    if control_lower in {"calendar debug", "debug calendar", "calendar probe"}:
        return "debug"

    if control_lower in {
        "open calendar widget",
        "show calendar widget",
        "calendar widget",
        "open agenda widget",
        "show agenda widget",
        "agenda widget"
    }:
        return "open_widget"

    if control_lower in {
        "open calendar",
        "show calendar",
        "calendar",
        "open agenda",
        "show agenda",
        "agenda",
        "calendar page",
        "open calendar page",
        "show calendar page",
        "agenda page",
        "open agenda page",
        "show agenda page"
    }:
        return "open_page"

    if control_lower in {"refresh calendar", "sync calendar"}:
        return "refresh_page"

    if control_lower in {
        "what is next",
        "next event",
        "what is my next event"
    }:
        return "next_event"

    if control_lower in {
        "what should i not forget",
        "do i have homework",
        "do i have a test",
        "any birthdays today",
        "any family birthdays"
    }:
        return "priority"

    calendar_terms = {"calendar", "agenda", "schedule"}
    has_calendar_term = any(term in value for term in calendar_terms)
    has_week_after = " week after " in value or " the week after " in value
    has_two_weeks = " next two weeks " in value or " two weeks " in value
    has_next_week = " next week " in value
    has_this_week = " this week " in value or " rest of week " in value or " rest of the week " in value
    has_tomorrow = " tomorrow " in value
    has_today = " today " in value

    if calendar_context_active:
        if control_lower in {"show me the month", "show month", "open month"}:
            return "open_page"

        if " birthday" in value or " birthdays" in value:
            return "priority"

        if " tomorrow" in value:
            return "tomorrow"

        if " week after" in value or " next week" in value:
            return "next_week"

        if " two weeks" in value:
            return "two_weeks"

    if (
        has_two_weeks
        or (has_next_week and has_week_after)
        or (has_this_week and (has_next_week or has_week_after))
        or (has_week_after and "what about" in value)
    ):
        return "two_weeks"

    if has_calendar_term and has_next_week:
        return "next_week"

    if has_calendar_term and has_week_after:
        return "next_week"

    if has_calendar_term and has_this_week:
        return "this_week"

    if has_calendar_term and has_tomorrow:
        return "tomorrow"

    if control_lower in {
        "what is on my calendar",
        "what is on my calendar today",
        "what is my schedule today",
        "what do i have today",
        "today agenda"
    } or (has_calendar_term and has_today):
        return "today"

    if control_lower in {
        "what is on my calendar tomorrow",
        "what do i have tomorrow",
        "tomorrow agenda",
        "schedule tomorrow"
    }:
        return "tomorrow"

    if control_lower in {
        "what is on my calendar this week",
        "what do i have this week",
        "week agenda",
        "schedule this week"
    }:
        return "this_week"

    if control_lower in {
        "what is on my calendar next week",
        "what do i have next week",
        "next week agenda",
        "week after",
        "the week after",
        "calendar week after"
    }:
        return "next_week"

    if control_lower in {
        "what is on my calendar this week and next week",
        "what is on my calendar this week and the week after",
        "what do i have for the next two weeks",
        "calendar next two weeks"
    }:
        return "two_weeks"

    return None


def calendar_events_response(events: List[Dict], label: str) -> str:
    if get_last_calendar_error():
        return "Calendar query failed"

    return calendar_summary_response(summarize_events(events, label))


def summarize_intel_payload(tab_name: str, payload: dict) -> str:
    tabs = payload.get("tabs", {}) if isinstance(payload, dict) else {}
    tab = tabs.get(tab_name) or tabs.get(payload.get("active_tab")) or {}
    items = tab.get("items", []) if isinstance(tab, dict) else []
    stats = tab.get("stats", {}) if isinstance(tab, dict) else {}
    label = tab.get("label", tab_name).replace("_", " ").title() if isinstance(tab, dict) else tab_name.title()

    headlines = []
    for item in items[:2]:
        if isinstance(item, dict) and item.get("title"):
            headlines.append(item["title"])

    if headlines:
        headline_text = " | ".join(headlines[:2])
        return f"{label} loaded, {headline_text}"

    headline_count = stats.get("headline_count") if isinstance(stats, dict) else None
    if headline_count:
        return f"{label} loaded, {headline_count} current signals found"

    return f"{label} loaded"


def friday_brain():
    if not GEMINI_API_KEY:
        print(f"{RED}ERROR: GEMINI_API_KEY missing from environment or .env file.{RESET}")
        return

    genai.configure(api_key=GEMINI_API_KEY)

    # Memory v1 -> v2, once. Safe to call on every start: each source is marked
    # as migrated in the new store, and the originals are backed up and left in
    # place rather than consumed.
    migration = migrate_memory_stores()
    imported = (
        int(migration.get("memory_txt", {}).get("migrated", 0))
        + int(migration.get("workshop", {}).get("migrated", 0))
    )

    if imported:
        print(f"{GRAY}[Memory: migrated {imported} entries into Memory v2]{RESET}")

    recognizer, mic = create_recognizer_and_mic()
    # calibrate_microphone reports whether the device is genuinely usable. A
    # missing, denied, or unresponsive microphone must never stop FRIDAY from
    # coming online with the interface and manual override intact.
    microphone_available = calibrate_microphone(recognizer, mic)
    # Open the Fish connection now so the first reply does not pay for the DNS
    # lookup and TLS handshake mid-sentence.
    prewarm_voice_backend()

    model = make_model()
    chat = model.start_chat(enable_automatic_function_calling=True)
    silent_model = make_workshop_text_model()
    # Silent Operator is stateless per turn now: history comes from the named
    # conversation, not from a shared chat object. The dict is kept so the
    # existing call signature is unchanged; the lock still serialises requests.
    silent_chat_state = {"chat": None}
    silent_chat_lock = threading.Lock()

    threading.Thread(target=background_watchdog, daemon=True).start()
    if FACE_ID_ENABLED:
        start_vision_core_thread()
    start_proactive_monitor()

    sentry_target = None
    music_context_active = False
    calendar_context_active = False
    clear_notes_pending = False
    friday_enabled = True
    workshop_suggestion_pending = False
    workshop_suggestion_last_at = 0
    workshop_suggestion_declined = False

    def maybe_suggest_workshop_mode() -> None:
        nonlocal workshop_suggestion_pending
        nonlocal workshop_suggestion_last_at

        if workshop_suggestion_declined:
            return

        now = time.time()
        state = get_hud_state_snapshot()

        if state.get("workshop_mode", {}).get("active"):
            return

        if len(state.get("active_cards") or []) <= 4:
            return

        if workshop_suggestion_pending:
            return

        if workshop_suggestion_last_at and now - workshop_suggestion_last_at < 1800:
            return

        workshop_suggestion_pending = True
        workshop_suggestion_last_at = now
        respond_to_user(
            "you are opening multiple workspaces, should I engage Workshop Mode",
            "system",
            False
        )

    startup_prompt = "FRIDAY MK1 standing by."

    clear_hud_shutdown_flag(broadcast=False)
    set_ai_status("IDLE", broadcast=False)
    apply_startup_mode(startup_prompt)
    append_transcript("SYSTEM", "FRIDAY MK1 Standby Layer Active", source="system")
    append_transcript("FRIDAY", startup_prompt, source="system")

    # Starting the brain starts FRIDAY — interface included.
    #
    # Nothing here launched the HUD before, so run_friday.sh produced a brain with
    # no interface and the UI had to be started by hand. That is what left voice
    # dead in practice: an independently started HUD is a DIFFERENT Electron
    # process from the one the brain tracks, and with the single-instance lock a
    # second launch simply focuses whichever instance already exists — including a
    # stale one from an earlier run, still holding cached renderer code and a
    # microphone that was never acquired. The interface looked fine and heard
    # nothing.
    #
    # Idempotent: launch_hud_interface() adopts a HUD that is already connected
    # rather than spawning a second one.
    print(f"{GRAY}[{show_hud_interface()}]{RESET}")

    print(f"{CYAN}FRIDAY Online | FRIDAY MK1 Standby Layer Active{RESET}")
    print(f"{CYAN}FRIDAY: {startup_prompt}{RESET}")

    while True:
        request_started_at = None
        # Report the previous voice turn once it has fully finished speaking.
        voice_metrics.finish_turn()

        try:
            raw_input = ""
            input_source = "voice"
            is_voice = False
            silent_action = False
            workshop_role = ""

            override_payload = pop_pending_override()

            if override_payload:
                raw_input = (
                    override_payload.get("text", "")
                    if isinstance(override_payload, dict)
                    else str(override_payload)
                )
                input_source = (
                    override_payload.get("source", "override")
                    if isinstance(override_payload, dict)
                    else "override"
                )
                silent_action = bool(
                    override_payload.get("silent", False)
                    if isinstance(override_payload, dict)
                    else False
                )
                workshop_role = (
                    override_payload.get("workshop_role", "")
                    if isinstance(override_payload, dict)
                    else ""
                )
                is_voice = False

            else:
                # The stdin poll used to cost a fixed half second before every
                # single microphone turn. When the microphone is live it only
                # needs long enough to notice a typed line.
                stdin_poll_seconds = 0.05 if microphone_available else 0.4
                readable, _, _ = select.select([sys.stdin], [], [], stdin_poll_seconds)

                if readable:
                    raw_input = sys.stdin.readline().strip()
                    input_source = "terminal"
                    is_voice = False
                elif microphone_available:
                    # listen_with_gemini_audio owns the LISTENING / USER_SPEAKING
                    # phase transitions so the orb tracks real microphone energy.
                    raw_input = listen_with_gemini_audio(recognizer, mic)
                    input_source = "voice"
                    is_voice = True
                else:
                    time.sleep(0.25)
                    input_source = "terminal"
                    is_voice = False

            if raw_input and is_full_shutdown_command(raw_input):
                request_started_at = time.time()
                perf_log("input received", request_started_at)
                perform_full_shutdown(input_source, is_voice)
                continue

            if not raw_input:
                maybe_suggest_workshop_mode()
                update_sleep_clock(broadcast=False)

                if get_voice_phase() != "IDLE" and not is_friday_speaking():
                    set_voice_phase("IDLE")

                continue

            request_started_at = time.time()
            raw_input = normalize_asr_command_text(raw_input)

            if raw_input.lower().startswith("silent:"):
                silent_action = True
                raw_input = raw_input.split(":", 1)[1].strip()

            raw_lower_global = raw_input.strip().lower()
            control_lower = normalize_control_text(raw_lower_global)
            raw_clean_global = re.sub(r"[,.!?]+", " ", raw_lower_global)
            raw_clean_global = re.sub(r"\s+", " ", raw_clean_global).strip()
            perf_log("input received", request_started_at)

            if is_full_shutdown_command(raw_input):
                perform_full_shutdown(input_source, is_voice)
                continue

            # An explicit memory instruction outranks the notes parser, which
            # also claims the word "remember". "remember to <do X>" is still a
            # task and "remember this" with nothing to attach is still a note —
            # parse_memory_command declines both, so this only takes over the
            # cases that are genuinely about memory.
            memory_intent = memory_manager.parse_memory_command(raw_input)
            pending_note_text = None if memory_intent else extract_note_text(raw_input)
            attention = (
                {
                    "should_respond": True,
                    "reason": "local_memory_command" if memory_intent else "local_note_command",
                    "confidence": 1.0,
                    "normalized": raw_input
                }
                if memory_intent or pending_note_text is not None
                else should_friday_respond(raw_input, input_source=input_source)
            )

            attention_reason = str(attention.get("reason") or "")
            should_respond = bool(attention.get("should_respond", True))

            # Inside the follow-up window a short direct question is accepted
            # without the wake word. Everything the gate rejects for a stronger
            # reason - other-language casual speech, third-party talk about
            # FRIDAY - stays rejected.
            if (
                not should_respond
                and is_voice
                and conversation_window_active()
                and attention_reason in CONVERSATION_WINDOW_RELAXABLE_REASONS
            ):
                should_respond = True
                attention_reason = "conversation_follow_up"
                perf_log("conversation follow-up accepted", request_started_at)

            if not should_respond:
                if get_voice_phase() != "IDLE" and not is_friday_speaking():
                    set_voice_phase("IDLE")

                voice_metrics.abandon_turn()
                perf_log(f"attention gate ignored {attention.get('reason', 'unknown')}", request_started_at)
                continue

            voice_metrics.mark("attention_done")
            attention_normalized = str(attention.get("normalized") or "").strip()

            if attention_reason in {"clear_direct_command", "polite_direct_command"} and attention_normalized:
                raw_input = attention_normalized
                raw_lower_global = raw_input.strip().lower()
                control_lower = normalize_control_text(raw_lower_global)
                raw_clean_global = re.sub(r"[,.!?]+", " ", raw_lower_global)
                raw_clean_global = re.sub(r"\s+", " ", raw_clean_global).strip()

            # Direct UI actions are trusted button/launcher requests.
            if silent_action or input_source in DIRECT_LAUNCHER_SOURCES:
                if is_showcase_mode_active() and is_showcase_exit_command(raw_lower_global, control_lower):
                    stop_showcase_mode(input_source, is_voice, emergency=True)
                    continue

                direct_started_at = time.time()
                handled_direct_action = handle_direct_launcher_action(raw_input, control_lower, workshop_role)
                set_ai_status("IDLE", broadcast=False)
                perf_log("silent action handler", direct_started_at)

                if not handled_direct_action:
                    set_last_error(f"Unknown direct launcher action: {control_lower}", broadcast=False)

                continue

            # Showcase owns input until the exact emergency exit is heard.
            if is_showcase_mode_active():
                if is_showcase_exit_command(raw_lower_global, control_lower):
                    stop_showcase_mode(input_source, is_voice, emergency=True)
                else:
                    set_ai_status("IDLE", broadcast=False)
                continue

            # ------------------------------------------------------------------
            # EXPLICIT MEMORY COMMANDS
            # ------------------------------------------------------------------
            # Deliberately the FIRST thing after the gate and showcase. "Remember
            # that I prefer Graphite mode" contains the word "graphite", and a
            # few hundred lines of theme, music and widget matchers sit below —
            # parsing memory first means none of them can ever claim a sentence
            # that was plainly about memory. It is also the fastest possible
            # answer: local, deterministic, no model call.
            #
            # parse_memory_command already declined the phrases that belong to
            # other systems ("remember to ..." is a task), so reaching here means
            # the request really is about memory.
            if memory_intent:
                memory_started_at = time.time()
                memory_chat_id = take_pending_workshop_chat() if input_source == "workshop_chat" else ""
                memory_conversation = memory_chat_id or VOICE_CONVERSATION_ID
                memory_reply = handle_memory_command(raw_input, conversation_id=memory_conversation)
                perf_log("memory command", memory_started_at)

                if memory_reply:
                    memory_manager.record_turn(memory_conversation, "Jon", raw_input)
                    broadcast_memory_state()

                    if input_source == "workshop_chat":
                        append_workshop_chat_fast(
                            "FRIDAY",
                            memory_reply,
                            {"source": "workshop_chat_response", "mode": "memory"},
                            chat_id=memory_chat_id
                        )
                    else:
                        respond_to_user(memory_reply, input_source, is_voice)

                    continue

                # Recognised the shape but there was nothing to act on — a bare
                # "remember this" with no previous turn. Hand it back to the
                # notes flow, which is what has always owned that phrase.
                pending_note_text = extract_note_text(raw_input)

            # Wake, sleep, and off controls stay ahead of normal routing.
            wake_sleep_started_at = time.time()
            # Built from a name-free stem crossed with the accepted names, so FRIDAY
            # and the retained jarvis wake alias can never drift apart the way the
            # hand-written lists did — "friday turn off" simply did not exist before.
            def named_phrases(stems, name_first=(), name_last=()):
                phrases = set(stems)

                for name in ("friday", "jarvis"):
                    phrases.update(f"{name} {stem}" for stem in name_first)
                    phrases.update(f"{stem} {name}" for stem in name_last)

                return phrases

            wake_stems = [
                "turn on",
                "activate",
                "on",
                "you there",
                "are you there",
                "wake up",
                "online",
                "bring online"
            ]
            wake_phrases = named_phrases(
                ["turn on", "wake up"],
                name_first=wake_stems,
                # "hello" only reads as a greeting before the name, never after it.
                name_last=["turn on", "activate", "you there", "are you there", "wake up", "hello"]
            )

            off_stems = ["turn off", "off", "power off", "hide", "shut the interface"]
            off_phrases = named_phrases(
                [
                    "turn off",
                    "power off interface",
                    "hide interface",
                    "close interface",
                    "dismiss interface",
                    "turn off the interface",
                    "turn the interface off",
                    "hide the interface",
                    "close the interface"
                ],
                name_first=off_stems,
                name_last=["turn off"]
            )

            sleep_stems = ["go to sleep", "standby", "stand by mode", "go to standby mode", "go to stand by mode"]
            sleep_phrases = named_phrases(
                [
                    "go to sleep",
                    "go to sleep screen",
                    "enter sleep screen",
                    "show sleep screen",
                    "open sleep screen",
                    "return to sleep screen",
                    "standby mode",
                    "stand by mode",
                    "sleep screen",
                    "go to stand by mode",
                    "go to standby mode"
                ],
                name_first=sleep_stems,
                name_last=["go to sleep"]
            )

            if any(phrase == raw_lower_global or phrase == raw_clean_global for phrase in off_phrases):
                friday_enabled = True
                message = "Interface offline. I am still listening."

                perform_direct_action("interface_off")
                respond_to_user(message, input_source, is_voice)
                perf_log("wake/sleep routing", wake_sleep_started_at)
                continue

            if any(phrase == raw_lower_global or phrase == raw_clean_global for phrase in sleep_phrases):
                friday_enabled = True
                message = "Sleep screen active."

                clear_hud_shutdown_flag(broadcast=False)
                set_hud_mode("SLEEP", orb_position="CENTER", prompt=get_sleep_prompt(), broadcast=False)
                launch_result = show_hud_interface()
                print(f"{GRAY}[{launch_result}]{RESET}")
                broadcast_state()
                respond_to_user(message, input_source, is_voice)
                perf_log("wake/sleep routing", wake_sleep_started_at)
                continue

            wake_controls = {
                "turn on",
                "on",
                "you there",
                "are you there",
                "wake up",
                "online"
            }

            if (
                any(phrase == raw_lower_global or phrase == raw_clean_global for phrase in wake_phrases)
                or control_lower in wake_controls
            ):
                friday_enabled = True

                # ONE turn-on implementation, shared with the voice path.
                #
                # This block used to be a second, divergent implementation: it set
                # hud_mode to SLEEP (so "turn on" landed back on the sleep screen
                # instead of restoring the Workstation), called show_hud_interface()
                # directly (so ensure_voice_ready() never ran and the MICROPHONE WAS
                # NEVER ACTIVATED), and did all of that differently again inside the
                # greeting-cooldown branch. Typing "turn on" therefore behaved
                # unlike saying it, and neither reliably produced a listening FRIDAY.
                ensure_workstation_visible("turn on")

                if turn_on_greeting_allowed():
                    respond_to_user(build_turn_on_greeting(), input_source, is_voice)

                perf_log("wake/sleep routing", wake_sleep_started_at)
                continue

            perf_log("wake/sleep routing", wake_sleep_started_at)

            if is_showcase_start_command(control_lower):
                start_showcase_mode(input_source, is_voice)
                continue

            if get_hud_state_snapshot().get("workshop_mode", {}).get("active"):
                workshop_direct_action = direct_action_for_text(raw_input, control_lower)

                if workshop_direct_action in WORKSHOP_WIDGET_ACTIONS:
                    if perform_direct_action(workshop_direct_action):
                        acknowledge_action(workshop_direct_action, input_source, is_voice)
                        continue

            if handle_settings_command(raw_input, input_source, is_voice):
                continue

            if handle_tasks_command(raw_input, input_source, is_voice):
                continue

            if control_lower in {
                "reset daily briefing",
                "reset intro"
            }:
                respond_to_user(reset_daily_briefing(), input_source, is_voice)
                continue

            if workshop_suggestion_pending and control_lower in {
                "yes",
                "yeah",
                "engage it",
                "do it",
                "open workshop mode",
                "engage workshop mode",
                "start workshop mode"
            }:
                workshop_suggestion_pending = False
                load_workshop_state_snapshot()
                set_workshop_mode(True)
                set_hud_mode("ACTIVE", orb_position="DOCKED_BOTTOM_RIGHT", broadcast=False)
                broadcast_to_hud("workshop_analytics_update", get_workshop_analytics_payload())
                respond_to_user("Workshop mode engaged", input_source, is_voice)
                continue

            if workshop_suggestion_pending and control_lower in {
                "no",
                "not now",
                "negative",
                "stand down"
            }:
                workshop_suggestion_pending = False
                workshop_suggestion_declined = True
                respond_to_user("Understood", input_source, is_voice)
                continue

            if not friday_enabled:
                update_sleep_clock(broadcast=False)
                set_ai_status("IDLE", broadcast=False)
                continue

            if input_source not in {"override", "workshop_chat", "launcher"}:
                append_transcript("Jon", raw_input, source=input_source)

            set_ai_status("THINKING", broadcast=input_source != "workshop_chat")

            if is_voice and raw_input.strip().lower() in {
                "service",
                "cuckoo",
                "george",
                "exit",
                "ex"
            }:
                set_ai_status("IDLE")
                continue

            if raw_lower_global in {
                "test voice",
                "test the voice",
                "voice test",
                "test vocal cords",
                "friday test voice",
                "friday test vocal cords",
                "jarvis test voice",
                "jarvis test vocal cords"
            }:
                respond_to_user("Voice online", input_source, is_voice)
                continue

            if raw_clean_global in VOICE_INTERRUPT_COMMANDS or raw_lower_global in VOICE_INTERRUPT_COMMANDS:
                # Explicit stop. Ends playback and the speaking animation, then
                # returns to listening without treating the cut-off speech as a
                # new request.
                stop_speaking()
                close_conversation_window()
                append_transcript("FRIDAY", "Quiet", source=input_source)
                broadcast_to_hud("friday_speech", {"text": "Quiet"})
                print(f"{CYAN}FRIDAY: Quiet{RESET}")
                set_voice_phase("IDLE")
                voice_metrics.abandon_turn()
                perf_log("voice interrupt", request_started_at)
                continue

            if raw_lower_global in {
                "say less",
                "be shorter",
                "friday say less",
                "friday be shorter",
                "jarvis say less",
                "jarvis be shorter"
            }:
                respond_to_user("Understood, shorter", input_source, is_voice)
                continue

            if raw_lower_global in {
                "repeat that",
                "friday repeat that",
                "jarvis repeat that"
            }:
                respond_to_user("Repeat buffer is not wired yet.", input_source, is_voice)
                continue

            if control_lower in WORKSHOP_OPEN_COMMANDS:
                workshop_suggestion_pending = False
                load_workshop_state_snapshot()
                set_workshop_mode(True)
                set_hud_mode("ACTIVE", orb_position="DOCKED_BOTTOM_RIGHT", broadcast=False)
                broadcast_to_hud("workshop_analytics_update", get_workshop_analytics_payload())
                respond_to_user("Workshop mode engaged", input_source, is_voice)
                perf_log("workshop routing", request_started_at)
                continue

            if control_lower in WORKSHOP_CLOSE_COMMANDS:
                workshop_suggestion_pending = False
                set_workshop_mode(False)
                respond_to_user("Workshop mode closed", input_source, is_voice)
                perf_log("workshop routing", request_started_at)
                continue

            if control_lower == "workshop status":
                respond_to_user(workshop_status_response(), input_source, is_voice)
                perf_log("workshop routing", request_started_at)
                continue

            if control_lower == "save workshop layout":
                save_workshop_state_snapshot()
                respond_to_user("Workshop layout saved", input_source, is_voice)
                perf_log("workshop routing", request_started_at)
                continue

            if control_lower == "restore workshop layout":
                snapshot = load_workshop_state_snapshot()

                if snapshot:
                    set_workshop_mode(True)
                    broadcast_to_hud("workshop_analytics_update", get_workshop_analytics_payload())
                    respond_to_user("Workshop layout restored", input_source, is_voice)
                else:
                    respond_to_user("No workshop layout saved", input_source, is_voice)
                perf_log("workshop routing", request_started_at)
                continue

            if control_lower == "reset workshop layout":
                reset_workshop_state_snapshot()
                respond_to_user("Workshop layout reset", input_source, is_voice)
                perf_log("workshop routing", request_started_at)
                continue

            if pending_note_text is not None:
                clear_notes_pending = False

                if not pending_note_text:
                    respond_to_user("What would you like me to note, Boss?", input_source, is_voice)
                    continue

                add_note(pending_note_text, source=input_source)
                create_notes_widget()
                respond_to_user("Note saved.", input_source, is_voice)
                continue

            if input_source == "workshop_chat":
                handle_workshop_chat_fast(raw_input, silent_model, silent_chat_state, silent_chat_lock)
                continue

            # Writing guard stays before local parsers so text drafts are not hijacked.
            if handle_writing_intent(raw_input, input_source, is_voice, chat):
                continue

            multi_actions = parse_multi_action_command(raw_input)

            if multi_actions:
                execute_multi_actions(multi_actions, input_source, is_voice)
                continue

            if handle_fast_music_command(control_lower, input_source, is_voice):
                music_context_active = True
                continue

            # Local widgets and workspace actions are handled before model routing.
            workspace_request = extract_workspace_widget_request(control_lower)

            if workspace_request:
                widget_type, workspace = workspace_request
                message = open_widget_in_workspace(widget_type, workspace)
                respond_to_user(message, input_source, is_voice)
                perf_log("notes/files/calendar/weather/music routing", request_started_at)
                continue

            local_fast_action = local_fast_action_payload(control_lower)

            if local_fast_action and execute_fast_action(local_fast_action, input_source, is_voice):
                continue

            if control_lower in {
                "open files",
                "open virtual finder",
                "open file manager",
                "friday open files",
                "jarvis open files"
            }:
                files_started_at = time.time()
                create_virtual_finder_widget()
                perf_log("notes/files routing", files_started_at)
                acknowledge_action("open_files", input_source, is_voice)
                continue

            folder_request = extract_virtual_folder_request(control_lower)

            if folder_request:
                create_result = create_virtual_folder(folder_request)
                create_virtual_finder_widget()
                respond_to_user(create_result, input_source, is_voice)
                continue

            if control_lower in {
                "go back in files",
                "back in finder",
                "file back",
                "finder back"
            }:
                broadcast_to_hud("virtual_finder_navigate", {"action": "back"})
                respond_to_user("Finder moved back", input_source, is_voice)
                continue

            if control_lower in {
                "go to all files",
                "go to root folder",
                "all files",
                "root folder"
            }:
                create_virtual_finder_widget()
                respond_to_user("Virtual Finder root opened", input_source, is_voice)
                continue

            file_search_match = re.match(r"^(?:search files for|find file|find files)\s+(.+)$", control_lower)

            if file_search_match:
                query = file_search_match.group(1).strip()
                create_virtual_finder_widget(search_query=query)
                respond_to_user("File search opened", input_source, is_voice)
                continue

            if control_lower in {
                "show notes",
                "open notes",
                "open sticky notes",
                "show sticky notes",
                "sticky notes",
                "open notes widget",
                "notes widget"
            }:
                notes_started_at = time.time()
                clear_notes_pending = False
                create_notes_widget()
                perf_log("notes/files routing", notes_started_at)
                acknowledge_action("open_notes", input_source, is_voice)
                continue

            if control_lower == "clear notes":
                clear_notes_pending = True
                respond_to_user("Confirm clear notes", input_source, is_voice)
                continue

            if control_lower == "confirm clear notes":
                if not clear_notes_pending:
                    respond_to_user("No clear notes request pending", input_source, is_voice)
                    continue

                clear_notes()
                clear_notes_pending = False
                create_notes_widget()
                acknowledge_action("close_notes", input_source, is_voice)
                continue

            if control_lower.startswith("delete note "):
                clear_notes_pending = False
                note_id = control_lower.replace("delete note ", "", 1).strip()
                deleted = delete_note(note_id)
                create_notes_widget()
                respond_to_user("Note removed" if deleted else "Note not found", input_source, is_voice)
                continue

            if clear_notes_pending:
                clear_notes_pending = False

            if control_lower in {
                "system health",
                "system status",
                "diagnostics",
                "diagnostic",
                "open diagnostics",
                "show diagnostics",
                "run diagnostics",
                "run a full system diagnostic",
                "hardware status",
                "hardware feature",
                "system check",
                "run system check",
                "open system health",
                "show system health"
            }:
                create_system_health_widget()
                acknowledge_action("open_system_health", input_source, is_voice)
                continue

            if control_lower in {
                "show manual override",
                "open manual override",
                "manual override"
            }:
                set_hud_mode("ACTIVE", orb_position="DOCKED_BOTTOM_RIGHT", broadcast=False)
                broadcast_to_hud("focus_manual_override", {})
                respond_to_user("Manual override focused", input_source, is_voice)
                continue

            if control_lower in {
                "open notifications",
                "show notifications",
                "open notification folder",
                "show notification center",
                "alerts",
                "show alerts"
            }:
                create_notification_center_card()
                acknowledge_action("open_notifications", input_source, is_voice)
                continue

            if control_lower in {
                "clear notifications",
                "clear alerts"
            }:
                respond_to_user(clear_notifications(), input_source, is_voice)
                continue

            if control_lower in {
                "reconnect calendar",
                "reauthorize calendar",
                "connect calendar",
                "fix calendar"
            }:
                respond_to_user(reconnect_calendar(), input_source, is_voice)
                continue

            birthday_lookup = extract_birthday_lookup(control_lower)

            if birthday_lookup is not None:
                calendar_context_active = True

                if not is_calendar_connected():
                    respond_to_user("Calendar is not connected yet", input_source, is_voice)
                    continue

                respond_to_user(
                    summarize_next_birthday(
                        birthday_lookup.get("name"),
                        allow_nearest=birthday_lookup.get("allow_nearest", True),
                        personal=birthday_lookup.get("personal", False)
                    ),
                    input_source,
                    is_voice
                )
                continue

            calendar_started_at = time.time()
            calendar_request = classify_calendar_request(control_lower, calendar_context_active)
            perf_log("calendar routing", calendar_started_at)

            if calendar_request == "status":
                calendar_context_active = True
                message = "Calendar connected" if is_calendar_connected() else "Calendar is not connected yet"
                respond_to_user(message, input_source, is_voice)
                continue

            if calendar_request == "debug":
                calendar_context_active = True
                respond_to_user(debug_calendar_probe(), input_source, is_voice)
                continue

            if calendar_request == "open_widget":
                calendar_context_active = True
                dock_orb_for_workspace(broadcast=False)
                seed_context_cards("calendar", replace_existing=False)
                acknowledge_action("open_calendar", input_source, is_voice)
                continue

            if calendar_request == "open_page":
                calendar_context_active = True
                dock_orb_for_workspace(broadcast=False)
                open_calendar_page()
                respond_to_user(build_calendar_opening_summary("opened"), input_source, is_voice)
                continue

            if calendar_request == "refresh_page":
                calendar_context_active = True
                dock_orb_for_workspace(broadcast=False)
                open_calendar_page()
                respond_to_user(build_calendar_opening_summary("synced"), input_source, is_voice)
                continue

            if calendar_request == "today":
                calendar_context_active = True
                if not is_calendar_connected():
                    respond_to_user("Calendar is not connected yet", input_source, is_voice)
                    continue

                events = get_today_agenda()
                respond_to_user(calendar_events_response(events, "today"), input_source, is_voice)
                continue

            if calendar_request == "tomorrow":
                calendar_context_active = True
                if not is_calendar_connected():
                    respond_to_user("Calendar is not connected yet", input_source, is_voice)
                    continue

                events = get_tomorrow_agenda()
                respond_to_user(calendar_events_response(events, "tomorrow"), input_source, is_voice)
                continue

            if calendar_request == "this_week":
                calendar_context_active = True
                if not is_calendar_connected():
                    respond_to_user("Calendar is not connected yet", input_source, is_voice)
                    continue

                events = get_week_agenda()
                respond_to_user(calendar_events_response(events, "this week"), input_source, is_voice)
                continue

            if calendar_request == "next_week":
                calendar_context_active = True
                if not is_calendar_connected():
                    respond_to_user("Calendar is not connected yet", input_source, is_voice)
                    continue

                events = get_next_week_agenda()
                respond_to_user(calendar_events_response(events, "next week"), input_source, is_voice)
                continue

            if calendar_request == "two_weeks":
                calendar_context_active = True
                if not is_calendar_connected():
                    respond_to_user("Calendar is not connected yet", input_source, is_voice)
                    continue

                events = get_next_two_weeks_agenda()
                respond_to_user(calendar_events_response(events, "in the next two weeks"), input_source, is_voice)
                continue

            if calendar_request == "next_event":
                calendar_context_active = True
                if not is_calendar_connected():
                    respond_to_user("Calendar is not connected yet", input_source, is_voice)
                    continue

                event = get_next_event()

                if get_last_calendar_error():
                    respond_to_user("Calendar query failed", input_source, is_voice)
                    continue

                if not event:
                    respond_to_user("No upcoming calendar events", input_source, is_voice)
                    continue

                respond_to_user(
                    f"Next event, {event.get('title', 'Untitled event')} at {format_event_start(event)}",
                    input_source,
                    is_voice
                )
                continue

            if calendar_request == "priority":
                calendar_context_active = True
                if not is_calendar_connected():
                    respond_to_user("Calendar is not connected yet", input_source, is_voice)
                    continue

                reminders = get_today_priority_reminders()

                if get_last_calendar_error():
                    respond_to_user("Calendar query failed", input_source, is_voice)
                    continue

                wanted_types = None

                if control_lower == "do i have homework":
                    wanted_types = {"homework"}
                elif control_lower == "do i have a test":
                    wanted_types = {"test"}
                elif control_lower in {"any birthdays today", "any family birthdays"}:
                    wanted_types = {"birthday"}

                respond_to_user(calendar_priority_response(reminders, wanted_types), input_source, is_voice)
                continue

            watch_topic = extract_proactive_topic(raw_lower_global, ("watch ", "monitor "))

            if watch_topic:
                respond_to_user(register_watch_topic(watch_topic), input_source, is_voice)
                continue

            stop_watch_topic = extract_proactive_topic(raw_lower_global, (
                "stop watching ",
                "stop monitoring "
            ))

            if stop_watch_topic:
                respond_to_user(disable_watch_topic(stop_watch_topic), input_source, is_voice)
                continue

            if control_lower in {
                "what are you monitoring",
                "proactive status",
                "monitor status"
            }:
                respond_to_user(get_watch_status(), input_source, is_voice)
                continue

            if control_lower in {
                "check alerts",
                "scan alerts",
                "force proactive scan"
            }:
                alerts = check_proactive_alerts(force=True)

                for alert in alerts:
                    create_proactive_hud_alert(alert)

                respond_to_user(summarize_proactive_scan(alerts), input_source, is_voice)
                continue

            widget_close_result = close_named_hud_widget(raw_input)

            if widget_close_result:
                respond_to_user(widget_close_result, input_source, is_voice)
                continue

            if control_lower in {
                "open workstation",
                "launch workstation",
                "enter workstation",
                "show workstation",
                "activate workstation",
                "workstation mode",
                "open workspace",
                "launch workspace",
                "enter workspace",
                "begin work",
                "open hud",
                "show hud"
            }:
                clear_hud_shutdown_flag(broadcast=False)
                activate_workstation_home()
                launch_result = show_hud_interface()
                print(f"{GRAY}[{launch_result}]{RESET}")
                acknowledge_action("open_workstation", input_source, is_voice)
                continue

            open_maps_phrases = {
                "open map",
                "open maps",
                "open the map",
                "open the maps",
                "show map",
                "show maps",
                "show me the map",
                "show me maps",
                "launch map",
                "launch maps",
                "tactical map",
                "open tactical map",
                "friday open map",
                "friday open maps",
                "friday open the map",
                "friday open the maps",
                "jarvis open map",
                "jarvis open maps",
                "jarvis open the map",
                "jarvis open the maps"
            }

            if raw_lower_global in open_maps_phrases:
                dock_orb_for_workspace(broadcast=False)
                map_result = open_map_widget("Planet Earth")
                respond_to_user(f"{map_result}", input_source, is_voice)
                continue

            map_destination = extract_map_destination(raw_input)

            if map_destination:
                dock_orb_for_workspace(broadcast=False)
                map_result = open_map_widget(map_destination)
                respond_to_user(f"{map_result}", input_source, is_voice)
                continue

            handled_music, music_context_active = handle_music_request(
                raw_lower_global,
                input_source,
                is_voice,
                music_context_active
            )

            if handled_music:
                continue

            if any(cmd in raw_input.lower() for cmd in ["weather", "forecast", "temperature"]):
                weather_started_at = time.time()
                location_match = re.search(
                    r"(?:in|for)\s+([a-zA-Z\s,]+)$",
                    raw_input,
                    re.IGNORECASE
                )
                location = location_match.group(1).strip() if location_match else current_default_location()

                dock_orb_for_workspace(broadcast=False)
                weather_result = get_weather(location)
                perf_log("weather routing", weather_started_at)
                respond_to_user(f"{expand_state_abbreviations(weather_result)}", input_source, is_voice)
                continue

            intel_started_at = time.time()
            intel_tab = classify_intel_request(raw_input)

            if input_source == "workshop_chat" and intel_tab and not is_explicit_intel_request(control_lower):
                intel_tab = None

            if intel_tab:
                dock_orb_for_workspace(broadcast=False)
                intel_topic = build_intel_topic(intel_tab)
                seed_context_cards(intel_topic, replace_existing=False)
                news_payload = get_news_payload(active_tab=intel_tab)
                message = summarize_intel_payload(intel_tab, news_payload)
                perf_log("intel/news routing", intel_started_at)
                respond_to_user(message, input_source, is_voice)
                continue

            if any(cmd in raw_input.lower() for cmd in [
                "clear saved layouts",
                "reset hud layout",
                "reset layout"
            ]):
                clear_hud_cards()
                respond_to_user(
                    "HUD cards cleared. Saved browser card layouts will reset on the interface side.",
                    input_source,
                    is_voice
                )
                continue

            if any(cmd in raw_input.lower() for cmd in [
                "clear hud",
                "clear huds",
                "clear cards",
                "clear the hud",
                "clear the huds",
                "clear all huds",
                "clear all cards",
                "remove every widget",
                "remove all widgets",
                "remove widgets",
                "clear workspace",
                "clear screen",
                "clear widgets",
                "clear all widgets",
                "close every widget",
                "close all widgets"
            ]):
                clear_hud_cards()
                acknowledge_action("clear_hud", input_source, is_voice)
                continue

            if any(cmd in raw_input.lower() for cmd in [
                "monitor my screen",
                "keep looking",
                "watch my screen",
                "always look at my screen",
                "keep watching my screen"
            ]):
                sentry_target = "SCREEN"
                respond_to_user("Screen sentry mode engaged", input_source, is_voice)
                continue

            if any(cmd in raw_input.lower() for cmd in [
                "monitor me",
                "keep looking at me",
                "watch me",
                "always look at me",
                "keep watching me"
            ]):
                sentry_target = "WEBCAM"
                respond_to_user("Webcam sentry engaged.", input_source, is_voice)
                continue

            if any(cmd in raw_input.lower() for cmd in [
                "stop looking",
                "stand down optics",
                "stop watching"
            ]):
                sentry_target = None
                respond_to_user("Optics disengaged, privacy restored", input_source, is_voice)
                continue

            # ------------------------------------------------------------------
            # TOOL INTELLIGENCE
            # ------------------------------------------------------------------
            # Last local stop before the model. Everything above this line is an
            # exact phrase FRIDAY already knew; everything below costs a network
            # round trip. This is where "how much battery do I have" becomes a
            # System Status reading instead of a guess, and it runs in well under
            # a millisecond, so an unmatched request loses nothing by trying.
            #
            # Ahead of the Ollama router deliberately: that one costs up to a
            # second and only ever produces an action, so anything Tool
            # Intelligence can answer should not be paying for it.
            #
            # Skipped entirely while a sentry is engaged. "Watch my screen" means
            # the next questions are about what is ON the screen, and routing one
            # of them to a tool would answer a question Jon did not ask.
            #
            # The planner is asked first and answers None immediately unless the
            # request genuinely names several things, so a single-capability
            # request reaches handle_tool_intelligence just as fast as it always
            # has. Asking the other way round would be too late: Tool
            # Intelligence resolves to ONE capability and would have already
            # dropped the rest of the request on the floor.
            if not sentry_target and handle_multi_step_plan(raw_input, input_source, is_voice, chat):
                continue

            if not sentry_target and handle_tool_intelligence(raw_input, input_source, is_voice):
                continue

            if should_try_ollama_fast_router(control_lower, input_source):
                fast_router_started_at = time.time()
                fast_action = route_fast_action(raw_input)
                perf_log("fast_router", fast_router_started_at)

                if fast_action and execute_fast_action(fast_action, input_source, is_voice):
                    continue

            # Gemini is the final fallback after deterministic local routes.
            screen_keys = [
                "look at my screen",
                "read my screen",
                "analyze the screen",
                "see my screen"
            ]

            webcam_keys = [
                "look at me",
                "see me",
                "what am i holding",
                "use the camera"
            ]

            detail_triggers = [
                "full scan",
                "detailed",
                "specifics",
                "exactly",
                "deep dive",
                "count",
                "how many"
            ]

            wants_detail = any(word in raw_input.lower() for word in detail_triggers)
            is_screen_target = any(k in raw_input.lower() for k in screen_keys) or sentry_target == "SCREEN"
            is_webcam_target = any(k in raw_input.lower() for k in webcam_keys) or sentry_target == "WEBCAM"

            voice_metrics.mark("routing_done")
            voice_metrics.mark("ai_request_start")

            if is_screen_target or is_webcam_target:
                set_voice_phase("THINKING")

                if is_screen_target:
                    visual_data = capture_optical_feed()
                    sensor_label = "tactical_feed.png"
                else:
                    visual_data = capture_webcam_feed()
                    sensor_label = "webcam_feed.jpg"

                if visual_data:
                    if wants_detail:
                        final_prompt = (
                            f"{raw_input} "
                            "(PROTOCOL: Perform a high-resolution detailed analysis of every element. Do not refuse.)"
                        )
                    else:
                        final_prompt = (
                            f"{raw_input} "
                            "(PROTOCOL: Provide a 1-sentence tactical summary only. Ignore minor details and background noise.)"
                        )

                    gemini_started_at = time.time()
                    response = chat.send_message([final_prompt, visual_data])

                    if os.path.exists(sensor_label):
                        os.remove(sensor_label)
                else:
                    gemini_started_at = time.time()
                    response = chat.send_message(
                        raw_input + " (System note: Optical hardware failed.)"
                    )
            else:
                # Only this path — the general model fallback — pays for memory
                # retrieval. Everything above it (widgets, music, calendar, tasks,
                # themes, wake/sleep) is deterministic and already answered, so a
                # command like "open music" never touches the memory store.
                gemini_started_at = time.time()
                response = chat.send_message(
                    memory_context_prompt(raw_input, VOICE_CONVERSATION_ID)
                )

            perf_log("Gemini call", gemini_started_at)
            voice_metrics.mark("ai_response_done")

            try:
                final_text = response.text.strip() or "Action complete."
                respond_to_user(final_text, input_source, is_voice)
                # Conversation memory for the spoken thread, then a conservative
                # look for anything durable. Both happen after the answer.
                memory_manager.record_turn(VOICE_CONVERSATION_ID, "Jon", raw_input)
                memory_manager.record_turn(VOICE_CONVERSATION_ID, "FRIDAY", final_text)
                capture_memory_candidates(raw_input, VOICE_CONVERSATION_ID)

            except ValueError:
                try:
                    gemini_started_at = time.time()
                    follow_up = chat.send_message(
                        "The previous tool/action completed but produced no user-facing text. "
                        "Report the completed result to Boss in one concise sentence."
                    )
                    perf_log("Gemini call", gemini_started_at)
                    message = follow_up.text.strip() or "Action complete."
                except Exception:
                    message = "Action complete."

                respond_to_user(message, input_source, is_voice)

            except Exception as e:
                error_text = f"LOGIC ERROR: {e}"
                set_last_error(error_text)
                print(f"{RED}{error_text}{RESET}")

            purge_limit = 2 if sentry_target else 10

            if len(chat.history) > purge_limit:
                chat = model.start_chat(enable_automatic_function_calling=True)
                print(f"{GRAY}[System: Cognitive cache purged to maintain speed.]{RESET}")

        except Exception as e:
            error_text = f"BRAIN ERROR: {e}"
            set_last_error(error_text)
            print(error_text)

        finally:
            if request_started_at:
                perf_log("total request", request_started_at)


if __name__ == "__main__":
    start_bridge_thread()
    start_task_reminder_monitor()
    start_presence_monitor()
    start_camera_presence_monitor()
    friday_brain()
