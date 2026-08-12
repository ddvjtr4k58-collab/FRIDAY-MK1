import datetime
import calendar
import os
import re
from pathlib import Path
from typing import Optional

try:
    from zoneinfo import ZoneInfo
except Exception:
    ZoneInfo = None

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CREDENTIALS_FILE = PROJECT_ROOT / "Core_Cognition" / "calendar_credentials.json"
TOKEN_FILE = PROJECT_ROOT / "Data" / "calendar_token.json"
SCOPES = ["https://www.googleapis.com/auth/calendar.readonly"]
USER_NAME = (os.getenv("FRIDAY_USER_NAME") or os.getenv("JARVIS_USER_NAME", "Jon")).strip() or "Jon"

_calendar_service = None
_dependency_warning_printed = False
_calendar_auth_warning_printed = False
_last_calendar_error = ""
FRIDAY_DEBUG = (os.getenv("FRIDAY_DEBUG") or os.getenv("JARVIS_DEBUG", "")).strip().lower() in {"1", "true", "yes", "on"}

try:
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build
except Exception:
    Request = None
    Credentials = None
    InstalledAppFlow = None
    build = None


def _dependencies_available() -> bool:
    global _dependency_warning_printed

    available = all([Request, Credentials, InstalledAppFlow, build])

    if not available and not _dependency_warning_printed:
        _dependency_warning_printed = True
        print(
            "Calendar dependencies missing. Install google-api-python-client "
            "google-auth-httplib2 google-auth-oauthlib."
        )

    return available


def _debug(message: str) -> None:
    if FRIDAY_DEBUG:
        print(f"[FRIDAY calendar debug: {message}]")


def _set_calendar_error(message: str) -> None:
    global _last_calendar_error
    _last_calendar_error = str(message or "").strip()


def _is_invalid_grant(error: Exception) -> bool:
    return "invalid_grant" in str(error or "").lower()


def _handle_auth_error(error: Exception) -> None:
    global _calendar_auth_warning_printed
    global _calendar_service

    _calendar_service = None

    if _is_invalid_grant(error):
        _set_calendar_error("Reauthorization required")

        if not _calendar_auth_warning_printed:
            _calendar_auth_warning_printed = True
            print(
                "[FRIDAY calendar connection failed: Reauthorization required. "
                "Run ./reconnect_calendar.sh or say reconnect calendar.]"
            )

        return

    _set_calendar_error(str(error))
    print(f"[FRIDAY calendar connection failed: {error}]")


def _remove_token_file(path: Path) -> None:
    try:
        if path.exists():
            path.unlink()
    except OSError:
        pass


def remove_calendar_tokens() -> None:
    for token_path in (
        TOKEN_FILE,
        PROJECT_ROOT / "calendar_token.json",
        PROJECT_ROOT / "token.json"
    ):
        _remove_token_file(token_path)


def _local_tz():
    try:
        return datetime.datetime.now().astimezone().tzinfo
    except Exception:
        if ZoneInfo is not None:
            return ZoneInfo("America/Chicago")

        return datetime.timezone(datetime.timedelta(hours=-5))


def _local_now() -> datetime.datetime:
    return datetime.datetime.now(_local_tz())


def _day_bounds(offset_days: int = 0) -> tuple[datetime.datetime, datetime.datetime]:
    target_date = (_local_now() + datetime.timedelta(days=offset_days)).date()
    start = datetime.datetime.combine(target_date, datetime.time.min, tzinfo=_local_tz())
    end = start + datetime.timedelta(days=1)
    return start, end


def _coerce_datetime(value: datetime.datetime) -> datetime.datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=_local_tz())

    return value.astimezone(_local_tz())


def _parse_event_datetime(value: str) -> Optional[datetime.datetime]:
    if not value:
        return None

    try:
        return datetime.datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone()
    except Exception:
        try:
            parsed_date = datetime.date.fromisoformat(value)
            return datetime.datetime.combine(parsed_date, datetime.time.min).astimezone()
        except Exception:
            return None


def _format_event_time(value: str, all_day: bool = False) -> str:
    if all_day:
        return "all day"

    parsed = _parse_event_datetime(value)

    if not parsed:
        return "unknown time"

    text = parsed.strftime("%I:%M %p").lstrip("0")
    return text.replace(":00 ", " ")


def _ordinal_day(day: int) -> str:
    if 10 <= day % 100 <= 20:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(day % 10, "th")

    return f"{day}{suffix}"


def _format_event_when(event: dict, include_today: bool = False) -> str:
    start_dt = _parse_event_datetime(str(event.get("start") or ""))

    if not start_dt:
        return "at unknown time"

    today = _local_now().date()
    event_date = start_dt.date()

    if event_date == today:
        day_text = "today" if include_today else ""
    elif event_date == today + datetime.timedelta(days=1):
        day_text = "tomorrow"
    elif event_date <= today + datetime.timedelta(days=6):
        day_text = start_dt.strftime("%A")
    else:
        day_text = f"{start_dt.strftime('%B')} {_ordinal_day(start_dt.day)}"

    if event.get("all_day"):
        if day_text:
            return f"{day_text} all day"

        return "all day"

    time_text = _format_event_time(str(event.get("start") or ""))

    if day_text:
        return f"{day_text} at {time_text}"

    return f"at {time_text}"


def _event_date_key(event: dict) -> str:
    if event.get("all_day") and event.get("start"):
        return str(event.get("start"))[:10]

    start_dt = _parse_event_datetime(str(event.get("start") or ""))

    if not start_dt:
        return ""

    return start_dt.date().isoformat()


def _event_text(event: dict) -> str:
    return f"{event.get('title', '')} {event.get('description', '')}".lower()


def _normalize_event(raw_event: dict) -> dict:
    start_data = raw_event.get("start", {}) if isinstance(raw_event, dict) else {}
    end_data = raw_event.get("end", {}) if isinstance(raw_event, dict) else {}
    start_value = start_data.get("dateTime") or start_data.get("date") or ""
    end_value = end_data.get("dateTime") or end_data.get("date") or ""
    all_day = "date" in start_data and "dateTime" not in start_data

    return {
        "id": str(raw_event.get("id") or ""),
        "title": str(raw_event.get("summary") or "Untitled event"),
        "start": start_value,
        "end": end_value,
        "all_day": all_day,
        "description": str(raw_event.get("description") or ""),
        "location": str(raw_event.get("location") or ""),
        # Attendees and organiser come back on the standard events.list response;
        # they were simply being discarded here. Carried through so the event
        # detail view can show real people rather than inventing any.
        "attendees": [
            {
                "email": str(person.get("email") or ""),
                "name": str(person.get("displayName") or ""),
                "status": str(person.get("responseStatus") or ""),
                "organizer": bool(person.get("organizer")),
                "self": bool(person.get("self"))
            }
            for person in (raw_event.get("attendees") or [])
            if isinstance(person, dict)
        ],
        "organizer": str(
            (raw_event.get("organizer") or {}).get("displayName")
            or (raw_event.get("organizer") or {}).get("email")
            or ""
        ),
        "calendar": "primary"
    }


def get_calendar_service(force_reauthorize: bool = False):
    global _calendar_service
    global _calendar_auth_warning_printed

    if _calendar_service is not None and not force_reauthorize:
        return _calendar_service

    if not _dependencies_available():
        _debug("dependencies missing")
        _set_calendar_error("Calendar dependencies missing")
        return None

    if not CREDENTIALS_FILE.exists():
        _debug(f"credentials missing at {CREDENTIALS_FILE}")
        _set_calendar_error("Calendar credentials missing")
        return None

    TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    credentials = None

    try:
        if force_reauthorize:
            remove_calendar_tokens()
            _calendar_auth_warning_printed = False

        if TOKEN_FILE.exists():
            _debug(f"loading token from {TOKEN_FILE}")
            credentials = Credentials.from_authorized_user_file(str(TOKEN_FILE), SCOPES)

            if hasattr(credentials, "has_scopes") and not credentials.has_scopes(SCOPES):
                _debug("token missing calendar.readonly scope")
                _set_calendar_error("Reauthorization required")
                credentials = None

        if credentials and credentials.expired and credentials.refresh_token:
            _debug("refreshing expired token")
            try:
                credentials.refresh(Request())
            except Exception as error:
                _handle_auth_error(error)
                return None

        if not credentials or not credentials.valid:
            if not force_reauthorize:
                _set_calendar_error("Reauthorization required" if TOKEN_FILE.exists() else "Calendar not connected")
                return None

            _debug(f"starting OAuth flow with {CREDENTIALS_FILE}")
            flow = InstalledAppFlow.from_client_secrets_file(str(CREDENTIALS_FILE), SCOPES)
            credentials = flow.run_local_server(port=0)

        with open(TOKEN_FILE, "w") as token_file:
            token_file.write(credentials.to_json())

        _debug(f"token saved to {TOKEN_FILE}")
        _calendar_service = build("calendar", "v3", credentials=credentials)
        _debug("calendar service built")
        _set_calendar_error("")
        return _calendar_service
    except Exception as error:
        _handle_auth_error(error)
        return None


def is_calendar_connected() -> bool:
    return get_calendar_service() is not None


def reauthorize_calendar() -> bool:
    remove_calendar_tokens()
    return get_calendar_service(force_reauthorize=True) is not None


def reconnect_calendar() -> str:
    if reauthorize_calendar():
        return "Calendar connected"

    error = get_last_calendar_error() or "OAuth failed"
    return f"Calendar reauthorization failed, {error}"


def get_events_for_range(
    start_dt: datetime.datetime,
    end_dt: datetime.datetime,
    max_results: int = 20
) -> list[dict]:
    global _last_calendar_error
    _last_calendar_error = ""
    service = get_calendar_service()

    if service is None:
        _last_calendar_error = "service unavailable"
        return []

    try:
        start_value = _coerce_datetime(start_dt).isoformat()
        end_value = _coerce_datetime(end_dt).isoformat()
        safe_max_results = max(1, int(max_results))
        _debug(f"query primary calendar {start_value} to {end_value}, max {safe_max_results}")
        events_result = service.events().list(
            calendarId="primary",
            timeMin=start_value,
            timeMax=end_value,
            maxResults=safe_max_results,
            singleEvents=True,
            orderBy="startTime"
        ).execute()
    except Exception as error:
        _last_calendar_error = str(error)
        print(f"[FRIDAY calendar read failed: {error}]")
        return []

    events = events_result.get("items", []) if isinstance(events_result, dict) else []
    _debug(f"calendar query returned {len(events)} events")
    return [_normalize_event(event) for event in events if isinstance(event, dict)]


def get_today_agenda() -> list[dict]:
    start, end = _day_bounds(0)
    return get_events_for_range(start, end)


def get_tomorrow_agenda() -> list[dict]:
    start, end = _day_bounds(1)
    return get_events_for_range(start, end)


def get_week_agenda(days: int = 7) -> list[dict]:
    safe_days = max(1, min(int(days or 7), 14))
    start = _local_now()
    end = start + datetime.timedelta(days=safe_days)
    return get_events_for_range(start, end, max_results=50)


def get_next_week_agenda() -> list[dict]:
    today = _local_now().date()
    start = datetime.datetime.combine(today + datetime.timedelta(days=7), datetime.time.min, tzinfo=_local_tz())
    end = start + datetime.timedelta(days=7)
    return get_events_for_range(start, end, max_results=50)


def get_next_two_weeks_agenda() -> list[dict]:
    start = _local_now()
    end = start + datetime.timedelta(days=14)
    return get_events_for_range(start, end, max_results=80)


def get_next_event() -> Optional[dict]:
    events = get_events_for_range(_local_now(), _local_now() + datetime.timedelta(days=30), max_results=10)
    return events[0] if events else None


def summarize_events(events: list[dict], label: str = "today") -> str:
    safe_label = str(label or "today").strip()

    if not events:
        return f"Calendar is clear {safe_label}"

    first_event = events[0]
    first_title = first_event.get("title", "Untitled event")
    first_when = _format_event_when(first_event, include_today=safe_label == "today")
    count = len(events)
    noun = "event" if count == 1 else "events"

    if safe_label == "tomorrow":
        first_time = _format_event_time(first_event.get("start", ""), first_event.get("all_day", False))
        time_phrase = first_time if first_event.get("all_day") else f"at {first_time}"
        return f"You have {count} {noun} tomorrow, {first_title} {time_phrase}"

    if safe_label in {"this week", "week", "next week", "the week after", "in the next two weeks"}:
        return f"You have {count} {noun} {safe_label}, next is {first_title} {first_when}"

    return f"You have {count} {noun} {safe_label}, {first_title} {first_when}"


def get_calendar_summary_for_daily_briefing() -> str:
    if not is_calendar_connected():
        if _last_calendar_error == "Reauthorization required":
            return "calendar needs reauthorization"

        return "calendar is not connected yet"

    events = get_today_agenda()

    if _last_calendar_error:
        return "calendar query failed"

    if not events:
        tomorrow_events = get_tomorrow_agenda()

        if _last_calendar_error:
            return "calendar query failed"

        if not tomorrow_events:
            return "calendar is clear today"

        first_event = tomorrow_events[0]
        first_title = first_event.get("title", "Untitled event")
        first_time = _format_event_time(first_event.get("start", ""), first_event.get("all_day", False))
        return f"calendar is clear today, tomorrow has {first_title} at {first_time}"

    first_event = events[0]
    first_title = first_event.get("title", "Untitled event")
    first_time = _format_event_time(first_event.get("start", ""), first_event.get("all_day", False))
    return f"you have {len(events)} calendar items today, first is {first_title} at {first_time}"


def get_last_calendar_error() -> str:
    return _last_calendar_error


def debug_calendar_probe() -> str:
    if not CREDENTIALS_FILE.exists():
        return "Calendar probe failed, credentials file missing"

    if not TOKEN_FILE.exists():
        _debug("token file missing, OAuth may start")

    service = get_calendar_service()

    if service is None:
        return "Calendar probe failed, service unavailable"

    events = get_events_for_range(_local_now(), _local_now() + datetime.timedelta(days=14), max_results=10)

    if _last_calendar_error:
        return "Calendar probe failed, query error"

    if not events:
        return "Calendar probe found no events in the next 14 days"

    next_event = events[0]
    return (
        f"Calendar probe found {len(events)} events, next is "
        f"{next_event.get('title', 'Untitled event')} {_format_event_when(next_event)}"
    )


def detect_calendar_intent(event: dict) -> dict:
    title = str(event.get("title") or "Untitled event").strip()
    text = _event_text(event)

    if any(term in text for term in ["test", "exam", "quiz", "midterm", "final"]):
        return {
            "type": "test",
            "priority": "high",
            "message": f"Do not forget your {title} today"
        }

    if any(term in text for term in ["homework", "assignment", "due", "deadline", "submit", "essay", "project due"]):
        assignment_title = _clean_assignment_title(title)
        return {
            "type": "homework",
            "priority": "high",
            "message": f"Assignment reminder, {assignment_title} is due today"
        }

    if any(term in text for term in ["study", "studying", "review", "prep", "prepare"]):
        return {
            "type": "study",
            "priority": "medium",
            "message": f"Study reminder, {title} is scheduled today"
        }

    if any(term in text for term in ["birthday", "bday", "born"]):
        name = _extract_birthday_name(title)
        family_terms = {
            "dad", "father", "baba", "mom", "mother", "brother", "sister", "wife", "husband",
            "son", "daughter", "grandma", "grandpa", "grandmother", "grandfather",
            "uncle", "aunt", "cousin"
        }
        is_family = any(term in name.lower().split() or term in text for term in family_terms)
        prefix = "Family birthday reminder" if is_family else "Birthday reminder"

        return {
            "type": "birthday",
            "priority": "medium",
            "message": f"{prefix}, congratulate {name} today"
        }

    return {
        "type": "event",
        "priority": "low",
        "message": f"Calendar reminder, {title} is scheduled today"
    }


def get_today_priority_reminders() -> list[dict]:
    return _priority_reminders_from_events(get_today_agenda())


def get_upcoming_priority_reminders(days: int = 7) -> list[dict]:
    return _priority_reminders_from_events(get_week_agenda(days))


def get_birthdays(days: int = 365) -> list[dict]:
    safe_days = max(1, min(int(days or 365), 730))
    now = _local_now()
    events = get_events_for_range(now, now + datetime.timedelta(days=safe_days), max_results=250)
    birthdays = []

    for event in events:
        text = _event_text(event)

        if not _is_birthday_text(text):
            continue

        name = _extract_birthday_name(str(event.get("title") or ""))
        start_dt = _parse_event_datetime(str(event.get("start") or ""))
        event_date = start_dt.date() if start_dt else now.date()
        family = _is_family_birthday(name, text)

        birthdays.append({
            "name": name,
            "title": event.get("title", "Birthday"),
            "date": _format_event_date(event),
            "iso_date": event_date.isoformat(),
            "days_until": max(0, (event_date - now.date()).days),
            "family": family,
            "event": event
        })

    birthdays.sort(key=lambda item: item.get("days_until", 9999))
    return birthdays


def get_next_birthday_record(name_query: Optional[str] = None, allow_nearest: bool = True, personal: bool = False) -> Optional[dict]:
    query = re.sub(r"\s+", " ", str(name_query or "").lower()).strip()
    birthdays = get_birthdays(370)

    if personal:
        personal_terms = {USER_NAME.lower(), "my", "me", "user"}

        for birthday in birthdays:
            searchable = f"{birthday.get('name', '')} {birthday.get('title', '')}".lower()

            if any(term in searchable for term in personal_terms):
                return birthday

        return birthdays[0] if allow_nearest and birthdays else None

    if query:
        for birthday in birthdays:
            searchable = f"{birthday.get('name', '')} {birthday.get('title', '')}".lower()

            if query in searchable:
                return birthday

        return None

    return birthdays[0] if allow_nearest and birthdays else None


def summarize_next_birthday(name_query: Optional[str] = None, allow_nearest: bool = True, personal: bool = False) -> str:
    record = get_next_birthday_record(name_query, allow_nearest=allow_nearest, personal=personal)

    if _last_calendar_error:
        return "Calendar query failed"

    if not record:
        return "No birthday record found"

    name = record.get("name", "birthday")
    date = record.get("date", "unknown date")

    if personal and str(name).lower() in {USER_NAME.lower(), "my", "me", "user"}:
        return f"Your birthday is {date}"

    if personal and str(record.get("title", "")).lower() in {"my birthday", "user birthday"}:
        return f"Your birthday is {date}"

    if name_query and str(name).lower() == USER_NAME.lower():
        return f"Your birthday is {date}"

    return f"Next birthday record is {name} on {date}"


def get_calendar_year(year: Optional[int] = None) -> dict:
    target_year = int(year or _local_now().year)
    start = datetime.datetime(target_year, 1, 1, tzinfo=_local_tz())
    end = datetime.datetime(target_year + 1, 1, 1, tzinfo=_local_tz())
    connected = is_calendar_connected()

    month_payloads = [
        {
            "month": month,
            "name": calendar.month_name[month],
            "events": []
        }
        for month in range(1, 13)
    ]

    if not connected:
        return {
            "year": target_year,
            "months": month_payloads,
            "today": _local_now().date().isoformat(),
            "connected": False,
            "error": _last_calendar_error
        }

    events = get_events_for_range(start, end, max_results=500)

    for event in events:
        widget_event = _widget_event(event)
        event_date = widget_event.get("date")

        if not event_date:
            continue

        try:
            event_month = int(event_date.split("-")[1])
        except Exception:
            continue

        if 1 <= event_month <= 12:
            month_payloads[event_month - 1]["events"].append(widget_event)

    return {
        "year": target_year,
        "months": month_payloads,
        "today": _local_now().date().isoformat(),
        "connected": True,
        "error": _last_calendar_error
    }


def get_calendar_widget_payload(year: Optional[int] = None) -> dict:
    connected = is_calendar_connected()
    target_year = int(year or _local_now().year)

    if not connected:
        status = _last_calendar_error if _last_calendar_error else "Calendar not connected"

        return {
            "connected": False,
            "year": target_year,
            "today": _local_now().date().isoformat(),
            "months": [],
            "today_events": [],
            "tomorrow_events": [],
            "upcoming_events": [],
            "priority_reminders": [],
            "birthdays": [],
            "status": status,
            "error": _last_calendar_error
        }

    year_payload = get_calendar_year(target_year)
    today_events = [_widget_event(event) for event in get_today_agenda()]
    tomorrow_events = [_widget_event(event) for event in get_tomorrow_agenda()]
    upcoming_events = [_widget_event(event) for event in get_events_for_range(_local_now(), _local_now() + datetime.timedelta(days=60), max_results=10)]
    priority_reminders = [
        _widget_reminder(reminder)
        for reminder in get_upcoming_priority_reminders(14)
    ]
    birthdays = get_birthdays(370)[:10]

    return {
        "connected": True,
        "year": target_year,
        "today": _local_now().date().isoformat(),
        "months": year_payload.get("months", []),
        "today_events": today_events,
        "tomorrow_events": tomorrow_events,
        "upcoming_events": upcoming_events,
        "priority_reminders": priority_reminders,
        "birthdays": birthdays,
        "status": "Google Calendar synced",
        "error": _last_calendar_error
    }


def event_starts_within(event: dict, minutes: int = 60) -> bool:
    if event.get("all_day"):
        return False

    start_dt = _parse_event_datetime(str(event.get("start") or ""))

    if not start_dt:
        return False

    delta = (start_dt - _local_now()).total_seconds()
    return 0 <= delta <= minutes * 60


def format_event_start(event: dict) -> str:
    return _format_event_time(str(event.get("start") or ""), bool(event.get("all_day")))


def format_event_date(event: dict) -> str:
    return _format_event_date(event)


def _priority_reminders_from_events(events: list[dict]) -> list[dict]:
    reminders = []

    for event in events:
        intent = detect_calendar_intent(event)

        if intent.get("type") == "event":
            continue

        reminders.append({
            **intent,
            "event": event
        })

    return reminders


def _extract_birthday_name(title: str) -> str:
    name = re.sub(r"\b(birth day|birthday|bday|born)\b", "", title, flags=re.IGNORECASE)
    name = re.sub(r"[’']s\b", "", name, flags=re.IGNORECASE)
    name = re.sub(r"\b(my|user)\b", USER_NAME, name, flags=re.IGNORECASE)
    name = re.sub(r"\s+", " ", name).strip(" \t\n\r,.;:!?-")
    return name or title or "them"


def _is_birthday_text(text: str) -> bool:
    return any(term in text for term in ["birthday", "bday", "birth day", "born"])


def _is_family_birthday(name: str, text: str) -> bool:
    family_terms = {
        "dad", "father", "baba", "mom", "mother", "brother", "sister", "wife", "husband",
        "son", "daughter", "grandma", "grandpa", "grandmother", "grandfather",
        "uncle", "aunt", "cousin"
    }
    name_words = set(str(name or "").lower().split())
    return bool(name_words.intersection(family_terms)) or any(term in text for term in family_terms)


def _widget_event(event: dict) -> dict:
    date_key = _event_date_key(event)
    return {
        "id": event.get("id", ""),
        "title": event.get("title", "Untitled event"),
        "start": event.get("start", ""),
        "end": event.get("end", ""),
        "date": date_key,
        "time": _format_event_time(str(event.get("start") or ""), bool(event.get("all_day"))).replace("all day", "All day"),
        "all_day": bool(event.get("all_day")),
        "location": event.get("location", ""),
        "description": event.get("description", ""),
        "attendees": event.get("attendees", []) or [],
        "organizer": event.get("organizer", ""),
        "calendar": event.get("calendar", "")
    }


def _widget_reminder(reminder: dict) -> dict:
    event = reminder.get("event", {}) if isinstance(reminder, dict) else {}
    return {
        "type": reminder.get("type", "event"),
        "priority": reminder.get("priority", "low"),
        "message": reminder.get("message", ""),
        "event": _widget_event(event)
    }


def _format_event_date(event: dict) -> str:
    start_dt = _parse_event_datetime(str(event.get("start") or ""))

    if not start_dt:
        return "unknown date"

    today = _local_now().date()
    event_date = start_dt.date()

    if event_date == today:
        return "today"

    if event_date == today + datetime.timedelta(days=1):
        return "tomorrow"

    return f"{start_dt.strftime('%B')} {_ordinal_day(start_dt.day)}"


def _clean_assignment_title(title: str) -> str:
    text = re.sub(r"\b(project due|due|deadline|submit)\b", "", title, flags=re.IGNORECASE)
    text = re.sub(r"\s+", " ", text).strip(" \t\n\r,.;:!?-")
    return text or title


if __name__ == "__main__":
    import argparse
    import sys

    parser = argparse.ArgumentParser(description="FRIDAY Google Calendar maintenance")
    parser.add_argument("--reauthorize", action="store_true", help="Run Google Calendar OAuth flow")
    args = parser.parse_args()

    if args.reauthorize:
        print("Starting Google Calendar reauthorization...")
        if reauthorize_calendar():
            print(f"Calendar connected. Token saved to {TOKEN_FILE}")
            sys.exit(0)

        print(f"Calendar reauthorization failed: {get_last_calendar_error() or 'unknown error'}")
        sys.exit(1)

    print("Calendar connected." if is_calendar_connected() else f"Calendar disconnected: {get_last_calendar_error()}")
