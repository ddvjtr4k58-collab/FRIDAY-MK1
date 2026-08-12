"""
Tool Intelligence test suite.

Offline and deterministic: no Gemini key, no network, no microphone, no
Ollama. Only the intent layer is exercised — resolve() decides what a request
means without calling any handler, so nothing here reads or writes real state.

Run from FRIDAY_OS:

    python3 -m Tests.test_tool_intelligence

Covered:
  1. every declared tool registers and exposes a manifest
  2. intent understanding: paraphrases reach the right tool
  3. conversation stays conversation
  4. obvious commands take the fast path and stay fast
  5. slots are extracted, and non-places are not mistaken for places
  6. the registry refuses to double-claim a phrase
  7. unsupported capabilities are declared but never routed to
  8. resolution is fast enough to sit on the response path
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


# ==========================================
# 1. REGISTRATION
# ==========================================
EXPECTED_TOOLS = {
    "calendar",
    "finder",
    "intel",
    "maps",
    "memory",
    "music",
    "notes",
    "notifications",
    "settings",
    "system_status",
    "tasks",
    "voice",
    "weather",
    "workshop",
}


def test_registration() -> None:
    tools = registry()
    registered = {tool.name for tool in tools.tools()}

    check(
        "every expected tool is registered",
        EXPECTED_TOOLS <= registered,
        f"missing {sorted(EXPECTED_TOOLS - registered)}"
    )

    manifest = tools.manifest()
    check("manifest covers every tool", len(manifest) == len(registered))

    complete = [
        entry for entry in manifest
        if entry["name"] and entry["description"] and entry["category"] and entry["capabilities"]
    ]
    check(
        "every tool declares name, description, category and capabilities",
        len(complete) == len(manifest),
        f"{len(manifest) - len(complete)} incomplete"
    )

    described = [
        capability
        for entry in manifest
        for capability in entry["capabilities"]
        if not capability["description"] or not capability["outputs"]
    ]
    check(
        "every capability declares a description and its outputs",
        not described,
        f"{len(described)} incomplete"
    )


# ==========================================
# 2. INTENT UNDERSTANDING
# ==========================================
# The point of the whole layer: none of these are commands FRIDAY was taught
# word for word.
INTENT_CASES = (
    ("What is the weather today?", "weather"),
    ("how humid is it", "weather"),
    ("do I need an umbrella", "weather"),
    ("what will the weather be tomorrow", "weather"),
    ("How much battery do I have?", "system_status"),
    ("how much storage is left", "system_status"),
    ("What time is it?", "system_status"),
    ("Open my calendar", "calendar"),
    ("what is my next meeting", "calendar"),
    ("how is my week looking", "calendar"),
    ("Play some music", "music"),
    ("what am I listening to", "music"),
    ("skip this song", "music"),
    ("What tasks do I still have?", "tasks"),
    ("anything overdue", "tasks"),
    ("remind me to call Sam tomorrow", "tasks"),
    ("I forgot what project I'm working on.", "memory"),
    ("what are my preferences", "memory"),
    ("do I have any notifications", "notifications"),
    ("open my files", "finder"),
    ("what notes do I have", "notes"),
    ("is workshop mode on", "workshop"),
    ("what are my settings", "settings"),
    ("what voice are you using", "voice"),
    ("how are the markets", "intel"),
)


def test_intent_understanding() -> None:
    tools = registry()
    wrong = []

    for text, expected in INTENT_CASES:
        resolution = tools.resolve(text)

        if not resolution.handled or resolution.tool.name != expected:
            wrong.append(f"{text!r} -> {resolution.label} (wanted {expected})")

    check(
        f"{len(INTENT_CASES)} paraphrased requests reach the right tool",
        not wrong,
        "; ".join(wrong)
    )


# ==========================================
# 3. CONVERSATION STAYS CONVERSATION
# ==========================================
CONVERSATION_CASES = (
    "Hello.",
    "Thank you.",
    "how are you doing today",
    "tell me a joke",
    "what should I eat for dinner",
    "what is the capital of France",
    "why is the sky blue",
    "explain how the weather works",
    "summarize this article",
    "write me an email about the weather",
    "email my teacher about being late",
    "remember to buy milk",
    "turn the volume up",
    "mute the music",
    "make it louder",
)


def test_conversation() -> None:
    tools = registry()
    claimed = []

    for text in CONVERSATION_CASES:
        resolution = tools.resolve(text)

        if resolution.handled:
            claimed.append(f"{text!r} -> {resolution.label}")

    check(
        f"{len(CONVERSATION_CASES)} conversational requests are left to Gemini",
        not claimed,
        "; ".join(claimed)
    )


# ==========================================
# 4. FAST PATH
# ==========================================
FAST_CASES = (
    ("Open Music", "music.open_music"),
    ("Open Notes", "notes.open_notes"),
    ("Open Weather", "weather.open_weather"),
    ("Open Calendar", "calendar.open_calendar"),
    ("open settings", "settings.open_settings"),
    ("open files", "finder.open_files"),
)


def test_fast_path() -> None:
    tools = registry()
    from Core_Cognition.tool_registry import ROUTE_FAST_PATH

    wrong = [
        f"{text!r} -> {tools.resolve(text).label} via {tools.resolve(text).route}"
        for text, expected in FAST_CASES
        if tools.resolve(text).label != expected or tools.resolve(text).route != ROUTE_FAST_PATH
    ]

    check("obvious commands take the fast path", not wrong, "; ".join(wrong))

    # The fast path is one dict lookup and must stay one dict lookup.
    samples = []

    for _ in range(200):
        started = time.perf_counter()
        tools.resolve("open music")
        samples.append((time.perf_counter() - started) * 1000)

    median = statistics.median(samples)
    check("fast path stays under 0.1 ms", median < 0.1, f"{median:.4f} ms")


# ==========================================
# 5. SLOTS
# ==========================================
def test_slots() -> None:
    tools = registry()

    located = tools.resolve("what is the weather in Chicago")
    check(
        "a named place is extracted as a location",
        located.slots.get("location") == "Chicago",
        repr(located.slots.get("location"))
    )

    timed = tools.resolve("what is the weather in the morning")
    check(
        "a time phrase is not mistaken for a place",
        not timed.slots.get("location"),
        repr(timed.slots.get("location"))
    )

    searched = tools.resolve("search files for invoices")
    check(
        "a file search query is extracted",
        searched.slots.get("query") == "invoices",
        repr(searched.slots.get("query"))
    )

    foldered = tools.resolve("create a folder for taxes")
    check(
        "a folder name is extracted",
        foldered.slots.get("name") == "taxes",
        repr(foldered.slots.get("name"))
    )

    reminded = tools.resolve("remind me to call Sam tomorrow")
    check(
        "a task title is extracted",
        reminded.slots.get("title") == "call Sam tomorrow",
        repr(reminded.slots.get("title"))
    )

    mapped = tools.resolve("show me Paris on the map")
    check(
        "a map destination is extracted",
        mapped.slots.get("destination") == "Paris",
        repr(mapped.slots.get("destination"))
    )


# ==========================================
# 6. REGISTRY INTEGRITY
# ==========================================
def test_registry_integrity() -> None:
    from Core_Cognition.tool_registry import (
        Capability,
        Tool,
        ToolRegistry,
        ToolResult,
    )

    def handler(_slots):
        return ToolResult(speech="ok")

    isolated = ToolRegistry()
    isolated.register(Tool(
        name="alpha",
        description="first",
        category="test",
        anchors=("alpha",),
        capabilities=(Capability("go", "go", handler, phrases=("do the thing",)),)
    ))

    duplicate_phrase = False

    try:
        isolated.register(Tool(
            name="beta",
            description="second",
            category="test",
            anchors=("beta",),
            capabilities=(Capability("go", "go", handler, phrases=("do the thing",)),)
        ))
    except ValueError:
        duplicate_phrase = True

    check("a phrase claimed by two tools is refused at registration", duplicate_phrase)

    duplicate_tool = False

    try:
        isolated.register(Tool(
            name="alpha",
            description="again",
            category="test",
            anchors=("alpha",),
            capabilities=(Capability("go", "go", handler),)
        ))
    except ValueError:
        duplicate_tool = True

    check("registering the same tool twice is refused", duplicate_tool)

    # A brand-new tool becomes routable with no other change anywhere.
    isolated.register(Tool(
        name="gamma",
        description="a tool added at runtime",
        category="test",
        anchors=("widgetron",),
        capabilities=(
            Capability("status", "status", handler, default=True, cues=("status",)),
        )
    ))
    discovered = isolated.resolve("what is the widgetron status")
    check(
        "a newly registered tool is discovered without touching the router",
        discovered.handled and discovered.label == "gamma.status",
        discovered.label
    )


# ==========================================
# 7. UNSUPPORTED CAPABILITIES
# ==========================================
def test_unsupported() -> None:
    tools = registry()
    volume = tools.REGISTRY.capability("music", "volume")

    check("music declares a volume capability", volume is not None)
    check("volume is declared unsupported", bool(volume) and not volume.supported)

    routed = [
        text for text in ("turn the volume up", "turn the music down", "make it quieter")
        if tools.resolve(text).handled
    ]
    check("volume requests are never routed to a tool", not routed, "; ".join(routed))


# ==========================================
# 8. PERFORMANCE
# ==========================================
def test_performance() -> None:
    tools = registry()
    corpus = [text for text, _ in INTENT_CASES] + list(CONVERSATION_CASES)

    # Warm the interpreter so the first-call cost is not measured as the steady state.
    for text in corpus:
        tools.resolve(text)

    samples = []

    for _ in range(20):
        for text in corpus:
            started = time.perf_counter()
            tools.resolve(text)
            samples.append((time.perf_counter() - started) * 1000)

    median = statistics.median(samples)
    worst = max(samples)

    check("median resolution under 0.5 ms", median < 0.5, f"{median:.4f} ms")
    check("worst-case resolution under 5 ms", worst < 5.0, f"{worst:.4f} ms")


def main() -> int:
    tests = (
        ("registration", test_registration),
        ("intent understanding", test_intent_understanding),
        ("conversation", test_conversation),
        ("fast path", test_fast_path),
        ("slots", test_slots),
        ("registry integrity", test_registry_integrity),
        ("unsupported capabilities", test_unsupported),
        ("performance", test_performance),
    )

    for label, test in tests:
        print(f"\n== {label} ==")

        try:
            test()
        except Exception as error:  # noqa: BLE001 - a crashed test is a failed test
            FAILED.append(label)
            print(f"[FAIL] {label} raised {type(error).__name__}: {error}")

    print("\n" + "=" * 52)
    print(f"Tool Intelligence: {len(PASSED)} passed, {len(FAILED)} failed")

    if FAILED:
        for name in FAILED:
            print(f"  failed: {name}")

    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
