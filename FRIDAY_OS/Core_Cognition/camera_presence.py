import datetime
import json
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional, Tuple


try:
    import cv2
except Exception:
    cv2 = None


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = PROJECT_ROOT / "Data"
CAMERA_PRESENCE_STATE_FILE = DATA_DIR / "camera_presence_state.json"

MODE_IDLE = "IDLE"
MODE_DOOR_WATCH = "DOOR_WATCH"
MODE_ENTRY_DETECTED = "ENTRY_DETECTED"
MODE_DESK_WATCH = "DESK_WATCH"
MODE_DESK_PRESENT = "DESK_PRESENT"
MODE_AWAY = "AWAY"

ROLE_DOOR = "door_camera"
ROLE_DESK = "desk_camera"
IDENTITY_UNKNOWN = "unknown"

_state_lock = threading.RLock()


def _now() -> datetime.datetime:
    return datetime.datetime.now().replace(microsecond=0)


def _now_iso() -> str:
    return _now().isoformat()


def _parse_iso(value: Any) -> Optional[datetime.datetime]:
    if not value:
        return None

    try:
        return datetime.datetime.fromisoformat(str(value))
    except Exception:
        return None


def camera_presence_available() -> bool:
    return cv2 is not None


def _default_camera_presence_state() -> Dict[str, Any]:
    return {
        "enabled": False,
        "mode": MODE_IDLE,
        "active_camera_role": None,
        "door_camera_index": 1,
        "desk_camera_index": 0,
        "last_door_seen_at": None,
        "last_desk_seen_at": None,
        "last_greeting_at": None,
        "last_camera_switch_at": None,
        "last_identity": IDENTITY_UNKNOWN,
        "last_motion_score": 0,
        "status": "idle",
    }


def _safe_int(value: Any, default: int, minimum: int = 0, maximum: int = 10) -> int:
    try:
        number = int(float(value))
    except (TypeError, ValueError):
        number = default

    return max(minimum, min(number, maximum))


def _safe_float(value: Any, default: float, minimum: float = 0.0, maximum: float = 255.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = default

    return max(minimum, min(number, maximum))


def _normalize_mode(value: Any) -> str:
    mode = str(value or MODE_IDLE).strip().upper()
    return mode if mode in {MODE_IDLE, MODE_DOOR_WATCH, MODE_ENTRY_DETECTED, MODE_DESK_WATCH, MODE_DESK_PRESENT, MODE_AWAY} else MODE_IDLE


def _active_role_for_mode(mode: str) -> Optional[str]:
    if mode == MODE_DOOR_WATCH:
        return ROLE_DOOR

    if mode in {MODE_DESK_WATCH, MODE_DESK_PRESENT}:
        return ROLE_DESK

    return None


def _normalize_camera_presence_state(state: Any) -> Dict[str, Any]:
    defaults = _default_camera_presence_state()
    normalized = dict(defaults)

    if isinstance(state, dict):
        normalized.update(state)

    normalized["enabled"] = bool(normalized.get("enabled", False))
    normalized["mode"] = _normalize_mode(normalized.get("mode"))
    normalized["active_camera_role"] = normalized.get("active_camera_role") or _active_role_for_mode(normalized["mode"])
    normalized["door_camera_index"] = _safe_int(normalized.get("door_camera_index"), defaults["door_camera_index"])
    normalized["desk_camera_index"] = _safe_int(normalized.get("desk_camera_index"), defaults["desk_camera_index"])
    normalized["last_door_seen_at"] = normalized.get("last_door_seen_at") or None
    normalized["last_desk_seen_at"] = normalized.get("last_desk_seen_at") or None
    normalized["last_greeting_at"] = normalized.get("last_greeting_at") or None
    normalized["last_camera_switch_at"] = normalized.get("last_camera_switch_at") or None
    normalized["last_identity"] = str(normalized.get("last_identity") or IDENTITY_UNKNOWN).strip() or IDENTITY_UNKNOWN
    normalized["last_motion_score"] = round(_safe_float(normalized.get("last_motion_score"), 0.0), 2)
    normalized["status"] = str(normalized.get("status") or defaults["status"]).strip() or defaults["status"]
    return normalized


def _atomic_write_json(path: Path, payload: Dict[str, Any]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    temp_file = path.with_suffix(f"{path.suffix}.tmp")

    with temp_file.open("w", encoding="utf-8") as file:
        json.dump(payload, file, indent=2)
        file.write("\n")

    temp_file.replace(path)


def load_camera_presence_state() -> dict:
    with _state_lock:
        DATA_DIR.mkdir(parents=True, exist_ok=True)

        if not CAMERA_PRESENCE_STATE_FILE.exists():
            state = _default_camera_presence_state()
            _atomic_write_json(CAMERA_PRESENCE_STATE_FILE, state)
            return state

        try:
            with CAMERA_PRESENCE_STATE_FILE.open("r", encoding="utf-8") as file:
                data = json.load(file)
        except (OSError, json.JSONDecodeError):
            data = {}

        state = _normalize_camera_presence_state(data)

        if state != data:
            _atomic_write_json(CAMERA_PRESENCE_STATE_FILE, state)

        return state


def save_camera_presence_state(state: dict) -> None:
    with _state_lock:
        _atomic_write_json(CAMERA_PRESENCE_STATE_FILE, _normalize_camera_presence_state(state))


def get_available_cameras(max_index: int = 5) -> list:
    if cv2 is None:
        return []

    cameras = []
    safe_max = _safe_int(max_index, 5, minimum=0, maximum=20)

    for index in range(safe_max + 1):
        capture = None

        try:
            capture = cv2.VideoCapture(index)

            if capture is not None and capture.isOpened():
                ok, _frame = capture.read()

                if ok:
                    cameras.append(index)
        except Exception:
            pass
        finally:
            try:
                if capture is not None:
                    capture.release()
            except Exception:
                pass

    return cameras


# A camera that cannot be opened (not connected, or macOS camera permission not
# granted) fails on every poll, and OpenCV prints several lines to stderr each
# time. At the 10 s poll interval that floods the log indefinitely. After a few
# consecutive failures the device is left alone for a while and reported once,
# then retried - so a camera plugged in later still recovers on its own.
CAMERA_FAILURE_LIMIT = 3
CAMERA_BACKOFF_SECONDS = 300.0

_camera_failures = {}
_camera_failure_lock = threading.Lock()


def _camera_in_backoff(index: int) -> bool:
    with _camera_failure_lock:
        entry = _camera_failures.get(index)
        return bool(entry and time.time() < entry.get("until", 0.0))


def _note_camera_failure(index: int) -> None:
    with _camera_failure_lock:
        entry = _camera_failures.setdefault(index, {"count": 0, "until": 0.0})
        entry["count"] += 1

        if entry["count"] < CAMERA_FAILURE_LIMIT:
            return

        entry["count"] = 0
        entry["until"] = time.time() + CAMERA_BACKOFF_SECONDS

    print(
        "[Camera presence] Camera {0} is unavailable, pausing checks for {1:.0f} minutes.".format(
            index, CAMERA_BACKOFF_SECONDS / 60.0
        )
    )


def _note_camera_success(index: int) -> None:
    with _camera_failure_lock:
        _camera_failures.pop(index, None)


def capture_frame(device_index):
    if cv2 is None:
        return None

    index = _safe_int(device_index, 0, minimum=0, maximum=20)

    if _camera_in_backoff(index):
        return None

    capture = None

    try:
        capture = cv2.VideoCapture(index)

        if capture is None or not capture.isOpened():
            _note_camera_failure(index)
            return None

        ok, frame = capture.read()

        if ok:
            _note_camera_success(index)
            return frame

        _note_camera_failure(index)
        return None
    except Exception:
        _note_camera_failure(index)
        return None
    finally:
        try:
            if capture is not None:
                capture.release()
        except Exception:
            pass


def _prepare_motion_frame(frame):
    if cv2 is None or frame is None:
        return None

    try:
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        return cv2.resize(gray, (160, 90))
    except Exception:
        return None


def estimate_motion_score(previous_frame, current_frame) -> float:
    if cv2 is None or previous_frame is None or current_frame is None:
        return 0.0

    previous = _prepare_motion_frame(previous_frame)
    current = _prepare_motion_frame(current_frame)

    if previous is None or current is None:
        return 0.0

    try:
        difference = cv2.absdiff(previous, current)
        return round(float(difference.mean()), 2)
    except Exception:
        return 0.0


def detect_presence_from_camera(device_index, previous_frame, threshold) -> dict:
    if cv2 is None:
        return {
            "available": False,
            "detected": False,
            "motion_score": 0.0,
            "frame": None,
            "error": "OpenCV unavailable",
        }

    current_frame = capture_frame(device_index)

    if current_frame is None:
        return {
            "available": False,
            "detected": False,
            "motion_score": 0.0,
            "frame": None,
            "error": "Camera unavailable",
        }

    comparison_frame = previous_frame

    if comparison_frame is None:
        time.sleep(0.12)
        comparison_frame = current_frame
        fresh_frame = capture_frame(device_index)
        current_frame = fresh_frame if fresh_frame is not None else comparison_frame

    score = estimate_motion_score(comparison_frame, current_frame)
    safe_threshold = _safe_float(threshold, 18.0, minimum=0.0, maximum=255.0)

    return {
        "available": True,
        "detected": score >= safe_threshold,
        "motion_score": score,
        "frame": current_frame,
        "error": "",
    }


def _settings_timeout(settings: dict) -> int:
    return _safe_int(settings.get("camera_presence_desk_absence_timeout_seconds"), 180, minimum=10, maximum=86400)


def _switch_allowed(state: dict, cooldown_seconds: int = 30) -> bool:
    last_switch_at = _parse_iso(state.get("last_camera_switch_at"))

    if not last_switch_at:
        return True

    return (_now() - last_switch_at).total_seconds() >= max(0, cooldown_seconds)


def mark_camera_switch(state: dict) -> dict:
    next_state = _normalize_camera_presence_state(state)
    next_state["last_camera_switch_at"] = _now_iso()
    return next_state


def _settings_enabled(settings: dict, key: str, default: bool = True) -> bool:
    return bool(settings.get(key, default))


def _settings_index(settings: dict, key: str, default: int) -> int:
    return _safe_int(settings.get(key), default, minimum=0, maximum=20)


def _sync_indices_from_settings(state: dict, settings: dict) -> dict:
    next_state = _normalize_camera_presence_state(state)
    next_state["door_camera_index"] = _settings_index(settings, "camera_presence_door_device_index", 1)
    next_state["desk_camera_index"] = _settings_index(settings, "camera_presence_desk_device_index", 0)
    next_state["enabled"] = bool(settings.get("camera_presence_enabled", False))
    return next_state


def transition_camera_presence_state(state: dict, settings: dict, detection: dict) -> dict:
    next_state = _sync_indices_from_settings(state, settings)
    now = _now_iso()

    if not bool(settings.get("camera_presence_enabled", False)):
        next_state["mode"] = MODE_IDLE
        next_state["active_camera_role"] = None
        next_state["status"] = "idle"
        next_state["enabled"] = False
        return next_state

    if cv2 is None:
        next_state["mode"] = MODE_IDLE
        next_state["active_camera_role"] = None
        next_state["status"] = "unavailable"
        next_state["enabled"] = True
        return next_state

    mode = _normalize_mode(next_state.get("mode"))

    if mode == MODE_IDLE:
        if _settings_enabled(settings, "camera_presence_door_enabled", True):
            mode = MODE_DOOR_WATCH
        elif _settings_enabled(settings, "camera_presence_desk_enabled", True):
            mode = MODE_DESK_WATCH

    auto_handoff_enabled = bool(settings.get("camera_presence_auto_handoff_enabled", False))

    if mode == MODE_AWAY and auto_handoff_enabled and _switch_allowed(next_state):
        mode = MODE_DOOR_WATCH if _settings_enabled(settings, "camera_presence_door_enabled", True) else MODE_DESK_WATCH
        next_state["last_camera_switch_at"] = now

    next_state["mode"] = mode
    next_state["active_camera_role"] = _active_role_for_mode(mode)
    next_state["status"] = "watching"

    if not isinstance(detection, dict):
        return next_state

    role = str(detection.get("role") or "")
    detected = bool(detection.get("detected", False))
    next_state["last_motion_score"] = round(_safe_float(detection.get("motion_score"), 0.0), 2)

    if not bool(detection.get("available", True)):
        next_state["status"] = "unavailable"
        return next_state

    if mode == MODE_DOOR_WATCH and role == ROLE_DOOR and detected:
        next_state["mode"] = MODE_ENTRY_DETECTED if auto_handoff_enabled and _switch_allowed(next_state) else MODE_DOOR_WATCH
        next_state["active_camera_role"] = ROLE_DOOR
        next_state["last_door_seen_at"] = now
        next_state["last_identity"] = IDENTITY_UNKNOWN
        next_state["status"] = "entry_detected"
        if next_state["mode"] == MODE_ENTRY_DETECTED:
            next_state["last_camera_switch_at"] = now
        return next_state

    if mode == MODE_DESK_WATCH and role == ROLE_DESK and detected:
        next_state["mode"] = MODE_DESK_PRESENT
        next_state["active_camera_role"] = ROLE_DESK
        next_state["last_desk_seen_at"] = now
        next_state["status"] = "desk_present"
        return next_state

    if mode == MODE_DESK_PRESENT and role == ROLE_DESK:
        if detected:
            next_state["last_desk_seen_at"] = now
            next_state["status"] = "desk_present"
            return next_state

        last_seen = _parse_iso(next_state.get("last_desk_seen_at"))
        elapsed = (_now() - last_seen).total_seconds() if last_seen else _settings_timeout(settings) + 1

        if elapsed >= _settings_timeout(settings):
            next_state["mode"] = MODE_AWAY
            next_state["active_camera_role"] = None
            next_state["status"] = "away"
            next_state["last_camera_switch_at"] = now
            return next_state

    return next_state


def build_camera_presence_payload() -> dict:
    try:
        from Core_Cognition.settings_manager import load_settings
        settings = load_settings()
    except Exception:
        settings = {}

    state = _sync_indices_from_settings(load_camera_presence_state(), settings)
    available = camera_presence_available()
    status = "Available" if available else "Unavailable"

    if not bool(settings.get("camera_presence_enabled", False)):
        status = "Disabled" if available else "Unavailable"

    return {
        "enabled": bool(settings.get("camera_presence_enabled", False)),
        "mode": state.get("mode", MODE_IDLE),
        "active_camera_role": state.get("active_camera_role"),
        "active_camera_label": "Door" if state.get("active_camera_role") == ROLE_DOOR else "Desk" if state.get("active_camera_role") == ROLE_DESK else "None",
        "door_camera_index": state.get("door_camera_index", 1),
        "desk_camera_index": state.get("desk_camera_index", 0),
        "last_door_seen_at": state.get("last_door_seen_at"),
        "last_desk_seen_at": state.get("last_desk_seen_at"),
        "last_greeting_at": state.get("last_greeting_at"),
        "last_camera_switch_at": state.get("last_camera_switch_at"),
        "last_identity": state.get("last_identity", IDENTITY_UNKNOWN),
        "last_motion_score": state.get("last_motion_score", 0),
        "status": status,
        "opencv_available": available,
        "camera_status": status,
        "identity_mode": settings.get("camera_presence_identity_mode", "unknown_only"),
        "door_enabled": bool(settings.get("camera_presence_door_enabled", True)),
        "desk_enabled": bool(settings.get("camera_presence_desk_enabled", True)),
        "check_interval_seconds": _safe_int(settings.get("camera_presence_check_interval_seconds"), 10, minimum=5, maximum=60),
        "greeting_cooldown_seconds": _safe_int(settings.get("camera_presence_greeting_cooldown_seconds"), 120, minimum=10, maximum=86400),
        "unknown_greeting_enabled": bool(settings.get("camera_presence_unknown_greeting_enabled", False)),
        "primary_user_greeting_enabled": bool(settings.get("camera_presence_primary_user_greeting_enabled", False)),
        "desk_absence_timeout_seconds": _settings_timeout(settings),
        "motion_threshold": _safe_float(settings.get("camera_presence_motion_threshold"), 18.0, minimum=0.0, maximum=255.0),
        "auto_handoff_enabled": bool(settings.get("camera_presence_auto_handoff_enabled", False)),
        "state": state,
    }


def reset_camera_presence_state(settings: Optional[dict] = None) -> dict:
    next_state = _default_camera_presence_state()

    if isinstance(settings, dict):
        next_state = _sync_indices_from_settings(next_state, settings)
        next_state["mode"] = MODE_DOOR_WATCH if next_state.get("enabled") else MODE_IDLE
        next_state["active_camera_role"] = _active_role_for_mode(next_state["mode"])
        next_state["status"] = "watching" if next_state.get("enabled") else "idle"

    save_camera_presence_state(next_state)
    return next_state


def force_camera_presence_mode(mode: str, settings: Optional[dict] = None) -> dict:
    state = load_camera_presence_state()
    next_state = _sync_indices_from_settings(state, settings or {})
    next_state["mode"] = _normalize_mode(mode)
    next_state["active_camera_role"] = _active_role_for_mode(next_state["mode"])
    next_state["last_camera_switch_at"] = _now_iso()
    next_state["status"] = "watching" if next_state["active_camera_role"] else "idle"
    save_camera_presence_state(next_state)
    return next_state
