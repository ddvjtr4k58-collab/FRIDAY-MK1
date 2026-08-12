"""FRIDAY Tool Intelligence — the registry every tool describes itself to.

WHY THIS EXISTS
---------------
FRIDAY used to answer only what she had been given the exact words for. "Open
weather" worked; "what is the weather today" fell through a few hundred lines of
phrase tables and ended up at Gemini, which then had to guess. The knowledge of
what FRIDAY can actually DO lived scattered across those tables, so nothing could
reason about it — not the router, not the model, not a future planner.

This module holds that knowledge in ONE place. A tool declares itself once —
name, description, category, capabilities, inputs, outputs — and from that
declaration it becomes routable. Nothing else has to be edited to add a tool, and
nothing here knows anything about weather, calendars or music: the registry is
pure mechanism, the tools are data.

THE THREE ROUTES, CHEAPEST FIRST
--------------------------------
  1. FAST PATH   exact normalized phrase -> capability. One dict lookup, and the
                 only route obvious commands ever take. "Open music" must not get
                 slower because FRIDAY got smarter.
  2. PATTERN     a capability's own regexes. Still local, still microseconds.
  3. KEYWORD     tool anchors + capability cues, scored. This is what turns
                 "how much battery do I have" into System Status without anyone
                 having written that sentence down anywhere.

Nothing in here calls a model. Resolution is deterministic and local, which is
what makes it safe to run on the response path: a miss costs microseconds and
hands the turn straight back to Gemini.

WHAT A MISS MEANS
-----------------
No match is a real answer, not a failure. `resolve()` returns a Resolution with
kind == CONVERSATION, and the caller carries on to the model. FRIDAY never
invents a tool she does not have.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple


# ==========================================================
# VOCABULARY
# ==========================================================
# What a capability DOES, which is also what the intent layer reports back:
#   DATA   reads something and answers in words        ("what is the weather")
#   ACTION changes state without opening a surface     ("pause the music")
#   OPEN   puts an application or widget on screen     ("open my calendar")
KIND_DATA = "data"
KIND_ACTION = "action"
KIND_OPEN = "open"

# The same distinction as seen by the planner, which only cares about one thing:
# does this step OBSERVE the world or CHANGE it. Reads may be reordered, retried
# and run in parallel; actions may not. Derived from `kind` rather than declared
# separately so a tool cannot describe itself two different ways.
STEP_READ = "read"
STEP_ACTION = "action"

# What the intent layer decides a request IS.
INTENT_CONVERSATION = "conversation"
INTENT_TOOL = "tool"
INTENT_APPLICATION = "application"

# How the match was made, for logging and tests.
ROUTE_FAST_PATH = "fast_path"
ROUTE_PATTERN = "pattern"
ROUTE_KEYWORD = "keyword"
ROUTE_GUARD = "guard"
ROUTE_NONE = "none"

# Confidence floor. Below this the request belongs to Gemini, not to a tool.
MIN_CONFIDENCE = 0.55

# Fixed scores per route. Deliberately a small ladder rather than a formula with
# tuned weights: when a route fires the wrong tool it has to be obvious WHY, and
# four numbers can be reasoned about at a glance.
SCORE_FAST_PATH = 1.0
SCORE_PATTERN = 0.95
SCORE_ANCHOR_AND_CUE = 0.85
SCORE_ANCHOR_DEFAULT = 0.65

# Seconds a step gets when neither its capability nor its tool says otherwise.
# Sized for a local reading, which is what most capabilities are.
DEFAULT_STEP_TIMEOUT = 4.0


_CONTRACTIONS = (
    (r"\bwhat'?s\b", "what is"),
    (r"\bhow'?s\b", "how is"),
    (r"\bwhere'?s\b", "where is"),
    (r"\bwho'?s\b", "who is"),
    (r"\bthat'?s\b", "that is"),
    (r"\bthere'?s\b", "there is"),
    (r"\bi'?m\b", "i am"),
    (r"\bi'?ve\b", "i have"),
    (r"\bi'?ll\b", "i will"),
    (r"\bdon'?t\b", "do not"),
    (r"\bdidn'?t\b", "did not"),
    (r"\bdoesn'?t\b", "does not"),
    (r"\bwon'?t\b", "will not"),
    (r"\bcan'?t\b", "cannot"),
)

_ADDRESS_PREFIX = re.compile(r"^(?:hey\s+|ok\s+|okay\s+)?(?:friday|jarvis|please)\b[\s,]*", re.IGNORECASE)


def normalize(text: str) -> str:
    """One spelling of a request, so every route compares like with like.

    Matches Core_Cognition/main.py's normalize_control_text on the parts that
    overlap (lower case, punctuation stripped, wake name and "please" removed) and
    adds contraction expansion, because the difference between "what's" and
    "what is" is not a difference a router should have to know about.
    """
    value = str(text or "").lower()

    for pattern, replacement in _CONTRACTIONS:
        value = re.sub(pattern, replacement, value)

    value = re.sub(r"[^\w\s]+", " ", value)
    value = re.sub(r"\s+", " ", value).strip()

    while True:
        updated = _ADDRESS_PREFIX.sub("", value).strip()

        if updated == value:
            break

        value = updated

    return value


def _padded(value: str) -> str:
    """Spaces on both ends so ` weather ` cannot match inside `weatherman`."""
    return f" {value} "


def _with_elapsed(resolution: "Resolution", started_at: float) -> "Resolution":
    """Stamp a finished Resolution with how long deciding it took."""
    return Resolution(
        kind=resolution.kind,
        text=resolution.text,
        normalized=resolution.normalized,
        tool=resolution.tool,
        capability=resolution.capability,
        confidence=resolution.confidence,
        route=resolution.route,
        slots=resolution.slots,
        reason=resolution.reason,
        elapsed_ms=(time.perf_counter() - started_at) * 1000
    )


# ==========================================================
# TOOL DESCRIPTION
# ==========================================================
@dataclass(frozen=True)
class Slot:
    """One named input a capability can pull out of the request.

    `pattern` is searched against the ORIGINAL text, not the normalized form, so
    a location comes back as "Chicago" rather than "chicago".
    """

    name: str
    pattern: str = ""
    default: Any = None
    description: str = ""


@dataclass(frozen=True)
class ToolResult:
    """What a capability hands back.

    `speech` is finished English. Structured values never leave this boundary —
    turning `temperature=81, humidity=55` into "It is currently 81 degrees with
    55% humidity" is the tool's job, because the tool is the only thing that
    knows what those numbers mean. `data` is carried for logging and for a future
    planner; nothing speaks it.
    """

    ok: bool = True
    speech: str = ""
    action: str = ""
    action_payload: Dict[str, Any] = field(default_factory=dict)
    data: Dict[str, Any] = field(default_factory=dict)
    event: str = "announce"
    error: str = ""
    action_ran: bool = False


@dataclass(frozen=True)
class Capability:
    """One thing a tool can do, and every way of asking for it."""

    name: str
    description: str
    handler: Callable[[Dict[str, Any]], ToolResult]
    kind: str = KIND_DATA
    # Exact normalized phrases. The fast path, and nothing else.
    phrases: Tuple[str, ...] = ()
    # Regexes against the normalized request. Strong evidence.
    patterns: Tuple[str, ...] = ()
    # Terms that point at THIS capability once the tool itself is in play.
    cues: Tuple[str, ...] = ()
    slots: Tuple[Slot, ...] = ()
    # Which capability wins when a tool matches but no cue does.
    default: bool = False
    # Tie-break between two capabilities of the same tool at the same score.
    priority: int = 0
    # Declared but deliberately not acted on. Registered so the registry stays an
    # honest description of the tool, skipped by resolve() so behaviour does not
    # change. Music volume is the case this exists for: FRIDAY is under a standing
    # rule never to touch macOS audio levels.
    supported: bool = True
    inputs: Tuple[str, ...] = ()
    outputs: Tuple[str, ...] = ()
    note: str = ""

    # ---- PLANNER METADATA ----------------------------------------------
    # Four fields, each added because the planner cannot be correct without it.
    # Nothing here changes single-capability routing.
    #
    # `destructive` marks an operation that is not safely repeatable or not
    # reversible. A plan containing one is refused outright, so an AI-shaped
    # decomposition can never chain a deletion onto something else.
    destructive: bool = False
    # Dotted paths into ToolResult.data that later steps may read. The last
    # segment is the field name a dependency binds by: "event.location"
    # publishes a field called `location`. Declaring it is what makes a
    # dependency possible; nothing is reachable that a tool did not publish.
    provides: Tuple[str, ...] = ()
    # Seconds this capability is allowed before the planner gives up on it.
    # 0 means "inherit the tool's timeout".
    timeout: float = 0.0
    # Override for the read/action parallelism rule below. Left None almost
    # always: the kind already answers the question.
    safe_parallel: Optional[bool] = None

    @property
    def step_kind(self) -> str:
        """READ observes; ACTION changes something. Opening a surface changes something."""
        return STEP_READ if self.kind == KIND_DATA else STEP_ACTION

    @property
    def parallel_safe(self) -> bool:
        """Whether two of these may be in flight at once. Reads yes, actions no."""
        if self.safe_parallel is not None:
            return bool(self.safe_parallel)

        return self.kind == KIND_DATA


@dataclass(frozen=True)
class Tool:
    """A registered tool.

    `anchors` are the terms that mean "this request is about me". They gate the
    keyword route entirely: a tool whose anchors are absent is never scored, which
    is what stops "Hello" and "Thank you" from being routed anywhere at all.
    """

    name: str
    description: str
    category: str
    anchors: Tuple[str, ...]
    capabilities: Tuple[Capability, ...]
    inputs: Tuple[str, ...] = ()
    outputs: Tuple[str, ...] = ()
    # Default seconds for this tool's capabilities. Local readings answer in
    # milliseconds; anything that leaves the machine needs room to. Declared per
    # tool because the tool is the only thing that knows which it is.
    timeout: float = DEFAULT_STEP_TIMEOUT

    def step_timeout(self, capability: "Capability") -> float:
        """The deadline for one step, most specific declaration first."""
        return float(capability.timeout or self.timeout or DEFAULT_STEP_TIMEOUT)


@dataclass(frozen=True)
class Resolution:
    """The intent layer's answer for one request."""

    kind: str
    text: str
    normalized: str
    tool: Optional[Tool] = None
    capability: Optional[Capability] = None
    confidence: float = 0.0
    route: str = ROUTE_NONE
    slots: Dict[str, Any] = field(default_factory=dict)
    reason: str = ""
    elapsed_ms: float = 0.0

    @property
    def handled(self) -> bool:
        return self.kind in {INTENT_TOOL, INTENT_APPLICATION} and self.capability is not None

    @property
    def label(self) -> str:
        if not self.handled:
            return "conversation"

        return f"{self.tool.name}.{self.capability.name}"


# ==========================================================
# REGISTRY
# ==========================================================
class ToolRegistry:
    """Everything FRIDAY can do, and the routing that follows from it."""

    def __init__(self) -> None:
        self._tools: Dict[str, Tool] = {}
        self._order: List[str] = []
        # normalized phrase -> (tool name, capability name). The fast path.
        self._phrase_index: Dict[str, Tuple[str, str]] = {}
        # (tool name, capability name) -> compiled regexes.
        self._patterns: Dict[Tuple[str, str], List[re.Pattern]] = {}
        self._slot_patterns: Dict[Tuple[str, str, str], re.Pattern] = {}
        self._guards: List[Callable[[str, str], bool]] = []
        self._action_bridge: Optional[Callable[[str, Dict[str, Any]], bool]] = None

    # ------------------------------------------------------
    # REGISTRATION
    # ------------------------------------------------------
    def register(self, tool: Tool) -> Tool:
        """Add a tool. This is the only step needed to make it routable."""
        if not isinstance(tool, Tool):
            raise TypeError("register() expects a Tool")

        if not tool.name:
            raise ValueError("a tool needs a name")

        if tool.name in self._tools:
            raise ValueError(f"tool already registered: {tool.name}")

        if not tool.capabilities:
            raise ValueError(f"tool {tool.name} declares no capabilities")

        defaults = [capability for capability in tool.capabilities if capability.default]

        if len(defaults) > 1:
            raise ValueError(f"tool {tool.name} declares {len(defaults)} default capabilities")

        for capability in tool.capabilities:
            key = (tool.name, capability.name)

            if not callable(capability.handler):
                raise TypeError(f"{tool.name}.{capability.name} has no handler")

            for phrase in capability.phrases:
                normalized_phrase = normalize(phrase)

                if not normalized_phrase:
                    continue

                # First registration wins. A phrase that two tools both claim is a
                # declaration bug, not something to resolve silently at runtime.
                existing = self._phrase_index.get(normalized_phrase)

                if existing and existing != key:
                    raise ValueError(
                        f"phrase '{normalized_phrase}' claimed by both "
                        f"{existing[0]}.{existing[1]} and {tool.name}.{capability.name}"
                    )

                self._phrase_index[normalized_phrase] = key

            self._patterns[key] = [
                re.compile(pattern) for pattern in capability.patterns
            ]

            for slot in capability.slots:
                if slot.pattern:
                    self._slot_patterns[(tool.name, capability.name, slot.name)] = re.compile(
                        slot.pattern,
                        re.IGNORECASE
                    )

        self._tools[tool.name] = tool
        self._order.append(tool.name)
        return tool

    def add_conversation_guard(self, guard: Callable[[str, str], bool]) -> None:
        """Veto routing for a class of request before any tool is scored.

        Called with (normalized, original). Returning True sends the turn to
        conversation. This is how "write me an email about the weather" stays a
        drafting request instead of becoming a forecast.
        """
        self._guards.append(guard)

    def set_action_bridge(self, bridge: Callable[[str, Dict[str, Any]], bool]) -> None:
        """Install the one function that performs interface actions.

        main.py owns handle_direct_action and its allowlist. Rather than import
        main.py from here — which would be a cycle, and would duplicate the
        allowlist besides — tools return an action key and the bridge runs it. One
        vocabulary, one dispatcher, no drift.
        """
        self._action_bridge = bridge

    # ------------------------------------------------------
    # INTROSPECTION
    # ------------------------------------------------------
    def tools(self) -> List[Tool]:
        return [self._tools[name] for name in self._order]

    def get(self, name: str) -> Optional[Tool]:
        return self._tools.get(str(name or "").strip().lower())

    def capability(self, tool_name: str, capability_name: str) -> Optional[Capability]:
        tool = self.get(tool_name)

        if not tool:
            return None

        return next(
            (item for item in tool.capabilities if item.name == capability_name),
            None
        )

    def manifest(self) -> List[Dict[str, Any]]:
        """The registry as plain data.

        This is what a planner, a voice manifest or a settings screen would read.
        It is generated from the same declarations routing uses, so a tool cannot
        be advertised and unroutable, or routable and undocumented.
        """
        return [
            {
                "name": tool.name,
                "description": tool.description,
                "category": tool.category,
                "inputs": list(tool.inputs),
                "outputs": list(tool.outputs),
                "capabilities": [
                    {
                        "name": capability.name,
                        "description": capability.description,
                        "kind": capability.kind,
                        "supported": capability.supported,
                        "inputs": list(capability.inputs),
                        "outputs": list(capability.outputs),
                        "note": capability.note,
                        # Planner metadata, from the same declaration routing
                        # uses. A plan proposer reads this and nothing else.
                        "step_kind": capability.step_kind,
                        "destructive": capability.destructive,
                        "safe_parallel": capability.parallel_safe,
                        "provides": list(capability.provides),
                        "timeout": tool.step_timeout(capability),
                        "arguments": [
                            {"name": slot.name, "description": slot.description}
                            for slot in capability.slots
                        ],
                    }
                    for capability in tool.capabilities
                ],
            }
            for tool in self.tools()
        ]

    # ------------------------------------------------------
    # RESOLUTION
    # ------------------------------------------------------
    def resolve(self, text: str) -> Resolution:
        """Decide what a request is: conversation, a tool, or an application."""
        started_at = time.perf_counter()
        original = str(text or "")
        normalized = normalize(original)

        def finish(resolution: Resolution) -> Resolution:
            return _with_elapsed(resolution, started_at)

        def conversation(reason: str, route: str = ROUTE_NONE) -> Resolution:
            return finish(Resolution(
                kind=INTENT_CONVERSATION,
                text=original,
                normalized=normalized,
                route=route,
                reason=reason
            ))

        if not normalized:
            return conversation("empty request")

        for guard in self._guards:
            try:
                if guard(normalized, original):
                    return conversation("conversation guard", ROUTE_GUARD)
            except Exception:
                # A broken guard must never take routing down with it.
                continue

        # 1. FAST PATH — one dict lookup, and the route obvious commands take.
        fast = self._phrase_index.get(normalized)

        if fast:
            tool = self._tools[fast[0]]
            capability = self.capability(fast[0], fast[1])

            if capability and capability.supported:
                return finish(self._match(
                    original,
                    normalized,
                    tool,
                    capability,
                    SCORE_FAST_PATH,
                    ROUTE_FAST_PATH
                ))

        # 2 and 3. PATTERN and KEYWORD, scored together so the best evidence wins
        #          regardless of which route produced it.
        best = self._best_match(normalized, self.tools())

        if not best:
            return conversation("no registered tool matched")

        score, _priority, _cues, tool, capability, route = best
        return finish(self._match(original, normalized, tool, capability, score, route))

    def _best_match(
        self,
        normalized: str,
        candidates: List[Tool]
    ) -> Optional[Tuple[float, int, int, Tool, Capability, str]]:
        """Score PATTERN and KEYWORD evidence across a set of tools.

        Split out of resolve() so the planner can ask the same question of ONE
        tool — "if this request belonged to the calendar, which capability would
        it be?" — without a second, drifting copy of the scoring ladder.
        """
        best: Optional[Tuple[float, int, int, Tool, Capability, str]] = None
        padded = _padded(normalized)

        for tool in candidates:
            anchor_hits = sum(1 for anchor in tool.anchors if _padded(anchor) in padded)

            for capability in tool.capabilities:
                if not capability.supported:
                    continue

                score = 0.0
                route = ROUTE_NONE
                cue_hits = 0

                if any(pattern.search(normalized) for pattern in self._patterns[(tool.name, capability.name)]):
                    score = SCORE_PATTERN
                    route = ROUTE_PATTERN

                if anchor_hits:
                    cue_hits = sum(1 for cue in capability.cues if _padded(cue) in padded)

                    if cue_hits and score < SCORE_ANCHOR_AND_CUE:
                        score = SCORE_ANCHOR_AND_CUE
                        route = ROUTE_KEYWORD
                    elif capability.default and score < SCORE_ANCHOR_DEFAULT:
                        score = SCORE_ANCHOR_DEFAULT
                        route = ROUTE_KEYWORD

                if score < MIN_CONFIDENCE:
                    continue

                candidate = (score, capability.priority, cue_hits, tool, capability, route)

                if best is None or candidate[:3] > best[:3]:
                    best = candidate

        return best

    def guarded(self, text: str) -> bool:
        """Whether the conversation guards veto this request outright.

        The planner asks before decomposing anything: a drafting request that
        happens to name three tools is still a drafting request, and splitting it
        into clauses would route around the guard that exists to stop exactly
        that.
        """
        normalized = normalize(text)

        if not normalized:
            return True

        for guard in self._guards:
            try:
                if guard(normalized, str(text or "")):
                    return True
            except Exception:
                continue

        return False

    def resolve_for_tool(self, text: str, tool_name: str) -> Optional[Resolution]:
        """Which capability of ONE named tool best fits this request, if any.

        Used when the planner already knows the tool — because a dependency needs
        a field only that tool publishes — and needs the capability. Guards are
        deliberately not applied: the caller has already decided this text is a
        tool request, and is asking a narrower question about it.
        """
        started_at = time.perf_counter()
        tool = self.get(tool_name)

        if not tool:
            return None

        original = str(text or "")
        normalized = normalize(original)

        if not normalized:
            return None

        fast = self._phrase_index.get(normalized)

        if fast and fast[0] == tool.name:
            capability = self.capability(fast[0], fast[1])

            if capability and capability.supported:
                matched = self._match(original, normalized, tool, capability, SCORE_FAST_PATH, ROUTE_FAST_PATH)
                return _with_elapsed(matched, started_at)

        best = self._best_match(normalized, [tool])

        if not best:
            return None

        score, _priority, _cues, _tool, capability, route = best
        return _with_elapsed(
            self._match(original, normalized, tool, capability, score, route),
            started_at
        )

    def _match(
        self,
        original: str,
        normalized: str,
        tool: Tool,
        capability: Capability,
        confidence: float,
        route: str
    ) -> Resolution:
        return Resolution(
            kind=INTENT_APPLICATION if capability.kind == KIND_OPEN else INTENT_TOOL,
            text=original,
            normalized=normalized,
            tool=tool,
            capability=capability,
            confidence=confidence,
            route=route,
            slots=self._extract_slots(tool, capability, original, normalized),
            reason=f"{tool.name}.{capability.name} via {route}"
        )

    def _extract_slots(
        self,
        tool: Tool,
        capability: Capability,
        original: str,
        normalized: str
    ) -> Dict[str, Any]:
        slots: Dict[str, Any] = {"_text": original, "_normalized": normalized}

        for slot in capability.slots:
            value = None
            pattern = self._slot_patterns.get((tool.name, capability.name, slot.name))

            if pattern:
                match = pattern.search(original)

                if match:
                    # A named `value` group is the convention; a slot pattern
                    # without one falls back to its last group, then to the whole
                    # match, so a simple pattern still works.
                    captured = match.groupdict().get("value")

                    if captured is None:
                        captured = match.group(match.lastindex) if match.lastindex else match.group(0)

                    value = str(captured or "").strip(" \t\n\r,.;:!?")

            if not value:
                value = slot.default() if callable(slot.default) else slot.default

            slots[slot.name] = value

        return slots

    # ------------------------------------------------------
    # EXECUTION
    # ------------------------------------------------------
    def execute(self, resolution: Resolution) -> ToolResult:
        """Run a resolved capability and return finished, speakable English.

        Never raises. A tool that fails hands the turn back with ok=False and no
        speech, and the caller falls through to Gemini exactly as it would for an
        unmatched request — a broken weather API must not become a dead turn.
        """
        if not resolution or not resolution.handled:
            return ToolResult(ok=False, error="nothing to execute")

        capability = resolution.capability

        if not capability.supported:
            return ToolResult(ok=False, error=f"{capability.name} is declared but not supported")

        try:
            result = capability.handler(dict(resolution.slots))
        except Exception as error:  # noqa: BLE001 - a tool failure is never fatal
            return ToolResult(ok=False, error=f"{resolution.label} failed: {error}")

        if not isinstance(result, ToolResult):
            return ToolResult(ok=False, error=f"{resolution.label} returned {type(result).__name__}")

        if result.action and self._action_bridge:
            payload = dict(result.action_payload)
            payload.setdefault("action", result.action)
            payload.setdefault("source", "tool_intelligence")

            try:
                ran = bool(self._action_bridge(result.action, payload))
            except Exception as error:  # noqa: BLE001
                return ToolResult(
                    ok=False,
                    speech=result.speech,
                    data=result.data,
                    event=result.event,
                    error=f"{resolution.label} action failed: {error}"
                )

            return ToolResult(
                ok=result.ok and ran,
                speech=result.speech,
                action=result.action,
                action_payload=result.action_payload,
                data=result.data,
                event=result.event,
                error="" if ran else f"{result.action} was not available",
                action_ran=ran
            )

        return result

    # ------------------------------------------------------
    # DIAGNOSTICS
    # ------------------------------------------------------
    def explain(self, text: str) -> Dict[str, Any]:
        """Why a request routed the way it did. For tests and for `perf` output."""
        resolution = self.resolve(text)
        return {
            "text": text,
            "normalized": resolution.normalized,
            "kind": resolution.kind,
            "match": resolution.label,
            "route": resolution.route,
            "confidence": round(resolution.confidence, 2),
            "slots": {
                key: value
                for key, value in resolution.slots.items()
                if not key.startswith("_")
            },
            "elapsed_ms": round(resolution.elapsed_ms, 3),
        }
