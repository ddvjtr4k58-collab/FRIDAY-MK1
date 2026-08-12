import datetime
import json
import re
import uuid
from pathlib import Path
from typing import Optional, Tuple


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = PROJECT_ROOT / "Data"
TASKS_FILE = DATA_DIR / "tasks.json"
VALID_PRIORITIES = {"low", "normal", "high", "urgent"}
WEEKDAY_INDEX = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6
}


def _now() -> datetime.datetime:
    return datetime.datetime.now().replace(microsecond=0)


def _now_iso() -> str:
    return _now().isoformat(timespec="seconds")


def _default_data() -> dict:
    return {"tasks": []}


def _backup_corrupt_file() -> None:
    if not TASKS_FILE.exists():
        return

    timestamp = _now().strftime("%Y%m%d_%H%M%S")
    backup = TASKS_FILE.with_name(f"tasks_corrupt_{timestamp}.json")

    try:
        TASKS_FILE.rename(backup)
    except Exception:
        pass


def _normalize_priority(priority: str) -> str:
    value = str(priority or "normal").strip().lower()
    return value if value in VALID_PRIORITIES else "normal"


def _parse_iso(value: Optional[str]) -> Optional[datetime.datetime]:
    if not value:
        return None

    try:
        return datetime.datetime.fromisoformat(str(value))
    except Exception:
        return None


def _safe_task(task: dict) -> dict:
    source = task if isinstance(task, dict) else {}
    title = str(source.get("title") or "").strip()

    return {
        "id": str(source.get("id") or f"task_{uuid.uuid4().hex[:10]}"),
        "title": title,
        "notes": str(source.get("notes") or ""),
        "created_at": str(source.get("created_at") or _now_iso()),
        "due_at": source.get("due_at") if source.get("due_at") else None,
        "completed": bool(source.get("completed", False)),
        "completed_at": source.get("completed_at") if source.get("completed_at") else None,
        "priority": _normalize_priority(source.get("priority")),
        "source": str(source.get("source") or "voice"),
        "reminder_sent": bool(source.get("reminder_sent", False)),
        "recurrence": source.get("recurrence") if source.get("recurrence") else None
    }


def load_tasks() -> dict:
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    if not TASKS_FILE.exists():
        return save_tasks(_default_data())

    try:
        with open(TASKS_FILE, "r") as tasks_file:
            data = json.load(tasks_file)
    except Exception:
        _backup_corrupt_file()
        return save_tasks(_default_data())

    if not isinstance(data, dict) or not isinstance(data.get("tasks"), list):
        _backup_corrupt_file()
        return save_tasks(_default_data())

    safe_data = {
        "tasks": [
            _safe_task(task)
            for task in data.get("tasks", [])
            if isinstance(task, dict) and str(task.get("title") or "").strip()
        ]
    }

    return safe_data


def save_tasks(data: dict) -> dict:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    safe_data = {
        "tasks": [
            _safe_task(task)
            for task in (data.get("tasks", []) if isinstance(data, dict) else [])
            if isinstance(task, dict) and str(task.get("title") or "").strip()
        ]
    }

    try:
        with open(TASKS_FILE, "w") as tasks_file:
            json.dump(safe_data, tasks_file, indent=2)
    except Exception:
        return _default_data()

    return safe_data


def get_all_tasks(include_completed: bool = False) -> list:
    tasks = load_tasks().get("tasks", [])

    if include_completed:
        return tasks

    return [task for task in tasks if not task.get("completed")]


def get_active_tasks() -> list:
    return get_all_tasks(include_completed=False)


def _task_due(task: dict) -> Optional[datetime.datetime]:
    return _parse_iso(task.get("due_at") if isinstance(task, dict) else None)


def _start_of_day(value: datetime.datetime) -> datetime.datetime:
    return value.replace(hour=0, minute=0, second=0, microsecond=0)


def _end_of_day(value: datetime.datetime) -> datetime.datetime:
    return _start_of_day(value) + datetime.timedelta(days=1)


def get_due_tasks(now: Optional[datetime.datetime] = None) -> list:
    current = now or _now()
    due = []

    for task in get_active_tasks():
        due_at = _task_due(task)

        if due_at and due_at <= current:
            due.append(task)

    return _sort_tasks(due, now=current)


def get_overdue_tasks(now: Optional[datetime.datetime] = None) -> list:
    return get_due_tasks(now)


def get_today_tasks(now: Optional[datetime.datetime] = None) -> list:
    current = now or _now()
    start = _start_of_day(current)
    end = _end_of_day(current)
    tasks = []

    for task in get_active_tasks():
        due_at = _task_due(task)

        if due_at and start <= due_at < end:
            tasks.append(task)

    return _sort_tasks(tasks, now=current)


def get_tomorrow_tasks(now: Optional[datetime.datetime] = None) -> list:
    current = now or _now()
    start = _end_of_day(current)
    end = start + datetime.timedelta(days=1)
    tasks = []

    for task in get_active_tasks():
        due_at = _task_due(task)

        if due_at and start <= due_at < end:
            tasks.append(task)

    return _sort_tasks(tasks, now=current)


def _priority_rank(task: dict) -> int:
    ranks = {"urgent": 0, "high": 1, "normal": 2, "low": 3}
    return ranks.get(str(task.get("priority") or "normal").lower(), 2)


def _sort_tasks(tasks: list, now: Optional[datetime.datetime] = None) -> list:
    current = now or _now()

    def key(task: dict):
        due_at = _task_due(task)
        overdue_rank = 0 if due_at and due_at <= current else 1
        due_rank = due_at or datetime.datetime.max
        created = _parse_iso(task.get("created_at")) or datetime.datetime.min
        return (overdue_rank, _priority_rank(task), due_rank, -created.timestamp())

    return sorted(tasks, key=key)


def add_task(
    title: str,
    due_at: Optional[str] = None,
    notes: str = "",
    priority: str = "normal",
    source: str = "voice"
) -> dict:
    clean_title = str(title or "").strip()

    if not clean_title:
        return {"ok": False, "error": "missing_title"}

    task = {
        "id": f"task_{uuid.uuid4().hex[:12]}",
        "title": clean_title,
        "notes": str(notes or ""),
        "created_at": _now_iso(),
        "due_at": due_at if due_at else None,
        "completed": False,
        "completed_at": None,
        "priority": _normalize_priority(priority),
        "source": str(source or "voice"),
        "reminder_sent": False,
        "recurrence": None
    }

    data = load_tasks()
    data["tasks"].append(task)
    save_tasks(data)
    return {"ok": True, "task": task}


def _query_terms(query: str) -> list:
    value = re.sub(r"[^a-z0-9 ]+", " ", str(query or "").lower())
    value = re.sub(
        r"\b(mark|complete|completed|finish|finished|done|task|reminder|delete|remove|clear|the|a|an|to|my|me)\b",
        " ",
        value
    )
    return [term for term in re.sub(r"\s+", " ", value).strip().split(" ") if len(term) >= 2]


def _match_task(query: str, tasks: list) -> Optional[dict]:
    raw_query = str(query or "").strip().lower()

    for task in tasks:
        if raw_query and raw_query == str(task.get("id") or "").lower():
            return task

    terms = _query_terms(query)
    query_value = " ".join(terms)

    if not terms:
        return None

    matches = []

    for task in tasks:
        title = str(task.get("title") or "").lower()
        title_words = set(re.findall(r"[a-z0-9]+", title))
        score = 0

        if query_value and query_value in title:
            score += 20

        for term in terms:
            if term in title:
                score += 4
            elif term in title_words:
                score += 3

        if score > 0:
            matches.append((score, task))

    if not matches:
        return None

    current = _now()
    matches.sort(
        key=lambda item: (
            -item[0],
            0 if _task_due(item[1]) and _task_due(item[1]) <= current else 1,
            _priority_rank(item[1]),
            _task_due(item[1]) or datetime.datetime.max,
            -(_parse_iso(item[1].get("created_at")) or datetime.datetime.min).timestamp()
        )
    )
    return matches[0][1]


def complete_task(query: str) -> Optional[dict]:
    data = load_tasks()
    task = _match_task(query, [item for item in data.get("tasks", []) if not item.get("completed")])

    if not task:
        return None

    task["completed"] = True
    task["completed_at"] = _now_iso()
    save_tasks(data)
    return task


def delete_task(query: str) -> Optional[dict]:
    data = load_tasks()
    task = _match_task(query, data.get("tasks", []))

    if not task:
        return None

    data["tasks"] = [item for item in data.get("tasks", []) if item.get("id") != task.get("id")]
    save_tasks(data)
    return task


def clear_completed_tasks() -> int:
    data = load_tasks()
    tasks = data.get("tasks", [])
    kept = [task for task in tasks if not task.get("completed")]
    removed = len(tasks) - len(kept)
    save_tasks({"tasks": kept})
    return removed


def _format_due(value: Optional[str]) -> str:
    due_at = _parse_iso(value)

    if not due_at:
        return ""

    return due_at.strftime("%b %-d at %-I:%M %p").replace(":00 ", " ")


def build_tasks_payload() -> dict:
    now = _now()
    tasks = load_tasks().get("tasks", [])
    active = [task for task in tasks if not task.get("completed")]
    completed = [task for task in tasks if task.get("completed")]
    overdue = get_overdue_tasks(now)
    today = get_today_tasks(now)
    tomorrow = get_tomorrow_tasks(now)
    week_end = _start_of_day(now) + datetime.timedelta(days=7)

    tomorrow_ids = {task.get("id") for task in tomorrow}
    today_ids = {task.get("id") for task in today}
    overdue_ids = {task.get("id") for task in overdue}
    this_week = []
    later = []

    for task in _sort_tasks(active, now=now):
        task_id = task.get("id")
        due_at = _task_due(task)

        if task_id in overdue_ids or task_id in today_ids or task_id in tomorrow_ids:
            continue

        if due_at and due_at < week_end:
            this_week.append(task)
        else:
            later.append(task)

    return {
        "tasks": tasks,
        "active": _sort_tasks(active, now=now),
        "completed": sorted(completed, key=lambda item: item.get("completed_at") or "", reverse=True),
        "overdue": overdue,
        "today": today,
        "tomorrow": tomorrow,
        "this_week": this_week,
        "later": later,
        "counts": {
            "total_active": len(active),
            "overdue": len(overdue),
            "today": len(today),
            "tomorrow": len(tomorrow),
            "completed": len(completed)
        },
        "next_task": _sort_tasks(active, now=now)[0] if active else None,
        "generated_at": now.isoformat(timespec="seconds")
    }


def format_tasks_summary() -> str:
    payload = build_tasks_payload()
    overdue_count = int(payload.get("counts", {}).get("overdue") or 0)
    today_count = int(payload.get("counts", {}).get("today") or 0)
    next_task = payload.get("next_task")

    if overdue_count:
        return f"{overdue_count} overdue task{'s' if overdue_count != 1 else ''}."

    if today_count:
        if next_task:
            due_text = _format_due(next_task.get("due_at"))
            suffix = f" at {due_text.split(' at ', 1)[-1]}" if due_text and " at " in due_text else ""
            return f"{today_count} tasks today, next is {next_task.get('title')}{suffix}."

        return f"{today_count} tasks today."

    return "No tasks due today."


def _parse_time_text(text: str) -> Optional[Tuple[int, int]]:
    value = str(text or "").strip().lower()
    value = value.replace(".", "")

    if value == "noon":
        return 12, 0

    if value == "midnight":
        return 0, 0

    match = re.search(r"\b(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\b", value)

    if not match:
        return None

    hour = int(match.group(1))
    minute = int(match.group(2) or 0)
    meridiem = match.group(3)

    if hour > 23 or minute > 59:
        return None

    if meridiem:
        if hour < 1 or hour > 12:
            return None

        if meridiem == "pm" and hour != 12:
            hour += 12
        elif meridiem == "am" and hour == 12:
            hour = 0
    elif hour <= 8:
        hour += 12

    return hour, minute


def _strip_phrase(text: str, start: int, end: int) -> str:
    return re.sub(r"\s+", " ", f"{text[:start]} {text[end:]}").strip(" ,.")


def _extract_at_time(value: str) -> Optional[Tuple[Tuple[int, int], int, int]]:
    patterns = (
        r"\bat\s+(noon|midnight|\d{1,2}(?::\d{2})?\s*(?:am|pm)?)\b",
        r"\b(noon|midnight|\d{1,2}(?::\d{2})?\s*(?:am|pm))\b"
    )

    for pattern in patterns:
        match = re.search(pattern, value, flags=re.IGNORECASE)

        if match:
            parsed = _parse_time_text(match.group(1))

            if parsed:
                return parsed, match.start(), match.end()

    return None


def parse_due_datetime_from_text(
    text: str,
    now: Optional[datetime.datetime] = None
) -> Tuple[Optional[str], str]:
    current = now or _now()
    original = str(text or "").strip()
    value = original.lower()

    match = re.search(r"\bin\s+(\d+)\s+(minute|minutes|hour|hours)\b", value)
    if match:
        amount = int(match.group(1))
        unit = match.group(2)
        delta = datetime.timedelta(minutes=amount) if unit.startswith("minute") else datetime.timedelta(hours=amount)
        due = current + delta
        return due.isoformat(timespec="seconds"), _strip_phrase(original, match.start(), match.end())

    target_date = None
    remove_start = None
    remove_end = None
    time_hint = _extract_at_time(value)
    parsed_time = time_hint[0] if time_hint else None

    for phrase in ("tomorrow", "today", "tonight"):
        match = re.search(rf"\b{phrase}\b", value)

        if match:
            if phrase in {"today", "tonight"}:
                target_date = current.date()
            else:
                target_date = (current + datetime.timedelta(days=1)).date()

            remove_start = match.start()
            remove_end = match.end()
            break

    if target_date is None:
        match = re.search(r"\bnext\s+(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b", value)

        if match:
            target = WEEKDAY_INDEX[match.group(1)]
            days = (target - current.weekday()) % 7

            if days == 0:
                days = 7

            target_date = (current + datetime.timedelta(days=days)).date()
            remove_start = match.start()
            remove_end = match.end()

    if target_date is None:
        match = re.search(r"\b(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b", value)

        if match:
            target = WEEKDAY_INDEX[match.group(1)]
            days = (target - current.weekday()) % 7

            if days == 0:
                days = 7

            target_date = (current + datetime.timedelta(days=days)).date()
            remove_start = match.start()
            remove_end = match.end()

    if target_date is None and parsed_time:
        target_date = current.date()

    if target_date is None:
        return None, original

    if parsed_time:
        hour, minute = parsed_time
    elif "tonight" in value:
        hour, minute = 20, 0
    else:
        hour, minute = 9, 0

    due = datetime.datetime.combine(target_date, datetime.time(hour=hour, minute=minute))

    if due <= current and target_date == current.date():
        due = due + datetime.timedelta(days=1)

    cleaned = original

    ranges = []
    if remove_start is not None and remove_end is not None:
        ranges.append((remove_start, remove_end))
    if time_hint:
        ranges.append((time_hint[1], time_hint[2]))

    for start, end in sorted(ranges, reverse=True):
        cleaned = _strip_phrase(cleaned, start, end)

    return due.isoformat(timespec="seconds"), cleaned


load_tasks()
