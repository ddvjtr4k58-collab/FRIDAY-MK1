"""
Multi-Step Planning test suite.

Offline and deterministic: no Gemini key, no network, no microphone, no Ollama.

Two halves, for two different reasons:

  PLANNING is exercised against the REAL registry — every tool FRIDAY actually
  has, resolving real sentences — because the thing worth testing is whether a
  request decomposes into the right capabilities, and a stubbed registry could
  not tell us that.

  EXECUTION is exercised against an isolated registry of fake tools, because the
  real handlers open widgets, call Google Calendar and hit a weather API. The
  behaviour under test — parallelism, ordering, timeouts, partial failure,
  dependencies — is in the executor, not in any particular tool.

Run from FRIDAY_OS:

    python3 -m Tests.test_multi_step_planning

Covered:
   1. planner metadata reaches the manifest, and destructive is marked
   2. multi-tool reads decompose into the right capabilities
   3. mixed read + action requests keep both halves
   4. dependent steps wire themselves from declared output fields
   5. simple commands never reach the planner, and pay almost nothing for it
   6. conversation is never planned
   7. a place FRIDAY does not cover is refused, not answered with local weather
   8. validation refuses unknown, unsupported, destructive, cyclic and oversized
   9. references resolve, and an unresolvable one skips its step honestly
  10. independent reads really do run in parallel
  11. one failing tool does not take the plan down
  12. a step that hangs is abandoned on its deadline
  13. fusion carries the goal, the results and the gaps, and never JSON
"""

import statistics
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
FRIDAY_ROOT = HERE.parent

if str(FRIDAY_ROOT) not in sys.path:
    sys.path.insert(0, str(FRIDAY_ROOT))

PASSED = []
FAILED = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        PASSED.append(name)
        print(f"[PASS] {name}")
    else:
        FAILED.append(name)
        print(f"[FAIL] {name}{(' — ' + detail) if detail else ''}")


def registry():
    from Core_Cognition import tools

    return tools


def planner():
    from Core_Cognition import tool_planner

    return tool_planner


def labels(built) -> list:
    return [f"{step.tool}.{step.capability}" for step in built.steps]


# ==========================================================
# 1. REGISTRY METADATA
# ==========================================================
def test_metadata() -> None:
    tools = registry()
    manifest = tools.manifest()

    missing = [
        f"{entry['name']}.{capability['name']}"
        for entry in manifest
        for capability in entry["capabilities"]
        if "step_kind" not in capability
        or "destructive" not in capability
        or "safe_parallel" not in capability
        or "provides" not in capability
        or not capability.get("timeout")
    ]
    check("every capability publishes its planner metadata", not missing, "; ".join(missing[:4]))

    kinds = {
        capability["step_kind"]
        for entry in manifest
        for capability in entry["capabilities"]
    }
    check("step kinds are only read or action", kinds <= {"read", "action"}, str(kinds))

    wrong = [
        f"{entry['name']}.{capability['name']}"
        for entry in manifest
        for capability in entry["capabilities"]
        if capability["safe_parallel"] != (capability["kind"] == "data")
    ]
    check("only reads are marked safe to run in parallel", not wrong, "; ".join(wrong))

    clear = tools.REGISTRY.capability("notifications", "clear_notifications")
    check("clearing every alert is declared destructive", bool(clear) and clear.destructive)

    next_event = tools.REGISTRY.capability("calendar", "next_event")
    check(
        "the next event publishes its location for later steps",
        bool(next_event) and "event.location" in next_event.provides,
        str(next_event.provides if next_event else None)
    )

    # A proposer must never be shown a step validation would then refuse.
    offered = {
        f"{entry['name']}.{capability['name']}"
        for entry in planner().planning_manifest()
        for capability in entry["capabilities"]
    }
    check(
        "the planning manifest hides destructive and unsupported capabilities",
        "notifications.clear_notifications" not in offered and "music.volume" not in offered
    )


# ==========================================================
# 2. MULTI-TOOL READS  (project tests 1, 2, 5, 11)
# ==========================================================
READ_CASES = (
    (
        "What do I have tomorrow and what's the weather?",
        ["calendar.tomorrow", "weather.current_conditions"],
    ),
    (
        "What's my battery percentage and how much disk space do I have?",
        ["system_status.battery", "system_status.disk_space"],
    ),
    (
        "What tasks are overdue and what's on my calendar today?",
        ["tasks.overdue", "calendar.todays_events"],
    ),
    (
        "Give me a quick briefing for today: calendar, tasks and weather.",
        ["calendar.todays_events", "tasks.list_tasks", "weather.current_conditions"],
    ),
    (
        "What project am I working on and what tasks do I have?",
        ["memory.project_memory", "tasks.list_tasks"],
    ),
    (
        "what is the weather and will it rain",
        ["weather.current_conditions", "weather.rain_chance"],
    ),
)


def test_multi_tool_reads() -> None:
    tool_planner = planner()
    wrong = []

    for text, expected in READ_CASES:
        built = tool_planner.plan(text)

        if built is None or labels(built) != expected:
            wrong.append(f"{text!r} -> {labels(built) if built else None}")

    check(f"{len(READ_CASES)} multi-tool reads decompose correctly", not wrong, "; ".join(wrong))

    built = tool_planner.plan(READ_CASES[0][0])
    check(
        "independent reads are planned to run in parallel",
        built.execution_mode == tool_planner.MODE_PARALLEL,
        built.execution_mode
    )
    check(
        "independent reads declare no dependencies",
        all(not step.depends_on for step in built.steps)
    )
    check(
        "every read step is classified READ",
        all(step.kind == "read" for step in built.steps)
    )


# ==========================================================
# 3. MIXED READ + ACTION  (project tests 3, 4, 10, 12)
# ==========================================================
MIXED_CASES = (
    (
        "Tell me what tasks are overdue and open Tasks.",
        ["tasks.overdue", "tasks.open_tasks"],
    ),
    (
        "Tell me what's on my calendar today and open it.",
        ["calendar.todays_events", "calendar.open_calendar"],
    ),
    (
        "Check the weather and open Weather.",
        ["weather.current_conditions", "weather.open_weather"],
    ),
    # The brain loop's existing multi-action handler claims these long before the
    # planner sees them, and must keep doing so. Planned here as a backstop, to
    # prove the planner would not turn "open calendar" into "read the calendar"
    # if it ever did get one.
    (
        "Open Music and Calendar.",
        ["music.open_music", "calendar.open_calendar"],
    ),
    (
        "Open Music, Weather, Tasks and Notes.",
        ["music.open_music", "weather.open_weather", "tasks.open_tasks", "notes.open_notes"],
    ),
)


def test_mixed_requests() -> None:
    tool_planner = planner()
    wrong = []

    for text, expected in MIXED_CASES:
        built = tool_planner.plan(text)

        if built is None or labels(built) != expected:
            wrong.append(f"{text!r} -> {labels(built) if built else None}")

    check(f"{len(MIXED_CASES)} mixed requests keep both halves", not wrong, "; ".join(wrong))

    built = tool_planner.plan("Tell me what tasks are overdue and open Tasks.")
    check(
        "reading and opening are classified differently",
        [step.kind for step in built.steps] == ["read", "action"],
        str([step.kind for step in built.steps])
    )

    ordered = tool_planner.plan("Open Music, Weather, Tasks and Notes.")
    check(
        "opened surfaces keep the order they were asked for",
        labels(ordered)[-1] == "notes.open_notes",
        str(labels(ordered))
    )


# ==========================================================
# 4. DEPENDENT STEPS  (project test 6)
# ==========================================================
def test_dependencies() -> None:
    tool_planner = planner()
    built = tool_planner.plan("What's the weather at the location of my next calendar event?")

    check("a compositional request becomes a plan", built is not None)

    if not built:
        return

    check(
        "the plan reads the event before the weather",
        labels(built) == ["calendar.next_event", "weather.current_conditions"],
        str(labels(built))
    )

    dependent = built.steps[1]
    check(
        "the weather step waits on the calendar step",
        dependent.depends_on == (built.steps[0].id,),
        str(dependent.depends_on)
    )
    check(
        "the location is a reference to the earlier step, not a guess",
        dependent.arguments.get("location") == f"$steps.{built.steps[0].id}.event.location",
        repr(dependent.arguments.get("location"))
    )

    # The dependency exists because the calendar DECLARED it publishes a
    # location, not because this sentence is written down anywhere.
    check(
        "a request naming two tools with nothing to share is not a dependency",
        tool_planner.plan("what is the weather at the moment") is None
    )


# ==========================================================
# 5. FAST PATH  (project test 8)
# ==========================================================
FAST_CASES = (
    "Open Music.",
    "Pause music.",
    "Open Calendar.",
    "What's my battery?",
    "What's the weather?",
    "open notes",
    "what time is it",
    "skip this song",
)


def test_fast_path_untouched() -> None:
    tool_planner = planner()
    claimed = [text for text in FAST_CASES if tool_planner.plan(text) is not None]

    check("simple commands are never planned", not claimed, "; ".join(claimed))

    # The whole cost of the new layer on a request that is not multi-step.
    for text in FAST_CASES:
        tool_planner.plan(text)

    samples = []

    for _ in range(200):
        started = time.perf_counter()
        tool_planner.plan("open music")
        samples.append((time.perf_counter() - started) * 1000)

    median = statistics.median(samples)
    check("declining a simple command costs under 0.05 ms", median < 0.05, f"{median:.4f} ms")

    # And the same for a request that has to be looked at more closely before it
    # can be declined.
    samples = []

    for _ in range(200):
        started = time.perf_counter()
        tool_planner.plan("what is the weather in Chicago")
        samples.append((time.perf_counter() - started) * 1000)

    median = statistics.median(samples)
    check("declining a single-tool question costs under 1 ms", median < 1.0, f"{median:.4f} ms")


# ==========================================================
# 6. CONVERSATION  (project test 9)
# ==========================================================
CONVERSATION_CASES = (
    "Hello.",
    "Thanks, that's great.",
    "how are you doing today",
    "explain how the weather forecast works and why it is wrong",
    "write me an email about the weather and my calendar",
    "email my teacher about my calendar and my tasks",
    "remind me to call Sam and Bob",
    "turn the volume up and pause the music",
    "what should I have for dinner tonight, and where",
)


def test_conversation_is_not_planned() -> None:
    tool_planner = planner()
    claimed = []

    for text in CONVERSATION_CASES:
        built = tool_planner.plan(text)

        if built is not None:
            claimed.append(f"{text!r} -> {labels(built)}")

    check(
        f"{len(CONVERSATION_CASES)} conversational requests are left alone",
        not claimed,
        "; ".join(claimed)
    )


# ==========================================================
# 7. UNSUPPORTED REQUESTS  (project test 10)
# ==========================================================
def test_unsupported_location() -> None:
    tools = registry()

    resolution = tools.resolve("What's the weather on Mars?")
    check(
        "a place named in a question is extracted",
        resolution.slots.get("location") == "Mars",
        repr(resolution.slots.get("location"))
    )

    result = tools.execute(resolution)
    check("a place FRIDAY does not cover is not answered", not result.ok)
    check(
        "and it says so rather than reporting local conditions",
        "mars" in result.speech.lower() and "degrees" not in result.speech.lower(),
        repr(result.speech)
    )

    home = tools.resolve("what is the weather")
    check(
        "the configured location still resolves with no location slot",
        home.handled and not home.slots.get("location")
    )


# ==========================================================
# 8. VALIDATION AND SAFETY
# ==========================================================
def test_validation() -> None:
    tool_planner = planner()
    tools = registry()

    check(
        "a plan containing a destructive step is refused",
        tool_planner.plan("clear my notifications and open my tasks") is None
    )

    # A destructive capability is untouched on the single-capability path: the
    # planner refusing to CHAIN it does not stop Jon asking for it directly.
    direct = tools.resolve("clear my notifications")
    check(
        "asking for a destructive action on its own still routes to it",
        direct.handled and direct.label == "notifications.clear_notifications",
        direct.label
    )

    rejected = tool_planner.plan_from_proposal("do things", {
        "steps": [
            {"tool": "weather", "capability": "current_conditions"},
            {"tool": "shell", "capability": "run", "arguments": {"command": "rm -rf /"}},
            {"tool": "calendar", "capability": "todays_events"},
        ]
    })
    check(
        "a proposed step naming a tool that does not exist is dropped",
        rejected is not None and labels(rejected) == ["weather.current_conditions", "calendar.todays_events"],
        str(labels(rejected) if rejected else None)
    )

    check(
        "a proposed plan containing a destructive step is refused whole",
        tool_planner.plan_from_proposal("tidy up", {
            "steps": [
                {"tool": "calendar", "capability": "todays_events"},
                {"tool": "notifications", "capability": "clear_notifications"},
            ]
        }) is None
    )

    check(
        "a proposed capability the tool does not have is dropped",
        tool_planner.plan_from_proposal("x", {
            "steps": [
                {"tool": "weather", "capability": "launch_missile"},
                {"tool": "calendar", "capability": "todays_events"},
            ]
        }) is None
    )

    smuggled = tool_planner.plan_from_proposal("x", {
        "steps": [
            {
                "tool": "weather",
                "capability": "current_conditions",
                "arguments": {"location": "Chicago", "command": "rm -rf /", "handler": "evil"},
            },
            {"tool": "calendar", "capability": "todays_events"},
        ]
    })
    check(
        "arguments are narrowed to the slots the capability declares",
        smuggled is not None and set(smuggled.steps[0].arguments) == {"location"},
        str(smuggled.steps[0].arguments if smuggled else None)
    )

    oversized = tool_planner.plan_from_proposal("everything", {
        "steps": [
            {"tool": "calendar", "capability": "todays_events"},
            {"tool": "tasks", "capability": "list_tasks"},
            {"tool": "weather", "capability": "current_conditions"},
            {"tool": "system_status", "capability": "battery"},
            {"tool": "notifications", "capability": "recent_notifications"},
            {"tool": "notes", "capability": "list_notes"},
            {"tool": "intel", "capability": "briefing"},
            {"tool": "memory", "capability": "project_memory"},
        ]
    })
    check(
        f"a plan is capped at {tool_planner.MAX_STEPS} steps",
        oversized is not None and len(oversized.steps) == tool_planner.MAX_STEPS,
        str(len(oversized.steps) if oversized else None)
    )
    check("a capped plan says it was capped", oversized is not None and oversized.truncated)

    cyclic = tool_planner.plan_from_proposal("x", {
        "steps": [
            {"id": "a", "tool": "calendar", "capability": "next_event", "depends_on": ["b"]},
            {"id": "b", "tool": "weather", "capability": "current_conditions", "depends_on": ["a"]},
        ]
    })
    check("a plan whose steps depend on each other is refused", cyclic is None)

    # A proposer is optional and is asked at most once, only for a request the
    # local decomposition could not read. It never overrides one it could.
    asked = []

    def proposer(goal, manifest):
        asked.append(goal)
        return {"steps": [
            {"tool": "calendar", "capability": "next_event"},
            {"tool": "weather", "capability": "rain_chance"},
        ]}

    proposed = tool_planner.plan(
        "what is on at the office and does that matter",
        proposer=proposer
    )
    check(
        "a proposer is asked when local decomposition finds nothing",
        len(asked) == 1 and proposed is not None,
        f"asked {len(asked)}x"
    )

    asked.clear()
    tool_planner.plan("What do I have tomorrow and what's the weather?", proposer=proposer)
    check("a proposer is not asked when the local planner already read the request", not asked)

    asked.clear()
    tool_planner.plan("open music", proposer=proposer)
    check("a proposer is never asked about a simple command", not asked)

    def broken(_goal, _manifest):
        raise RuntimeError("the proposer is down")

    check(
        "a proposer that fails ends planning rather than the turn",
        tool_planner.plan("what is on at the office and does that matter", proposer=broken) is None
    )

    bad_reference = tool_planner.plan_from_proposal("x", {
        "steps": [
            {"id": "a", "tool": "calendar", "capability": "next_event"},
            {
                "id": "b",
                "tool": "weather",
                "capability": "current_conditions",
                "arguments": {"location": "$steps.a.event.location; drop table"},
                "depends_on": ["a"],
            },
        ]
    })
    check(
        "an argument that only looks like a reference is dropped, not passed through",
        bad_reference is not None and "location" not in bad_reference.steps[1].arguments,
        str(bad_reference.steps[1].arguments if bad_reference else None)
    )


# ==========================================================
# ISOLATED TOOLS FOR EXECUTION
# ==========================================================
# Fake tools, because the real ones open widgets and call live services. What is
# under test here is the executor, which does not know what a tool is.
def build_fixture(pause: float = 0.0):
    from Core_Cognition.tool_registry import (
        KIND_ACTION,
        KIND_DATA,
        Capability,
        Slot,
        Tool,
        ToolRegistry,
        ToolResult,
    )

    order = []

    def reader(name: str, value):
        def run(_slots):
            if pause:
                time.sleep(pause)

            order.append(name)
            return ToolResult(speech=f"{name} says {value}.", data={"value": value, "name": name})

        return run

    def failing(_slots):
        raise RuntimeError("the remote end hung up")

    def silent(_slots):
        return ToolResult(ok=False, speech="", error="nothing to report")

    def slow(_slots):
        time.sleep(3.0)
        return ToolResult(speech="eventually.")

    def acting(name: str):
        def run(_slots):
            order.append(name)
            return ToolResult(speech="", action=f"open_{name}", event="acknowledge")

        return run

    def consumer(slots):
        order.append("consumer")
        return ToolResult(speech=f"consumed {slots.get('subject')}.", data={"subject": slots.get("subject")})

    isolated = ToolRegistry()
    isolated.set_action_bridge(lambda action, payload: True)
    isolated.register(Tool(
        name="alpha",
        description="first fixture tool",
        category="test",
        anchors=("alpha",),
        timeout=1.0,
        capabilities=(
            Capability("read", "read alpha", reader("alpha", 1), kind=KIND_DATA, provides=("value",)),
            Capability("act", "act on alpha", acting("alpha"), kind=KIND_ACTION),
            Capability("fail", "fail on alpha", failing, kind=KIND_DATA),
            Capability("silent", "say nothing", silent, kind=KIND_DATA),
            Capability("slow", "never answer", slow, kind=KIND_DATA, timeout=0.3),
        )
    ))
    isolated.register(Tool(
        name="beta",
        description="second fixture tool",
        category="test",
        anchors=("beta",),
        timeout=1.0,
        capabilities=(
            Capability("read", "read beta", reader("beta", 2), kind=KIND_DATA),
            Capability("act", "act on beta", acting("beta"), kind=KIND_ACTION),
            Capability(
                "consume",
                "consume an earlier value",
                consumer,
                kind=KIND_DATA,
                slots=(Slot("subject", description="what to consume"),)
            ),
        )
    ))
    isolated.register(Tool(
        name="gamma",
        description="third fixture tool",
        category="test",
        anchors=("gamma",),
        timeout=1.0,
        capabilities=(
            Capability("read", "read gamma", reader("gamma", 3), kind=KIND_DATA),
        )
    ))

    return isolated, order


def fixture_plan(goal: str, steps, isolated):
    """Build a plan straight from step descriptions, through the real validator."""
    return planner().plan_from_proposal(goal, {"steps": steps}, registry=isolated)


# ==========================================================
# 9. REFERENCES
# ==========================================================
def test_references() -> None:
    tool_planner = planner()
    isolated, _order = build_fixture()

    built = fixture_plan("chain them", [
        {"id": "source", "tool": "alpha", "capability": "read"},
        {
            "id": "sink",
            "tool": "beta",
            "capability": "consume",
            "arguments": {"subject": "$steps.source.value"},
            "depends_on": ["source"],
        },
    ], isolated)

    check("a referencing plan validates", built is not None)

    if not built:
        return

    tool_planner.execute(built, registry=isolated)
    check(
        "a later step reads a value the earlier step produced",
        built.steps[1].status == "ok" and built.steps[1].arguments.get("subject") == 1,
        f"{built.steps[1].status} {built.steps[1].arguments}"
    )

    # The same plan, pointed at a field nothing publishes.
    missing = fixture_plan("chain them", [
        {"id": "source", "tool": "alpha", "capability": "read"},
        {
            "id": "sink",
            "tool": "beta",
            "capability": "consume",
            "arguments": {"subject": "$steps.source.nowhere.at.all"},
            "depends_on": ["source"],
        },
    ], isolated)
    tool_planner.execute(missing, registry=isolated)

    check(
        "a reference that cannot be resolved skips its step",
        missing.steps[1].status == "skipped",
        missing.steps[1].status
    )
    check(
        "and the step that did work still counts",
        missing.steps[0].status == "ok" and missing.usable
    )

    # A skipped step is not a broken tool, and must not be described as one.
    skipped_answer = tool_planner.summarize(missing, registry=isolated)
    check(
        "a skipped step is reported as missing input, not as an outage",
        "did not have what I needed" in skipped_answer and "could not get to" not in skipped_answer,
        skipped_answer
    )

    # A dependant whose source failed outright.
    orphaned = fixture_plan("chain them", [
        {"id": "source", "tool": "alpha", "capability": "fail"},
        {
            "id": "sink",
            "tool": "beta",
            "capability": "consume",
            "arguments": {"subject": "$steps.source.value"},
            "depends_on": ["source"],
        },
    ], isolated)
    tool_planner.execute(orphaned, registry=isolated)

    check(
        "a step whose source failed is skipped, not run with a hole in it",
        orphaned.steps[1].status == "skipped" and orphaned.steps[1].result is None,
        orphaned.steps[1].status
    )


# ==========================================================
# 10. PARALLEL EXECUTION
# ==========================================================
def test_parallel_execution() -> None:
    tool_planner = planner()
    pause = 0.15
    isolated, _order = build_fixture(pause=pause)

    built = fixture_plan("three reads", [
        {"tool": "alpha", "capability": "read"},
        {"tool": "beta", "capability": "read"},
        {"tool": "gamma", "capability": "read"},
    ], isolated)

    check(
        "three independent reads are planned as parallel",
        built is not None and built.execution_mode == tool_planner.MODE_PARALLEL,
        built.execution_mode if built else None
    )

    started = time.perf_counter()
    tool_planner.execute(built, registry=isolated)
    elapsed = time.perf_counter() - started
    sequential = pause * 3

    check("every parallel read completed", built.status == tool_planner.PLAN_COMPLETE, built.status)
    check(
        "three 150 ms reads take closer to one than to three",
        elapsed < sequential * 0.6,
        f"{elapsed * 1000:.0f} ms vs {sequential * 1000:.0f} ms sequential"
    )

    # Actions keep their order, because the window manager puts the last one on
    # top and that has to be the one Jon named last.
    isolated, order = build_fixture()
    ordered = fixture_plan("three opens", [
        {"tool": "alpha", "capability": "act"},
        {"tool": "beta", "capability": "act"},
    ], isolated)
    tool_planner.execute(ordered, registry=isolated)

    check("actions run in the order they were asked for", order == ["alpha", "beta"], str(order))
    check(
        "actions are never planned to run in parallel",
        ordered.execution_mode == tool_planner.MODE_SEQUENTIAL,
        ordered.execution_mode
    )


# ==========================================================
# 11. PARTIAL FAILURE  (project test 7)
# ==========================================================
def test_partial_failure() -> None:
    tool_planner = planner()
    isolated, _order = build_fixture()

    built = fixture_plan("one of each", [
        {"tool": "alpha", "capability": "read"},
        {"tool": "beta", "capability": "read"},
        {"tool": "alpha", "capability": "fail"},
    ], isolated)

    tool_planner.execute(built, registry=isolated)

    check("a raising tool does not take the plan down", built.status == tool_planner.PLAN_PARTIAL, built.status)
    check("the working tools still returned", len(built.completed) == 2, str(labels(built)))
    check("the plan is still usable", built.usable)

    failed = next(step for step in built.steps if step.capability == "fail")
    check("the failure is recorded for debugging", "hung up" in failed.error, failed.error)

    answer = tool_planner.summarize(built, registry=isolated)
    check("the local summary reports what worked", "alpha says 1" in answer, answer)
    check("and says plainly what did not", "could not get to" in answer, answer)
    check("without leaking the exception", "RuntimeError" not in answer and "hung up" not in answer, answer)

    prompt = tool_planner.fusion_prompt(built, registry=isolated)
    check("the fusion prompt keeps internal errors internal", "hung up" not in prompt)
    check("the fusion prompt names the gap", "no result" in prompt, prompt[:200])

    # A tool that returns nothing at all is a failure, not an empty answer.
    silent = fixture_plan("one silent", [
        {"tool": "alpha", "capability": "read"},
        {"tool": "alpha", "capability": "silent"},
    ], isolated)
    tool_planner.execute(silent, registry=isolated)

    check(
        "a tool that returns nothing is recorded as failed",
        [step.status for step in silent.steps] == ["ok", "failed"],
        str([step.status for step in silent.steps])
    )

    # Everything failing means there is nothing to answer with, and the turn goes
    # back to the model rather than producing a confident nothing.
    dead = fixture_plan("all broken", [
        {"tool": "alpha", "capability": "fail"},
        {"tool": "alpha", "capability": "silent"},
    ], isolated)
    tool_planner.execute(dead, registry=isolated)

    check("a plan where nothing worked is not usable", not dead.usable and dead.status == tool_planner.PLAN_FAILED)


# ==========================================================
# 12. TIMEOUTS
# ==========================================================
def test_timeouts() -> None:
    tool_planner = planner()
    isolated, _order = build_fixture()

    built = fixture_plan("one slow", [
        {"tool": "alpha", "capability": "read"},
        {"tool": "alpha", "capability": "slow"},
    ], isolated)

    started = time.perf_counter()
    tool_planner.execute(built, registry=isolated)
    elapsed = time.perf_counter() - started

    slow_step = next(step for step in built.steps if step.capability == "slow")
    check("a hanging tool is given up on", slow_step.status == "timeout", slow_step.status)
    check(
        "and it is given up on near its declared deadline, not its own pace",
        elapsed < 1.5,
        f"{elapsed:.2f}s"
    )
    check("the other step still answered", built.usable)
    check(
        "the timeout is not reported as an exception",
        "did not respond in time" in tool_planner.fusion_prompt(built, registry=isolated)
    )


# ==========================================================
# 13. RESULT FUSION
# ==========================================================
def test_fusion() -> None:
    tool_planner = planner()
    isolated, _order = build_fixture()

    built = fixture_plan("two readings", [
        {"tool": "alpha", "capability": "read"},
        {"tool": "beta", "capability": "read"},
    ], isolated)
    tool_planner.execute(built, registry=isolated)

    goal = "what do alpha and beta say"
    built.goal = goal
    prompt = tool_planner.fusion_prompt(built, registry=isolated)

    check("the fusion prompt carries the original question", goal in prompt, prompt[:120])
    check("it carries each tool's finished sentence", "alpha says 1." in prompt and "beta says 2." in prompt)
    check("it tells FRIDAY to answer rather than list", "Answer the question Jon actually asked" in prompt)
    check("it forbids filling gaps with a guess", "Never fill a gap with a guess" in prompt)
    check("it contains no JSON", "{" not in prompt and "}" not in prompt, prompt[:200])

    summary = tool_planner.summarize(built, registry=isolated)
    check(
        "the local fallback answer is made only of what the tools said",
        summary == "alpha says 1. beta says 2.",
        summary
    )


def main() -> int:
    tests = (
        ("registry metadata", test_metadata),
        ("multi-tool reads", test_multi_tool_reads),
        ("mixed read and action", test_mixed_requests),
        ("dependent steps", test_dependencies),
        ("fast path untouched", test_fast_path_untouched),
        ("conversation", test_conversation_is_not_planned),
        ("unsupported location", test_unsupported_location),
        ("validation and safety", test_validation),
        ("result references", test_references),
        ("parallel execution", test_parallel_execution),
        ("partial failure", test_partial_failure),
        ("timeouts", test_timeouts),
        ("result fusion", test_fusion),
    )

    for label, test in tests:
        print(f"\n== {label} ==")

        try:
            test()
        except Exception as error:  # noqa: BLE001 - a crashed test is a failed test
            FAILED.append(label)
            print(f"[FAIL] {label} raised {type(error).__name__}: {error}")

    print("\n" + "=" * 52)
    print(f"Multi-Step Planning: {len(PASSED)} passed, {len(FAILED)} failed")

    if FAILED:
        for name in FAILED:
            print(f"  failed: {name}")

    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
