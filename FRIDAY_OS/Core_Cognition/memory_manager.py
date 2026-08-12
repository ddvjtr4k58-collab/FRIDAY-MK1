"""
FRIDAY Memory v2 — storage, retrieval, mutation and context construction.

WHY THIS MODULE EXISTS
----------------------
Memory v1 was a single append-only text file (memory.txt). Every line of it was
pasted into the system prompt of every model at startup, and nothing was ever
updated, removed, ranked, or scoped. That has three problems:

  1. The prompt grows forever, and most of it is irrelevant to the current turn.
  2. A fact can never be corrected — "Jon prefers Graphite" and "Jon prefers
     Midnight" simply pile up and the model picks whichever it likes.
  3. Nothing is scoped, so a note about one project leaks into every other one.

Memory v2 replaces that with four clearly separated kinds of memory, a small
structured store on disk, and ONE retrieval function that decides what is worth
showing the model for a given request.

THE FOUR KINDS OF MEMORY
------------------------
  A. CONVERSATION  — the recent turns of ONE Silent Operator chat. Owned by
                     state_manager (it already persists chat sessions); this
                     module only reads it. A new chat never inherits another
                     chat's turns.
  B. LONG-TERM     — durable facts about Jon that stay useful across the whole
                     system. Stored in long_term.json.
  C. PROJECT       — facts tied to one project/workspace. Stored in
                     projects.json, and never retrieved for a different project.
  D. WORKING       — short-lived references ("the file we were just discussing").
                     Stored in working.json with an expiry, and allowed to die.

THE FLOW
--------
    user message
        -> should_retrieve()          cheap gate; deterministic commands skip all of this
        -> build_memory_context()     ranks and selects a handful of memories
        -> format_memory_block()      renders clean prose lines, never raw JSON
        -> model
        -> capture_candidates()       conservatively stores anything worth keeping

Everything here is local, synchronous and dependency-free. There is no vector
database and no embedding call: retrieval is deterministic keyword + metadata
ranking, which is fast enough to be invisible next to a speech round trip and
simple enough to debug by reading a JSON file.
"""

import copy
import datetime
import json
import os
import re
import shutil
import threading
import uuid
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# ==========================================
# STORAGE LAYOUT
# ==========================================
# Data/memory/
#     long_term.json   durable user-scoped memories
#     projects.json    the project registry + project-scoped memories
#     working.json     short-lived per-conversation references
#     backups/         pre-migration copies, never touched again
#
# FRIDAY_MEMORY_DIR exists so the test suite can point the whole store at a
# temporary directory without touching real memory.

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MEMORY_DIR = PROJECT_ROOT / "Data" / "memory"
MEMORY_DIR = Path((os.getenv("FRIDAY_MEMORY_DIR") or os.getenv("JARVIS_MEMORY_DIR")) or DEFAULT_MEMORY_DIR)

LONG_TERM_FILE = MEMORY_DIR / "long_term.json"
PROJECTS_FILE = MEMORY_DIR / "projects.json"
WORKING_FILE = MEMORY_DIR / "working.json"
BACKUP_DIR = MEMORY_DIR / "backups"

STORE_VERSION = 2

# Scopes. A memory is either about Jon (user) or about one project (project).
SCOPE_USER = "user"
SCOPE_PROJECT = "project"

# Categories are a coarse label used for ranking and for the UI filter chips.
# They are deliberately few: more categories would mean more ways to mis-file a
# memory, and the retrieval layer only uses them as a mild ranking hint.
CATEGORIES = (
    "identity",     # who Jon is: name, birthday, how to address him
    "preference",   # likes, working style, themes, tools
    "person",       # important people
    "routine",      # recurring habits and schedules
    "goal",         # longer-term intentions
    "location",     # places that come up repeatedly
    "decision",     # a choice that was made and should stick
    "constraint",   # a limitation or rule to respect
    "fact"          # anything else worth keeping
)

# Core identity is the small set that may be offered to the model even when the
# request does not obviously mention it — a preferred name is relevant to every
# reply. Everything else has to earn its place by relevance.
CORE_CATEGORIES = {"identity", "preference"}

IMPORTANCE_MIN = 1
IMPORTANCE_MAX = 5

# Retrieval budget. These caps are what stop the prompt from growing without
# bound; they are intentionally small.
MAX_LONG_TERM = 4
MAX_PROJECT = 4
MAX_WORKING = 3
MAX_TURNS = 6
MAX_CORE_IDENTITY = 3
MIN_RELEVANCE_SCORE = 1.2
# Deleting needs a stronger match than reading: a wrong retrieval is noise, a
# wrong deletion is lost information.
FORGET_MIN_SCORE = 2.0
MAX_BLOCK_CHARS = 1800
MAX_LINE_CHARS = 200

# Working memory dies on its own. 45 minutes is long enough to cover "the file
# we were just discussing" and short enough that it never becomes a fact.
WORKING_TTL_SECONDS = 45 * 60
MAX_WORKING_PER_CONVERSATION = 12

# Growth limits for the durable stores. Pinned memories are never evicted.
MAX_LONG_TERM_RECORDS = 400
MAX_PROJECT_RECORDS = 400

DEFAULT_PROJECT_NAME = "FRIDAY-MK1"

_lock = threading.RLock()

# In-process caches. Reading three small JSON files on every turn would be
# wasteful, so each store is loaded once and reloaded only when the file on disk
# is newer than what we hold (which happens when a second FRIDAY window writes).
_cache: Dict[str, dict] = {}
_cache_mtime: Dict[str, float] = {}

# Token index for retrieval, keyed by memory id. Rebuilt lazily whenever the
# store changes, so ranking never re-tokenises the same text twice.
_token_index: Dict[str, Tuple[frozenset, frozenset]] = {}

# Recent conversation turns for sources that are NOT persisted chat sessions
# (spoken turns, the terminal). Workshop chats are read from state_manager
# instead, because that is where they actually live.
_turn_buffers: Dict[str, List[dict]] = {}
MAX_BUFFERED_TURNS = 20

# The last memory this module created or touched per conversation, so "forget
# that" and "update that memory" have something concrete to act on.
_last_touched: Dict[str, str] = {}

# The UI payload is rebuilt only when the store actually changed. state_update is
# broadcast on every interface change, and rebuilding the whole view each time
# would put a scan of the memory store in the path of opening a widget.
_view_cache: Dict[str, object] = {"key": None, "payload": None}
_store_revision = 0


# ==========================================
# SMALL HELPERS
# ==========================================
def _now() -> datetime.datetime:
    return datetime.datetime.now().replace(microsecond=0)


def _now_iso() -> str:
    return _now().isoformat(timespec="seconds")


def _parse_iso(value: Optional[str]) -> Optional[datetime.datetime]:
    if not value:
        return None

    try:
        return datetime.datetime.fromisoformat(str(value))
    except Exception:
        return None


def _new_id(prefix: str = "mem") -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


def _clean_text(value) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _clamp_importance(value) -> int:
    try:
        number = int(round(float(value)))
    except Exception:
        number = 2

    return max(IMPORTANCE_MIN, min(IMPORTANCE_MAX, number))


def _slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", str(value or "").strip().lower()).strip("-")
    return slug or "project"


# ==========================================
# TOKENISATION AND MATCHING
# ==========================================
# Retrieval is keyword based, so the quality of the whole system rests on this
# handful of lines. Two tokens count as related when they share a four-character
# prefix ("prefer" / "prefers" / "preference"), which is a crude stemmer that
# never needs a dictionary and never surprises anyone reading the code.

STOPWORDS = {
    "a", "about", "all", "am", "an", "and", "any", "are", "as", "at", "be",
    "been", "but", "by", "can", "did", "do", "does", "for", "from", "get",
    "got", "had", "has", "have", "he", "her", "him", "his", "how", "i", "if",
    "in", "into", "is", "it", "its", "just", "know", "let", "like", "me",
    "my", "no", "not", "of", "on", "one", "or", "our", "out", "please", "put",
    "she", "should", "so", "some", "tell", "than", "that", "the", "their",
    "them", "then", "there", "these", "they", "this", "to", "too", "up", "us",
    "use", "very", "was", "we", "were", "what", "when", "where", "which",
    "who", "why", "will", "with", "would", "you", "your", "friday", "jarvis"
}

PREFIX_LENGTH = 4

# A deliberately tiny domain vocabulary, NOT a general ontology.
#
# Keyword matching cannot know that "Graphite" is a theme, so "forget my theme
# preference" would either miss the memory that says "I prefer Graphite mode" or,
# worse, take out every memory containing the word "prefer". Naming FRIDAY's own
# enumerated values fixes exactly that, and nothing else. Add to it only when a
# value is a fixed part of this application's vocabulary.
CONCEPT_SYNONYMS = {
    "theme": (
        "graphite", "midnight", "tactical green", "high contrast", "white mode",
        "dark mode", "light mode", "jarvis blue", "friday blue"
    ),
    "voice": ("fish audio", "moira", "text to speech", "spoken reply"),
    # "ide" and "editor" are two words for the same question, so a memory about
    # VS Code has to be findable by either.
    "editor": (
        "vs code", "vscode", "xcode", "pycharm", "sublime text", "neovim",
        "ide", "code editor", "text editor"
    )
}

# Matched on word boundaries, not as raw substrings: "ide" appears inside
# "video", "guide" and "identity", and tagging those as editor talk would poison
# retrieval. Compiled once, so the boundary check costs no more than the
# substring one did.
_CONCEPT_PATTERNS = {
    concept: re.compile(r"\b(?:" + "|".join(re.escape(value) for value in values) + r")\b")
    for concept, values in CONCEPT_SYNONYMS.items()
}


def _tokens(text: str) -> List[str]:
    lowered = str(text or "").lower()
    words = re.findall(r"[a-z0-9]+", lowered)
    tokens = [word for word in words if len(word) > 2 and word not in STOPWORDS]

    # Expand known values to the concept they belong to, on both the memory and
    # the query side, so the two can meet in the middle.
    for concept, pattern in _CONCEPT_PATTERNS.items():
        if concept in tokens:
            continue

        if pattern.search(lowered):
            tokens.append(concept)

    return tokens


def _prefixes(tokens: List[str]) -> frozenset:
    return frozenset(word[:PREFIX_LENGTH] for word in tokens if len(word) >= PREFIX_LENGTH)


def _index_for(record: dict) -> Tuple[frozenset, frozenset]:
    """Tokens and prefixes for one memory, cached by id."""
    key = str(record.get("id") or "")
    cached = _token_index.get(key)

    if cached is not None:
        return cached

    tokens = _tokens(record.get("text"))
    entry = (frozenset(tokens), _prefixes(tokens))

    if key:
        _token_index[key] = entry

    return entry


def _forget_index(memory_id: str) -> None:
    _token_index.pop(str(memory_id or ""), None)


# ==========================================
# RECORD SHAPE
# ==========================================
def _safe_record(value: dict, scope: str = SCOPE_USER) -> Optional[dict]:
    """Normalise one stored memory, or return None if it carries no text.

    Every field a memory can have is listed here in one place, so a file written
    by an older build is upgraded simply by being read.
    """
    source = value if isinstance(value, dict) else {}
    text = _clean_text(source.get("text"))

    if not text:
        return None

    category = str(source.get("category") or "fact").strip().lower()

    if category not in CATEGORIES:
        category = "fact"

    record_scope = str(source.get("scope") or scope).strip().lower()

    if record_scope not in {SCOPE_USER, SCOPE_PROJECT}:
        record_scope = scope

    created = str(source.get("created_at") or _now_iso())

    return {
        "id": str(source.get("id") or _new_id()),
        "text": text,
        "category": category,
        "scope": record_scope,
        "project_id": str(source.get("project_id") or ""),
        "created_at": created,
        "updated_at": str(source.get("updated_at") or created),
        "importance": _clamp_importance(source.get("importance", 2)),
        "source": str(source.get("source") or "explicit"),
        "pinned": bool(source.get("pinned", False)),
        "last_accessed": source.get("last_accessed") or None,
        "access_count": max(0, int(source.get("access_count") or 0)),
        "conversation_id": str(source.get("conversation_id") or ""),
        # What this memory used to say, before a correction replaced it. Kept
        # short and only ever appended by the contradiction flow in
        # memory_learning — a superseded value is the most useful thing to have
        # when the correction turns out to be the mistake.
        "history": [
            {
                "text": _clean_text(entry.get("text")),
                "replaced_at": str(entry.get("replaced_at") or "")
            }
            for entry in (source.get("history") or [])
            if isinstance(entry, dict) and _clean_text(entry.get("text"))
        ][-3:]
    }


# ==========================================
# FILE IO
# ==========================================
def _ensure_dirs() -> None:
    MEMORY_DIR.mkdir(parents=True, exist_ok=True)


def _read_json(path: Path) -> Optional[dict]:
    try:
        with open(path, "r") as handle:
            data = json.load(handle)

        return data if isinstance(data, dict) else None
    except FileNotFoundError:
        return None
    except Exception:
        # A corrupt store is moved aside rather than deleted: losing memory
        # silently is exactly the failure this module exists to prevent.
        _backup_file(path, reason="corrupt")
        return None


def _write_json(path: Path, payload: dict) -> None:
    """Write atomically so a crash mid-write cannot truncate the store."""
    global _store_revision

    _ensure_dirs()
    temporary = path.with_suffix(path.suffix + ".tmp")

    with open(temporary, "w") as handle:
        json.dump(payload, handle, indent=2)

    os.replace(temporary, path)
    _cache_mtime[str(path)] = _file_mtime(path)
    _store_revision += 1


def _file_mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except Exception:
        return 0.0


def _backup_file(path: Path, reason: str = "backup") -> Optional[Path]:
    if not path.exists():
        return None

    try:
        BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        stamp = _now().strftime("%Y%m%d_%H%M%S")
        target = BACKUP_DIR / f"{path.stem}_{reason}_{stamp}{path.suffix}"
        shutil.copy2(path, target)
        return target
    except Exception:
        return None


def _default_long_term() -> dict:
    return {
        "version": STORE_VERSION,
        "updated_at": _now_iso(),
        "migrated_sources": [],
        "memories": []
    }


def _default_projects() -> dict:
    return {
        "version": STORE_VERSION,
        "updated_at": _now_iso(),
        "active_project_id": "",
        "projects": [],
        "memories": []
    }


def _default_working() -> dict:
    return {
        "version": STORE_VERSION,
        "updated_at": _now_iso(),
        "entries": []
    }


def _load(path: Path, default_factory) -> dict:
    """Load a store, reusing the cache unless the file changed underneath us."""
    key = str(path)
    disk_mtime = _file_mtime(path)

    if key in _cache and _cache_mtime.get(key) == disk_mtime:
        return _cache[key]

    raw = _read_json(path) or default_factory()
    _cache[key] = raw
    _cache_mtime[key] = disk_mtime
    return raw


def _load_long_term() -> dict:
    data = _load(LONG_TERM_FILE, _default_long_term)
    data.setdefault("memories", [])
    data.setdefault("migrated_sources", [])
    return data


def _load_projects() -> dict:
    data = _load(PROJECTS_FILE, _default_projects)
    data.setdefault("projects", [])
    data.setdefault("memories", [])
    data.setdefault("active_project_id", "")
    return data


def _load_working() -> dict:
    data = _load(WORKING_FILE, _default_working)
    data.setdefault("entries", [])
    return data


def _save_long_term(data: dict) -> None:
    data["version"] = STORE_VERSION
    data["updated_at"] = _now_iso()
    _write_json(LONG_TERM_FILE, data)
    _cache[str(LONG_TERM_FILE)] = data


def _save_projects(data: dict) -> None:
    data["version"] = STORE_VERSION
    data["updated_at"] = _now_iso()
    _write_json(PROJECTS_FILE, data)
    _cache[str(PROJECTS_FILE)] = data


def _save_working(data: dict) -> None:
    data["version"] = STORE_VERSION
    data["updated_at"] = _now_iso()
    _write_json(WORKING_FILE, data)
    _cache[str(WORKING_FILE)] = data


def reload_from_disk() -> None:
    """Drop every cache. Used by the tests and after a migration."""
    with _lock:
        _cache.clear()
        _cache_mtime.clear()
        _token_index.clear()


def configure_memory_dir(directory) -> None:
    """Point the whole store at a different directory (tests only)."""
    global MEMORY_DIR, LONG_TERM_FILE, PROJECTS_FILE, WORKING_FILE, BACKUP_DIR

    with _lock:
        MEMORY_DIR = Path(directory)
        LONG_TERM_FILE = MEMORY_DIR / "long_term.json"
        PROJECTS_FILE = MEMORY_DIR / "projects.json"
        WORKING_FILE = MEMORY_DIR / "working.json"
        BACKUP_DIR = MEMORY_DIR / "backups"
        reload_from_disk()


# ==========================================
# PROJECTS
# ==========================================
def _safe_project(value: dict) -> Optional[dict]:
    source = value if isinstance(value, dict) else {}
    name = _clean_text(source.get("name"))

    if not name:
        return None

    return {
        "id": str(source.get("id") or _slugify(name)),
        "name": name,
        "created_at": str(source.get("created_at") or _now_iso()),
        "updated_at": str(source.get("updated_at") or _now_iso())
    }


def list_projects() -> List[dict]:
    with _lock:
        data = _load_projects()
        projects = [entry for entry in (_safe_project(item) for item in data["projects"]) if entry]

        if not projects:
            projects = [_safe_project({"name": DEFAULT_PROJECT_NAME})]
            data["projects"] = projects
            data["active_project_id"] = projects[0]["id"]
            _save_projects(data)

        return projects


def get_active_project() -> dict:
    with _lock:
        projects = list_projects()
        data = _load_projects()
        active_id = str(data.get("active_project_id") or "")
        match = next((entry for entry in projects if entry["id"] == active_id), None)

        if match is None:
            match = projects[0]
            data["active_project_id"] = match["id"]
            _save_projects(data)

        return match


def get_active_project_id() -> str:
    return get_active_project()["id"]


def ensure_project(name: str) -> dict:
    """Find a project by name or id, creating it if it does not exist yet."""
    label = _clean_text(name)

    if not label:
        return get_active_project()

    slug = _slugify(label)

    with _lock:
        data = _load_projects()
        projects = list_projects()

        for entry in projects:
            if entry["id"] == slug or entry["name"].lower() == label.lower():
                return entry

        created = _safe_project({"name": label, "id": slug})
        data["projects"] = projects + [created]
        _save_projects(data)
        return created


def set_active_project(name: str) -> dict:
    project = ensure_project(name)

    with _lock:
        data = _load_projects()
        data["active_project_id"] = project["id"]
        _save_projects(data)

    return project


def resolve_project_id(project_id: Optional[str] = None) -> str:
    """Turn whatever the caller has into a real project id.

    None means "whatever project is active", which is what almost every caller
    wants — project memory follows the active project rather than being passed
    down through half the codebase.
    """
    value = _clean_text(project_id)

    if not value:
        return get_active_project_id()

    for entry in list_projects():
        if entry["id"] == value or entry["name"].lower() == value.lower():
            return entry["id"]

    return ensure_project(value)["id"]


def project_name(project_id: str) -> str:
    for entry in list_projects():
        if entry["id"] == project_id:
            return entry["name"]

    return project_id or ""


# ==========================================
# READING MEMORIES
# ==========================================
def _long_term_records() -> List[dict]:
    data = _load_long_term()
    records = []
    changed = False

    for item in data["memories"]:
        record = _safe_record(item, SCOPE_USER)

        if record is None:
            changed = True
            continue

        records.append(record)

    if changed or len(records) != len(data["memories"]):
        data["memories"] = records
        _save_long_term(data)

    return records


def _project_records() -> List[dict]:
    data = _load_projects()
    records = []

    for item in data["memories"]:
        record = _safe_record(item, SCOPE_PROJECT)

        if record is None:
            continue

        if not record["project_id"]:
            record["project_id"] = get_active_project_id()

        records.append(record)

    return records


def all_memories(
    scope: Optional[str] = None,
    project_id: Optional[str] = None,
    include_all_projects: bool = True
) -> List[dict]:
    """Every stored memory, newest first. Used by the UI and by search.

    Records are rebuilt from the raw store on every call, so what comes back is
    always a fresh copy — a caller cannot corrupt the store by editing one.
    """
    with _lock:
        records: List[dict] = []

        if scope in (None, SCOPE_USER):
            records.extend(_long_term_records())

        if scope in (None, SCOPE_PROJECT):
            project_records = _project_records()

            if not include_all_projects:
                wanted = resolve_project_id(project_id)
                project_records = [item for item in project_records if item["project_id"] == wanted]

            records.extend(project_records)

        records.sort(key=lambda item: str(item.get("updated_at") or ""), reverse=True)
        return records


def get_memory(memory_id: str) -> Optional[dict]:
    target = _clean_text(memory_id)

    if not target:
        return None

    for record in all_memories():
        if record["id"] == target:
            return record

    return None


def stats() -> dict:
    records = all_memories()
    working = get_working_entries()

    return {
        "total": len(records),
        "user": len([item for item in records if item["scope"] == SCOPE_USER]),
        "project": len([item for item in records if item["scope"] == SCOPE_PROJECT]),
        "pinned": len([item for item in records if item["pinned"]]),
        "working": len(working)
    }


# ==========================================
# WRITING MEMORIES
# ==========================================
def _jaccard(left: frozenset, right: frozenset) -> float:
    if not left or not right:
        return 0.0

    union = left | right

    if not union:
        return 0.0

    return len(left & right) / len(union)


# Only the strongest single-valued phrasing gets a fact key.
#
# "my favourite IDE is X" can only have one answer, so a new one replaces the
# old. "I prefer X" cannot — Jon can prefer several things at once — so those
# statements stay separate records and are ranked, not merged.
FACT_KEY_PATTERNS = (
    r"^(?:my|jon'?s)\s+(?P<subject>[a-z0-9 ]{2,40}?)\s+(?:is|are|was)\b",
)


def _fact_key(text: str, scope: str, project_id: str) -> str:
    """A stable key for "the same fact stated again".

    This is what turns "my favourite IDE is Vim" into an UPDATE of the existing
    "my favourite IDE is VS Code" rather than a second, contradictory record.
    Only the strong, unambiguous phrasings get a key; everything else falls back
    to near-duplicate text matching.
    """
    value = _clean_text(text).lower()

    for pattern in FACT_KEY_PATTERNS:
        match = re.match(pattern, value)

        if match:
            subject = re.sub(r"\s+", " ", match.group("subject")).strip()
            return f"{scope}:{project_id}:{subject}"

    return ""


def _find_duplicate(
    text: str,
    records: List[dict],
    scope: str,
    project_id: str
) -> Optional[dict]:
    """Find an existing record that this text should update instead of joining.

    Three tiers, cheapest first: identical text, the same fact key, then a very
    high token overlap. The overlap threshold is high on purpose — "I prefer
    Graphite mode" and "I prefer Midnight mode" must stay two different claims
    so the newer one can win by being newer, not by being merged.
    """
    normalized = _clean_text(text).lower().rstrip(".")
    key = _fact_key(text, scope, project_id)
    tokens = frozenset(_tokens(text))

    for record in records:
        if _clean_text(record["text"]).lower().rstrip(".") == normalized:
            return record

    if key:
        for record in records:
            if _fact_key(record["text"], record["scope"], record["project_id"]) == key:
                return record

    for record in records:
        if _jaccard(tokens, _index_for(record)[0]) >= 0.8:
            return record

    return None


def remember(
    text: str,
    scope: str = SCOPE_USER,
    category: Optional[str] = None,
    project_id: Optional[str] = None,
    importance: Optional[int] = None,
    source: str = "explicit",
    pinned: bool = False,
    conversation_id: str = ""
) -> Optional[dict]:
    """Store one memory, or update the existing record it supersedes.

    Returns the stored record with an extra "created" flag so callers can say
    "noted" versus "updated" without guessing.
    """
    clean = _clean_text(text)

    if len(clean) < 3:
        return None

    with _lock:
        scope_value = SCOPE_PROJECT if scope == SCOPE_PROJECT else SCOPE_USER
        resolved_project = resolve_project_id(project_id) if scope_value == SCOPE_PROJECT else ""

        if scope_value == SCOPE_USER:
            data = _load_long_term()
            records = _long_term_records()
        else:
            data = _load_projects()
            records = [item for item in _project_records() if item["project_id"] == resolved_project]

        existing = _find_duplicate(clean, records, scope_value, resolved_project)
        now = _now_iso()

        if existing is not None:
            existing["text"] = clean
            existing["updated_at"] = now
            existing["source"] = source or existing["source"]
            existing["importance"] = max(
                existing["importance"],
                _clamp_importance(importance if importance is not None else existing["importance"])
            )

            if category in CATEGORIES:
                existing["category"] = category

            if pinned:
                existing["pinned"] = True

            _forget_index(existing["id"])
            stored = existing
            created = False
        else:
            final_category = category if category in CATEGORIES else infer_category(clean)
            final_importance = importance if importance is not None else (3 if source == "explicit" else 2)

            # Who Jon is outranks everything else. A name or a birthday that he
            # actually stated is relevant to almost any reply, which is what puts
            # it in the always-on block handed to the voice layer. Facts merely
            # imported from memory v1 do not get the bump — they were never
            # deliberately stated to this system.
            if final_category == "identity" and source in {"explicit", "tool"}:
                final_importance = max(final_importance, 4)

            stored = _safe_record({
                "text": clean,
                "category": final_category,
                "scope": scope_value,
                "project_id": resolved_project,
                "created_at": now,
                "updated_at": now,
                "importance": final_importance,
                "source": source,
                "pinned": pinned,
                "conversation_id": conversation_id
            })
            created = True

        if scope_value == SCOPE_USER:
            others = [item for item in _long_term_records() if item["id"] != stored["id"]]
            data["memories"] = _trim(others + [stored], MAX_LONG_TERM_RECORDS)
            _save_long_term(data)
        else:
            others = [item for item in _project_records() if item["id"] != stored["id"]]
            data["memories"] = _trim(others + [stored], MAX_PROJECT_RECORDS)
            _save_projects(data)

        if conversation_id:
            _last_touched[conversation_id] = stored["id"]

        result = copy.deepcopy(stored)
        result["created"] = created
        return result


def _trim(records: List[dict], limit: int) -> List[dict]:
    """Cap a store without ever evicting a pinned memory."""
    if len(records) <= limit:
        return records

    pinned = [item for item in records if item.get("pinned")]
    unpinned = [item for item in records if not item.get("pinned")]
    unpinned.sort(key=lambda item: str(item.get("updated_at") or ""))
    room = max(limit - len(pinned), 0)
    keep_ids = {item["id"] for item in pinned}
    keep_ids.update(item["id"] for item in unpinned[-room:] if room)

    for record in records:
        if record["id"] not in keep_ids:
            _forget_index(record["id"])

    return [item for item in records if item["id"] in keep_ids]


def update_memory(
    memory_id: str,
    text: Optional[str] = None,
    pinned: Optional[bool] = None,
    importance: Optional[int] = None,
    category: Optional[str] = None,
    project_id: Optional[str] = None,
    history: Optional[List[dict]] = None
) -> Optional[dict]:
    """Edit a memory in place. The id survives; updated_at moves."""
    target = _clean_text(memory_id)

    if not target:
        return None

    with _lock:
        for loader, saver, key in (
            (_load_long_term, _save_long_term, "memories"),
            (_load_projects, _save_projects, "memories")
        ):
            data = loader()
            records = [
                record for record in (
                    _safe_record(item, SCOPE_USER if loader is _load_long_term else SCOPE_PROJECT)
                    for item in data[key]
                ) if record
            ]

            for record in records:
                if record["id"] != target:
                    continue

                if text is not None:
                    clean = _clean_text(text)

                    if not clean:
                        return None

                    record["text"] = clean
                    _forget_index(record["id"])

                if pinned is not None:
                    record["pinned"] = bool(pinned)

                if importance is not None:
                    record["importance"] = _clamp_importance(importance)

                if category is not None and category in CATEGORIES:
                    record["category"] = category

                if project_id is not None and record["scope"] == SCOPE_PROJECT:
                    record["project_id"] = resolve_project_id(project_id)

                if history is not None:
                    record["history"] = [
                        entry for entry in history if isinstance(entry, dict)
                    ][-3:]

                record["updated_at"] = _now_iso()
                data[key] = records
                saver(data)
                return copy.deepcopy(record)

        return None


def set_pinned(memory_id: str, pinned: bool) -> bool:
    return update_memory(memory_id, pinned=bool(pinned)) is not None


def forget_by_id(memory_id: str) -> bool:
    """Delete one memory from persistent storage and from the retrieval cache."""
    target = _clean_text(memory_id)

    if not target:
        return False

    with _lock:
        for loader, saver in ((_load_long_term, _save_long_term), (_load_projects, _save_projects)):
            data = loader()
            before = len(data["memories"])
            data["memories"] = [
                item for item in data["memories"]
                if str((item or {}).get("id") or "") != target
            ]

            if len(data["memories"]) != before:
                saver(data)
                _forget_index(target)

                for conversation, remembered in list(_last_touched.items()):
                    if remembered == target:
                        _last_touched.pop(conversation, None)

                return True

        return False


def forget(query: str, limit: int = 3, project_id: Optional[str] = None) -> List[dict]:
    """Delete the memories that match a phrase.

    Deliberately narrow, in three ways, because deleting the wrong memory is far
    worse than failing to delete the right one:

      1. A higher score bar than ordinary retrieval uses.
      2. If ANY candidate shares a literal word with the request, only those are
         considered — "forget my Graphite preference" must not take out
         "I prefer concise answers" just because both contain a form of "prefer".
      3. Only the best match and anything scoring within 10% of it, capped.
    """
    clean = _clean_text(query)

    if not clean:
        return []

    matches = search_memories(clean, limit=8, min_score=FORGET_MIN_SCORE, project_id=project_id)

    if not matches:
        return []

    query_tokens = frozenset(_tokens(clean))
    literal = [pair for pair in matches if query_tokens & _index_for(pair[0])[0]]
    pool = literal or matches
    best = pool[0][1]
    removed = []

    for record, score in pool[:limit]:
        if score < best * 0.9:
            break

        if forget_by_id(record["id"]):
            removed.append(record)

    return removed


def touch(memory_ids) -> None:
    """Record that memories were actually used. Feeds the ranking, cheaply.

    Takes a list (or one id) and writes each store at most once, so a turn that
    used four memories still costs at most two small file writes.
    """
    if isinstance(memory_ids, str):
        targets = {_clean_text(memory_ids)}
    else:
        targets = {_clean_text(value) for value in (memory_ids or [])}

    targets.discard("")

    if not targets:
        return

    stamp = _now_iso()

    with _lock:
        for loader, saver in ((_load_long_term, _save_long_term), (_load_projects, _save_projects)):
            data = loader()
            changed = False

            for item in data["memories"]:
                if isinstance(item, dict) and str(item.get("id") or "") in targets:
                    item["last_accessed"] = stamp
                    item["access_count"] = int(item.get("access_count") or 0) + 1
                    changed = True

            if changed:
                saver(data)


# ==========================================
# WORKING MEMORY
# ==========================================
def note_working(text: str, conversation_id: str = "", ttl_seconds: int = WORKING_TTL_SECONDS) -> Optional[dict]:
    """Remember something only for the next little while."""
    clean = _clean_text(text)

    if not clean:
        return None

    with _lock:
        data = _load_working()
        entries = _live_working_entries(data)
        # No minimum lifetime is imposed: the caller knows how long the reference
        # is useful for, and a floor here would only make short-lived entries
        # silently outlive their purpose.
        expires = (_now() + datetime.timedelta(seconds=max(1, int(ttl_seconds)))).isoformat(timespec="seconds")

        entry = {
            "id": _new_id("work"),
            "text": clean,
            "conversation_id": str(conversation_id or ""),
            "created_at": _now_iso(),
            "expires_at": expires
        }

        same_conversation = [
            item for item in entries
            if item["conversation_id"] == entry["conversation_id"]
            and item["text"].lower() != clean.lower()
        ]
        others = [item for item in entries if item["conversation_id"] != entry["conversation_id"]]
        data["entries"] = others + same_conversation[-(MAX_WORKING_PER_CONVERSATION - 1):] + [entry]
        _save_working(data)
        return copy.deepcopy(entry)


def _live_working_entries(data: dict) -> List[dict]:
    """Working entries that have not expired. Expiry happens on read."""
    now = _now()
    live = []

    for item in data.get("entries", []):
        if not isinstance(item, dict) or not _clean_text(item.get("text")):
            continue

        expires = _parse_iso(item.get("expires_at"))

        # Timestamps are second-resolution, so the stamped second is the first
        # one at which the entry is gone — hence <= rather than <.
        if expires is not None and expires <= now:
            continue

        live.append({
            "id": str(item.get("id") or _new_id("work")),
            "text": _clean_text(item.get("text")),
            "conversation_id": str(item.get("conversation_id") or ""),
            "created_at": str(item.get("created_at") or _now_iso()),
            "expires_at": str(item.get("expires_at") or "")
        })

    return live


def get_working_entries(conversation_id: str = "") -> List[dict]:
    with _lock:
        data = _load_working()
        entries = _live_working_entries(data)

        if len(entries) != len(data.get("entries", [])):
            data["entries"] = entries
            _save_working(data)

        if conversation_id:
            entries = [item for item in entries if item["conversation_id"] == str(conversation_id)]

        return copy.deepcopy(entries)


def clear_working(conversation_id: str = "") -> int:
    with _lock:
        data = _load_working()
        entries = _live_working_entries(data)

        if conversation_id:
            keep = [item for item in entries if item["conversation_id"] != str(conversation_id)]
        else:
            keep = []

        removed = len(entries) - len(keep)
        data["entries"] = keep
        _save_working(data)
        return removed


FILE_REFERENCE_PATTERN = re.compile(r"(?:/[\w.\-]+){2,}|\b[\w\-]+\.(?:py|js|json|md|txt|css|html|sh|ts|tsx|jsx|yml|yaml|csv)\b")


def capture_working_references(text: str, conversation_id: str = "") -> List[dict]:
    """Notice concrete artefacts a request mentions, so follow-ups can resolve.

    "the file we were just discussing" only means something if the file was
    recorded when it was named. File paths and filenames are unambiguous enough
    to capture automatically; nothing else is.
    """
    captured = []

    for match in FILE_REFERENCE_PATTERN.findall(str(text or ""))[:3]:
        entry = note_working(f"Currently discussing {match}", conversation_id)

        if entry:
            captured.append(entry)

    return captured


# ==========================================
# CATEGORY INFERENCE
# ==========================================
CATEGORY_HINTS = (
    ("identity", ("my name", "call me", "i am jon", "birthday", "full name", "address me")),
    ("preference", ("prefer", "favourite", "favorite", "i like", "i love", "i hate", "theme",
                    "style", "always use", "usually use", "keep it", "don't use", "dont use")),
    ("person", ("my wife", "my husband", "my mother", "my father", "my brother", "my sister",
                "my friend", "my manager", "my boss", "my colleague")),
    ("routine", ("every morning", "every day", "every week", "each morning", "routine",
                 "i usually", "i always start")),
    ("goal", ("my goal", "i want to build", "i'm building", "im building", "i am building",
              "milestone", "roadmap", "aiming to")),
    ("location", ("i live", "my house", "my office", "my desk", "based in")),
    ("decision", ("we decided", "i decided", "decision", "we chose", "going with")),
    ("constraint", ("must not", "never ", "do not ", "don't ", "limitation", "cannot", "requirement"))
)


def infer_category(text: str) -> str:
    """Pick a category from the wording. A hint for ranking, not a promise.

    Matched on word boundaries, not raw substrings: "Gemini Live" contains the
    letters of "i live", and a plain `in` test filed it under location.
    """
    value = _clean_text(text).lower()

    for category, hints in CATEGORY_HINTS:
        for hint in hints:
            if re.search(rf"\b{re.escape(hint.strip())}\b", value):
                return category

    return "fact"


# ==========================================
# RETRIEVAL
# ==========================================
def _recency_bonus(record: dict) -> float:
    updated = _parse_iso(record.get("updated_at")) or _parse_iso(record.get("created_at"))

    if updated is None:
        return 0.0

    age_days = max(0.0, (_now() - updated).total_seconds() / 86400)

    if age_days <= 7:
        return 1.0

    if age_days <= 30:
        return 0.5

    if age_days <= 120:
        return 0.2

    return 0.0


def score_memory(record: dict, query_tokens: frozenset, query_prefixes: frozenset, query_text: str) -> float:
    """How relevant is this memory to this request?

    Signals, in the order they matter:
      keyword overlap  — the request and the memory talk about the same things
      phrase match     — the memory's wording appears verbatim in the request
      importance       — how much weight the memory was given when stored
      pinned           — Jon marked it as mattering
      recency          — newer facts are more likely to still be true
      usage            — memories that keep proving useful stay near the top
    """
    tokens, prefixes = _index_for(record)

    if not tokens:
        return 0.0

    shared = query_tokens & tokens
    exact = len(shared)
    # Prefix hits already covered by an exact match must not be counted twice.
    exact_prefixes = frozenset(word[:PREFIX_LENGTH] for word in shared if len(word) >= PREFIX_LENGTH)
    fuzzy = len((query_prefixes & prefixes) - exact_prefixes)
    overlap = exact + fuzzy * 0.6

    if overlap <= 0:
        # No shared words at all: metadata alone must never be enough to get in.
        return 0.0

    # Normalised by memory length so a long memory cannot win on volume.
    score = 3.0 * overlap / max(1.0, (len(tokens) ** 0.5))

    lowered = _clean_text(record["text"]).lower()

    if lowered and lowered.rstrip(".") in query_text:
        score += 2.0

    score += record["importance"] * 0.4

    if record["pinned"]:
        score += 1.5

    score += _recency_bonus(record)
    score += min(int(record.get("access_count") or 0), 5) * 0.1

    if record["category"] in CORE_CATEGORIES and record["importance"] >= 4:
        score += 0.5

    return score


def search_memories(
    query: str,
    limit: int = 10,
    scope: Optional[str] = None,
    project_id: Optional[str] = None,
    min_score: float = 0.0,
    include_all_projects: bool = True
) -> List[Tuple[dict, float]]:
    """Rank memories against a query. The one place ranking happens."""
    query_text = _clean_text(query).lower()
    query_tokens = frozenset(_tokens(query_text))
    query_prefixes = _prefixes(list(query_tokens))

    if not query_tokens:
        return []

    scored = []

    for record in all_memories(scope=scope, project_id=project_id, include_all_projects=include_all_projects):
        score = score_memory(record, query_tokens, query_prefixes, query_text)

        if score >= min_score and score > 0:
            scored.append((record, score))

    scored.sort(key=lambda pair: pair[1], reverse=True)
    return scored[:limit]


def core_identity_memories(limit: int = MAX_CORE_IDENTITY) -> List[dict]:
    """The handful of memories that are relevant to almost any reply.

    Identity or preference, AND either pinned by Jon or important enough to have
    been stated deliberately. This is the only path by which a memory reaches the
    model without matching the request, and it is capped at three lines for
    exactly that reason.
    """
    records = [
        record for record in all_memories(scope=SCOPE_USER)
        if record["category"] in CORE_CATEGORIES
        and (record["pinned"] or record["importance"] >= 4)
    ]
    records.sort(
        key=lambda item: (item["pinned"], item["importance"], str(item.get("updated_at") or "")),
        reverse=True
    )
    return records[:limit]


# Requests that never need memory. Retrieval is cheap, but a deterministic UI
# command should not pay for it at all — and more importantly, should not risk
# a memory line changing how a fixed action is carried out.
_TRIVIAL_COMMAND_PATTERN = re.compile(
    r"^(?:friday|jarvis|hey friday|please)?\s*"
    r"(?:open|close|show|hide|start|stop|play|pause|resume|skip|next|previous|clear|refresh|mute|unmute|"
    r"turn on|turn off|sleep|wake up|shut down|exit|quit)\b[\w\s]{0,24}$"
)


def should_retrieve(query: str) -> bool:
    """Is it worth building memory context for this request?"""
    clean = _clean_text(query)

    if len(clean) < 4:
        return False

    if _TRIVIAL_COMMAND_PATTERN.match(clean.lower()):
        return False

    return True


def get_conversation_turns(conversation_id: str, limit: int = MAX_TURNS) -> List[dict]:
    """Recent turns of ONE conversation.

    Workshop chats are persisted by state_manager, so they are read from there —
    duplicating them here is how two sources of truth start disagreeing. Voice
    and terminal turns are not persisted per-conversation, so those live in an
    in-process ring buffer instead.

    The state_manager import is deliberately inside the function: state_manager
    imports this module at start-up, and a top-level import here would make that
    circular.
    """
    key = str(conversation_id or "")

    if not key:
        return []

    if key.startswith("workshop_chat_"):
        try:
            from Core_Cognition import state_manager

            return state_manager.get_chat_session_turns(key, limit)
        except Exception:
            return []

    return list(_turn_buffers.get(key, [])[-limit:])


def record_turn(conversation_id: str, role: str, text: str) -> None:
    """Buffer a turn for conversations that are not persisted chat sessions."""
    key = str(conversation_id or "")
    clean = _clean_text(text)

    if not key or not clean or key.startswith("workshop_chat_"):
        return

    with _lock:
        buffer = _turn_buffers.setdefault(key, [])
        buffer.append({"role": str(role or "user"), "text": clean[:400]})
        del buffer[:-MAX_BUFFERED_TURNS]


def last_user_turn(conversation_id: str) -> str:
    """The previous thing Jon said in this conversation, for "remember this"."""
    turns = get_conversation_turns(conversation_id, limit=MAX_BUFFERED_TURNS)

    for turn in reversed(turns):
        role = str(turn.get("role") or "").strip().lower()

        if role in {"friday", "jarvis", "assistant", "system"}:
            continue

        text = _clean_text(turn.get("text"))

        if text:
            return text

    return ""


# ==========================================
# THE CONTEXT BUILDER
# ==========================================
def build_memory_context(
    query: str,
    conversation_id: str = "",
    project_id: Optional[str] = None,
    max_long_term: int = MAX_LONG_TERM,
    max_project: int = MAX_PROJECT,
    max_turns: int = MAX_TURNS,
    include_conversation: bool = True
) -> dict:
    """Assemble everything FRIDAY should know for ONE request.

    This is the single entry point for memory context. Both Silent Operator and
    the voice layer go through it, so there is exactly one answer to "what does
    FRIDAY remember right now" and one place to change it.

    Returns a dict rather than a string so callers can inspect what was chosen
    (the UI and the tests both do); `block` is the ready-to-send prose.
    """
    clean_query = _clean_text(query)
    resolved_project = resolve_project_id(project_id)

    context = {
        "query": clean_query,
        "conversation_id": str(conversation_id or ""),
        "project_id": resolved_project,
        "project_name": project_name(resolved_project),
        "long_term": [],
        "project": [],
        "working": [],
        "conversation": [],
        "block": ""
    }

    if include_conversation and conversation_id:
        context["conversation"] = get_conversation_turns(conversation_id, max_turns)

    if not should_retrieve(clean_query):
        # Still hand back the conversation: continuity is not "retrieval", it is
        # just the thread the user is already in.
        context["block"] = format_memory_block(context)
        return context

    with _lock:
        user_hits = search_memories(
            clean_query,
            limit=max_long_term,
            scope=SCOPE_USER,
            min_score=MIN_RELEVANCE_SCORE
        )
        project_hits = search_memories(
            clean_query,
            limit=max_project,
            scope=SCOPE_PROJECT,
            project_id=resolved_project,
            include_all_projects=False,
            min_score=MIN_RELEVANCE_SCORE
        )

        selected_user = [record for record, _score in user_hits]
        chosen_ids = {record["id"] for record in selected_user}

        # Core identity tops up the list only if there is room left. It never
        # displaces a memory that actually matched the request.
        for record in core_identity_memories():
            if len(selected_user) >= max_long_term:
                break

            if record["id"] not in chosen_ids:
                selected_user.append(record)
                chosen_ids.add(record["id"])

        context["long_term"] = selected_user
        context["project"] = [record for record, _score in project_hits]

        working = get_working_entries(conversation_id)
        query_tokens = frozenset(_tokens(clean_query))
        relevant_working = [
            entry for entry in working
            if query_tokens & frozenset(_tokens(entry["text"]))
        ]
        # Working memory is small and short-lived, so if nothing matches by
        # keyword the most recent entries are still the best available guess at
        # "what we are doing right now".
        context["working"] = (relevant_working or working)[-MAX_WORKING:]
        touch([record["id"] for record in context["long_term"] + context["project"]])

    context["block"] = format_memory_block(context)
    return context


def _bullet(text: str) -> str:
    value = _clean_text(text)

    if len(value) > MAX_LINE_CHARS:
        value = value[:MAX_LINE_CHARS - 1].rstrip() + "…"

    return f"- {value}"


def format_memory_block(context: dict) -> str:
    """Render selected context as clean prose lines.

    Never raw JSON, never field names — the model reads this as English, and
    anything that looks like a data structure invites it to talk like one.
    """
    sections = []

    if context.get("long_term"):
        lines = [_bullet(record["text"]) for record in context["long_term"]]
        sections.append("Relevant long-term context:\n" + "\n".join(lines))

    if context.get("project"):
        label = context.get("project_name") or "current project"
        lines = [_bullet(record["text"]) for record in context["project"]]
        sections.append(f"Relevant project context ({label}):\n" + "\n".join(lines))

    if context.get("working"):
        lines = [_bullet(entry["text"]) for entry in context["working"]]
        sections.append("Working context:\n" + "\n".join(lines))

    if context.get("conversation"):
        lines = []

        for turn in context["conversation"]:
            role = str(turn.get("role") or "user").strip()
            speaker = "FRIDAY" if role.lower() in {"friday", "jarvis", "assistant"} else "Jon"
            lines.append(_bullet(f"{speaker}: {turn.get('text')}"))

        sections.append("Recent conversation:\n" + "\n".join(lines))

    block = "\n\n".join(sections)

    if len(block) > MAX_BLOCK_CHARS:
        block = block[:MAX_BLOCK_CHARS].rsplit("\n", 1)[0]

    return block


def build_prompt(
    user_message: str,
    conversation_id: str = "",
    project_id: Optional[str] = None,
    instructions: str = ""
) -> str:
    """A complete prompt: context, then instructions, then the actual message.

    Used by Silent Operator, which is stateless per turn precisely so that two
    chats can never share history.
    """
    context = build_memory_context(user_message, conversation_id, project_id)
    parts = []

    if context["block"]:
        parts.append(
            "Context for this turn — the recent turns of THIS conversation, plus anything "
            "FRIDAY remembers that is relevant. Use what helps and ignore the rest; never read "
            "it back as a list, and never mention that it came from memory.\n" + context["block"]
        )

    if instructions:
        parts.append(instructions)

    parts.append(f"Jon's message:\n{_clean_text(user_message) or user_message}")
    return "\n\n".join(parts)


def identity_prompt_block(limit: int = MAX_CORE_IDENTITY) -> str:
    """The small always-on block handed to the live voice layer at connect.

    The voice session has no per-turn prompt to attach context to, so the pinned
    core identity memories ride in the system instruction and everything else is
    fetched on demand through the recall tool.
    """
    records = core_identity_memories(limit)

    if not records:
        return ""

    lines = "\n".join(_bullet(record["text"]) for record in records)
    return (
        "WHAT YOU ALREADY KNOW ABOUT BOSS\n"
        f"{lines}\n"
        "Use recall_memory for anything else you might have been told before."
    )


# ==========================================
# MEMORY CREATION — CANDIDATES
# ==========================================
# Memory v2 recognised durable statements with a list of patterns right here and
# wrote any match straight into long-term memory. Memory v2.5 replaced that with
# a confidence pipeline, so extraction, scoring, reinforcement and promotion all
# live in Core_Cognition/memory_learning.py — the patterns that used to sit here
# are the ones in EXTRACTION_PATTERNS there.
#
# This function stays as the single named entry point for "learn from a turn".

def capture_candidates(
    text: str,
    conversation_id: str = "",
    project_id: Optional[str] = None,
    role: str = "user"
) -> dict:
    """Learn from one completed turn.

    Memory v2 wrote a matching sentence straight into long-term memory. Memory
    v2.5 routes it through the candidate pipeline instead, so a claim has to
    earn its place before it becomes a fact — see memory_learning.observe().

    The import is deliberately local: memory_learning is built ON TOP of this
    module, so a top-level import here would be circular. This function stays as
    the single named entry point callers already use.
    """
    try:
        from Core_Cognition import memory_learning

        return memory_learning.observe(
            text,
            conversation_id=conversation_id,
            project_id=project_id,
            role=role
        )
    except Exception:
        # Learning must never be able to break a conversation. Falling back to
        # the working-memory capture keeps "the file we were just discussing"
        # working even if the pipeline above has a problem.
        capture_working_references(text, conversation_id)
        return {"observed": False, "created": [], "reinforced": [], "promoted": [], "updated": [], "temporary": [], "blocked": []}


# ==========================================
# EXPLICIT MEMORY COMMANDS
# ==========================================
# These run BEFORE any model call, so "remember that I prefer dark themes" is a
# deterministic store-and-confirm rather than something a model might decide to
# paraphrase instead of doing.

def _split_project_scope(text: str, project_id: Optional[str]) -> Tuple[str, str, Optional[str]]:
    """Work out whether a remember request is about a project.

    Lower-casing never changes a string's length, so matching on the lowered
    text and slicing the ORIGINAL by the match offsets keeps capitalisation
    intact — "Gemini Live" must not come back as "gemini live".

    Returns (text, scope, project_id).
    """
    clean = _clean_text(text)
    lowered = clean.lower()

    # "for this project, keep the interface simple"
    match = re.match(r"^(?:for|on|about)\s+(?:this|the)\s+project\s*[:,]?\s*(?P<rest>.+)$", lowered)

    if match:
        return clean[match.start("rest"):].strip(), SCOPE_PROJECT, project_id

    # "for Project Sentinel: keep it simple" / "about FRIDAY-MK1, voice is local"
    match = re.match(
        r"^(?:for|about)\s+(?:project\s+)?(?P<name>[\w\-][\w\- ]{1,38}?)\s*[:,]\s*(?P<rest>.+)$",
        lowered
    )

    if match:
        return (
            clean[match.start("rest"):].strip(),
            SCOPE_PROJECT,
            clean[match.start("name"):match.end("name")].strip()
        )

    # A sentence that names the active project is about the project.
    active = project_name(resolve_project_id(project_id)).lower()

    if active and active in lowered:
        return clean, SCOPE_PROJECT, project_id

    return clean, SCOPE_USER, None


def parse_memory_command(text: str) -> Optional[dict]:
    """Recognise an explicit memory instruction.

    Returns an intent dict, or None when the text is not a memory command — in
    which case the caller carries on with its normal routing.

    Precedence note: "remember to <do something>" is a TASK and is left alone
    here, as is a bare "remember this" with nothing to attach it to, which is
    still the notes flow.
    """
    # `body` keeps the original capitalisation and `lowered` is the same string
    # in lower case — identical length, so a match offset in one is valid in the
    # other. That is how "Remember that I prefer Graphite mode" is stored as
    # "I prefer Graphite mode" and not "i prefer graphite mode".
    body = _clean_text(text).rstrip(" .!?")
    body = re.sub(r"^(?:hey\s+)?(?:friday|jarvis)[,\s]+", "", body, flags=re.IGNORECASE).strip()
    body = re.sub(r"^please\s+", "", body, flags=re.IGNORECASE).strip()
    lowered = body.lower()

    if not lowered:
        return None

    def tail(match: "re.Match", group: str = "value") -> str:
        return body[match.start(group):].strip(" .!,")

    # ---- recall ------------------------------------------------------
    recall = re.match(
        r"^(?:what|which)\s+(?:do|did)\s+you\s+(?:remember|know)(?:\s+about)?\s*(?P<topic>.*)$",
        lowered
    )

    if recall:
        return {"action": "recall", "topic": _clean_text(recall.group("topic"))}

    if lowered in {"what do you remember", "what do you know", "show my memories", "list my memories"}:
        return {"action": "recall", "topic": ""}

    # ---- reject a pending observation --------------------------------
    # "Don't remember that" is NOT "forget that": nothing has been stored yet.
    # It refuses something FRIDAY was in the middle of learning, so it has to be
    # matched before the forget patterns, which would otherwise claim it.
    reject = re.match(
        r"^(?:don'?t|do not|never)\s+(?:remember|save|store|learn)\s*"
        r"(?:that|this|it)?\s*(?P<topic>.*)$",
        lowered
    )

    if reject:
        return {"action": "reject_candidate", "topic": _clean_text(reject.group("topic"))}

    # ---- forget ------------------------------------------------------
    if lowered in {"forget that", "forget that one", "forget the last one", "forget what i just said"}:
        return {"action": "forget_last"}

    forget = re.match(
        r"^(?:forget|delete|remove|drop)\s+(?:the\s+|my\s+|that\s+|about\s+)?(?P<topic>.+)$",
        lowered
    )

    if forget:
        topic = _clean_text(forget.group("topic"))
        # Only the "memory about ..." wrapper is removed. Words like "preference"
        # are left in place because they are often the ONLY overlap between the
        # request and the stored wording.
        topic = re.sub(r"^(?:memory|memories|note)\s+(?:about|for|on)\s+", "", topic).strip()

        if topic in {"everything", "all", "it", "all of it", "all my memories"}:
            return {"action": "forget_all_refused"}

        if len(topic) >= 3:
            return {"action": "forget", "topic": topic}

    # ---- update ------------------------------------------------------
    update = re.match(
        r"^(?:update|change|correct)\s+that\s+(?:memory|one)\s+(?:to|so it says)\s+(?P<value>.+)$",
        lowered
    )

    if update:
        return {"action": "update_last", "text": tail(update)}

    # ---- store -------------------------------------------------------
    # "remember to ..." is a task, not a memory. Left for the task router.
    if re.match(r"^(?:remember|don'?t forget)\s+to\s+", lowered):
        return None

    if lowered in {"remember this", "remember that"}:
        return {"action": "store_previous"}

    store = re.match(
        r"^(?:remember|keep in mind|don'?t forget|do not forget)\s+"
        r"(?:that\s+|this[:,]\s*|the fact that\s+)?(?P<value>.+)$",
        lowered
    )

    if store:
        value = tail(store)

        if len(value) >= 3:
            return {"action": "store", "text": value}

    return None


def run_memory_command(
    intent: dict,
    conversation_id: str = "",
    project_id: Optional[str] = None
) -> str:
    """Carry out a parsed memory command and return what FRIDAY should say.

    Returning the sentence rather than speaking it keeps this module free of any
    dependency on the voice or HUD layers.
    """
    action = str((intent or {}).get("action") or "")

    if action == "store":
        text, scope, target_project = _split_project_scope(intent.get("text", ""), project_id)
        record = remember(
            text,
            scope=scope,
            project_id=target_project if scope == SCOPE_PROJECT else None,
            importance=3,
            source="explicit",
            conversation_id=conversation_id
        )

        if not record:
            return "There was nothing to remember there."

        if scope == SCOPE_PROJECT:
            label = project_name(record["project_id"])
            return ("Updated" if not record["created"] else "Noted") + f", filed under {label}."

        return "Updated." if not record["created"] else "Noted."

    if action == "store_previous":
        previous = last_user_turn(conversation_id)

        if not previous:
            return ""

        record = remember(
            previous,
            scope=SCOPE_USER,
            importance=3,
            source="explicit",
            conversation_id=conversation_id
        )
        return "Noted." if record else "There was nothing to remember there."

    if action == "forget_last":
        memory_id = _last_touched.get(str(conversation_id or ""), "")
        record = get_memory(memory_id) if memory_id else None

        if record and forget_by_id(record["id"]):
            return "Forgotten."

        return "I'm not sure which memory you mean."

    if action == "forget":
        removed = forget(intent.get("topic", ""), project_id=project_id)

        if not removed:
            return "I don't have anything stored about that."

        if len(removed) == 1:
            return "Forgotten."

        return f"Forgotten, {len(removed)} entries removed."

    if action == "reject_candidate":
        # Refuse something that was still only an observation. If nothing is
        # pending it may already be a stored memory, so fall through to a normal
        # forget rather than claiming there was nothing there.
        try:
            from Core_Cognition import memory_learning

            rejected = memory_learning.reject_matching_candidate(
                intent.get("topic", ""),
                conversation_id=conversation_id
            )
        except Exception:
            rejected = None

        if rejected:
            return "Understood, I won't keep that."

        topic = _clean_text(intent.get("topic", ""))

        if topic:
            removed = forget(topic, project_id=project_id)

            if removed:
                return "Forgotten."

        memory_id = _last_touched.get(str(conversation_id or ""), "")

        if memory_id and forget_by_id(memory_id):
            return "Forgotten."

        return "Nothing was being saved from that."

    if action == "forget_all_refused":
        return "Tell me which memory to forget, or clear them from the Workshop memory panel."

    if action == "update_last":
        memory_id = _last_touched.get(str(conversation_id or ""), "")
        record = update_memory(memory_id, text=intent.get("text", "")) if memory_id else None
        return "Updated." if record else "I'm not sure which memory you mean."

    if action == "recall":
        return describe_memories(intent.get("topic", ""), project_id=project_id)

    return ""


def describe_memories(topic: str = "", project_id: Optional[str] = None, limit: int = 5) -> str:
    """A spoken-friendly answer to "what do you remember about X"."""
    clean = _clean_text(topic)
    generic_self = clean.lower().strip(" ?") in {"", "me", "myself", "about me", "jon", "us"}

    if generic_self:
        records = [
            record for record in all_memories(scope=SCOPE_USER)
            if record["importance"] >= 2
        ]
        records.sort(
            key=lambda item: (item["pinned"], item["importance"], str(item.get("updated_at") or "")),
            reverse=True
        )
        selected = records[:limit]
    else:
        # A named project asks for that project's memory specifically.
        named_project = next(
            (entry for entry in list_projects() if entry["name"].lower() in clean.lower()),
            None
        )

        if named_project:
            hits = search_memories(
                clean,
                limit=limit,
                scope=SCOPE_PROJECT,
                project_id=named_project["id"],
                include_all_projects=False
            )

            if not hits:
                selected = [
                    record for record in all_memories(scope=SCOPE_PROJECT, project_id=named_project["id"], include_all_projects=False)
                ][:limit]
            else:
                selected = [record for record, _score in hits]
        else:
            selected = [record for record, _score in search_memories(clean, limit=limit)]

    if not selected:
        return "Nothing stored about that yet."

    touch([record["id"] for record in selected])

    if len(selected) == 1:
        return selected[0]["text"].rstrip(".") + "."

    return " ".join(record["text"].rstrip(".") + "." for record in selected)


# ==========================================
# MAINTENANCE
# ==========================================
def maintain() -> dict:
    """Housekeeping that keeps the store from growing without bound.

    Everything here is conservative. Pinned memories, explicitly saved memories
    and anything that has ever been retrieved are never removed automatically.
    """
    removed_duplicates = 0
    expired = 0

    with _lock:
        for loader, saver, scope in (
            (_load_long_term, _save_long_term, SCOPE_USER),
            (_load_projects, _save_projects, SCOPE_PROJECT)
        ):
            data = loader()
            records = [record for record in (_safe_record(item, scope) for item in data["memories"]) if record]
            kept: List[dict] = []
            seen: Dict[str, dict] = {}

            for record in records:
                key = f"{record['scope']}:{record['project_id']}:{_clean_text(record['text']).lower().rstrip('.')}"
                previous = seen.get(key)

                if previous is not None:
                    # Merge: keep the older id (stable), the newer timestamp, the
                    # stronger importance and any pin.
                    previous["updated_at"] = max(previous["updated_at"], record["updated_at"])
                    previous["importance"] = max(previous["importance"], record["importance"])
                    previous["pinned"] = previous["pinned"] or record["pinned"]
                    previous["access_count"] += record["access_count"]
                    _forget_index(record["id"])
                    removed_duplicates += 1
                    continue

                seen[key] = record
                kept.append(record)

            survivors = []
            cutoff = _now() - datetime.timedelta(days=90)

            for record in kept:
                stale = (
                    record["source"] == "inferred"
                    and not record["pinned"]
                    and record["importance"] <= 1
                    and record["access_count"] == 0
                    and (_parse_iso(record["updated_at"]) or _now()) < cutoff
                )

                if stale:
                    _forget_index(record["id"])
                    expired += 1
                    continue

                survivors.append(record)

            limit = MAX_LONG_TERM_RECORDS if scope == SCOPE_USER else MAX_PROJECT_RECORDS
            data["memories"] = _trim(survivors, limit)
            saver(data)

        working_before = len(_load_working().get("entries", []))
        live = get_working_entries()
        expired_working = max(0, working_before - len(live))

    # Observations get the same treatment as memories: the weak and untouched
    # ones are allowed to die rather than accumulating forever.
    expired_candidates = 0

    try:
        from Core_Cognition import memory_learning

        expired_candidates = memory_learning.expire_candidates().get("expired_candidates", 0)
    except Exception:
        pass

    return {
        "merged_duplicates": removed_duplicates,
        "expired": expired,
        "expired_working": expired_working,
        "expired_candidates": expired_candidates
    }


# ==========================================
# MIGRATION FROM MEMORY v1
# ==========================================
LEGACY_MEMORY_TXT_LINE = re.compile(r"^\s*(?:\[(?P<date>[^\]]+)\]\s*)?(?P<text>.+?)\s*$")

# Lines that were obviously scratch input during development. They are still
# migrated (nothing is discarded) but at the lowest importance so they never
# outrank a real fact and are eligible for the 90-day expiry above.
LOW_VALUE_MARKERS = ("test", "note called", "requested")


def _legacy_importance(text: str) -> int:
    lowered = text.lower()

    if any(marker in lowered for marker in LOW_VALUE_MARKERS) and len(lowered) < 40:
        return 1

    return 2


def migrate_memory_txt(path: Optional[Path] = None) -> dict:
    """Import the old memory.txt, once, keeping a backup of the original.

    memory.txt is left in place: it is the only copy of memory v1 and deleting
    it would make this migration irreversible.
    """
    source = Path(path) if path else (PROJECT_ROOT / "memory.txt")
    marker = f"memory_txt:{source}"

    with _lock:
        data = _load_long_term()

        if marker in data.get("migrated_sources", []):
            return {"migrated": 0, "skipped": "already migrated"}

        if not source.exists():
            data.setdefault("migrated_sources", []).append(marker)
            _save_long_term(data)
            return {"migrated": 0, "skipped": "no memory.txt"}

        try:
            raw_lines = source.read_text().splitlines()
        except Exception as error:
            return {"migrated": 0, "error": str(error)}

        _ensure_dirs()
        BACKUP_DIR.mkdir(parents=True, exist_ok=True)

        try:
            shutil.copy2(source, BACKUP_DIR / f"memory_txt_{_now().strftime('%Y%m%d_%H%M%S')}.bak")
        except Exception:
            pass

        migrated = 0

        for line in raw_lines:
            match = LEGACY_MEMORY_TXT_LINE.match(line)

            if not match:
                continue

            text = _clean_text(match.group("text"))

            if not text:
                continue

            record = remember(
                text,
                scope=SCOPE_USER,
                importance=_legacy_importance(text),
                source="migration",
                category=infer_category(text)
            )

            if not record:
                continue

            migrated += 1
            created = str(match.group("date") or "")

            # memory.txt stamped each line with the day it was written. Keeping
            # that date means the recency signal reflects when Jon actually said
            # it, not when this migration happened to run.
            if re.match(r"^\d{4}-\d{2}-\d{2}$", created):
                stored = _load_long_term()

                for item in stored["memories"]:
                    if item.get("id") == record["id"]:
                        item["created_at"] = f"{created}T00:00:00"
                        item["updated_at"] = f"{created}T00:00:00"
                        break

                _save_long_term(stored)

        data = _load_long_term()
        data.setdefault("migrated_sources", []).append(marker)
        _save_long_term(data)
        return {"migrated": migrated}


def migrate_workshop_memory(items: List[dict]) -> dict:
    """Import the old Workshop "Project Memory" notes into project memory."""
    marker = "workshop_project_memory"

    with _lock:
        data = _load_projects()
        migrated_sources = data.setdefault("migrated_sources", [])

        if marker in migrated_sources:
            return {"migrated": 0, "skipped": "already migrated"}

        target_project = get_active_project_id()
        migrated = 0

        for item in items if isinstance(items, list) else []:
            if not isinstance(item, dict):
                continue

            text = _clean_text(item.get("text"))

            if not text:
                continue

            # The old panel was the only place to put ANY note, so personal facts
            # ended up filed under a project. An identity fact scoped to one
            # project would only be recalled while that project is active, which
            # is plainly wrong for something like a preferred name — so those are
            # re-filed as user memory. Everything else stays where Jon put it.
            category = infer_category(text)
            scope = SCOPE_USER if category == "identity" else SCOPE_PROJECT

            record = remember(
                text,
                scope=scope,
                project_id=target_project if scope == SCOPE_PROJECT else None,
                importance=3 if item.get("pinned") else 2,
                pinned=bool(item.get("pinned")),
                source="migration",
                category=category
            )

            if record:
                migrated += 1

        data = _load_projects()
        data.setdefault("migrated_sources", []).append(marker)
        _save_projects(data)
        return {"migrated": migrated}


def run_startup_migration(workshop_memory_items: Optional[List[dict]] = None) -> dict:
    """Everything that must happen once, at start-up, before the first request."""
    _ensure_dirs()
    list_projects()  # materialise the default project
    text_result = migrate_memory_txt()
    workshop_result = migrate_workshop_memory(workshop_memory_items or [])
    maintenance = maintain()

    return {
        "memory_txt": text_result,
        "workshop": workshop_result,
        "maintenance": maintenance
    }


# ==========================================
# UI VIEW
# ==========================================
def _view_key(limit: int) -> tuple:
    """Identity of the current store contents, for the view cache."""
    return (
        limit,
        _store_revision,
        _file_mtime(LONG_TERM_FILE),
        _file_mtime(PROJECTS_FILE),
        _file_mtime(WORKING_FILE)
    )


def memory_view(limit: int = 200) -> dict:
    """The payload the Workshop memory panel renders from.

    A capped, already-sorted list plus the project registry, so the renderer
    never has to know how the store is laid out on disk. Cached against the
    store contents, because this runs on every interface broadcast.
    """
    with _lock:
        key = _view_key(limit)

        if _view_cache.get("key") == key and _view_cache.get("payload") is not None:
            return copy.deepcopy(_view_cache["payload"])

        payload = _build_memory_view(limit)
        _view_cache["key"] = key
        _view_cache["payload"] = payload
        return copy.deepcopy(payload)


def _build_memory_view(limit: int) -> dict:
    records = all_memories()
    records.sort(
        key=lambda item: (bool(item["pinned"]), str(item.get("updated_at") or "")),
        reverse=True
    )

    projects = list_projects()
    active = get_active_project_id()
    names = {entry["id"]: entry["name"] for entry in projects}

    items = []

    for record in records[:limit]:
        item = dict(record)
        item["project_name"] = names.get(record["project_id"], "")
        items.append(item)

    return {
        "items": items,
        "projects": projects,
        "active_project_id": active,
        "stats": stats(),
        "truncated": len(records) > limit
    }
