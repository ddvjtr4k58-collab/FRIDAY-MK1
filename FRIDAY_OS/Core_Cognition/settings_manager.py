import copy
import datetime
import json
import os
from pathlib import Path
from typing import Any, Dict, Optional


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = PROJECT_ROOT / "Data"
SETTINGS_FILE = DATA_DIR / "settings.json"

THEME_LABELS = {
    "blue": "FRIDAY Blue",
    "green": "Tactical Green",
    "white": "White Mode",
    "midnight": "Midnight",
    "graphite": "Graphite",
    "high-contrast": "High Contrast"
}

THEME_ALIASES = {
    "friday blue": "blue",
    "jarvis blue": "blue",  # legacy name for the same theme
    "blue": "blue",
    "dark blue": "blue",
    "default": "blue",
    "green": "green",
    "tactical green": "green",
    "white": "white",
    "white mode": "white",
    "light": "white",
    "light mode": "white",
    "midnight": "midnight",
    "midnight theme": "midnight",
    "graphite": "graphite",
    "graphite mode": "graphite",
    "charcoal": "graphite",
    "steel": "graphite",
    "monochrome": "graphite",
    "high contrast": "high-contrast",
    "high-contrast": "high-contrast"
}

VOICE_PROVIDERS = ("friday_local", "fish", "auto")

DEFAULT_SETTINGS = {
    "theme": "blue",
    "voice_enabled": True,
    "performance_logs": True,
    "default_location": "Warrenville, IL",
    "silent_operator_model": "gemini-2.5-flash",
    "startup_mode": "sleep",
    "privacy_mode": False,
    "show_notifications": True,
    "show_task_widget_on_wake": False,
    "show_hotbar": True,
    "silent_operator_enabled": True,
    "workshop_restore_layout": True,
    "showcase_return_to_sleep": True,
    "voice_speed": 0.85,
    # Voice pipeline. "friday_local" is the fast native macOS path and the
    # default for the FRIDAY identity: it starts speaking in a fraction of the
    # time a cloud round trip takes. "fish" remains available as the optional
    # higher-fidelity provider; "auto" is Fish-first with local fallback.
    "voice_provider": "friday_local",
    "voice_local_name": "Moira",
    "voice_rate": 190,
    "assistant_name": "FRIDAY",
    "voice_volume": 1.0,
    "voice_pause_threshold": 0.6,
    "voice_listen_timeout": 8,
    "voice_phrase_time_limit": 18,
    "voice_energy_threshold": 0,
    "voice_follow_up_window_seconds": 10,
    "voice_echo_settle_ms": 220,
    "voice_audio_reactive_orb": True,
    "voice_performance_logs": True,
    "voice_interrupt_enabled": True,
    "presence_mode_enabled": False,
    "presence_idle_timeout_minutes": 10,
    "presence_return_greeting_enabled": True,
    "presence_auto_sleep_enabled": True,
    "presence_auto_launch_ui": False,
    "presence_auto_open_workstation": False,
    "presence_quiet_hours_enabled": False,
    "camera_presence_enabled": False,
    "camera_presence_door_enabled": True,
    "camera_presence_desk_enabled": True,
    "camera_presence_door_device_index": 1,
    "camera_presence_desk_device_index": 0,
    "camera_presence_check_interval_seconds": 10,
    "camera_presence_greeting_cooldown_seconds": 120,
    "camera_presence_desk_absence_timeout_seconds": 180,
    "camera_presence_motion_threshold": 18,
    "camera_presence_identity_mode": "unknown_only",
    "camera_presence_unknown_greeting_enabled": False,
    "camera_presence_primary_user_greeting_enabled": False,
    "camera_presence_auto_handoff_enabled": False
}


def _safe_default_settings() -> Dict[str, Any]:
    defaults = copy.deepcopy(DEFAULT_SETTINGS)
    defaults["default_location"] = (os.getenv("FRIDAY_DEFAULT_LOCATION") or os.getenv("JARVIS_DEFAULT_LOCATION", defaults["default_location"]))
    defaults["silent_operator_model"] = (
        (os.getenv("FRIDAY_SILENT_MODEL") or os.getenv("JARVIS_SILENT_MODEL", defaults["silent_operator_model"])).strip()
        or defaults["silent_operator_model"]
    )
    return defaults


def normalize_theme(value: Any) -> str:
    raw = str(value or "").strip().lower().replace("_", "-")
    raw = " ".join(raw.split())
    normalized = THEME_ALIASES.get(raw, raw)
    return normalized if normalized in THEME_LABELS else "blue"


def theme_label(value: Any) -> str:
    return THEME_LABELS.get(normalize_theme(value), THEME_LABELS["blue"])


def _coerce_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value

    if value is None:
        return bool(default)

    if isinstance(value, str):
        normalized = value.strip().lower()

        if normalized in {"1", "true", "yes", "on", "enabled"}:
            return True

        if normalized in {"0", "false", "no", "off", "disabled"}:
            return False

    return bool(value)


def _coerce_timeout_minutes(value: Any, default: int = 10) -> int:
    try:
        minutes = int(float(value))
    except (TypeError, ValueError):
        minutes = default

    return max(1, min(minutes, 1440))


def _coerce_voice_speed(value: Any, default: float = 0.85) -> float:
    try:
        speed = float(value)
    except (TypeError, ValueError):
        speed = default

    return max(0.65, min(speed, 1.1))


def _coerce_int_range(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        number = int(float(value))
    except (TypeError, ValueError):
        number = default

    return max(minimum, min(number, maximum))


def _coerce_float_range(value: Any, default: float, minimum: float, maximum: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = default

    return max(minimum, min(number, maximum))


def normalize_voice_provider(value: Any) -> str:
    raw = str(value or "").strip().lower().replace("-", "_")

    aliases = {
        "": "friday_local",
        "local": "friday_local",
        "friday": "friday_local",
        "friday local": "friday_local",
        "native": "friday_local",
        "macos": "friday_local",
        "say": "friday_local",
        "system": "friday_local",
        "fish_audio": "fish",
        "cloud": "fish",
        "paid": "fish",
        "clone": "fish",
        "jarvis": "fish",  # legacy alias — kept so saved settings still load
        "hybrid": "auto",
    }

    normalized = aliases.get(raw, raw)
    return normalized if normalized in VOICE_PROVIDERS else "friday_local"


def _coerce_voice_name(value: Any) -> str:
    # Empty means "detect a suitable installed voice at runtime".
    name = str(value or "").strip()
    return name if len(name) <= 64 else name[:64].strip()


def _normalize_settings(settings: Dict[str, Any]) -> Dict[str, Any]:
    defaults = _safe_default_settings()
    merged = defaults

    if isinstance(settings, dict):
        merged.update(settings)

    merged["theme"] = normalize_theme(merged.get("theme"))
    merged["voice_enabled"] = bool(merged.get("voice_enabled", True))
    merged["performance_logs"] = bool(merged.get("performance_logs", True))
    merged["default_location"] = str(
        merged.get("default_location") or defaults["default_location"]
    ).strip() or defaults["default_location"]
    merged["silent_operator_model"] = str(
        merged.get("silent_operator_model") or defaults["silent_operator_model"]
    ).strip() or defaults["silent_operator_model"]
    merged["startup_mode"] = str(merged.get("startup_mode") or "sleep").strip().lower()

    if merged["startup_mode"] == "active":
        merged["startup_mode"] = "workstation"

    if merged["startup_mode"] not in {"sleep", "workstation"}:
        merged["startup_mode"] = "sleep"

    for key in (
        "privacy_mode",
        "show_notifications",
        "show_task_widget_on_wake",
        "show_hotbar",
        "silent_operator_enabled",
        "workshop_restore_layout",
        "showcase_return_to_sleep"
    ):
        merged[key] = bool(merged.get(key, defaults.get(key, False)))

    for key in (
        "presence_mode_enabled",
        "presence_return_greeting_enabled",
        "presence_auto_sleep_enabled",
        "presence_auto_launch_ui",
        "presence_auto_open_workstation",
        "presence_quiet_hours_enabled",
        "camera_presence_enabled",
        "camera_presence_door_enabled",
        "camera_presence_desk_enabled",
        "camera_presence_unknown_greeting_enabled",
        "camera_presence_primary_user_greeting_enabled",
        "camera_presence_auto_handoff_enabled"
    ):
        merged[key] = _coerce_bool(merged.get(key), defaults.get(key, False))

    merged["presence_idle_timeout_minutes"] = _coerce_timeout_minutes(
        merged.get("presence_idle_timeout_minutes"),
        defaults.get("presence_idle_timeout_minutes", 10)
    )
    merged["voice_speed"] = _coerce_voice_speed(
        merged.get("voice_speed"),
        defaults.get("voice_speed", 0.85)
    )

    for key in ("voice_audio_reactive_orb", "voice_performance_logs", "voice_interrupt_enabled"):
        merged[key] = _coerce_bool(merged.get(key), defaults.get(key, True))

    merged["voice_provider"] = normalize_voice_provider(merged.get("voice_provider"))
    merged["assistant_name"] = str(
        merged.get("assistant_name") or defaults.get("assistant_name", "FRIDAY")
    ).strip()[:24] or "FRIDAY"
    merged["voice_local_name"] = _coerce_voice_name(merged.get("voice_local_name"))
    merged["voice_rate"] = _coerce_int_range(
        merged.get("voice_rate"), defaults.get("voice_rate", 178), 90, 300
    )
    merged["voice_volume"] = _coerce_float_range(
        merged.get("voice_volume"), defaults.get("voice_volume", 1.0), 0.1, 1.0
    )
    merged["voice_pause_threshold"] = _coerce_float_range(
        merged.get("voice_pause_threshold"), defaults.get("voice_pause_threshold", 0.6), 0.3, 2.0
    )
    merged["voice_listen_timeout"] = _coerce_int_range(
        merged.get("voice_listen_timeout"), defaults.get("voice_listen_timeout", 8), 2, 30
    )
    merged["voice_phrase_time_limit"] = _coerce_int_range(
        merged.get("voice_phrase_time_limit"), defaults.get("voice_phrase_time_limit", 18), 4, 60
    )
    merged["voice_energy_threshold"] = _coerce_int_range(
        merged.get("voice_energy_threshold"), defaults.get("voice_energy_threshold", 0), 0, 4000
    )
    merged["voice_follow_up_window_seconds"] = _coerce_int_range(
        merged.get("voice_follow_up_window_seconds"),
        defaults.get("voice_follow_up_window_seconds", 10),
        0,
        60,
    )
    merged["voice_echo_settle_ms"] = _coerce_int_range(
        merged.get("voice_echo_settle_ms"), defaults.get("voice_echo_settle_ms", 220), 0, 1500
    )
    merged["camera_presence_door_device_index"] = _coerce_int_range(
        merged.get("camera_presence_door_device_index"),
        defaults.get("camera_presence_door_device_index", 1),
        0,
        20
    )
    merged["camera_presence_desk_device_index"] = _coerce_int_range(
        merged.get("camera_presence_desk_device_index"),
        defaults.get("camera_presence_desk_device_index", 0),
        0,
        20
    )
    merged["camera_presence_check_interval_seconds"] = _coerce_int_range(
        merged.get("camera_presence_check_interval_seconds"),
        defaults.get("camera_presence_check_interval_seconds", 10),
        5,
        60
    )
    merged["camera_presence_greeting_cooldown_seconds"] = _coerce_int_range(
        merged.get("camera_presence_greeting_cooldown_seconds"),
        defaults.get("camera_presence_greeting_cooldown_seconds", 120),
        10,
        86400
    )
    merged["camera_presence_desk_absence_timeout_seconds"] = _coerce_int_range(
        merged.get("camera_presence_desk_absence_timeout_seconds"),
        defaults.get("camera_presence_desk_absence_timeout_seconds", 180),
        10,
        86400
    )
    merged["camera_presence_motion_threshold"] = _coerce_float_range(
        merged.get("camera_presence_motion_threshold"),
        defaults.get("camera_presence_motion_threshold", 18),
        0,
        255
    )
    merged["camera_presence_identity_mode"] = "unknown_only"

    return merged


def _backup_corrupt_settings_file() -> None:
    if not SETTINGS_FILE.exists():
        return

    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = SETTINGS_FILE.with_name(f"settings_corrupt_{timestamp}.json")

    try:
        SETTINGS_FILE.rename(backup_path)
    except OSError:
        pass


def load_settings() -> Dict[str, Any]:
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    if not SETTINGS_FILE.exists():
        settings = _normalize_settings({})
        save_settings(settings)
        return settings

    corrupted = False

    try:
        with SETTINGS_FILE.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        data = {}
        corrupted = True

    if not isinstance(data, dict):
        corrupted = True

    if corrupted:
        _backup_corrupt_settings_file()

    settings = _normalize_settings(data if isinstance(data, dict) else {})

    if settings != data:
        save_settings(settings)

    return settings


def save_settings(settings: Dict[str, Any]) -> Dict[str, Any]:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    normalized = _normalize_settings(settings)
    temp_file = SETTINGS_FILE.with_suffix(".json.tmp")

    with temp_file.open("w", encoding="utf-8") as f:
        json.dump(normalized, f, indent=2)
        f.write("\n")

    temp_file.replace(SETTINGS_FILE)
    return normalized


def get_setting(key: str, default: Optional[Any] = None) -> Any:
    settings = load_settings()
    return settings.get(key, default)


def set_setting(key: str, value: Any) -> Dict[str, Any]:
    settings = load_settings()
    settings[str(key)] = value
    return save_settings(settings)


def update_settings(values: Dict[str, Any]) -> Dict[str, Any]:
    settings = load_settings()

    if isinstance(values, dict):
        settings.update(values)

    return save_settings(settings)
