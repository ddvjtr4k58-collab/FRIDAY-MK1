import datetime
import json
import subprocess
import threading
from pathlib import Path
from typing import Any, Dict, Optional


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = PROJECT_ROOT / "Data"
PRESENCE_STATE_FILE = DATA_DIR / "presence_state.json"
MAC_IDLE_COMMAND = "ioreg -c IOHIDSystem | awk '/HIDIdleTime/ {print int($NF/1000000000); exit}'"
PRESENCE_SOURCE = "mac_idle"
RETURN_IDLE_THRESHOLD_SECONDS = 30

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


def _default_presence_state() -> Dict[str, Any]:
    return {
        "user_present": True,
        "last_seen_at": _now_iso(),
        "away_since": None,
        "last_return_greeting_at": None,
        "last_idle_seconds": 0,
        "source": PRESENCE_SOURCE
    }


def _normalize_presence_state(state: Any) -> Dict[str, Any]:
    default_state = _default_presence_state()
    normalized = dict(default_state)

    if isinstance(state, dict):
        normalized.update(state)

    normalized["user_present"] = bool(normalized.get("user_present", True))
    normalized["last_seen_at"] = str(normalized.get("last_seen_at") or default_state["last_seen_at"])
    normalized["away_since"] = normalized.get("away_since") or None
    normalized["last_return_greeting_at"] = normalized.get("last_return_greeting_at") or None

    try:
        last_idle_seconds = int(normalized.get("last_idle_seconds") or 0)
    except (TypeError, ValueError):
        last_idle_seconds = 0

    normalized["last_idle_seconds"] = max(0, last_idle_seconds)
    normalized["source"] = PRESENCE_SOURCE
    return normalized


def _atomic_write_json(path: Path, payload: Dict[str, Any]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    temp_file = path.with_suffix(f"{path.suffix}.tmp")

    with temp_file.open("w", encoding="utf-8") as file:
        json.dump(payload, file, indent=2)
        file.write("\n")

    temp_file.replace(path)


def load_presence_state() -> dict:
    with _state_lock:
        DATA_DIR.mkdir(parents=True, exist_ok=True)

        if not PRESENCE_STATE_FILE.exists():
            state = _default_presence_state()
            _atomic_write_json(PRESENCE_STATE_FILE, state)
            return state

        try:
            with PRESENCE_STATE_FILE.open("r", encoding="utf-8") as file:
                data = json.load(file)
        except (OSError, json.JSONDecodeError):
            data = {}

        state = _normalize_presence_state(data)

        if state != data:
            _atomic_write_json(PRESENCE_STATE_FILE, state)

        return state


def save_presence_state(state: dict) -> None:
    with _state_lock:
        _atomic_write_json(PRESENCE_STATE_FILE, _normalize_presence_state(state))


def get_idle_seconds() -> Optional[int]:
    try:
        result = subprocess.run(
            ["sh", "-c", MAC_IDLE_COMMAND],
            capture_output=True,
            text=True,
            timeout=2,
            check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        return None

    if result.returncode != 0:
        return None

    output = str(result.stdout or "").strip().splitlines()

    if not output:
        return None

    try:
        return max(0, int(output[0].strip()))
    except (TypeError, ValueError):
        return None


def _timeout_seconds(settings: dict) -> int:
    try:
        minutes = int(float(settings.get("presence_idle_timeout_minutes", 10)))
    except (TypeError, ValueError):
        minutes = 10

    return max(1, minutes) * 60


def _away_seconds(state: dict) -> Optional[int]:
    away_since = _parse_iso(state.get("away_since"))

    if not away_since:
        return None

    return max(0, int((_now() - away_since).total_seconds()))


def get_presence_snapshot() -> dict:
    state = load_presence_state()
    idle_seconds = get_idle_seconds()

    if idle_seconds is None:
        status = "Unknown"
    elif bool(state.get("user_present", True)):
        status = "Present"
    else:
        status = "Away"

    return {
        "user_present": bool(state.get("user_present", True)) if idle_seconds is not None else None,
        "user_presence": status,
        "status": status,
        "last_seen_at": state.get("last_seen_at"),
        "away_since": state.get("away_since"),
        "last_return_greeting_at": state.get("last_return_greeting_at"),
        "last_idle_seconds": state.get("last_idle_seconds", 0),
        "idle_seconds": idle_seconds,
        "away_seconds": _away_seconds(state),
        "source": PRESENCE_SOURCE
    }


def is_user_present(settings: dict) -> bool:
    idle_seconds = get_idle_seconds()

    if idle_seconds is None:
        return False

    return idle_seconds < _timeout_seconds(settings)


def should_mark_away(settings: dict, state: dict) -> bool:
    if not bool(settings.get("presence_mode_enabled", False)):
        return False

    if not bool(state.get("user_present", True)):
        return False

    idle_seconds = state.get("last_idle_seconds")

    try:
        idle_value = int(idle_seconds)
    except (TypeError, ValueError):
        return False

    return idle_value >= _timeout_seconds(settings)


def should_mark_returned(settings: dict, state: dict) -> bool:
    if not bool(settings.get("presence_mode_enabled", False)):
        return False

    if bool(state.get("user_present", True)):
        return False

    idle_seconds = state.get("last_idle_seconds")

    try:
        idle_value = int(idle_seconds)
    except (TypeError, ValueError):
        return False

    return idle_value < RETURN_IDLE_THRESHOLD_SECONDS


def mark_away(state: dict) -> dict:
    next_state = _normalize_presence_state(state)
    now = _now()

    try:
        idle_seconds = int(next_state.get("last_idle_seconds") or 0)
    except (TypeError, ValueError):
        idle_seconds = 0

    if idle_seconds > 0:
        last_seen_at = now - datetime.timedelta(seconds=idle_seconds)
        next_state["last_seen_at"] = last_seen_at.replace(microsecond=0).isoformat()

    next_state["user_present"] = False
    next_state["away_since"] = next_state.get("away_since") or now.isoformat()
    next_state["source"] = PRESENCE_SOURCE
    return next_state


def mark_returned(state: dict) -> dict:
    next_state = _normalize_presence_state(state)
    next_state["user_present"] = True
    next_state["last_seen_at"] = _now_iso()
    next_state["away_since"] = None
    next_state["source"] = PRESENCE_SOURCE
    return next_state


def format_return_greeting(away_seconds: Optional[int]) -> str:
    if away_seconds is None or away_seconds < 60:
        return "Welcome back, Boss."

    minutes = max(1, int(round(float(away_seconds) / 60.0)))
    unit = "minute" if minutes == 1 else "minutes"
    return f"Welcome back, Boss. You were away for {minutes} {unit}."


def build_presence_payload() -> dict:
    try:
        from Core_Cognition.settings_manager import load_settings
        settings = load_settings()
    except Exception:
        settings = {}

    snapshot = get_presence_snapshot()
    timeout_minutes = settings.get("presence_idle_timeout_minutes", 10)

    try:
        timeout_minutes = int(timeout_minutes)
    except (TypeError, ValueError):
        timeout_minutes = 10

    return {
        "presence_mode": "Enabled" if bool(settings.get("presence_mode_enabled", False)) else "Disabled",
        "presence_mode_enabled": bool(settings.get("presence_mode_enabled", False)),
        "presence_source": "Mac idle",
        "presence_source_id": PRESENCE_SOURCE,
        "user_presence": snapshot.get("user_presence", "Unknown"),
        "idle_seconds": snapshot.get("idle_seconds"),
        "last_idle_seconds": snapshot.get("last_idle_seconds", 0),
        "away_timeout_minutes": max(1, timeout_minutes),
        "last_seen_at": snapshot.get("last_seen_at"),
        "away_since": snapshot.get("away_since"),
        "away_seconds": snapshot.get("away_seconds"),
        "return_greeting_enabled": bool(settings.get("presence_return_greeting_enabled", True)),
        "auto_sleep_enabled": bool(settings.get("presence_auto_sleep_enabled", True)),
        "auto_launch_ui": bool(settings.get("presence_auto_launch_ui", False)),
        "auto_open_workstation": bool(settings.get("presence_auto_open_workstation", False)),
        "quiet_hours_enabled": bool(settings.get("presence_quiet_hours_enabled", False)),
        "state": snapshot
    }
