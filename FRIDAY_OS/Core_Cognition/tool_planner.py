"""FRIDAY Multi-Step Planning — more than one capability for one request.

WHY THIS EXISTS
---------------
Tool Intelligence answers the question "which ONE thing is this request about?"
and answers it very well, in microseconds, without a model. But a request is not
always about one thing. "What do I have tomorrow and will it rain?" is two
questions sharing a sentence, and `resolve()` — which returns a single
Resolution — could only ever pick the winner and drop the rest. The calendar
answer came back and the weather question silently vanished.

This module adds the layer above that, and nothing else:

    Tool Registry        what FRIDAY CAN do
    Tool Intelligence    which ONE capability fits a simple request
    Planner (here)       which capabilities are needed, and in what order
    Executor (here)      runs the validated steps and collects the results

WHAT IT IS NOT
--------------
Not an agent. There is no think/act loop, no replanning, no autonomy. A plan is
built once, validated once, executed once, and answered once. It is capped at
five steps. A tool cannot cause another plan. If the planner cannot see a clean
decomposition it returns None and the turn continues exactly as it did before,
which is the behaviour every request that is not multi-step gets.

THE ROUTE
---------
    fast path / existing deterministic handlers   unchanged, still first
    single obvious capability                     unchanged, Tool Intelligence
    two or more capabilities                      here
    nothing registered fits                       Gemini, unchanged

The cost of the new layer on a request that is not multi-step is one regex
search. `plan()` returns before touching the registry unless the request
actually looks compositional, because "Open music" getting slower is not an
acceptable price for FRIDAY getting smarter.

WHAT THE PLANNER MAY NOT DO
---------------------------
Every step names a registered tool and a registered capability, and is executed
through the same ToolRegistry.execute() and the same action bridge that a single
capability goes through. There is no shell, no generated code, no invented
action, and no second copy of the action allowlist. A step the registry does not
recognise is dropped at validation, not at execution.
"""

from __future__ import annotations

import re
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from Core_Cognition.tool_registry import (
    KIND_OPEN,
    ROUTE_FAST_PATH,
    ROUTE_KEYWORD,
    SCORE_ANCHOR_DEFAULT,
    STEP_READ,
    Capability,
    Resolution,
    Tool,
    ToolRegistry,
    ToolResult,
    normalize,
)

# ==========================================================
# LIMITS
# ==========================================================
# Conservative on purpose. A plan that grew without limit would be an agent, and
# FRIDAY MK1 is deliberately not one.
MAX_STEPS = 5
HARD_MAX_STEPS = 8
# How many reads may be in flight at once. Bounded so a wide plan cannot spawn a
# thread per tool.
MAX_PARALLEL = 4
# The most wall-clock time a whole plan may spend, however its steps are
# arranged. One slow API is a partial answer; five of them must not become a
# minute of silence.
PLAN_BUDGET = 12.0

# Step status.
STATUS_PENDING = "pending"
STATUS_OK = "ok"
STATUS_FAILED = "failed"
STATUS_TIMEOUT = "timeout"
STATUS_SKIPPED = "skipped"

# Plan status.
PLAN_READY = "ready"
PLAN_COMPLETE = "complete"
PLAN_PARTIAL = "partial"
PLAN_FAILED = "failed"

# How the steps were run, for logging.
MODE_PARALLEL = "parallel"
MODE_SEQUENTIAL = "sequential"
MODE_MIXED = "mixed"

# How the plan was built.
ORIGIN_CLAUSES = "clauses"
ORIGIN_REFERENCE = "reference"
ORIGIN_MODEL = "model"


# ==========================================================
# LANGUAGE
# ==========================================================
# The cheap gate. If none of these appear the request is not a list of things and
# `plan()` returns immediately, which is what keeps "open music" as fast as it
# was before this module existed.
_COMPOSITE_PROBE = re.compile(r"[,;:&]|\b(?:and|then|also|plus)\b", re.IGNORECASE)

# The second cheap gate, for the other shape a plan can have: one clause whose
# answer hangs off another tool's answer. "the weather AT THE location OF MY next
# event". Deliberately narrow — a preposition immediately followed by a
# determiner — so that the work of checking for a real dependency is only ever
# paid by a request that is at least shaped like one.
_REFERENCE_PROBE = re.compile(r"\b(?:of|at|near|where)\s+(?:my|the|that)\b", re.IGNORECASE)

# Where one clause ends and the next begins.
_CLAUSE_SPLIT = re.compile(
    r"\s*(?:,|;|&|\band\b|\bthen\b|\balso\b|\bplus\b|\bafter that\b|\bas well as\b)\s*",
    re.IGNORECASE
)

# "Give me a briefing for today: calendar, tasks and weather." Everything before
# the colon is a lead-in; the enumeration after it is the actual request. Only
# applied when the tail really does enumerate, so "note that: buy milk" is safe.
_PREAMBLE = re.compile(r"^[^:]{0,80}:\s*(?P<tail>.+)$", re.DOTALL)

# "...and open it." A clause that is a bare open verb plus a pronoun refers to
# whatever the previous clause was about.
_PRONOUN_OPEN = re.compile(
    r"^(?:open|show|pull up|bring up|launch|display)\s+(?:it|that|them|those|this)$",
    re.IGNORECASE
)

# A request whose second half hangs off the first: "the weather AT the location
# OF my next event". Without one of these the two tools named in a sentence are
# two separate topics, not a dependency.
_LINK_WORDS = (" of ", " at ", " for ", " where ", " near ", " in ")

# `$steps.<step id>.<dotted path>`. The ONLY reference syntax. Parsed with this
# regex and looked up by key; never evaluated, never formatted into anything that
# gets executed.
_REFERENCE = re.compile(r"^\$steps\.(?P<step>[a-z0-9_]+)\.(?P<path>[a-z0-9_]+(?:\.[a-z0-9_]+)*)$", re.IGNORECASE)


# ==========================================================
# OBSERVABILITY
# ==========================================================
_LOG: Optional[Callable[[str], None]] = None


def set_logger(logger: Optional[Callable[[str], None]]) -> None:
    """Install the sink for [PLAN] lines.

    main.py passes one that respects the existing performance-log setting, so
    planning is silent unless Jon has asked to see timings. The planner does not
    import main.py to find that out — that would be a cycle, and the planner has
    no business knowing what a setting is.
    """
    global _LOG

    _LOG = logger


def _log(message: str) -> None:
    if _LOG is None:
        return

    try:
        _LOG(f"[PLAN] {message}")
    except Exception:
        pass


# ==========================================================
# PLAN
# ==========================================================
@dataclass
class Step:
    """One capability call inside a plan.

    Mutable, unlike everything in the registry: a step accumulates its own
    outcome as the plan runs. Nothing outside the executor writes to it.
    """

    id: str
    tool: str
    capability: str
    kind: str = STEP_READ
    arguments: Dict[str, Any] = field(default_factory=dict)
    depends_on: Tuple[str, ...] = ()
    source_text: str = ""
    timeout: float = 4.0
    status: str = STATUS_PENDING
    result: Optional[ToolResult] = None
    error: str = ""
    # Measured from the moment the step was handed to the pool, not from the
    # moment the executor got round to waiting on it — otherwise a read that
    # finished first while its neighbour was still running reports 0 ms.
    started_at: float = 0.0
    elapsed_ms: float = 0.0

    @property
    def label(self) -> str:
        return f"{self.tool}.{self.capability}"

    @property
    def succeeded(self) -> bool:
        return self.status == STATUS_OK


@dataclass
class Plan:
    """A bounded, validated set of steps that answers one request."""

    goal: str
    steps: List[Step] = field(default_factory=list)
    execution_mode: str = MODE_SEQUENTIAL
    requires_confirmation: bool = False
    status: str = PLAN_READY
    origin: str = ORIGIN_CLAUSES
    reason: str = ""
    truncated: bool = False
    elapsed_ms: float = 0.0

    @property
    def completed(self) -> List[Step]:
        return [step for step in self.steps if step.succeeded]

    @property
    def unfinished(self) -> List[Step]:
        return [step for step in self.steps if not step.succeeded]

    @property
    def usable(self) -> bool:
        """Whether this plan produced enough to answer with.

        One successful step is enough: a partial answer with an honest gap beats
        handing the whole turn back and losing what did work.
        """
        return any(step.succeeded and (step.result is not None) for step in self.steps)

    def step(self, step_id: str) -> Optional[Step]:
        return next((item for item in self.steps if item.id == step_id), None)

    def describe(self) -> Dict[str, Any]:
        """The plan as plain data, for tests and diagnostics."""
        return {
            "goal": self.goal,
            "origin": self.origin,
            "execution_mode": self.execution_mode,
            "requires_confirmation": self.requires_confirmation,
            "status": self.status,
            "truncated": self.truncated,
            "steps": [
                {
                    "id": step.id,
                    "tool": step.tool,
                    "capability": step.capability,
                    "kind": step.kind,
                    "arguments": {
                        key: value
                        for key, value in step.arguments.items()
                        if not key.startswith("_")
                    },
                    "depends_on": list(step.depends_on),
                    "status": step.status,
                }
                for step in self.steps
            ],
        }


# ==========================================================
# PLANNING
# ==========================================================
def plan(
    text: str,
    registry: Optional[ToolRegistry] = None,
    proposer: Optional[Callable[[str, List[Dict[str, Any]]], Any]] = None
) -> Optional[Plan]:
    """Build a plan for a request, or return None if this is not a plan.

    None is the normal answer and is not a failure. It means the request is a
    single capability, a conversation, or something the planner cannot decompose
    honestly — in every one of those cases the caller carries on down the path it
    already had.
    """
    started_at = time.perf_counter()
    original = str(text or "").strip()

    if not original:
        return None

    registry = registry or _default_registry()

    # 1. THE CHEAP GATE. Two regexes, one per shape a plan can have. Everything
    #    that is neither a list of things nor a reference to another tool's
    #    answer leaves here, which is nearly every request FRIDAY gets.
    composite = bool(_COMPOSITE_PROBE.search(original))
    referential = bool(_REFERENCE_PROBE.search(original))

    if not composite and not referential:
        return None

    # 2. GUARDS. A drafting request that happens to name three tools is still a
    #    drafting request; splitting it into clauses would route around the very
    #    guard that exists to stop that.
    if registry.guarded(original):
        return None

    # 3. THE EXACT PHRASE STILL WINS. If the whole sentence is a command FRIDAY
    #    already knows word for word, it is not a plan, whatever punctuation it
    #    contains.
    whole = registry.resolve(original)

    if whole.route == ROUTE_FAST_PATH:
        return None

    origin = ORIGIN_CLAUSES
    steps = _steps_from_clauses(original, registry) if composite else []

    if len(steps) < 2 and referential:
        origin = ORIGIN_REFERENCE
        steps = _steps_from_reference(original, whole, registry)

    if len(steps) < 2:
        # 4. THE MODEL, IF ONE IS INSTALLED. Asked ONCE, only for a request that
        #    is shaped like a plan and that the local decomposition could not
        #    read, and never for a second opinion on one it could. Whatever comes
        #    back goes through the same validation as everything else.
        return _propose(original, proposer, registry) if proposer else None

    built = _finalize(original, steps, origin, registry)
    built.elapsed_ms = (time.perf_counter() - started_at) * 1000

    if built.reason:
        _log(f"declined: {built.reason}")
        return None

    _log(f"goal {original!r}")
    _log(f"{len(built.steps)} steps, {built.execution_mode}")

    for step in built.steps:
        depends = f" <- {', '.join(step.depends_on)}" if step.depends_on else ""
        _log(f"  {step.label} [{step.kind}]{depends}")

    return built


def _default_registry() -> ToolRegistry:
    from Core_Cognition import tools

    return tools.REGISTRY


def _propose(goal: str, proposer, registry: ToolRegistry) -> Optional[Plan]:
    """Ask an installed proposer for a plan, and refuse it unless it validates.

    Exactly one attempt. A proposer that returns nothing usable ends the turn's
    planning — there is no repair round and no second ask, because a planner that
    can ask again is a planner that can ask forever.
    """
    try:
        proposal = proposer(goal, planning_manifest(registry))
    except Exception as error:  # noqa: BLE001 - a proposer failing is not a turn failing
        _log(f"proposer failed: {type(error).__name__}")
        return None

    built = plan_from_proposal(goal, proposal, registry)

    if built:
        _log(f"proposed plan accepted: {len(built.steps)} steps")

    return built


# ----------------------------------------------------------
# DECOMPOSITION 1: clauses
# ----------------------------------------------------------
def _steps_from_clauses(text: str, registry: ToolRegistry) -> List[Step]:
    """Split a request on its coordinators and resolve each part on its own.

    This is the common shape by a wide margin: "X and Y", "X, Y and Z". Each
    clause goes through the SAME resolve() a whole request would, so a clause
    reaches the tool it would have reached if Jon had said it by itself. No
    combination is written down anywhere, and a tool added tomorrow takes part
    without a line changing here.
    """
    body = text
    preamble = _PREAMBLE.match(text)

    if preamble:
        # A colon lead-in only counts as one if what follows genuinely
        # enumerates. Otherwise the whole sentence is kept.
        tail = preamble.group("tail")

        if len(_split_clauses(tail)) >= 2:
            body = tail

    clauses = _split_clauses(body)

    if len(clauses) < 2:
        return []

    steps: List[Step] = []
    seen: set = set()
    opening = False

    for clause in clauses:
        resolution = registry.resolve(clause)

        if not resolution.handled:
            carried = (
                _pronoun_open_step(clause, steps, registry)
                or (_bare_open_step(clause, steps, registry) if opening else None)
            )

            if carried:
                steps.append(carried)

            continue

        resolution = _carry_verb(resolution, opening, registry)
        opening = resolution.capability.kind == KIND_OPEN
        key = (resolution.tool.name, resolution.capability.name)

        if key in seen:
            continue

        seen.add(key)
        steps.append(_step_from_resolution(resolution, clause))

    return steps


def _carry_verb(resolution: Resolution, opening: bool, registry: ToolRegistry) -> Resolution:
    """"Open music and calendar" — the verb from the first clause governs the rest.

    A bare tool name in a later clause carries no verb of its own, so resolve()
    can only fall back to that tool's default capability: "calendar" alone means
    today's agenda. After an OPEN, it means open the calendar.

    Applies ONLY to a clause that matched on the tool's anchor and nothing else —
    no pattern, no cue. A clause that said what it wanted ("and what is overdue")
    keeps what it asked for.
    """
    if not opening or resolution.capability.kind == KIND_OPEN:
        return resolution

    if resolution.route != ROUTE_KEYWORD or resolution.confidence > SCORE_ANCHOR_DEFAULT:
        return resolution

    opener = next(
        (
            capability for capability in resolution.tool.capabilities
            if capability.supported and capability.kind == KIND_OPEN
        ),
        None
    )

    if not opener:
        return resolution

    return Resolution(
        kind=resolution.kind,
        text=resolution.text,
        normalized=resolution.normalized,
        tool=resolution.tool,
        capability=opener,
        confidence=resolution.confidence,
        route=resolution.route,
        slots=resolution.slots,
        reason=f"{resolution.tool.name}.{opener.name} via carried verb"
    )


def _split_clauses(text: str) -> List[str]:
    # "a, b, and c" splits into three, not four with an empty middle.
    value = re.sub(r"\s*,\s*and\s+", ", ", str(text or ""), flags=re.IGNORECASE)
    return [part.strip(" \t\n\r.?!") for part in _CLAUSE_SPLIT.split(value) if part.strip(" \t\n\r.?!")]


_ARTICLE = re.compile(r"^(?:the|my|a|an)\s+", re.IGNORECASE)


def _bare_open_step(clause: str, steps: List[Step], registry: ToolRegistry) -> Optional[Step]:
    """"Open music, weather, tasks and notes" — a bare tool name after an open verb.

    Some tools deliberately declare no default capability, because the verbs
    around their name mean genuinely different things: Notes is the case that
    exists for. That is right for a lone request and wrong here, where the verb
    was already said out loud one clause ago. Matched against the tool's declared
    anchors, so it can only ever name a real tool, and only ever open it.
    """
    name = _ARTICLE.sub("", normalize(clause)).strip()

    if not name:
        return None

    for tool in registry.tools():
        if name not in tool.anchors:
            continue

        opener = next(
            (
                capability for capability in tool.capabilities
                if capability.supported and capability.kind == KIND_OPEN
            ),
            None
        )

        if not opener:
            continue

        return Step(
            id=_step_id(tool.name, opener.name, steps),
            tool=tool.name,
            capability=opener.name,
            kind=opener.step_kind,
            arguments={},
            source_text=clause,
            timeout=tool.step_timeout(opener)
        )

    return None


def _pronoun_open_step(clause: str, steps: List[Step], registry: ToolRegistry) -> Optional[Step]:
    """"...and open it." — the pronoun means the tool the last clause was about.

    Resolved against that tool's own declared OPEN capability rather than
    guessed, so "open it" after a calendar clause can only ever open the
    calendar, and after a clause about a tool with nothing to open it does
    nothing at all.
    """
    if not steps or not _PRONOUN_OPEN.match(str(clause or "").strip()):
        return None

    tool = registry.get(steps[-1].tool)

    if not tool:
        return None

    opener = next(
        (
            capability for capability in tool.capabilities
            if capability.supported and capability.kind == KIND_OPEN
        ),
        None
    )

    if not opener:
        return None

    return Step(
        id=_step_id(tool.name, opener.name, steps),
        tool=tool.name,
        capability=opener.name,
        kind=opener.step_kind,
        arguments={},
        source_text=clause,
        timeout=tool.step_timeout(opener)
    )


# ----------------------------------------------------------
# DECOMPOSITION 2: a reference to another tool's output
# ----------------------------------------------------------
def _steps_from_reference(
    text: str,
    whole: Resolution,
    registry: ToolRegistry
) -> List[Step]:
    """"What is the weather at the location of my next calendar event?"

    One clause, two tools. The rule is entirely declaration-driven and contains
    no sentence: the resolved capability has an input it could not fill from the
    request, and some OTHER tool named in the same request publishes a field by
    that name. That is a dependency, and it is the only shape that produces one.

    If nothing publishes the missing field, no plan is built and the request goes
    down the ordinary single-capability path — which is the honest outcome, not a
    degraded one.
    """
    if not whole.handled:
        return []

    padded = f" {normalize(text)} "

    if not any(link in padded for link in _LINK_WORDS):
        return []

    missing = [
        slot.name for slot in whole.capability.slots
        if not whole.slots.get(slot.name)
    ]

    if not missing:
        return []

    for field_name in missing:
        provider = _provider_for(field_name, whole.tool, text, registry)

        if not provider:
            continue

        source_tool, source_capability, path = provider
        source = Step(
            id=_step_id(source_tool.name, source_capability.name, []),
            tool=source_tool.name,
            capability=source_capability.name,
            kind=source_capability.step_kind,
            arguments={},
            source_text=text,
            timeout=source_tool.step_timeout(source_capability)
        )
        dependent = _step_from_resolution(whole, text, [source])
        dependent.arguments[field_name] = f"$steps.{source.id}.{path}"
        dependent.depends_on = (source.id,)

        return [source, dependent]

    return []


def _provider_for(
    field_name: str,
    exclude: Tool,
    text: str,
    registry: ToolRegistry
) -> Optional[Tuple[Tool, Capability, str]]:
    """Which other tool named in this request publishes `field_name`."""
    padded = f" {normalize(text)} "

    for tool in registry.tools():
        if tool.name == exclude.name:
            continue

        if not any(f" {anchor} " in padded for anchor in tool.anchors):
            continue

        producers = [
            (capability, path)
            for capability in tool.capabilities
            if capability.supported and capability.step_kind == STEP_READ
            for path in capability.provides
            if path.rsplit(".", 1)[-1] == field_name
        ]

        if not producers:
            continue

        # More than one capability of the tool can publish the field. Ask the
        # same scorer the intent layer uses which one this request is about, and
        # fall back to the highest-priority producer if it picks one that does
        # not publish the field at all.
        preferred = registry.resolve_for_tool(text, tool.name)

        if preferred and preferred.handled:
            for capability, path in producers:
                if capability.name == preferred.capability.name:
                    return tool, capability, path

        capability, path = min(producers, key=lambda item: item[0].priority)
        return tool, capability, path

    return None


# ----------------------------------------------------------
# STEP CONSTRUCTION
# ----------------------------------------------------------
def _step_from_resolution(
    resolution: Resolution,
    source_text: str,
    existing: Optional[List[Step]] = None
) -> Step:
    arguments = {
        key: value
        for key, value in resolution.slots.items()
        if not key.startswith("_") and value not in (None, "")
    }

    return Step(
        id=_step_id(resolution.tool.name, resolution.capability.name, existing or []),
        tool=resolution.tool.name,
        capability=resolution.capability.name,
        kind=resolution.capability.step_kind,
        arguments=arguments,
        source_text=source_text,
        timeout=resolution.tool.step_timeout(resolution.capability)
    )


def _step_id(tool: str, capability: str, existing: Sequence[Step]) -> str:
    base = f"{tool}_{capability}".lower()
    taken = {step.id for step in existing}

    if base not in taken:
        return base

    index = 2

    while f"{base}_{index}" in taken:
        index += 1

    return f"{base}_{index}"


# ==========================================================
# VALIDATION
# ==========================================================
# Every plan passes through here, whoever proposed it. The deterministic
# decomposition cannot really produce an invalid step — it builds them from
# Resolutions — but a model can, and there must be exactly one place that
# decides whether a plan is allowed to run.
def _finalize(goal: str, steps: List[Step], origin: str, registry: ToolRegistry) -> Plan:
    built = Plan(goal=goal, steps=[], origin=origin)
    validated: List[Step] = []

    for step in steps:
        capability = registry.capability(step.tool, step.capability)
        tool = registry.get(step.tool)

        if not tool or not capability:
            _log(f"rejected unknown step {step.tool}.{step.capability}")
            continue

        if not capability.supported:
            _log(f"rejected unsupported step {step.label}")
            continue

        if capability.destructive:
            # A destructive capability still works exactly as it always has on
            # the single-capability path, where Jon asked for that one thing and
            # nothing else. What it may not do is arrive as one item in a list.
            built.requires_confirmation = True
            built.reason = f"{step.label} is destructive"
            return built

        # Arguments are restricted to the slots the capability declares, so a
        # proposed plan cannot smuggle a key a handler was never written for.
        declared = {slot.name for slot in capability.slots}
        step.arguments = {
            key: value for key, value in step.arguments.items()
            if key in declared
        }
        step.kind = capability.step_kind
        step.timeout = tool.step_timeout(capability)
        validated.append(step)

    if len(validated) > HARD_MAX_STEPS:
        validated = validated[:HARD_MAX_STEPS]
        built.truncated = True

    if len(validated) > MAX_STEPS:
        validated = validated[:MAX_STEPS]
        built.truncated = True

    known = {step.id for step in validated}

    for step in validated:
        # A dependency on a step that did not survive validation is dropped
        # rather than left dangling; the reference itself then fails to resolve
        # and the step is skipped honestly at execution.
        step.depends_on = tuple(item for item in step.depends_on if item in known and item != step.id)

    if _has_cycle(validated):
        built.reason = "dependency cycle"
        return built

    if len(validated) < 2:
        built.reason = "fewer than two valid steps"
        return built

    built.steps = validated
    built.execution_mode = _execution_mode(validated, registry)

    if built.truncated:
        _log(f"truncated to {len(validated)} steps")

    return built


def _has_cycle(steps: Sequence[Step]) -> bool:
    remaining = {step.id: set(step.depends_on) for step in steps}

    while remaining:
        ready = [step_id for step_id, deps in remaining.items() if not deps]

        if not ready:
            return True

        for step_id in ready:
            remaining.pop(step_id)

        for deps in remaining.values():
            deps.difference_update(ready)

    return False


def _execution_mode(steps: Sequence[Step], registry: ToolRegistry) -> str:
    parallel = sum(1 for step in steps if _parallel_safe(step, registry))

    if parallel == len(steps) and not any(step.depends_on for step in steps):
        return MODE_PARALLEL

    if parallel == 0:
        return MODE_SEQUENTIAL

    return MODE_MIXED


def _parallel_safe(step: Step, registry: ToolRegistry) -> bool:
    capability = registry.capability(step.tool, step.capability)
    return bool(capability and capability.parallel_safe)


# ==========================================================
# MODEL-PROPOSED PLANS
# ==========================================================
def plan_from_proposal(
    goal: str,
    proposal: Any,
    registry: Optional[ToolRegistry] = None
) -> Optional[Plan]:
    """Turn a model's structured proposal into a plan, or refuse it.

    The model proposes; the planner decides. Every field is read defensively,
    every tool and capability is checked against the registry, arguments are
    narrowed to declared slots, references must match the reference grammar
    exactly, and the whole thing is capped. Nothing that arrives here is ever
    executed as text.

    Left unwired by default. The deterministic decomposition above covers the
    compositional requests FRIDAY actually gets, and does it without a round trip
    — but when a proposer is worth its latency, this is the door it comes
    through, and it is the same validation the local planner uses.
    """
    registry = registry or _default_registry()
    raw_steps = proposal.get("steps") if isinstance(proposal, dict) else proposal

    if not isinstance(raw_steps, (list, tuple)) or not raw_steps:
        return None

    steps: List[Step] = []

    for entry in list(raw_steps)[:HARD_MAX_STEPS]:
        if not isinstance(entry, dict):
            continue

        tool = str(entry.get("tool") or "").strip().lower()
        capability = str(entry.get("capability") or "").strip().lower()

        if not tool or not capability:
            continue

        arguments = entry.get("arguments")
        arguments = dict(arguments) if isinstance(arguments, dict) else {}
        clean: Dict[str, Any] = {}

        for key, value in arguments.items():
            if not isinstance(key, str):
                continue

            if isinstance(value, str) and value.startswith("$") and not _REFERENCE.match(value):
                # Looks like a reference and is not one. Dropping it is the only
                # safe reading: passing it through would hand a handler the
                # literal string "$steps.whatever".
                continue

            if isinstance(value, (str, int, float, bool)) or value is None:
                clean[key] = value

        depends = entry.get("depends_on") or ()
        depends = tuple(
            str(item).strip().lower()
            for item in (depends if isinstance(depends, (list, tuple)) else [depends])
            if str(item or "").strip()
        )

        steps.append(Step(
            id=str(entry.get("id") or "").strip().lower() or _step_id(tool, capability, steps),
            tool=tool,
            capability=capability,
            arguments=clean,
            depends_on=depends,
            source_text=str(entry.get("text") or goal)
        ))

    if len(steps) < 2:
        return None

    built = _finalize(str(goal or ""), steps, ORIGIN_MODEL, registry)

    if built.reason:
        _log(f"proposal refused: {built.reason}")
        return None

    return built


def planning_manifest(registry: Optional[ToolRegistry] = None) -> List[Dict[str, Any]]:
    """What a proposer is allowed to choose from.

    The registry's own manifest with the unsupported and destructive
    capabilities removed, so a proposer is never shown a step that validation
    would then refuse.
    """
    registry = registry or _default_registry()
    entries = []

    for entry in registry.manifest():
        capabilities = [
            capability for capability in entry["capabilities"]
            if capability["supported"] and not capability["destructive"]
        ]

        if capabilities:
            entries.append({**entry, "capabilities": capabilities})

    return entries


# ==========================================================
# RESULT REFERENCES
# ==========================================================
def _resolve_reference(value: Any, built: Plan) -> Tuple[bool, Any]:
    """Read `$steps.<id>.<path>` out of a finished step's data.

    Returns (resolved, value). A reference that names an unknown step, a step
    that did not succeed, or a path that is not in the data comes back
    unresolved, and the step that needed it is skipped rather than run with a
    hole in it.
    """
    if not isinstance(value, str) or not value.startswith("$"):
        return True, value

    match = _REFERENCE.match(value)

    if not match:
        return False, None

    source = built.step(match.group("step"))

    # `ok` matters here in a way it does not for phrasing: a step that reported
    # honestly that it could not answer still has speech worth relaying, but its
    # data is not something to build another step on.
    if not source or not source.succeeded or source.result is None or not source.result.ok:
        return False, None

    current: Any = source.result.data

    for segment in match.group("path").split("."):
        if isinstance(current, dict):
            current = current.get(segment)
        else:
            return False, None

        if current is None:
            return False, None

    if isinstance(current, str) and not current.strip():
        return False, None

    return True, current


# ==========================================================
# EXECUTION
# ==========================================================
def execute(built: Plan, registry: Optional[ToolRegistry] = None) -> Plan:
    """Run a validated plan. Never raises.

    Independent reads run together, bounded; actions run one at a time in the
    order Jon said them, so the window manager still puts the last thing he asked
    for on top. Every step has a deadline. A step that fails, times out, or
    cannot resolve a reference is recorded and the rest of the plan carries on —
    a weather API being down must not cost Jon his calendar.
    """
    registry = registry or _default_registry()
    started_at = time.perf_counter()
    deadline = time.monotonic() + PLAN_BUDGET
    pending = list(built.steps)
    done: set = set()
    guard = 0
    # Not a context manager on purpose. `with` shuts the pool down with
    # wait=True, which would block the turn on exactly the thread that already
    # blew its deadline — the one case the timeout exists to escape. A step that
    # overran is abandoned to finish in the background instead.
    pool = ThreadPoolExecutor(max_workers=MAX_PARALLEL, thread_name_prefix="friday-plan")

    try:
        while pending:
            guard += 1

            if guard > HARD_MAX_STEPS + 1:
                # Cannot happen with a validated acyclic plan. Present because a
                # planner that can loop is a planner that can hang FRIDAY.
                for step in pending:
                    step.status = STATUS_SKIPPED
                    step.error = "plan did not make progress"

                break

            ready = [step for step in pending if set(step.depends_on) <= done]

            if not ready:
                for step in pending:
                    step.status = STATUS_SKIPPED
                    step.error = "a step it depended on did not complete"

                break

            _run_wave(ready, built, registry, pool, deadline)

            for step in ready:
                pending.remove(step)

                if step.succeeded:
                    done.add(step.id)
    finally:
        pool.shutdown(wait=False, cancel_futures=True)

    built.elapsed_ms = (time.perf_counter() - started_at) * 1000
    succeeded = len(built.completed)

    if succeeded == len(built.steps):
        built.status = PLAN_COMPLETE
    elif succeeded:
        built.status = PLAN_PARTIAL
    else:
        built.status = PLAN_FAILED

    if built.status == PLAN_PARTIAL:
        _log(f"partial: {succeeded} of {len(built.steps)} steps")

    _log(f"total {built.elapsed_ms:.0f}ms ({built.status})")
    return built


def _run_wave(
    ready: List[Step],
    built: Plan,
    registry: ToolRegistry,
    pool: ThreadPoolExecutor,
    deadline: float
) -> None:
    """Run one dependency level: reads together, actions in order."""
    parallel = [step for step in ready if _parallel_safe(step, registry)]
    sequential = [step for step in ready if not _parallel_safe(step, registry)]
    futures = {}

    for step in parallel:
        if not _bind_arguments(step, built):
            continue

        step.started_at = time.perf_counter()
        futures[step.id] = (step, pool.submit(_run_step, step, registry))

    # Actions are submitted one at a time and waited on, so the order Jon asked
    # for is the order the interface sees — which is what keeps the last widget
    # he named on top. They go through the pool rather than inline only so that
    # they have a deadline like everything else.
    for step in sequential:
        if not _bind_arguments(step, built):
            continue

        step.started_at = time.perf_counter()
        _collect(step, pool.submit(_run_step, step, registry), deadline)

    for step, future in futures.values():
        _collect(step, future, deadline)


def _bind_arguments(step: Step, built: Plan) -> bool:
    """Replace every reference with a real value, or skip the step."""
    for key, value in list(step.arguments.items()):
        resolved, actual = _resolve_reference(value, built)

        if not resolved:
            step.status = STATUS_SKIPPED
            step.error = f"could not read {value} from an earlier step"
            _log(f"  {step.label} skipped: unresolved reference")
            return False

        step.arguments[key] = actual

    return True


def _run_step(step: Step, registry: ToolRegistry) -> Tuple[ToolResult, float]:
    """Run one capability, and report how long IT took.

    Timed inside the worker and returned rather than written onto the step: a
    read that finished early must not be credited with the time its neighbour
    spent, and a step already given up on must not be able to rewrite its own
    duration when it eventually returns.
    """
    started_at = time.perf_counter()
    result = _invoke(step, registry)
    return result, (time.perf_counter() - started_at) * 1000


def _invoke(step: Step, registry: ToolRegistry) -> ToolResult:
    tool = registry.get(step.tool)
    capability = registry.capability(step.tool, step.capability)

    if not tool or not capability:
        return ToolResult(ok=False, error=f"{step.label} is not registered")

    slots = dict(step.arguments)
    slots["_text"] = step.source_text
    slots["_normalized"] = normalize(step.source_text)

    for slot in capability.slots:
        slots.setdefault(slot.name, slot.default() if callable(slot.default) else slot.default)

    return registry.execute(Resolution(
        kind="tool",
        text=step.source_text,
        normalized=slots["_normalized"],
        tool=tool,
        capability=capability,
        confidence=1.0,
        slots=slots,
        reason=f"planned {step.label}"
    ))


def _collect(step: Step, future, deadline: float) -> None:
    # Whichever runs out first: this step's own allowance, or what is left of the
    # whole plan's. Without the second, five slow steps could each spend their
    # full timeout and turn one request into most of a minute.
    allowance = max(0.05, min(step.timeout, deadline - time.monotonic()))

    try:
        result, step.elapsed_ms = future.result(timeout=allowance)
    except FutureTimeout:
        step.status = STATUS_TIMEOUT
        step.error = f"{step.label} did not answer within {allowance:.1f}s"
        step.elapsed_ms = (time.perf_counter() - step.started_at) * 1000
        _log(f"  {step.label} timed out after {allowance:.1f}s")
        return
    except Exception as error:  # noqa: BLE001 - a tool failure is never fatal
        step.status = STATUS_FAILED
        step.error = f"{step.label} raised {type(error).__name__}: {error}"
        step.elapsed_ms = (time.perf_counter() - step.started_at) * 1000
        _log(f"  {step.label} failed")
        return

    step.result = result

    # A step counts as done when it produced something to say or something it
    # did — NOT when `ok` is true. "I could not reach the weather service just
    # now" is a real answer to relay, and it is the difference between FRIDAY
    # saying that and FRIDAY saying nothing about the weather at all. A handler
    # that returns neither speech nor an action has genuinely failed.
    if result.speech or result.action_ran:
        step.status = STATUS_OK
        _log(f"  {step.label} completed {step.elapsed_ms:.0f}ms")
    else:
        step.status = STATUS_FAILED
        step.error = result.error or f"{step.label} returned nothing"
        _log(f"  {step.label} declined")


# ==========================================================
# RESULT FUSION
# ==========================================================
# There is no English formatter per tool combination here, and there must never
# be one: that is a table that grows as the square of the tool count. Tools
# return finished sentences and structured data; the plan collects them; FRIDAY
# is given the ORIGINAL question and told to answer it. The wording is hers.
_FUSION_RULES = (
    "Answer the question Jon actually asked, in one natural reply, in your own voice.\n"
    "Do not read the checks back to him as a list, and do not mention tools, steps or field names.\n"
    "Where something could not be checked, say so plainly in passing. Never fill a gap with a guess.\n"
    "Length as the request warrants: a short question gets a short answer."
)

# How many structured values one step contributes. Enough for FRIDAY to reason
# with, small enough that the prompt cannot become a data dump.
_MAX_FIELDS = 6


def fusion_prompt(built: Plan, registry: Optional[ToolRegistry] = None) -> str:
    """What FRIDAY is given once the plan has run."""
    registry = registry or _default_registry()
    lines = [f'Jon asked: "{built.goal.strip()}"', "", "You checked the following and got these results."]

    for step in built.steps:
        capability = registry.capability(step.tool, step.capability)
        title = str(capability.description if capability else step.label).rstrip(".")
        heading = f"{step.tool.replace('_', ' ').title()} — {title}"

        if step.succeeded and step.result is not None:
            lines.append("")
            lines.append(f"{heading}:")

            speech = str(step.result.speech or "").strip()

            if speech:
                lines.append(f"  {speech}")
            elif step.result.action_ran:
                lines.append("  done")

            for label, value in _readable_fields(step, capability):
                lines.append(f"  {label}: {value}")
        else:
            lines.append("")
            lines.append(f"{heading}: no result — {_plain_failure(step)}")

    if built.truncated:
        lines.append("")
        lines.append(
            f"Jon asked for more than you check in one go; these {len(built.steps)} are what you covered."
        )

    lines.append("")
    lines.append(_FUSION_RULES)
    return "\n".join(lines)


def _readable_fields(step: Step, capability: Optional[Capability]) -> List[Tuple[str, Any]]:
    """A few scalar readings from a step's data, never a nested dump."""
    data = step.result.data if step.result else {}

    if not isinstance(data, dict):
        return []

    fields: List[Tuple[str, Any]] = []

    for path in (capability.provides if capability else ()):
        current: Any = data

        for segment in path.split("."):
            current = current.get(segment) if isinstance(current, dict) else None

        if isinstance(current, (str, int, float)) and str(current).strip():
            fields.append((path.rsplit(".", 1)[-1].replace("_", " "), current))

    for key, value in data.items():
        if len(fields) >= _MAX_FIELDS:
            break

        if key.startswith("_") or not isinstance(value, (str, int, float, bool)):
            continue

        if isinstance(value, str) and (not value.strip() or len(value) > 120):
            continue

        if any(label == key.replace("_", " ") for label, _ in fields):
            continue

        fields.append((key.replace("_", " "), value))

    return fields[:_MAX_FIELDS]


def _plain_failure(step: Step) -> str:
    """The internal error stays internal; this is what FRIDAY is told."""
    if step.status == STATUS_TIMEOUT:
        return "it did not respond in time"

    if step.status == STATUS_SKIPPED:
        return "the information it needed was not available"

    return "it was not available"


def summarize(built: Plan, registry: Optional[ToolRegistry] = None) -> str:
    """A plain answer assembled locally, with no model involved.

    Used when the model call for fusion fails. Blunter than FRIDAY would put it,
    but it is accurate, it never invents anything, and it means a plan that ran
    successfully still produces an answer when the network does not.
    """
    registry = registry or _default_registry()
    parts = []

    for step in built.steps:
        if not step.succeeded or step.result is None:
            continue

        speech = str(step.result.speech or "").strip()

        if speech:
            parts.append(speech if speech.endswith((".", "!", "?")) else f"{speech}.")
        elif step.result.action_ran:
            capability = registry.capability(step.tool, step.capability)
            parts.append(f"{str(capability.description if capability else step.label).rstrip('.')}.")

    # Two different gaps, said two different ways. A step that was skipped did
    # not fail — the thing it needed was simply not there, and saying "I could
    # not reach it" instead would be inventing a reason.
    unreachable = _name_tools(built, lambda step: step.status in (STATUS_FAILED, STATUS_TIMEOUT))
    unanswerable = _name_tools(built, lambda step: step.status == STATUS_SKIPPED)

    if unreachable:
        parts.append(f"I could not get to the {unreachable} just now.")

    if unanswerable:
        parts.append(f"I did not have what I needed to check the {unanswerable}.")

    return " ".join(parts).strip()


def _name_tools(built: Plan, matches) -> str:
    names = sorted({step.tool.replace("_", " ") for step in built.steps if matches(step)})

    if not names:
        return ""

    return names[0] if len(names) == 1 else f"{', '.join(names[:-1])} or {names[-1]}"
