"""
Memory v2.5 test suite — intelligent memory capture.

Offline and deterministic: no Gemini key, no network, no microphone. Every test
points the store at a fresh temporary directory, so running this can never touch
real memory.

Run from FRIDAY_OS:

    python3 -m Tests.test_memory_v25

Covered (the numbering follows the Memory v2.5 spec):
   1. candidate creation from ordinary speech
   2. confidence reinforcement over repeated mentions
   3. auto-promotion once confident enough
   4. duplicate prevention
   5. contradictions: strong corrections update, weak ones wait
   6. project scoping of auto-learned decisions
   7. temporary statements expire instead of becoming facts
   8. explicit "remember" still overrides everything
   9. explicit "forget" still removes
  10. rejecting a pending candidate, and that rejection sticking
  11. pinned memories resist automatic replacement
  12. commands, greetings and questions never create memories
  13. direct commands bypass the pipeline entirely (latency)
"""

import os
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
FRIDAY_ROOT = HERE.parent

if str(FRIDAY_ROOT) not in sys.path:
    sys.path.insert(0, str(FRIDAY_ROOT))

# Learning logs are useful when tuning thresholds and pure noise in a test run.
os.environ["FRIDAY_PERF_LOG"] = "0"

PASSED = []
FAILED = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        PASSED.append(name)
        print(f"[PASS] {name}")
    else:
        FAILED.append(name)
        print(f"[FAIL] {name}{(' — ' + detail) if detail else ''}")


def fresh():
    """A brand-new memory directory with both stores reset."""
    from Core_Cognition import memory_manager as mm
    from Core_Cognition import memory_learning as ml

    mm.configure_memory_dir(tempfile.mkdtemp(prefix="friday_v25_"))
    ml.reset_cache()
    return mm, ml


def texts(records):
    return [record["text"] for record in records]


# ==========================================
# 1-3. CANDIDATE -> CONFIDENCE -> PROMOTION
# ==========================================
def test_auto_learning():
    mm, ml = fresh()

    # Spec TEST A: three ordinary mentions, no "remember" anywhere.
    ml.observe("I usually code in VS Code", conversation_id="chat_a")
    first = ml.all_candidates()

    check("an ordinary statement creates a candidate", len(first) == 1, str(texts(first)))
    check("nothing is stored on the first mention", not mm.all_memories(), str(texts(mm.all_memories())))
    check(
        "a first mention starts below the auto-save bar",
        first[0]["confidence"] < ml.AUTO_SAVE_THRESHOLD,
        str(first[0]["confidence"])
    )

    start = first[0]["confidence"]
    ml.observe("I really prefer VS Code when I'm coding Python", conversation_id="chat_a")
    second = ml.all_candidates()

    check("a restatement reinforces the SAME candidate", len(second) == 1, str(texts(second)))
    check("confidence rises with evidence", second[0]["confidence"] > start, str(second[0]["confidence"]))
    check("occurrences are counted", second[0]["occurrences"] == 2, str(second[0]["occurrences"]))
    check("still nothing stored at two mentions", not mm.all_memories())

    # A command mentioning the value is weak supporting evidence — enough to
    # tip an already-strong candidate over, never enough to create one.
    ml.observe("Open VS Code", conversation_id="chat_a")
    memories = mm.all_memories()

    check(
        "the preference is learned without ever being told to remember it",
        any("VS Code" in text for text in texts(memories)),
        str(texts(memories))
    )
    check("the promoted memory is marked as learned", memories and memories[0]["source"] == "learned")
    check(
        "the candidate is marked stored, not left pending",
        all(record["status"] != ml.STATUS_PENDING for record in ml.all_candidates()),
        str([(r["text"][:30], r["status"]) for r in ml.all_candidates()])
    )

    # Spec §21: a promoted memory is an ordinary memory from then on.
    context = mm.build_memory_context("what IDE do I normally use?")
    check(
        "an auto-learned memory is retrieved by the normal path",
        any("VS Code" in record["text"] for record in context["long_term"]),
        str(texts(context["long_term"]))
    )


def test_confidence_is_not_repetition_alone():
    mm, ml = fresh()

    # A weak opinion repeated in the same breath must not become a fact: two
    # sentences of one utterance are one piece of evidence, not two.
    ml.observe("I like blue. I like blue.", conversation_id="chat_a")
    candidates = ml.all_candidates()

    check("one utterance counts once", candidates and candidates[0]["occurrences"] == 1, str(candidates))
    check("a weak opinion is not stored from one utterance", not mm.all_memories())

    # And a definitive statement does not need repeating.
    ml.observe("My name is Jon Meholli", conversation_id="chat_a")
    check(
        "a definitive identity statement is stored immediately",
        any("Jon Meholli" in text for text in texts(mm.all_memories())),
        str(texts(mm.all_memories()))
    )


# ==========================================
# 4. DUPLICATES
# ==========================================
def test_no_duplicates():
    mm, ml = fresh()

    for _ in range(4):
        ml.observe("I prefer VS Code", conversation_id="chat_a")

    check("repeats reinforce one candidate", len(ml.all_candidates()) == 1, str(texts(ml.all_candidates())))
    check("only one memory is produced", len(mm.all_memories()) == 1, str(texts(mm.all_memories())))

    # Saying it again after promotion must not create a second copy.
    ml.observe("I prefer VS Code", conversation_id="chat_a")
    check("a post-promotion repeat adds no second memory", len(mm.all_memories()) == 1, str(texts(mm.all_memories())))

    # Different wording for the same claim also folds together.
    mm2, ml2 = fresh()
    ml2.observe("I use Visual Studio Code", conversation_id="chat_a")
    ml2.observe("I prefer VS Code", conversation_id="chat_a")
    check(
        "an alias is recognised as the same claim",
        len(ml2.all_candidates()) == 1,
        str(texts(ml2.all_candidates()))
    )


# ==========================================
# 5. CONTRADICTIONS
# ==========================================
def test_contradictions():
    mm, ml = fresh()

    original = mm.remember("I prefer Graphite mode", category="preference", importance=3, source="explicit")

    # Spec TEST D: an explicit, committed correction updates in place.
    ml.observe("I actually like Midnight better now. Use Midnight going forward.", conversation_id="chat_a")
    memories = mm.all_memories()

    check("a strong correction replaces the old value", len(memories) == 1, str(texts(memories)))
    check("the new value is stored", "Midnight" in memories[0]["text"], memories[0]["text"])
    check("the memory id is preserved", memories[0]["id"] == original["id"])
    check(
        "the previous value is kept in history",
        any("Graphite" in entry.get("text", "") for entry in memories[0].get("history") or []),
        str(memories[0].get("history"))
    )
    check("no conflicting duplicate is left behind", not any("Graphite" in text for text in texts(memories)))

    # A hedged contradiction waits for better evidence instead of overwriting.
    mm, ml = fresh()
    mm.remember("I prefer Graphite mode", category="preference", importance=3, source="explicit")
    ml.observe("I think I prefer Midnight instead", conversation_id="chat_a")

    check("a weak contradiction does NOT overwrite", texts(mm.all_memories()) == ["I prefer Graphite mode"], str(texts(mm.all_memories())))
    pending = ml.all_candidates(ml.STATUS_PENDING)
    check("the disagreeing claim is held as pending", len(pending) == 1, str(texts(pending)))
    check("the conflict is recorded on the candidate", pending and bool(pending[0]["contradicts"]))


# ==========================================
# 6. PROJECT SCOPING
# ==========================================
def test_project_learning():
    mm, ml = fresh()
    mm.set_active_project("FRIDAY-MK1")

    # Spec TEST C: an engineering decision, stated once, is high confidence.
    ml.observe("For FRIDAY-MK1, Workshop must never create a second voice session", conversation_id="chat_a")
    project_memories = mm.all_memories(scope=mm.SCOPE_PROJECT)

    check("a project decision is learned at once", len(project_memories) == 1, str(texts(project_memories)))
    check("it is filed under the named project", project_memories and project_memories[0]["project_id"] == "friday-mk1")
    check("it is NOT filed as user memory", not mm.all_memories(scope=mm.SCOPE_USER), str(texts(mm.all_memories(scope=mm.SCOPE_USER))))

    # And it stays out of an unrelated project's context.
    mm.set_active_project("Project Sentinel")
    context = mm.build_memory_context("how does the voice session work")
    check(
        "an auto-learned project fact does not leak to another project",
        not context["project"],
        str(texts(context["project"]))
    )

    mm.set_active_project("FRIDAY-MK1")
    context = mm.build_memory_context("how does the voice session work")
    check(
        "it is retrieved again in its own project",
        any("voice session" in record["text"] for record in context["project"]),
        str(texts(context["project"]))
    )


# ==========================================
# 7. TEMPORARY
# ==========================================
def test_temporary():
    mm, ml = fresh()

    # Spec TEST B.
    ml.observe("I'm tired today", conversation_id="chat_a")

    check("a passing state does not become a memory", not mm.all_memories(), str(texts(mm.all_memories())))
    check("a passing state does not become a candidate", not ml.all_candidates(), str(texts(ml.all_candidates())))
    working = mm.get_working_entries("chat_a")
    check("it goes to working memory instead", any("tired" in item["text"] for item in working), str(working))

    ml.observe("I'm working from the library right now", conversation_id="chat_a")
    check("another here-and-now statement stays temporary", not mm.all_memories(), str(texts(mm.all_memories())))

    # Conversation-scoped naming expires too, and never reaches long-term.
    ml.observe("For this conversation, call the prototype Alpha", conversation_id="chat_a")
    check("a conversation-scoped instruction is not a permanent fact", not mm.all_memories(), str(texts(mm.all_memories())))
    check(
        "it is available for the rest of the conversation",
        any("Alpha" in item["text"] for item in mm.get_working_entries("chat_a")),
        str(mm.get_working_entries("chat_a"))
    )
    check(
        "and not in another conversation",
        not any("Alpha" in item["text"] for item in mm.get_working_entries("chat_b")),
        str(mm.get_working_entries("chat_b"))
    )

    # Expiry is real, not decorative.
    mm.note_working("This expires almost at once", "chat_a", ttl_seconds=1)
    time.sleep(2.2)
    check(
        "temporary memory expires on its own",
        not any("expires almost" in item["text"] for item in mm.get_working_entries("chat_a"))
    )


# ==========================================
# 8-10. USER CONTROL BEATS CONFIDENCE
# ==========================================
def test_user_control():
    mm, ml = fresh()

    # Explicit remember still works, still instant, still unconditional.
    reply = mm.run_memory_command(
        mm.parse_memory_command("Remember that I prefer Graphite mode"),
        conversation_id="chat_a"
    )
    check("explicit remember stores immediately", reply == "Noted.", reply)
    check("no confidence gate applies to it", len(mm.all_memories()) == 1, str(texts(mm.all_memories())))

    # Explicit forget still works.
    reply = mm.run_memory_command(mm.parse_memory_command("Forget my theme preference"), conversation_id="chat_a")
    check("explicit forget removes it", reply.startswith("Forgotten"), reply)
    check("it is gone from storage", not mm.all_memories(), str(texts(mm.all_memories())))

    # "Don't remember that" rejects a pending observation.
    mm, ml = fresh()
    ml.observe("I usually code in VS Code", conversation_id="chat_a")
    check("there is something pending to refuse", len(ml.all_candidates(ml.STATUS_PENDING)) == 1)

    intent = mm.parse_memory_command("Don't remember that")
    check("'don't remember that' is recognised", (intent or {}).get("action") == "reject_candidate", str(intent))

    reply = mm.run_memory_command(intent, conversation_id="chat_a")
    check("FRIDAY confirms the refusal", "won't keep" in reply, reply)
    check("nothing is left pending", not ml.all_candidates(ml.STATUS_PENDING), str(texts(ml.all_candidates(ml.STATUS_PENDING))))

    # And the rejection sticks: saying it again does not restart the collection.
    ml.observe("I usually code in VS Code", conversation_id="chat_a")
    ml.observe("I really prefer VS Code", conversation_id="chat_a")
    ml.observe("Open VS Code", conversation_id="chat_a")
    check("a rejected claim is not re-learned", not mm.all_memories(), str(texts(mm.all_memories())))

    # Manual promotion overrides the confidence gate in the other direction.
    mm, ml = fresh()
    ml.observe("I like dark interfaces", conversation_id="chat_a")
    pending = ml.all_candidates(ml.STATUS_PENDING)
    check("a weak claim waits as pending", len(pending) == 1, str(texts(pending)))

    promoted = ml.promote_candidate(pending[0]["id"])
    check("promoting by hand stores it regardless of confidence", promoted is not None)
    check("and it is a real memory afterwards", len(mm.all_memories()) == 1, str(texts(mm.all_memories())))


# ==========================================
# 11. CORE MEMORY PROTECTION
# ==========================================
def test_pinned_protection():
    mm, ml = fresh()

    pinned = mm.remember("I prefer Graphite mode", category="preference", importance=3, source="explicit")
    mm.set_pinned(pinned["id"], True)

    # Even a confident, explicitly-worded correction does not replace a pinned
    # memory on the strength of a single utterance.
    ml.observe("I actually like Midnight better now. Use Midnight going forward.", conversation_id="chat_a")
    check(
        "a pinned memory survives one strong correction",
        texts(mm.all_memories()) == ["I prefer Graphite mode"],
        str(texts(mm.all_memories()))
    )
    check("the pin is intact", mm.all_memories()[0]["pinned"])
    check("the correction waits as pending", len(ml.all_candidates(ml.STATUS_PENDING)) == 1)

    # Said again in a separate turn, it is allowed through.
    ml.observe("Use Midnight going forward.", conversation_id="chat_a")
    check(
        "a repeated correction does update it",
        any("Midnight" in text for text in texts(mm.all_memories())),
        str(texts(mm.all_memories()))
    )
    check("and the memory stays pinned", mm.all_memories()[0]["pinned"])


# ==========================================
# 12. NEVER LEARN THESE
# ==========================================
def test_never_learn():
    mm, ml = fresh()

    quiet = [
        "Open Music",
        "What's the weather?",
        "Thanks",
        "Thank you so much",
        "Hey FRIDAY",
        "Play the next track",
        "Pause",
        "Search for flights to Berlin",
        "That song is pretty good",
        "How many joints did I say?",
        "Show me the calendar",
        "Turn off the interface"
    ]

    for line in quiet:
        ml.observe(line, conversation_id="chat_a")

    check("no memories from routine speech", not mm.all_memories(), str(texts(mm.all_memories())))
    check("no candidates from routine speech", not ml.all_candidates(), str(texts(ml.all_candidates())))

    # FRIDAY's own words are never evidence about Jon.
    ml.observe("I prefer VS Code for Python work", conversation_id="chat_a", role="assistant")
    check("FRIDAY does not learn from herself", not ml.all_candidates(), str(texts(ml.all_candidates())))


# ==========================================
# 13. LATENCY
# ==========================================
def test_latency():
    mm, ml = fresh()

    for index in range(40):
        ml.observe(f"I prefer tool number {index} for subsystem {index}", conversation_id="chat_a")

    commands = ["Open music", "Pause", "Next track", "Open weather", "Clear the workspace"]
    started = time.time()

    for _ in range(40):
        for command in commands:
            ml.should_observe(command)

    gate_us = (time.time() - started) * 1_000_000 / (40 * len(commands))
    check(f"the no-learn gate is effectively free ({gate_us:.1f} µs per command)", gate_us < 200, f"{gate_us:.1f} µs")

    check("a direct command is gated out before any analysis", ml.should_observe("Open music") is False)
    check("so is a question", ml.should_observe("What's the weather?") is False)
    check("a real statement is not gated out", ml.should_observe("I prefer VS Code for Python") is True)

    started = time.time()

    for _ in range(20):
        ml.observe("Open music", conversation_id="chat_a")

    per_call_ms = (time.time() - started) * 1000 / 20
    check(
        f"a full observe() of a command stays cheap ({per_call_ms:.2f} ms)",
        per_call_ms < 15,
        f"{per_call_ms:.2f} ms"
    )


# ==========================================
# PERSISTENCE AND V2 COMPATIBILITY
# ==========================================
def test_persistence_and_compatibility():
    mm, ml = fresh()

    ml.observe("I usually code in VS Code", conversation_id="chat_a")
    ml.observe("My name is Jon Meholli", conversation_id="chat_a")

    directory = str(mm.MEMORY_DIR)
    mm.reload_from_disk()
    ml.reset_cache()
    mm.configure_memory_dir(directory)

    check(
        "candidates survive a store reload",
        any("VS Code" in text for text in texts(ml.all_candidates())),
        str(texts(ml.all_candidates()))
    )
    check(
        "promoted memories survive a store reload",
        any("Jon Meholli" in text for text in texts(mm.all_memories())),
        str(texts(mm.all_memories()))
    )

    # A v2 record — no history field, no candidates file — must still load.
    mm2, ml2 = fresh()
    legacy = mm2._default_long_term()
    legacy["memories"] = [{
        "id": "mem_legacy001",
        "text": "Jon's birthday is August 20th.",
        "category": "identity",
        "scope": "user",
        "project_id": "",
        "created_at": "2026-05-14T00:00:00",
        "updated_at": "2026-05-14T00:00:00",
        "importance": 2,
        "source": "migration",
        "pinned": False,
        "last_accessed": None,
        "access_count": 1,
        "conversation_id": ""
    }]
    mm2._save_long_term(legacy)
    mm2.reload_from_disk()

    loaded = mm2.all_memories()
    check("a Memory v2 record still loads", len(loaded) == 1, str(texts(loaded)))
    check("its id is untouched", loaded and loaded[0]["id"] == "mem_legacy001")
    check("it gains the new history field, empty", loaded and loaded[0]["history"] == [])
    check("no candidates file is required", ml2.all_candidates() == [])

    report = mm2.maintain()
    check("maintenance reports candidate expiry", "expired_candidates" in report, str(report))
    check("maintenance does not destroy the v2 record", len(mm2.all_memories()) == 1, str(texts(mm2.all_memories())))


def main() -> int:
    tests = (
        ("auto learning", test_auto_learning),
        ("evidence, not repetition", test_confidence_is_not_repetition_alone),
        ("duplicates", test_no_duplicates),
        ("contradictions", test_contradictions),
        ("project learning", test_project_learning),
        ("temporary memory", test_temporary),
        ("user control", test_user_control),
        ("pinned protection", test_pinned_protection),
        ("never learn", test_never_learn),
        ("latency", test_latency),
        ("persistence and v2 compatibility", test_persistence_and_compatibility)
    )

    for label, test in tests:
        print(f"\n== {label} ==")

        try:
            test()
        except Exception as error:  # noqa: BLE001 - a crashed test is a failed test
            FAILED.append(label)
            print(f"[FAIL] {label} raised {type(error).__name__}: {error}")

    print("\n" + "=" * 52)
    print(f"Memory v2.5: {len(PASSED)} passed, {len(FAILED)} failed")

    if FAILED:
        for name in FAILED:
            print(f"  failed: {name}")

    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
