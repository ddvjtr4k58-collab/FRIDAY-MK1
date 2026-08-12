"""
FRIDAY Memory v2.5 — intelligent memory capture.

WHY THIS MODULE EXISTS
----------------------
Memory v2 could store, rank and forget facts, but it only ever learned something
when Jon said "remember that ...". Its automatic capture was a single pass of
eleven regexes that wrote straight to long-term memory: a sentence either matched
and became a permanent fact on the spot, or it vanished. That is both too eager
(one phrasing is enough to create a permanent record) and too timid (anything
said in different words is lost forever).

Memory v2.5 puts an OBSERVATION stage in between:

    something Jon said
        -> extract()          does this sentence assert anything durable?
        -> a CANDIDATE        an observation, not yet a fact
        -> reinforcement      the same claim, said again, gets more credible
        -> promotion          once confident enough, it becomes a real memory

A candidate is what FRIDAY thinks she might have learned. A memory is what she
has accepted. Keeping the two apart is the whole design: it lets her be
conservative about writing to long-term memory while still noticing patterns
over a whole conversation, and it gives Jon a pending list he can promote or
reject instead of a store that silently fills with guesses.

CONFIDENCE VERSUS IMPORTANCE
----------------------------
They are deliberately separate numbers and they answer different questions:

    confidence — how sure are we this claim is true and stable?
    importance — how useful would remembering it be?

"My coffee is cold" can be perfectly confident and worthless. "I prefer VS Code"
starts less certain but is worth a lot once it holds up. Confidence decides
WHETHER something is stored; importance decides how it ranks once it is.

NO MODEL CALLS
--------------
Extraction and scoring here are entirely deterministic — pattern tiers plus a
small set of linguistic modifiers. That is a deliberate choice, not a shortcut:
this code runs after every conversational turn, a model call would add cost and
a failure mode to a path that must never slow FRIDAY down, and a model asked to
"find facts" will cheerfully invent them. Every stored fact traces back to words
Jon actually said. See score_candidate() for the full rule set.
"""

import datetime
import json
import os
import re
import threading
import uuid
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from Core_Cognition import memory_manager as mm

# ==========================================
# TUNING
# ==========================================
# Every threshold in one place. These are the knobs that decide how eager FRIDAY
# is, and they are set conservatively on purpose: a store full of half-true
# guesses is worse than one that missed a few things.

# A candidate at or above this is accepted as a real memory automatically.
AUTO_SAVE_THRESHOLD = 0.85

# Below this a candidate is not even worth keeping around to build on.
MIN_KEEP_CONFIDENCE = 0.30

# A candidate must be observed at least this many times before it can be
# promoted — UNLESS it was a definitive statement (see TIER_DEFINITIVE), which
# is Jon stating a fact outright rather than mentioning something in passing.
MIN_OCCURRENCES_FOR_AUTO_SAVE = 2

# Replacing an existing memory because a new statement contradicts it.
CONTRADICTION_UPDATE_CONFIDENCE = 0.75
# A pinned memory is something Jon deliberately marked. Overwriting one takes
# both an explicit correction AND near-certainty.
PINNED_OVERRIDE_CONFIDENCE = 0.90

# How much of the remaining distance to 1.0 a repeat mention closes. Strong
# restatements move a candidate a lot; hearing the value in passing moves it a
# little. Diminishing by construction: confidence approaches 1.0, never reaches.
REINFORCE_STRONG = 0.50
REINFORCE_MODERATE = 0.40
REINFORCE_INCIDENTAL = 0.25

CONFIDENCE_CEILING = 0.99

# Housekeeping. Candidates are observations, so they are allowed to die.
CANDIDATE_STALE_DAYS = 14        # weak and untouched for this long -> expired
CANDIDATE_MAX_AGE_DAYS = 60      # pending for this long without promotion -> expired
MAX_CANDIDATES = 200
MAX_EVIDENCE_PER_CANDIDATE = 4

# Working-memory lifetimes for things that are useful now and wrong tomorrow.
TEMPORARY_TTL_SECONDS = 6 * 60 * 60          # "I'm debugging Calendar this morning"
CONVERSATION_TTL_SECONDS = 12 * 60 * 60      # "for this conversation, call it Alpha"

STATUS_PENDING = "pending"
STATUS_STORED = "stored"
STATUS_REJECTED = "rejected"
STATUS_EXPIRED = "expired"

SCOPE_USER = mm.SCOPE_USER
SCOPE_PROJECT = mm.SCOPE_PROJECT
SCOPE_CONVERSATION = "conversation"
SCOPE_TEMPORARY = "temporary"

# Pattern strength tiers. The tier sets the starting confidence, because HOW
# something is said is the best available evidence for how settled it is.
TIER_DEFINITIVE = "definitive"   # "My name is Jon Meholli"      — a stated fact
TIER_STRONG = "strong"           # "I prefer VS Code"            — a stated preference
TIER_MODERATE = "moderate"       # "I usually code in VS Code"   — a described habit
TIER_WEAK = "weak"               # "I like dark interfaces"      — an opinion in passing

TIER_BASE_CONFIDENCE = {
    TIER_DEFINITIVE: 0.86,
    TIER_STRONG: 0.62,
    TIER_MODERATE: 0.50,
    TIER_WEAK: 0.38
}

TIER_REINFORCEMENT = {
    TIER_DEFINITIVE: REINFORCE_STRONG,
    TIER_STRONG: REINFORCE_STRONG,
    TIER_MODERATE: REINFORCE_MODERATE,
    TIER_WEAK: REINFORCE_INCIDENTAL
}

_lock = threading.RLock()

# The candidate file is read on every UI broadcast, so it is cached against its
# own mtime. The path is resolved fresh each time from memory_manager.MEMORY_DIR,
# which is what makes configure_memory_dir() (used by the tests) apply here too
# without any cross-module bookkeeping.
_cache: Dict[str, object] = {"path": None, "mtime": None, "data": None}


# ==========================================
# DEBUG LOGGING
# ==========================================
def _debug_enabled() -> bool:
    """Learning logs ride the existing performance-log setting.

    They are genuinely useful when tuning thresholds and pure noise otherwise,
    so they share the switch Jon already turns off when he wants a quiet terminal.
    """
    if (os.getenv("FRIDAY_PERF_LOG") or os.getenv("JARVIS_PERF_LOG", "1")).strip().lower() not in {"1", "true", "yes", "on"}:
        return False

    try:
        from Core_Cognition.settings_manager import get_setting

        return bool(get_setting("performance_logs", True))
    except Exception:
        return True


def _log(event: str, detail: str = "") -> None:
    if not _debug_enabled():
        return

    line = f"[MEMORY] {event}"

    if detail:
        line += f": {detail}"

    print(line)


# ==========================================
# STORAGE
# ==========================================
def candidates_file() -> Path:
    """Resolved fresh so a reconfigured memory directory is picked up at once."""
    return mm.MEMORY_DIR / "candidates.json"


def _default_store() -> dict:
    return {
        "version": 1,
        "updated_at": mm._now_iso(),
        "candidates": []
    }


def _safe_candidate(value: dict) -> Optional[dict]:
    """Normalise one candidate; None if it carries nothing usable.

    Every field is listed here once, so a file written by an earlier build is
    upgraded just by being read — the same contract memory_manager uses.
    """
    source = value if isinstance(value, dict) else {}
    text = mm._clean_text(source.get("text"))

    if not text:
        return None

    status = str(source.get("status") or STATUS_PENDING).strip().lower()

    if status not in {STATUS_PENDING, STATUS_STORED, STATUS_REJECTED, STATUS_EXPIRED}:
        status = STATUS_PENDING

    scope = str(source.get("scope") or SCOPE_USER).strip().lower()

    if scope not in {SCOPE_USER, SCOPE_PROJECT, SCOPE_CONVERSATION, SCOPE_TEMPORARY}:
        scope = SCOPE_USER

    first_seen = str(source.get("first_seen") or mm._now_iso())

    try:
        confidence = float(source.get("confidence", 0.0))
    except Exception:
        confidence = 0.0

    return {
        "id": str(source.get("id") or f"cand_{uuid.uuid4().hex[:10]}"),
        "text": text,
        "normalized_fact": mm._clean_text(source.get("normalized_fact")) or normalize_fact(text),
        "fact_domain": str(source.get("fact_domain") or ""),
        "category": str(source.get("category") or "fact"),
        "scope": scope,
        "project_id": str(source.get("project_id") or ""),
        "confidence": round(max(0.0, min(CONFIDENCE_CEILING, confidence)), 3),
        "importance": mm._clamp_importance(source.get("importance", 2)),
        "occurrences": max(1, int(source.get("occurrences") or 1)),
        "first_seen": first_seen,
        "last_seen": str(source.get("last_seen") or first_seen),
        "source": str(source.get("source") or "observed"),
        "tier": str(source.get("tier") or TIER_MODERATE),
        "conversation_id": str(source.get("conversation_id") or ""),
        "auto_save_eligible": bool(source.get("auto_save_eligible", False)),
        "status": status,
        # Short quotes of what was actually said, so a pending candidate can be
        # judged by the user without trusting our paraphrase of it.
        "evidence": [
            mm._clean_text(item)[:180]
            for item in (source.get("evidence") or [])
            if mm._clean_text(item)
        ][-MAX_EVIDENCE_PER_CANDIDATE:],
        "contradicts": str(source.get("contradicts") or ""),
        "promoted_memory_id": str(source.get("promoted_memory_id") or "")
    }


def _load() -> dict:
    path = candidates_file()
    mtime = mm._file_mtime(path)

    if _cache["path"] == str(path) and _cache["mtime"] == mtime and _cache["data"] is not None:
        return _cache["data"]

    raw = None

    try:
        with open(path, "r") as handle:
            loaded = json.load(handle)

        raw = loaded if isinstance(loaded, dict) else None
    except FileNotFoundError:
        raw = None
    except Exception:
        mm._backup_file(path, reason="corrupt")
        raw = None

    data = raw or _default_store()
    data.setdefault("candidates", [])
    _cache.update({"path": str(path), "mtime": mtime, "data": data})
    return data


def _save(data: dict) -> None:
    path = candidates_file()
    data["version"] = 1
    data["updated_at"] = mm._now_iso()
    mm._write_json(path, data)
    _cache.update({"path": str(path), "mtime": mm._file_mtime(path), "data": data})


def all_candidates(status: Optional[str] = None) -> List[dict]:
    """Every candidate, most recently seen first."""
    with _lock:
        data = _load()
        records = [record for record in (_safe_candidate(item) for item in data["candidates"]) if record]

        if status:
            records = [record for record in records if record["status"] == status]

        records.sort(key=lambda item: str(item.get("last_seen") or ""), reverse=True)
        return records


def get_candidate(candidate_id: str) -> Optional[dict]:
    target = mm._clean_text(candidate_id)

    for record in all_candidates():
        if record["id"] == target:
            return record

    return None


def _write_candidates(records: List[dict]) -> None:
    """Persist the full candidate list, capped, keeping pending work first."""
    pending = [item for item in records if item["status"] == STATUS_PENDING]
    other = [item for item in records if item["status"] != STATUS_PENDING]
    # Resolved candidates are only history; pending ones are live work, so they
    # are the last thing to be dropped when the cap bites.
    other.sort(key=lambda item: str(item.get("last_seen") or ""), reverse=True)
    room = max(0, MAX_CANDIDATES - len(pending))
    data = _load()
    data["candidates"] = pending + other[:room]
    _save(data)


def reset_cache() -> None:
    """Drop the in-process cache. Used by the tests after switching directories."""
    with _lock:
        _cache.update({"path": None, "mtime": None, "data": None})


# ==========================================
# NORMALISATION
# ==========================================
# Two statements that mean the same thing must reinforce ONE candidate rather
# than creating two half-confident ones. This is the smallest normaliser that
# achieves that: alias folding for names this application actually knows, then
# filler removal.

VALUE_ALIASES = {
    "vs code": ("vscode", "visual studio code", "vs-code", "code editor vscode"),
    "pycharm": ("py charm",),
    "neovim": ("nvim",),
    "graphite": ("graphite mode", "graphite theme", "charcoal", "steel"),
    "midnight": ("midnight mode", "midnight theme"),
    "high contrast": ("high-contrast",),
    "tactical green": ("green theme",),
    "white mode": ("light mode", "white theme", "light theme"),
    "gemini live": ("gemini-live", "live api", "gemini live api")
}

FILLER_WORDS = (
    "really", "actually", "basically", "definitely", "honestly", "personally",
    "generally", "usually", "always", "normally", "typically", "mostly",
    "kind of", "sort of", "pretty much", "i think", "i guess", "you know"
)


def normalize_fact(text: str) -> str:
    """A comparable form of a claim: lower case, alias-folded, filler removed."""
    value = mm._clean_text(text).lower().strip(" .!?,")

    for canonical, aliases in VALUE_ALIASES.items():
        for alias in aliases:
            value = re.sub(rf"\b{re.escape(alias)}\b", canonical, value)

    for filler in FILLER_WORDS:
        value = re.sub(rf"\b{re.escape(filler)}\b", " ", value)

    value = re.sub(r"\s+", " ", value).strip()
    return value


# ==========================================
# FACT DOMAINS
# ==========================================
# A domain is "the question this claim answers" — the preferred theme, the
# preferred editor, the user's name. Two claims in the same domain with
# different values contradict each other; two claims in different domains do
# not, however similar the words are.
#
# Deliberately small. A general ontology would be guesswork; these are the
# single-valued facts FRIDAY actually reasons about.

DOMAIN_VALUE_SETS = {
    "preference:theme": (
        "graphite", "midnight", "tactical green", "high contrast", "white mode",
        "dark mode", "light mode", "jarvis blue", "friday blue", "dark", "light"
    ),
    "preference:editor": ("vs code", "xcode", "pycharm", "sublime text", "neovim", "vim", "emacs"),
    "preference:voice": ("fish audio", "moira", "local voice", "friday local")
}

DOMAIN_SUBJECT_PATTERNS = (
    (r"\b(?:my name is|call me|i'?m called)\b", "identity:name"),
    (r"\bmy birthday\b", "identity:birthday"),
    (r"\bmy (?:major|degree|course)\b", "identity:major"),
    (r"\bmy (?:school|university|college)\b", "identity:school"),
    (r"\bmy (?:job|occupation|role|title)\b", "identity:occupation"),
    (r"\bmy (?:main|primary|current) project\b", "goal:main_project"),
    (r"\bmy (?:email|e-mail)\b", "identity:email"),
    (r"\bmy (?:phone|number)\b", "identity:phone"),
    (r"\bmy timezone\b", "identity:timezone")
)


def fact_domain(text: str, category: str = "") -> str:
    """Which single-valued question does this claim answer, if any?

    The category matters: extraction has already decided whether the sentence is
    a preference or a habit, and naming a known value inside one of those is a
    claim about it. Outside them the sentence has to say so itself — a passing
    mention of "graphite" is not a statement about the theme.
    """
    value = normalize_fact(text)

    for pattern, domain in DOMAIN_SUBJECT_PATTERNS:
        if re.search(pattern, value):
            return domain

    if str(category or "").lower() in {"preference", "routine"} or _expresses_preference(value):
        for domain, values in DOMAIN_VALUE_SETS.items():
            if any(re.search(rf"\b{re.escape(item)}\b", value) for item in values):
                return domain

    return ""


def domain_value(text: str, domain: str) -> str:
    """The value a claim assigns within its domain, for comparing two claims."""
    value = normalize_fact(text)

    if domain in DOMAIN_VALUE_SETS:
        for item in DOMAIN_VALUE_SETS[domain]:
            if re.search(rf"\b{re.escape(item)}\b", value):
                return item

        return ""

    match = re.search(r"\b(?:is|are|was|=)\s+(?P<value>.+)$", value)

    if match:
        return match.group("value").strip(" .!?,")

    match = re.search(r"\b(?:call me|i'?m called)\s+(?P<value>.+)$", value)

    if match:
        return match.group("value").strip(" .!?,")

    return ""


PREFERENCE_VERBS = (
    "prefer", "prefers", "preferred", "favou?rite", "like", "likes", "love",
    "hate", "use", "uses", "using", "want", "keep", "stick with",
    "switch to", "go with", "default", "code in", "work in", "write in"
)


def _expresses_preference(value: str) -> bool:
    return bool(re.search(rf"\b(?:{'|'.join(PREFERENCE_VERBS)})\b", value))


# ==========================================
# EXTRACTION
# ==========================================
# The gate is: does this sentence ASSERT something durable about Jon, his
# projects, or how he wants FRIDAY to behave? Everything else is conversation.

# Things that are never worth learning from, checked before anything else.
# Cheap, and it is what keeps "Open music" and "thanks" out of the pipeline.
NEVER_LEARN_PATTERNS = (
    r"^(?:thanks|thank you|thx|cheers|ok|okay|cool|nice|great|sure|yes|no|yep|nope|yeah)\b",
    r"^(?:hi|hey|hello|morning|good morning|good afternoon|good evening|goodnight|bye)\b",
    r"^(?:open|close|show|hide|start|stop|play|pause|resume|skip|next|previous|clear|refresh|"
    r"mute|unmute|turn on|turn off|sleep|wake up|shut down|exit|quit|switch to|set)\b",
    r"^(?:what|when|where|who|why|how|which|is|are|do|does|did|can|could|would|will|should)\b",
    r"^(?:search|google|look up|find|tell me|show me|give me|read)\b",
    r"\?$"
)

# A statement about right now, not about Jon. These go to working memory with a
# lifetime instead of becoming a fact.
TEMPORARY_MARKERS = (
    "today", "tonight", "right now", "at the moment", "currently", "this morning",
    "this afternoon", "this evening", "for now", "just now", "at present",
    "tired", "hungry", "sleepy", "bored", "sick", "busy", "stressed"
)

# Not serious. Confidence is cut hard rather than the sentence being dropped,
# because "haha I basically live in VS Code" is still evidence, just weak.
HUMOUR_MARKERS = ("lol", "haha", "jk", "just kidding", "kidding", "joking", "😂", "🤣")

# Hedged. The claim is real but Jon is not committing to it.
HEDGE_MARKERS = (
    "maybe", "might", "i think", "probably", "possibly", "kind of", "sort of",
    "not sure", "i guess", "perhaps", "could be", "leaning towards", "leaning toward"
)

# Committed. Language that says this is settled and should stick.
DURABLE_MARKERS = (
    "always", "never", "by default", "going forward", "from now on", "every time",
    "in general", "as a rule", "permanently", "definitely", "for good"
)

# An explicit correction of something previously said.
CORRECTION_MARKERS = (
    "actually", "instead", "no longer", "not any more", "not anymore", "changed my mind",
    "i changed", "scratch that", "correction", "rather than", "these days", "now i", "i now"
)

# Engineering decisions. These are high-value and usually stated once, plainly,
# which is why they are allowed a definitive tier of their own.
PROJECT_DECISION_PATTERNS = (
    r"\bwe (?:decided|agreed|chose|settled on)\b",
    r"\bi decided\b",
    r"\b(?:should|must|has to|needs to) (?:use|be|stay|remain|never|always)\b",
    r"\b(?:must|should) (?:not|never)\b",
    r"\bnever (?:do|use|create|add|fake|open|make)\b",
    r"\bdo not (?:fake|invent|create|add)\b",
    r"\bthe architecture\b",
    r"\bkeep (?:this|it) simple\b",
    r"\bfor this project\b",
    r"\buse .+ for this project\b"
)

# Adverbs that sit between the subject and the verb and break an anchored
# pattern without changing what is being claimed. "I really prefer VS Code" is
# the same claim as "I prefer VS Code", so they are removed before matching —
# but NOT before scoring, where words like "definitely" are real evidence.
MATCH_NOISE_WORDS = (
    "really", "actually", "honestly", "personally", "basically", "literally",
    "totally", "absolutely", "definitely", "certainly", "obviously", "just",
    "quite", "very", "pretty much", "kind of", "sort of", "sometimes"
)


# Hedged openings. Stripped for matching so "I think I prefer Midnight" is
# recognised as the preference it is — the hedge is not discarded, it is scored
# as a hedge, which is what keeps the candidate below the auto-save bar.
_HEDGE_PREFIX = re.compile(
    r"^(?:i think|i guess|i reckon|i feel like|i suppose|maybe|perhaps|honestly|i'd say|id say)\s+"
)


def _match_form(lowered: str) -> str:
    """The sentence with hedges and intervening adverbs removed, for matching only."""
    value = lowered

    for _ in range(2):
        stripped = _HEDGE_PREFIX.sub("", value).strip()

        if stripped == value:
            break

        value = stripped

    for word in MATCH_NOISE_WORDS:
        value = re.sub(rf"\b{re.escape(word)}\b", " ", value)

    return re.sub(r"\s+", " ", value).strip()


# Sentences are analysed one at a time. "I actually like Midnight better now.
# Use Midnight going forward." carries a weak opinion AND a definitive
# instruction; scoring them as one blob loses the instruction, which is the part
# that matters.
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+|\n+")
MAX_SENTENCES_PER_TURN = 6
MAX_EXTRACTIONS_PER_TURN = 2


def _sentences(text: str) -> List[str]:
    parts = [mm._clean_text(part) for part in _SENTENCE_SPLIT.split(str(text or ""))]
    return [part for part in parts if len(part) >= 6][:MAX_SENTENCES_PER_TURN]


class Extraction:
    """One durable claim found in a sentence."""

    def __init__(self, text, category, tier, scope, importance, domain="", project_id=""):
        self.text = text
        self.category = category
        self.tier = tier
        self.scope = scope
        self.importance = importance
        self.domain = domain
        self.project_id = project_id

    def as_dict(self) -> dict:
        return {
            "text": self.text,
            "category": self.category,
            "tier": self.tier,
            "scope": self.scope,
            "importance": self.importance,
            "fact_domain": self.domain,
            "project_id": self.project_id
        }


# (pattern, category, tier, importance) — matched against the lower-cased
# sentence. Order matters: the first match wins, so definitive forms come first.
EXTRACTION_PATTERNS = (
    # ---- identity: stated outright, single-valued, high value ----
    (r"^my name is\s+.{2,60}$", "identity", TIER_DEFINITIVE, 5),
    (r"^(?:you can\s+)?call me\s+.{2,40}$", "identity", TIER_DEFINITIVE, 5),
    (r"^my (?:birthday|email|phone|timezone) is\s+.{2,60}$", "identity", TIER_DEFINITIVE, 4),
    (r"^my (?:major|degree|course) is\s+.{2,80}$", "identity", TIER_DEFINITIVE, 4),
    (r"^i (?:study|major in)\s+.{3,80}$", "identity", TIER_DEFINITIVE, 4),
    (r"^i (?:go to|attend)\s+.{3,80}$", "identity", TIER_DEFINITIVE, 4),
    (r"^i(?:'m| am) (?:a|an)\s+.{3,60}(?:engineer|developer|student|designer|scientist|"
     r"researcher|manager|founder|writer|analyst)\b.*$", "identity", TIER_STRONG, 4),
    (r"^my (?:job|occupation|role) is\s+.{3,70}$", "identity", TIER_DEFINITIVE, 4),

    # ---- goals ----
    (r"^my (?:main|primary|current) project is\s+.{2,60}$", "goal", TIER_DEFINITIVE, 4),
    (r"^i(?:'m| am) building\s+.{4,90}$", "goal", TIER_STRONG, 3),
    (r"^my goal is\s+.{4,90}$", "goal", TIER_DEFINITIVE, 4),
    (r"^i(?:'m| am) (?:learning|studying)\s+.{3,80}$", "goal", TIER_MODERATE, 3),

    # ---- preferences: stated ----
    (r"^(?:use|keep)\s+.{2,60}\s+by default$", "preference", TIER_DEFINITIVE, 4),
    (r"^(?:use|switch to|go with|keep|stick with|default to)\s+.{2,60}\s+"
     r"(?:going forward|from now on|permanently|for good)$", "preference", TIER_DEFINITIVE, 4),
    # A standing instruction about how FRIDAY herself should behave. "Keep
    # FRIDAY dark" is a preference, not a one-off command, which is why it is
    # matched here and not filtered out as an imperative.
    (r"^(?:keep|make)\s+(?:friday|jarvis|the interface|the ui|everything|it)\s+.{2,40}$",
     "preference", TIER_DEFINITIVE, 4),
    (r"^my (?:favou?rite|preferred)\s+.{3,80}$", "preference", TIER_STRONG, 3),
    (r"^i prefer\s+.{3,90}$", "preference", TIER_STRONG, 3),
    (r"^i(?:'d| would) (?:rather|prefer)\s+.{3,90}$", "preference", TIER_STRONG, 3),
    (r"^i (?:always|never)\s+.{4,90}$", "preference", TIER_STRONG, 3),

    # ---- preferences: described habits ----
    (r"^i (?:usually|normally|generally|typically|mostly|often)\s+.{4,90}$", "routine", TIER_MODERATE, 3),
    (r"^i (?:use|run|code in|work in|write)\s+.{3,90}$", "preference", TIER_MODERATE, 3),
    (r"^i (?:code|develop|build|work)\s+(?:in|on|with)\s+.{3,80}$", "preference", TIER_MODERATE, 3),

    # ---- preferences: opinions in passing ----
    (r"^i (?:like|love|enjoy)\s+.{4,90}$", "preference", TIER_WEAK, 2),
    (r"^i (?:hate|dislike|avoid|can'?t stand)\s+.{4,90}$", "preference", TIER_WEAK, 2),
    (r"^.{4,60}\s+(?:hurt|hurts|bother|bothers|strain|strains)\s+my eyes\b.*$", "preference", TIER_MODERATE, 3),

    # ---- places and people ----
    (r"^i (?:live|work)\s+(?:in|at)\s+.{3,70}$", "location", TIER_STRONG, 3),
    (r"^my (?:wife|husband|partner|mother|father|brother|sister|manager|boss|friend)\s+"
     r"(?:is|works|lives)\s+.{2,70}$", "person", TIER_STRONG, 3),

    # ---- routines ----
    (r"^(?:every|each) (?:morning|day|week|monday|friday|weekend)\b.{3,90}$", "routine", TIER_MODERATE, 3),
    (r"^i start (?:my|the) day\s+.{3,80}$", "routine", TIER_MODERATE, 3)
)


def _matches_any(value: str, patterns) -> bool:
    return any(re.search(pattern, value) for pattern in patterns)


def _contains_any(value: str, markers) -> bool:
    return any(marker in value for marker in markers)


def should_observe(text: str) -> bool:
    """Cheap gate: is this turn worth analysing at all?

    Runs before everything else so that a direct UI command costs a single
    regex sweep and nothing more. This is what keeps "Open music" free.
    """
    clean = mm._clean_text(text)

    if len(clean) < 8:
        return False

    lowered = clean.lower().rstrip(" .!")

    if _matches_any(lowered, NEVER_LEARN_PATTERNS):
        return False

    return True


def extract(text: str, project_id: Optional[str] = None) -> List[Extraction]:
    """Find the durable claims in one thing Jon said.

    Analysed sentence by sentence, at most one claim per sentence. A sentence
    asserting two unrelated durable facts is rare, and splitting one on a guess
    produces two half-formed candidates instead of one good one — whereas two
    SENTENCES really can carry two different claims, and often do:
    "I actually like Midnight better now. Use Midnight going forward."
    """
    clean = mm._clean_text(text)

    if not should_observe(clean):
        return []

    found: List[Extraction] = []

    for sentence in _sentences(clean):
        extraction = _extract_sentence(sentence, project_id)

        if extraction is not None:
            found.append(extraction)

        if len(found) >= MAX_EXTRACTIONS_PER_TURN:
            break

    return found


def _extract_sentence(sentence: str, project_id: Optional[str]) -> Optional[Extraction]:
    """The single strongest durable claim in one sentence, if there is one."""
    lowered = sentence.lower().rstrip(" .!")
    match_form = _match_form(lowered)

    # A sentence that is itself a command carries no claim, even when the turn
    # around it does. This is what stops "Open VS Code" from ever CREATING a
    # preference (reinforcing an existing one is handled separately).
    if _matches_any(lowered, NEVER_LEARN_PATTERNS):
        return None

    # Project decisions first. "FRIDAY-MK1 must never open a second voice
    # session" is a project fact whichever way it is phrased, and phrasing it as
    # a rule is exactly what makes it one.
    project_scope = _detect_project_scope(sentence, lowered, project_id)

    if project_scope is not None:
        target_project, decision_text = project_scope
        decided = _matches_any(lowered, (r"\bwe (?:decided|agreed|chose)\b", r"\bi decided\b"))
        return Extraction(
            text=decision_text,
            category="decision" if decided else "constraint",
            tier=TIER_DEFINITIVE,
            scope=SCOPE_PROJECT,
            importance=4,
            project_id=target_project
        )

    # "For this conversation, call the prototype Alpha."
    conversation_claim = _detect_conversation_scope(sentence, lowered)

    if conversation_claim:
        return Extraction(
            text=conversation_claim,
            category="fact",
            tier=TIER_MODERATE,
            scope=SCOPE_CONVERSATION,
            importance=2
        )

    # A statement about right now never becomes a fact about Jon.
    if _contains_any(lowered, TEMPORARY_MARKERS):
        return Extraction(
            text=sentence.rstrip(" .!"),
            category="fact",
            tier=TIER_WEAK,
            scope=SCOPE_TEMPORARY,
            importance=1
        )

    for pattern, category, tier, importance in EXTRACTION_PATTERNS:
        if re.match(pattern, match_form):
            body = sentence.rstrip(" .!")
            return Extraction(
                text=body,
                category=category,
                tier=tier,
                scope=SCOPE_USER,
                importance=importance,
                domain=fact_domain(body, category)
            )

    return None


def _detect_project_scope(clean: str, lowered: str, project_id: Optional[str]) -> Optional[Tuple[str, str]]:
    """Is this an engineering decision, and about which project?

    Returns (project_id, text) or None. Project facts must never be filed
    against the wrong project, so a named project always wins over the active
    one, and a sentence with no project signal at all is not a project fact.
    """
    named = None

    for entry in mm.list_projects():
        if re.search(rf"\b{re.escape(entry['name'].lower())}\b", lowered):
            named = entry["id"]
            break

    explicit = re.match(
        r"^(?:for|on|in)\s+(?:this|the)\s+project\s*[:,]?\s*(?P<rest>.+)$",
        lowered
    )

    if explicit:
        return (mm.resolve_project_id(project_id), clean[explicit.start("rest"):].strip(" .!"))

    if not _matches_any(lowered, PROJECT_DECISION_PATTERNS):
        return None

    # A decision phrased as a rule, with a project named in it, belongs to that
    # project. Without a named project it falls to whichever one is active.
    return (named or mm.resolve_project_id(project_id), clean.rstrip(" .!"))


def _detect_conversation_scope(clean: str, lowered: str) -> str:
    match = re.match(
        r"^(?:for|in)\s+(?:this|the)\s+(?:conversation|chat|thread)\s*[:,]?\s*(?P<rest>.+)$",
        lowered
    )

    if match:
        return clean[match.start("rest"):].strip(" .!")

    return ""


# ==========================================
# SCORING
# ==========================================
def score_candidate(extraction: Extraction, text: str) -> Tuple[float, int]:
    """Starting confidence and importance for a newly observed claim.

    Confidence starts from the pattern tier — how the claim was phrased is the
    best available evidence for how settled it is — and is then adjusted by a
    handful of linguistic signals:

        durable framing ("always", "by default")   +0.08
        an explicit correction ("actually", "now")  +0.05
        hedging ("maybe", "I think")               -0.18
        humour ("lol", "just kidding")             -0.30
        conditional framing ("if", "when I")       -0.08

    Importance is scored separately and never affects whether something is
    stored — only how it ranks afterwards. See the module docstring.
    """
    lowered = mm._clean_text(text).lower()
    confidence = TIER_BASE_CONFIDENCE.get(extraction.tier, 0.45)
    importance = extraction.importance

    if _contains_any(lowered, DURABLE_MARKERS):
        confidence += 0.08
        importance += 1

    if _contains_any(lowered, CORRECTION_MARKERS):
        confidence += 0.05

    if _contains_any(lowered, HEDGE_MARKERS):
        confidence -= 0.18

    if _contains_any(lowered, HUMOUR_MARKERS):
        confidence -= 0.30

    if re.search(r"\b(?:if|whenever|in case|suppose|imagine)\b", lowered):
        confidence -= 0.08

    # A claim about a single-valued question is more useful than a loose remark,
    # because it can be looked up, corrected and superseded cleanly.
    if extraction.domain:
        importance += 1

    if extraction.scope == SCOPE_TEMPORARY:
        importance = 1

    return (
        round(max(0.0, min(CONFIDENCE_CEILING, confidence)), 3),
        mm._clamp_importance(importance)
    )


_TIER_ORDER = (TIER_WEAK, TIER_MODERATE, TIER_STRONG, TIER_DEFINITIVE)


def _strongest_tier(left: str, right: str) -> str:
    ranked = {tier: index for index, tier in enumerate(_TIER_ORDER)}
    return left if ranked.get(left, 0) >= ranked.get(right, 0) else right


def _reinforcement_step(confidence: float, tier: str, incidental: bool) -> float:
    """Close some of the remaining distance to certainty.

    Diminishing by construction: each repeat is worth less than the last, so a
    claim approaches 1.0 without ever getting there and nothing can be talked
    into being a fact by sheer repetition of a weak phrasing.
    """
    factor = REINFORCE_INCIDENTAL if incidental else TIER_REINFORCEMENT.get(tier, REINFORCE_MODERATE)
    return round(min(CONFIDENCE_CEILING, confidence + (1.0 - confidence) * factor), 3)


# ==========================================
# OBSERVATION — the main entry point
# ==========================================
def observe(
    text: str,
    conversation_id: str = "",
    project_id: Optional[str] = None,
    role: str = "user"
) -> dict:
    """Analyse one turn and update the candidate store accordingly.

    This is the whole learning pipeline in one call, and it is designed to be
    run OFF the response path — see main.py, which hands it to a background
    worker after the reply has already gone out.

    Returns a report describing what happened, which is what the learning toast
    is built from. An empty report means nothing was learned, which is the
    normal and expected outcome for most turns.
    """
    report = {
        "observed": False,
        "created": [],
        "reinforced": [],
        "promoted": [],
        "updated": [],
        "temporary": [],
        "blocked": []
    }

    # FRIDAY's own words are not evidence about Jon. Learning from them would
    # let a single paraphrase of hers harden into a fact he never stated.
    if str(role or "user").strip().lower() not in {"user", "jon", ""}:
        return report

    clean = mm._clean_text(text)

    if not should_observe(clean):
        # A command is not a claim, so it can never create a candidate — but it
        # can still be evidence FOR one. "Open VS Code" said while a VS Code
        # preference is pending is a real, if weak, vote for that preference.
        # This is the only thing a no-learn turn is allowed to do.
        reinforced = _reinforce_from_mention(clean)

        if reinforced:
            report["observed"] = True
            report["reinforced"].append(reinforced["candidate"])

            if reinforced.get("promoted"):
                report["promoted"].append(reinforced["promoted"])

        return report

    report["observed"] = True
    extractions = extract(clean, project_id=project_id)

    if not extractions:
        # Nothing asserted, but a named file is still worth holding briefly so
        # "the file we were just discussing" resolves. Existing v2 behaviour.
        mm.capture_working_references(clean, conversation_id)
        return report

    # Ids already touched by THIS utterance. Two sentences making the same claim
    # are one piece of evidence, not two — "I like Midnight now. Use Midnight
    # going forward." must not count as two independent confirmations, or a
    # single sentence pair could talk its way past any threshold.
    seen_this_turn: set = set()

    for extraction in extractions:
        if extraction.scope in {SCOPE_TEMPORARY, SCOPE_CONVERSATION}:
            entry = _store_temporary(extraction, conversation_id)

            if entry:
                report["temporary"].append(entry)

            continue

        outcome = _observe_durable(extraction, clean, conversation_id, project_id, seen_this_turn)

        for key in ("created", "reinforced", "promoted", "updated", "blocked"):
            if outcome.get(key):
                report[key].append(outcome[key])

    mm.capture_working_references(clean, conversation_id)
    return report


def _reinforce_from_mention(text: str) -> Optional[dict]:
    """Nudge a pending candidate whose value was mentioned in passing.

    Strictly one-way: this can only strengthen a candidate that already exists,
    and only through the value it is a claim ABOUT. It can never create one, so
    no amount of "Open music" will ever invent a preference.
    """
    with _lock:
        records = all_candidates()
        pending = [record for record in records if record["status"] == STATUS_PENDING and record["fact_domain"]]

        if not pending:
            return None

        lowered = mm._clean_text(text).lower()
        target = None

        for record in pending:
            value = domain_value(record["text"], record["fact_domain"])

            if value and re.search(rf"\b{re.escape(value)}\b", normalize_fact(lowered)):
                target = record
                break

        if target is None:
            return None

        target["confidence"] = _reinforcement_step(target["confidence"], target["tier"], incidental=True)
        target["occurrences"] += 1
        target["last_seen"] = mm._now_iso()
        target["auto_save_eligible"] = _auto_save_eligible(target)
        _log(
            "candidate reinforced",
            f"{target['text'][:60]} -> {target['confidence']:.2f} (incidental mention)"
        )

        promoted = None

        if target["auto_save_eligible"] and detect_contradiction(target) is None:
            promoted = _promote(target)

        _write_candidates(records)
        return {"candidate": target, "promoted": promoted}


def _store_temporary(extraction: Extraction, conversation_id: str) -> Optional[dict]:
    """Route a here-and-now statement into working memory, where it expires."""
    ttl = CONVERSATION_TTL_SECONDS if extraction.scope == SCOPE_CONVERSATION else TEMPORARY_TTL_SECONDS
    entry = mm.note_working(extraction.text, conversation_id, ttl_seconds=ttl)

    if entry:
        _log("temporary noted", f"{extraction.text[:60]} (expires {entry['expires_at']})")

    return entry


def _observe_durable(
    extraction: Extraction,
    raw_text: str,
    conversation_id: str,
    project_id: Optional[str],
    seen_this_turn: Optional[set] = None
) -> dict:
    """Create or reinforce one candidate, and promote it if it has earned it."""
    outcome: Dict[str, object] = {}
    confidence, importance = score_candidate(extraction, raw_text)
    normalized = normalize_fact(extraction.text)
    scope = extraction.scope
    target_project = extraction.project_id or (
        mm.resolve_project_id(project_id) if scope == SCOPE_PROJECT else ""
    )

    with _lock:
        records = all_candidates()
        existing = _find_matching_candidate(
            normalized, extraction.domain, scope, target_project, records
        )
        now = mm._now_iso()

        if existing is not None and existing["status"] == STATUS_REJECTED:
            # Jon said no to this once. Do not quietly start collecting it again.
            _log("candidate blocked", f"previously rejected: {existing['text'][:60]}")
            outcome["blocked"] = existing
            return outcome

        if existing is not None:
            # Same claim heard again, and this time possibly stated better.
            # A stronger phrasing upgrades the candidate's tier, because the
            # best evidence heard so far is what should drive how fast it
            # converges — "I usually code in VS Code" followed by "I prefer VS
            # Code" is a firmer claim than the first sentence alone.
            incidental = extraction.tier == TIER_WEAK and existing["tier"] != TIER_WEAK
            best_tier = _strongest_tier(existing["tier"], extraction.tier)
            existing["tier"] = best_tier
            same_turn = seen_this_turn is not None and existing["id"] in seen_this_turn

            if same_turn:
                # Another sentence of the same utterance. Take the better
                # phrasing and the higher starting confidence, but do not treat
                # it as a second sighting.
                existing["confidence"] = round(max(existing["confidence"], confidence), 3)
            else:
                existing["confidence"] = _reinforcement_step(
                    max(existing["confidence"], confidence), best_tier, incidental
                )
                existing["occurrences"] += 1

            existing["last_seen"] = now
            existing["importance"] = mm._clamp_importance(max(existing["importance"], importance))
            existing["text"] = extraction.text if extraction.tier != TIER_WEAK else existing["text"]
            existing["normalized_fact"] = normalize_fact(existing["text"])
            existing["evidence"] = (existing["evidence"] + [raw_text])[-MAX_EVIDENCE_PER_CANDIDATE:]

            if extraction.domain and not existing["fact_domain"]:
                existing["fact_domain"] = extraction.domain

            candidate = existing
            outcome["reinforced"] = candidate
            _log(
                "candidate reinforced",
                f"{candidate['text'][:60]} -> {candidate['confidence']:.2f} "
                f"({candidate['occurrences']}x)"
            )

            # Already a real memory: saying it again is not new information, but
            # it IS more evidence that the fact matters. Mark it used and let a
            # repeatedly-confirmed fact climb the ranking, without ever creating
            # a second copy of it.
            if candidate["status"] == STATUS_STORED and candidate["promoted_memory_id"]:
                mm.touch(candidate["promoted_memory_id"])

                if candidate["occurrences"] >= 4:
                    stored_memory = mm.get_memory(candidate["promoted_memory_id"])

                    if stored_memory and stored_memory["importance"] < candidate["importance"] + 1:
                        mm.update_memory(
                            candidate["promoted_memory_id"],
                            importance=min(mm.IMPORTANCE_MAX, stored_memory["importance"] + 1)
                        )
        else:
            candidate = _safe_candidate({
                "text": extraction.text,
                "normalized_fact": normalized,
                "fact_domain": extraction.domain,
                "category": extraction.category,
                "scope": scope,
                "project_id": target_project,
                "confidence": confidence,
                "importance": importance,
                "occurrences": 1,
                "first_seen": now,
                "last_seen": now,
                "source": "observed",
                "tier": extraction.tier,
                "conversation_id": conversation_id,
                "status": STATUS_PENDING,
                "evidence": [raw_text]
            })

            if candidate is None:
                return outcome

            if candidate["confidence"] < MIN_KEEP_CONFIDENCE:
                _log("candidate ignored", f"{candidate['text'][:60]} ({candidate['confidence']:.2f})")
                return outcome

            records.append(candidate)
            outcome["created"] = candidate
            _log(
                "candidate created",
                f"{candidate['text'][:60]} ({candidate['tier']}, {candidate['confidence']:.2f})"
            )

        if seen_this_turn is not None:
            seen_this_turn.add(candidate["id"])

        candidate["auto_save_eligible"] = _auto_save_eligible(candidate)
        conflict = detect_contradiction(candidate)
        promoted = None

        if conflict is not None:
            resolution = _resolve_contradiction(candidate, conflict, raw_text)

            if resolution.get("updated"):
                outcome["updated"] = resolution["updated"]
            elif resolution.get("held"):
                candidate["contradicts"] = conflict["id"]
        elif candidate["auto_save_eligible"]:
            promoted = _promote(candidate)

            if promoted:
                outcome["promoted"] = promoted

        _write_candidates(records)

    return outcome


def _find_matching_candidate(
    normalized: str,
    domain: str,
    scope: str,
    project_id: str,
    records: List[dict]
) -> Optional[dict]:
    """The candidate this observation should reinforce, if any.

    Domain first: two statements answering the same single-valued question are
    the same candidate even when worded completely differently. Otherwise fall
    back to close textual overlap, which catches rephrasings within a domain we
    do not model.
    """
    scoped = [
        record for record in records
        if record["scope"] == scope
        and (scope != SCOPE_PROJECT or record["project_id"] == project_id)
    ]

    if domain:
        for record in scoped:
            if record["fact_domain"] == domain:
                # Same question AND same answer reinforces. A different answer is
                # a contradiction, which is handled against stored memories, not
                # by silently merging two conflicting candidates.
                if domain_value(record["text"], domain) == domain_value(normalized, domain):
                    return record

    target_tokens = frozenset(mm._tokens(normalized))

    for record in scoped:
        if record["normalized_fact"] == normalized:
            return record

        if mm._jaccard(target_tokens, frozenset(mm._tokens(record["normalized_fact"]))) >= 0.72:
            return record

    return None


def _auto_save_eligible(candidate: dict) -> bool:
    """Has this candidate earned a place in long-term memory?

    Two independent bars, both of which must be cleared: enough confidence, and
    enough evidence. A definitive statement carries its own evidence — Jon
    stating his name once is not a guess — so it is exempt from the repeat
    requirement. Nothing else is.
    """
    if candidate["status"] != STATUS_PENDING:
        return False

    if candidate["confidence"] < AUTO_SAVE_THRESHOLD:
        return False

    if candidate["tier"] == TIER_DEFINITIVE:
        return True

    return candidate["occurrences"] >= MIN_OCCURRENCES_FOR_AUTO_SAVE


# ==========================================
# CONTRADICTIONS
# ==========================================
def detect_contradiction(candidate: dict) -> Optional[dict]:
    """Find a stored memory this candidate disagrees with.

    Only within a modelled domain, and only when the VALUES actually differ.
    Without a domain there is no way to tell "another preference" from "a
    different answer to the same question", and guessing would mean deleting
    facts on a hunch.
    """
    domain = candidate.get("fact_domain") or ""

    if not domain:
        return None

    new_value = domain_value(candidate["text"], domain)

    if not new_value:
        return None

    scope = candidate["scope"]
    memories = mm.all_memories(
        scope=scope,
        project_id=candidate["project_id"] or None,
        include_all_projects=(scope != SCOPE_PROJECT)
    )

    for record in memories:
        if fact_domain(record["text"], record["category"]) != domain:
            continue

        old_value = domain_value(record["text"], domain)

        if old_value and old_value != new_value:
            return record

    return None


def _resolve_contradiction(candidate: dict, conflict: dict, raw_text: str) -> dict:
    """Decide whether a new claim replaces an existing memory.

    The bar is high and deliberately asymmetric. Updating on weak evidence
    destroys something Jon told FRIDAY; leaving the candidate pending costs
    nothing but a line in the pending list he can accept himself.
    """
    lowered = mm._clean_text(raw_text).lower()
    explicit_correction = _contains_any(lowered, CORRECTION_MARKERS) or _contains_any(lowered, DURABLE_MARKERS)
    required = PINNED_OVERRIDE_CONFIDENCE if conflict["pinned"] else CONTRADICTION_UPDATE_CONFIDENCE

    # A pinned memory is one Jon deliberately protected, so automatic mutation
    # additionally needs to have heard the correction in more than one turn.
    # He can still change it instantly by saying "remember that ..." or by
    # promoting the pending candidate himself — both are explicit acts.
    enough_evidence = candidate["occurrences"] >= 2 if conflict["pinned"] else True

    if not explicit_correction or candidate["confidence"] < required or not enough_evidence:
        _log(
            "contradiction held",
            f"{conflict['text'][:40]} vs {candidate['text'][:40]} "
            f"(confidence {candidate['confidence']:.2f}/{required:.2f}, "
            f"{candidate['occurrences']}x, correction={explicit_correction})"
        )
        return {"held": True}

    # Keep what was replaced. A superseded value is the single most useful thing
    # to have when a correction turns out to be the mistake.
    history = list(conflict.get("history") or [])
    history.append({"text": conflict["text"], "replaced_at": mm._now_iso()})

    updated = mm.update_memory(
        conflict["id"],
        text=candidate["text"],
        importance=max(conflict["importance"], candidate["importance"]),
        category=candidate["category"] if candidate["category"] in mm.CATEGORIES else None,
        history=history[-3:]
    )

    if not updated:
        return {"held": True}

    candidate["status"] = STATUS_STORED
    candidate["promoted_memory_id"] = conflict["id"]
    candidate["contradicts"] = ""
    _log("contradiction resolved", f"{conflict['text'][:40]} -> {candidate['text'][:40]}")
    return {"updated": {"memory": updated, "candidate": candidate, "previous": conflict["text"]}}


# ==========================================
# PROMOTION
# ==========================================
def _promote(candidate: dict) -> Optional[dict]:
    """Turn an accepted candidate into a real Memory v2 record.

    Promotion writes through memory_manager.remember(), which means an
    auto-learned memory is an ordinary memory from that moment on: same store,
    same dedupe, same ranking, same retrieval path. There is deliberately no
    second class of memory and no second retrieval route.
    """
    record = mm.remember(
        candidate["text"],
        scope=candidate["scope"] if candidate["scope"] == SCOPE_PROJECT else SCOPE_USER,
        category=candidate["category"] if candidate["category"] in mm.CATEGORIES else None,
        project_id=candidate["project_id"] or None,
        importance=candidate["importance"],
        source="learned",
        conversation_id=candidate["conversation_id"]
    )

    if not record:
        return None

    candidate["status"] = STATUS_STORED
    candidate["promoted_memory_id"] = record["id"]
    candidate["auto_save_eligible"] = False
    _log(
        "memory promoted",
        f"{record['text'][:60]} (confidence {candidate['confidence']:.2f}, "
        f"{candidate['occurrences']}x)"
    )
    return {"memory": record, "candidate": candidate}


def promote_candidate(candidate_id: str) -> Optional[dict]:
    """Accept a pending candidate because Jon said so. Bypasses confidence."""
    with _lock:
        records = all_candidates()
        target = next((item for item in records if item["id"] == mm._clean_text(candidate_id)), None)

        if target is None or target["status"] != STATUS_PENDING:
            return None

        # A manual promotion is Jon's decision, so it also settles any
        # contradiction the candidate was held up by.
        conflict = detect_contradiction(target)

        if conflict is not None:
            history = list(conflict.get("history") or [])
            history.append({"text": conflict["text"], "replaced_at": mm._now_iso()})
            updated = mm.update_memory(
                conflict["id"],
                text=target["text"],
                importance=max(conflict["importance"], target["importance"]),
                history=history[-3:]
            )

            if updated:
                target["status"] = STATUS_STORED
                target["promoted_memory_id"] = conflict["id"]
                target["contradicts"] = ""
                _write_candidates(records)
                _log("candidate promoted by user", f"replaced {conflict['text'][:40]}")
                return {"memory": updated, "candidate": target, "previous": conflict["text"]}

        promoted = _promote(target)
        _write_candidates(records)

        if promoted:
            _log("candidate promoted by user", target["text"][:60])

        return promoted


def reject_candidate(candidate_id: str) -> Optional[dict]:
    """Refuse a candidate permanently.

    Rejection sticks: the candidate stays in the store with status "rejected" so
    that the same claim, heard again, is recognised and dropped rather than
    quietly starting to accumulate confidence a second time.
    """
    with _lock:
        records = all_candidates()
        target = next((item for item in records if item["id"] == mm._clean_text(candidate_id)), None)

        if target is None or target["status"] == STATUS_STORED:
            return None

        target["status"] = STATUS_REJECTED
        target["auto_save_eligible"] = False
        _write_candidates(records)
        _log("candidate rejected", target["text"][:60])
        return target


def reject_matching_candidate(query: str = "", conversation_id: str = "") -> Optional[dict]:
    """Reject the candidate a spoken "don't remember that" refers to.

    With a topic, the best keyword match wins; without one it is the most recent
    pending candidate from this conversation, which is what "that" means when
    someone says it straight after speaking.
    """
    pending = [item for item in all_candidates(STATUS_PENDING)]

    if not pending:
        return None

    clean = mm._clean_text(query)

    if clean:
        query_tokens = frozenset(mm._tokens(clean))
        scored = []

        for item in pending:
            overlap = len(query_tokens & frozenset(mm._tokens(item["text"])))

            if overlap:
                scored.append((overlap, item))

        if scored:
            scored.sort(key=lambda pair: pair[0], reverse=True)
            return reject_candidate(scored[0][1]["id"])

        return None

    from_conversation = [
        item for item in pending
        if not conversation_id or item["conversation_id"] == conversation_id
    ]
    newest = (from_conversation or pending)[0]
    return reject_candidate(newest["id"])


# ==========================================
# HOUSEKEEPING
# ==========================================
def expire_candidates() -> dict:
    """Let weak observations die.

    Candidates are guesses, and a guess nobody repeated for two weeks was
    probably wrong. Stored, rejected and recently reinforced candidates are all
    left alone.
    """
    now = mm._now()
    stale_cutoff = now - datetime.timedelta(days=CANDIDATE_STALE_DAYS)
    age_cutoff = now - datetime.timedelta(days=CANDIDATE_MAX_AGE_DAYS)
    expired = 0

    with _lock:
        records = all_candidates()

        for record in records:
            if record["status"] != STATUS_PENDING:
                continue

            last_seen = mm._parse_iso(record["last_seen"]) or now
            first_seen = mm._parse_iso(record["first_seen"]) or now
            weak_and_stale = record["confidence"] < 0.55 and last_seen < stale_cutoff
            simply_old = first_seen < age_cutoff

            if weak_and_stale or simply_old:
                record["status"] = STATUS_EXPIRED
                expired += 1
                _log("candidate expired", f"{record['text'][:60]} ({record['confidence']:.2f})")

        if expired:
            _write_candidates(records)

    return {"expired_candidates": expired}


def candidate_view(limit: int = 60) -> dict:
    """The payload the Workshop memory panel renders pending candidates from."""
    pending = all_candidates(STATUS_PENDING)

    return {
        "candidates": [
            {
                "id": record["id"],
                "text": record["text"],
                "category": record["category"],
                "scope": record["scope"],
                "project_id": record["project_id"],
                "confidence": record["confidence"],
                "occurrences": record["occurrences"],
                "importance": record["importance"],
                "first_seen": record["first_seen"],
                "last_seen": record["last_seen"],
                "contradicts": record["contradicts"],
                "evidence": record["evidence"]
            }
            for record in pending[:limit]
        ],
        "pending_count": len(pending)
    }


def learning_summary(report: dict) -> Optional[dict]:
    """A one-line description of what was learned, for the UI toast.

    Returns None unless something was ACTUALLY stored or materially updated —
    there is no toast for "a candidate got slightly more confident", because a
    notification that fires on invisible internal state is just noise.
    """
    if not isinstance(report, dict):
        return None

    for entry in report.get("updated", []):
        memory = entry.get("memory") or {}
        return {
            "title": "Memory updated",
            "category": str(memory.get("category") or "").title(),
            "text": memory.get("text") or "",
            "previous": entry.get("previous") or "",
            "memory_id": memory.get("id") or ""
        }

    for entry in report.get("promoted", []):
        memory = entry.get("memory") or {}
        return {
            "title": "Learned",
            "category": str(memory.get("category") or "").title(),
            "text": memory.get("text") or "",
            "previous": "",
            "memory_id": memory.get("id") or ""
        }

    return None
