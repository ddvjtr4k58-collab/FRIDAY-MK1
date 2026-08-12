import datetime
import json
import os
import re
import threading
import time
import uuid
from pathlib import Path

from Core_Cognition.state_manager import (
    broadcast_notification,
    broadcast_notification_center,
    broadcast_notification_history,
    get_presence_state,
)
from Sensory_Array.calendar_tools import (
    detect_calendar_intent,
    event_starts_within,
    format_event_start,
    get_calendar_summary_for_daily_briefing,
    get_today_agenda,
    get_today_priority_reminders,
    is_calendar_connected
)
from Sensory_Array.system_tools import get_news_payload, get_weather_payload

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = PROJECT_ROOT / "Data"
STATE_FILE = DATA_DIR / "proactive_state.json"
SEEN_FILE = DATA_DIR / "proactive_seen.json"
NOTIFICATIONS_FILE = DATA_DIR / "notifications.json"
MAX_NOTIFICATIONS = 50

DEFAULT_COOLDOWN_SECONDS = int((os.getenv("FRIDAY_PROACTIVE_COOLDOWN_SECONDS") or os.getenv("JARVIS_PROACTIVE_COOLDOWN_SECONDS", "7200")))
DEFAULT_TOPICS = {
    "kosovo": False,
    "markets": False,
    "conflict": False,
    "weather": False,
    "calendar": False,
    "tasks": False
}

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

WATCH_TOPICS = set(DEFAULT_TOPICS.keys())
NOTIFICATION_ONLY_TOPICS = {"presence", "tasks"}
WEATHER_TERMS = {
    "storm", "snow", "tornado", "severe", "warning", "watch", "thunderstorm",
    "hail", "flood", "extreme", "dangerous"
}
MARKET_TERMS = {
    "selloff", "crash", "surge", "rally", "fed", "inflation", "rates",
    "volatility", "recession", "jobs", "treasury"
}
CONFLICT_TERMS = {
    "attack", "ceasefire", "missile", "invasion", "war", "killed", "strike",
    "escalation", "troops", "nato", "iran", "ukraine", "gaza"
}

state_lock = threading.RLock()
monitor_thread = None


def _now() -> datetime.datetime:
    return datetime.datetime.now()


def _now_iso() -> str:
    return _now().isoformat(timespec="seconds")


def _today() -> str:
    return _now().date().isoformat()


def _natural_date() -> str:
    now = _now()
    day = now.day

    if 10 <= day % 100 <= 20:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(day % 10, "th")

    return f"{now.strftime('%A %B')} {day}{suffix} {now.year}"


def _ensure_data_dir() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)


def _read_json(path: Path, default):
    try:
        if not path.exists():
            return default

        with open(path, "r") as file:
            data = json.load(file)

        return data if isinstance(data, type(default)) else default
    except Exception as error:
        print(f"[FRIDAY proactive read failed: {path.name}: {error}]")
        return default


def _write_json(path: Path, data) -> None:
    _ensure_data_dir()
    temp_path = path.with_suffix(f"{path.suffix}.tmp")

    try:
        with open(temp_path, "w") as file:
            json.dump(data, file, indent=2, sort_keys=True)

        os.replace(temp_path, path)
    except Exception as error:
        print(f"[FRIDAY proactive write failed: {path.name}: {error}]")


def _default_state() -> dict:
    return {
        "daily_briefing_date": "",
        "enabled_topics": dict(DEFAULT_TOPICS),
        "last_alert_at": {},
        "away_alerts": []
    }


def _load_state() -> dict:
    _ensure_data_dir()
    state = _read_json(STATE_FILE, _default_state())

    if not isinstance(state.get("enabled_topics"), dict):
        state["enabled_topics"] = {}

    for topic, enabled in DEFAULT_TOPICS.items():
        state["enabled_topics"].setdefault(topic, enabled)

    if not isinstance(state.get("last_alert_at"), dict):
        state["last_alert_at"] = {}

    if not isinstance(state.get("away_alerts"), list):
        state["away_alerts"] = []

    state.setdefault("daily_briefing_date", "")
    return state


def _save_state(state: dict) -> None:
    _write_json(STATE_FILE, state)


def _load_seen() -> dict:
    _ensure_data_dir()
    seen = _read_json(SEEN_FILE, {})

    for topic in WATCH_TOPICS:
        if not isinstance(seen.get(topic), list):
            seen[topic] = []

    return seen


def _save_seen(seen: dict) -> None:
    _write_json(SEEN_FILE, seen)


def _load_notifications() -> list[dict]:
    _ensure_data_dir()
    notifications = _read_json(NOTIFICATIONS_FILE, [])

    if not isinstance(notifications, list):
        return []

    return [
        notification
        for notification in notifications
        if isinstance(notification, dict)
    ]


def _save_notifications(notifications: list[dict]) -> None:
    _write_json(NOTIFICATIONS_FILE, notifications[-MAX_NOTIFICATIONS:])


def _headline_items(payload: dict, tab_name: str) -> list[dict]:
    tabs = payload.get("tabs", {}) if isinstance(payload, dict) else {}
    tab = tabs.get(tab_name) or tabs.get(payload.get("active_tab")) or {}
    items = tab.get("items", []) if isinstance(tab, dict) else []
    return [item for item in items if isinstance(item, dict) and item.get("title")]


def _new_items(topic: str, items: list[dict], seen: dict, force: bool) -> list[dict]:
    titles = [str(item.get("title", "")).strip() for item in items if item.get("title")]
    seen_titles = set(seen.get(topic, []))

    if not force and not seen_titles:
        seen[topic] = list(dict.fromkeys(titles[:80]))
        return []

    if force:
        new_titles = titles[:3]
    else:
        new_titles = [title for title in titles if title and title not in seen_titles]

    seen[topic] = list(dict.fromkeys((titles + seen.get(topic, []))[:80]))
    title_set = set(new_titles)
    return [item for item in items if item.get("title") in title_set][:3]


def _contains_any(value: str, terms: set[str]) -> bool:
    lowered = value.lower()
    return any(term in lowered for term in terms)


def _parse_iso(value: str):
    if not value:
        return None

    try:
        return datetime.datetime.fromisoformat(value)
    except Exception:
        return None


def _cooldown_remaining(topic: str, state: dict) -> int:
    last_alert = _parse_iso(state.get("last_alert_at", {}).get(topic, ""))

    if not last_alert:
        return 0

    elapsed = (_now() - last_alert).total_seconds()
    return max(0, int(DEFAULT_COOLDOWN_SECONDS - elapsed))


def _allowed_by_cooldown(topic: str, state: dict, force: bool) -> bool:
    return force or _cooldown_remaining(topic, state) <= 0


def _priority_for_topic(topic: str, text: str) -> str:
    lowered = text.lower()

    if topic == "weather" and _contains_any(lowered, {"tornado", "severe", "warning", "dangerous", "flood"}):
        return "high"

    if topic == "conflict" and _contains_any(lowered, {"attack", "missile", "invasion", "killed", "escalation"}):
        return "high"

    if topic == "markets" and _contains_any(lowered, {"crash", "selloff", "recession"}):
        return "high"

    if topic in {"kosovo", "markets", "conflict", "weather"}:
        return "medium"

    return "low"


def _build_alert(topic: str, title: str, message: str, items: list, priority: str = "medium") -> dict:
    return {
        "topic": topic,
        "priority": priority,
        "title": title,
        "message": message,
        "items": items,
        "timestamp": _now_iso(),
        "speak": priority == "high"
    }


def _check_news_topic(topic: str, state: dict, seen: dict, force: bool) -> list[dict]:
    if not _allowed_by_cooldown(topic, state, force):
        return []

    payload = get_news_payload(active_tab=topic)
    items = _headline_items(payload, topic)
    new_items = _new_items(topic, items, seen, force)

    if not new_items:
        return []

    title_text = " ".join(str(item.get("title", "")) for item in new_items)

    if topic == "markets" and not force and not _contains_any(title_text, MARKET_TERMS):
        return []

    if topic == "conflict" and not force and not _contains_any(title_text, CONFLICT_TERMS):
        return []

    labels = {
        "kosovo": "Kosovo monitor",
        "markets": "Market monitor",
        "conflict": "Conflict monitor"
    }
    topic_messages = {
        "kosovo": "New Kosovo signal detected.",
        "markets": "Market signal changed.",
        "conflict": "Conflict signal changed."
    }
    priority = _priority_for_topic(topic, title_text)
    alert_items = [item.get("title", "") for item in new_items[:3]]
    alert = _build_alert(topic, labels[topic], topic_messages[topic], alert_items, priority)
    state["last_alert_at"][topic] = alert["timestamp"]
    return [alert]


def _check_weather_topic(default_location: str, state: dict, seen: dict, force: bool) -> list[dict]:
    topic = "weather"

    if not _allowed_by_cooldown(topic, state, force):
        return []

    weather_text = _weather_summary(default_location)
    seen_weather = seen.get(topic, [])

    if not force and weather_text in seen_weather:
        return []

    seen[topic] = [weather_text] + [item for item in seen_weather if item != weather_text][:20]

    if not force and not _contains_any(weather_text, WEATHER_TERMS):
        return []

    priority = _priority_for_topic(topic, weather_text)
    alert = _build_alert(
        topic,
        "Weather monitor",
        weather_text or "Weather changed.",
        [weather_text] if weather_text else [],
        priority
    )
    state["last_alert_at"][topic] = alert["timestamp"]
    return [alert]


def _check_calendar_topic(state: dict, seen: dict, force: bool) -> list[dict]:
    topic = "calendar"

    if not _allowed_by_cooldown(topic, state, force):
        return []

    if not is_calendar_connected():
        return []

    alerts = []
    seen_keys = list(seen.get(topic, []))
    seen_set = set(seen_keys)
    new_seen_keys = []

    for reminder in get_today_priority_reminders():
        event = reminder.get("event", {})
        key = _calendar_seen_key(reminder.get("type", "event"), event)

        if not force and key in seen_set:
            continue

        new_seen_keys.append(key)
        alerts.append(
            _build_alert(
                topic,
                "Calendar monitor",
                str(reminder.get("message") or "Calendar reminder"),
                [event.get("title", "Calendar item")],
                str(reminder.get("priority") or "medium")
            )
        )

    for event in get_today_agenda():
        if not event_starts_within(event, minutes=60):
            continue

        intent = detect_calendar_intent(event)

        if intent.get("type") != "event":
            continue

        key = _calendar_seen_key("soon", event)

        if not force and key in seen_set:
            continue

        title = event.get("title", "Calendar item")
        start_time = format_event_start(event)
        new_seen_keys.append(key)
        alerts.append(
            _build_alert(
                topic,
                "Calendar monitor",
                f"Calendar reminder, {title} starts at {start_time}",
                [title],
                "medium" if intent.get("priority") == "low" else str(intent.get("priority") or "medium")
            )
        )

    if alerts:
        seen[topic] = list(dict.fromkeys(new_seen_keys + seen_keys))[:120]
        state["last_alert_at"][topic] = alerts[0]["timestamp"]

    return alerts


def _deliver_alert(alert: dict) -> None:
    presence_state = get_presence_state()

    if presence_state in {"VACANT", "UNAUTHORIZED"}:
        queue_away_alert(alert)
        return

    create_proactive_hud_alert(alert)

    if alert.get("speak") and presence_state == "AUTHORIZED":
        try:
            from Sensory_Array.audio_engine import speak
            speak(alert.get("message", "Proactive alert ready."))
        except Exception as error:
            print(f"[FRIDAY proactive speech failed: {error}]")


def _monitor_loop(interval_seconds: int) -> None:
    while True:
        try:
            alerts = check_proactive_alerts(force=False)

            for alert in alerts:
                _deliver_alert(alert)
        except Exception as error:
            print(f"[FRIDAY proactive monitor failed: {error}]")

        time.sleep(max(60, int(interval_seconds or 900)))


def start_proactive_monitor(interval_seconds: int = 900) -> None:
    global monitor_thread

    with state_lock:
        if monitor_thread and monitor_thread.is_alive():
            return

        broadcast_notification_history(get_notifications())

        monitor_thread = threading.Thread(
            target=_monitor_loop,
            args=(interval_seconds,),
            daemon=True
        )
        monitor_thread.start()


def build_daily_briefing(default_location: str) -> str:
    weather_summary = _weather_summary(default_location)
    calendar_summary = _compact_calendar_summary(get_calendar_summary_for_daily_briefing())

    return (
        f"Welcome Boss, {_natural_date()}, {weather_summary}, "
        f"{calendar_summary}, Kosovo markets active wars tracked."
    )


def should_show_daily_briefing() -> bool:
    with state_lock:
        return _load_state().get("daily_briefing_date") != _today()


def mark_daily_briefing_shown() -> None:
    with state_lock:
        state = _load_state()
        state["daily_briefing_date"] = _today()
        _save_state(state)


def reset_daily_briefing() -> str:
    with state_lock:
        state = _load_state()
        state["daily_briefing_date"] = ""
        _save_state(state)

    return "Daily briefing reset."


def build_short_return_greeting() -> str:
    return "Systems online, monitoring continues."


def check_proactive_alerts(force: bool = False) -> list[dict]:
    alerts = []

    with state_lock:
        state = _load_state()
        seen = _load_seen()
        enabled_topics = state.get("enabled_topics", {})
        default_location = (os.getenv("FRIDAY_DEFAULT_LOCATION") or os.getenv("JARVIS_DEFAULT_LOCATION", "Warrenville, IL"))

        for topic in ["kosovo", "markets", "conflict"]:
            if enabled_topics.get(topic, False):
                try:
                    alerts.extend(_check_news_topic(topic, state, seen, force))
                except Exception as error:
                    print(f"[FRIDAY proactive {topic} check failed: {error}]")

        if enabled_topics.get("weather", False):
            try:
                alerts.extend(_check_weather_topic(default_location, state, seen, force))
            except Exception as error:
                print(f"[FRIDAY proactive weather check failed: {error}]")

        if enabled_topics.get("calendar", False):
            try:
                alerts.extend(_check_calendar_topic(state, seen, force))
            except Exception as error:
                print(f"[FRIDAY proactive calendar check failed: {error}]")

        _save_seen(seen)
        _save_state(state)

    return alerts


def register_watch_topic(topic: str) -> str:
    normalized = _normalize_topic(topic)

    if normalized not in WATCH_TOPICS:
        return "Unknown monitor."

    with state_lock:
        state = _load_state()
        state["enabled_topics"][normalized] = True
        _save_state(state)

    return f"Monitoring {_topic_command_label(normalized)}."


def disable_watch_topic(topic: str) -> str:
    normalized = _normalize_topic(topic)

    if normalized not in WATCH_TOPICS:
        return "Unknown monitor."

    with state_lock:
        state = _load_state()
        state["enabled_topics"][normalized] = False
        _save_state(state)

    return f"{_topic_label(normalized)} monitor disabled."


def get_watch_status() -> str:
    with state_lock:
        state = _load_state()
        enabled = [
            _topic_label(topic)
            for topic in WATCH_TOPICS
            if state.get("enabled_topics", {}).get(topic, False)
        ]

        if not enabled:
            return "No proactive monitors are active."

        cooldown_hours = DEFAULT_COOLDOWN_SECONDS // 3600
        topics = ", ".join(sorted(enabled))
        return f"Monitoring {topics}, cooldown is {cooldown_hours} hours."


def queue_away_alert(alert: dict) -> None:
    notification = record_notification(alert)

    with state_lock:
        state = _load_state()
        away_alerts = state.setdefault("away_alerts", [])
        away_alerts.append(notification)
        state["away_alerts"] = away_alerts[-20:]
        _save_state(state)


def consume_away_alerts() -> list[dict]:
    with state_lock:
        state = _load_state()
        alerts = list(state.get("away_alerts", []))
        state["away_alerts"] = []
        _save_state(state)
        return alerts


def build_away_summary(alerts: list[dict]) -> str:
    if not alerts:
        return "No critical changes while you were away."

    topics = []

    for alert in alerts:
        label = _topic_label(str(alert.get("topic", ""))).lower()

        if label and label not in topics:
            topics.append(label)

    if not topics:
        return "No critical changes while you were away."

    if len(topics) == 1:
        topic_text = topics[0]
    else:
        topic_text = ", ".join(topics[:-1]) + f" and {topics[-1]}"

    return f"While you were away, {topic_text} changed."


def record_notification(alert: dict) -> dict:
    topic = _normalize_topic(str(alert.get("topic", "")))
    timestamp = str(alert.get("timestamp") or _now_iso())
    items = alert.get("items", [])

    if not isinstance(items, list):
        items = []

    notification = {
        "id": str(alert.get("id") or f"note_{int(time.time() * 1000)}_{uuid.uuid4().hex[:8]}"),
        "topic": topic if topic in WATCH_TOPICS or topic in NOTIFICATION_ONLY_TOPICS else "calendar",
        "title": str(alert.get("title") or "Proactive alert"),
        "message": str(alert.get("message") or ""),
        "items": [str(item) for item in items[:5]],
        "priority": str(alert.get("priority") or "low"),
        "timestamp": timestamp,
        "target": _notification_target(topic),
        # Explicitly unread on arrival; the Notification Center's unread count and
        # "mark read" action both need a state to move away from.
        "read": False
    }

    with state_lock:
        notifications = _load_notifications()
        notifications.append(notification)
        _save_notifications(notifications)

    broadcast_notification_history(get_notifications())
    return notification


def get_notifications(limit: int = 50) -> list[dict]:
    try:
        safe_limit = max(1, min(int(limit), MAX_NOTIFICATIONS))
    except Exception:
        safe_limit = MAX_NOTIFICATIONS

    with state_lock:
        notifications = _load_notifications()

    return list(reversed(notifications[-safe_limit:]))


def clear_notifications() -> str:
    with state_lock:
        _save_notifications([])

    broadcast_notification_history([])
    return "Notifications cleared."


def dismiss_notification(notification_id: str) -> bool:
    """Remove a single notification. Returns whether anything was removed."""
    target = str(notification_id or "").strip()

    if not target:
        return False

    with state_lock:
        notifications = _load_notifications()
        remaining = [n for n in notifications if str(n.get("id") or "") != target]

        if len(remaining) == len(notifications):
            return False

        _save_notifications(remaining)

    broadcast_notification_history(get_notifications())
    return True


def _set_read_state(notification_ids, read: bool) -> int:
    """Mark specific notifications read/unread. Empty ids means all of them."""
    wanted = {str(value) for value in (notification_ids or []) if str(value).strip()}
    changed = 0

    with state_lock:
        notifications = _load_notifications()

        for notification in notifications:
            if wanted and str(notification.get("id") or "") not in wanted:
                continue

            if bool(notification.get("read")) != read:
                notification["read"] = read
                changed += 1

        if changed:
            _save_notifications(notifications)

    if changed:
        broadcast_notification_history(get_notifications())

    return changed


def mark_notifications_read(notification_ids=None) -> int:
    return _set_read_state(notification_ids, True)


def unread_notification_count() -> int:
    with state_lock:
        return sum(1 for n in _load_notifications() if not n.get("read"))


def create_notification_center_card() -> None:
    broadcast_notification_center(get_notifications())


def create_proactive_hud_alert(alert: dict) -> None:
    notification = record_notification(alert)
    broadcast_notification(notification)


def _notification_target(topic: str) -> str:
    normalized = _normalize_topic(topic)

    if normalized in WATCH_TOPICS:
        return normalized

    if normalized == "tasks":
        return "tasks"

    if normalized == "presence":
        return "presence"

    return "calendar"


def _weather_summary(default_location: str) -> str:
    try:
        payload = get_weather_payload(default_location)
        location = _clean_briefing_fragment(payload.get("location") or default_location).replace(", ", " ")
        temperature = payload.get("temperature")

        if temperature is None:
            return "weather is unavailable"

        return f"{location} {temperature} degrees"
    except Exception:
        return "weather is unavailable"


def _clean_briefing_fragment(value: str) -> str:
    text = str(value or "").strip()

    for abbreviation, full_name in STATE_NAME_MAP.items():
        text = re.sub(rf"\b{abbreviation}\b", full_name, text)

    text = text.replace("!", "").replace("¡", "")
    text = text.replace(";", ",").replace(":", ",").replace(".", ",")
    text = re.sub(r"\s*,\s*", ", ", text)
    text = re.sub(r",{2,}", ",", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip(" \t\n\r,.;:!?")


def _compact_calendar_summary(value: str) -> str:
    text = _clean_briefing_fragment(value)
    text = re.sub(r"^you have\s+", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s+calendar items today,\s+first is\s+", " calendar items, first ", text, flags=re.IGNORECASE)
    text = re.sub(r"\s+events today,\s+first is\s+", " events, first ", text, flags=re.IGNORECASE)
    text = text.replace(" at ", " ")
    return text


def _calendar_seen_key(reminder_type: str, event: dict) -> str:
    title = str(event.get("title") or "calendar item").lower().strip()
    start = str(event.get("start") or "").strip()
    key_text = re.sub(r"[^a-z0-9]+", "-", f"{_today()} {reminder_type} {title} {start}".lower())
    return key_text.strip("-")[:180]


def _normalize_topic(topic: str) -> str:
    value = re.sub(r"\s+", " ", str(topic or "").lower()).strip()
    value = re.sub(r"^the\s+", "", value).strip()
    aliases = {
        "kosova": "kosovo",
        "market": "markets",
        "financial markets": "markets",
        "stock": "markets",
        "stocks": "markets",
        "conflict": "conflict",
        "conflicts": "conflict",
        "active wars": "conflict",
        "wars": "conflict",
        "war": "conflict"
    }
    return aliases.get(value, value)


def _topic_label(topic: str) -> str:
    labels = {
        "kosovo": "Kosovo",
        "markets": "markets",
        "conflict": "active wars",
        "weather": "weather",
        "calendar": "calendar"
    }
    return labels.get(topic, topic)


def _topic_command_label(topic: str) -> str:
    labels = {
        "kosovo": "Kosovo",
        "markets": "markets",
        "conflict": "conflicts",
        "weather": "weather",
        "calendar": "calendar"
    }
    return labels.get(topic, topic)
