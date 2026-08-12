"""
Memory v2 test suite.

Offline and deterministic: no Gemini key, no network, no microphone. Every test
points the store at a fresh temporary directory through FRIDAY_MEMORY_DIR, so
running this can never touch real memory.

Run from FRIDAY_OS:

    python3 -m Tests.test_memory_v2

Covered:
  1. long-term memory survives a real process restart
  2. forgetting removes a fact from retrieval
  3. two Silent Operator chats do not see each other's turns
  4. project memory does not leak into another project
  5. a pinned memory stays pinned across a restart and ranks higher
  6. storing the same fact twice does not create a second record
  7. memory.txt and the old Workshop notes migrate without loss
  8. command precedence: reminders stay tasks, notes stay notes
  9. working memory expires
 10. retrieval is fast, and trivial commands skip it entirely
"""

import json
import os
import subprocess
import sys
import tempfile
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


def fresh_store():
    """A brand-new memory directory, with every cache dropped."""
    directory = tempfile.mkdtemp(prefix="friday_memory_test_")
    from Core_Cognition import memory_manager

    memory_manager.configure_memory_dir(directory)
    return memory_manager, directory


# ==========================================
# 1. LONG-TERM MEMORY ACROSS A REAL RESTART
# ==========================================
RESTART_PROBE = """
import os, sys
sys.path.insert(0, {root!r})
os.environ["FRIDAY_MEMORY_DIR"] = {directory!r}
from Core_Cognition import memory_manager as mm
context = mm.build_memory_context("What theme do I prefer?")
pinned = [r["text"] for r in mm.all_memories() if r["pinned"]]
print(mm.json.dumps({{
    "recalled": [r["text"] for r in context["long_term"]],
    "block": context["block"],
    "pinned": pinned,
    "top": context["long_term"][0]["text"] if context["long_term"] else ""
}}))
"""


def restart_and_query(directory: str) -> dict:
    """Run a separate Python process against the same store on disk.

    A genuinely new process, not a cache reset: this is the only way to prove
    the memory really persisted rather than merely surviving in RAM.
    """
    script = RESTART_PROBE.format(root=str(FRIDAY_ROOT), directory=directory)
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        timeout=60
    )

    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip()[-500:])

    return json.loads(result.stdout.strip().splitlines()[-1])


def test_long_term_persistence():
    mm, directory = fresh_store()

    intent = mm.parse_memory_command("Remember that I prefer Graphite mode")
    check("remember command is recognised", bool(intent) and intent["action"] == "store")

    reply = mm.run_memory_command(intent, conversation_id="chat_a")
    check("FRIDAY confirms storing", reply == "Noted.", f"got {reply!r}")

    after_restart = restart_and_query(directory)
    check(
        "long-term memory survives restart",
        any("graphite" in text.lower() for text in after_restart["recalled"]),
        f"recalled {after_restart['recalled']}"
    )
    check(
        "restored memory reaches the prompt block",
        "Graphite" in after_restart["block"],
        after_restart["block"]
    )


# ==========================================
# 2. FORGETTING
# ==========================================
def test_forget():
    mm, _ = fresh_store()

    mm.run_memory_command(mm.parse_memory_command("Remember that I prefer Graphite mode"))
    mm.run_memory_command(mm.parse_memory_command("Remember that I prefer concise answers"))

    before = mm.build_memory_context("What theme do I prefer?")
    check(
        "theme preference is recalled before forgetting",
        any("Graphite" in record["text"] for record in before["long_term"])
    )

    reply = mm.run_memory_command(mm.parse_memory_command("Forget my theme preference"))
    check("FRIDAY confirms forgetting", reply.startswith("Forgotten"), f"got {reply!r}")

    after = mm.build_memory_context("What theme do I prefer?")
    check(
        "theme preference is gone from retrieval",
        not any("Graphite" in record["text"] for record in after["long_term"]),
        str([record["text"] for record in after["long_term"]])
    )
    check(
        "theme preference is gone from storage",
        not any("Graphite" in record["text"] for record in mm.all_memories())
    )
    check(
        "the unrelated preference was NOT deleted",
        any("concise" in record["text"] for record in mm.all_memories()),
        str([record["text"] for record in mm.all_memories()])
    )


# ==========================================
# 3. CHAT SEPARATION
# ==========================================
def test_chat_separation():
    mm, _ = fresh_store()

    from Core_Cognition import state_manager

    # Point the interface state file at a temporary location too, so the test
    # never writes over the real Workshop layout.
    workshop_file = Path(tempfile.mkdtemp(prefix="friday_workshop_test_")) / "workshop_state.json"
    state_manager.WORKSHOP_STATE_FILE = workshop_file
    state_manager.hud_state["workshop_mode"] = state_manager._default_workshop_state()

    chat_a = state_manager.create_workshop_chat(broadcast=False)["id"]
    state_manager.append_workshop_chat("Jon", "My robot arm should have six joints", broadcast=False, chat_id=chat_a)
    state_manager.append_workshop_chat("FRIDAY", "Six joints noted for the arm.", broadcast=False, chat_id=chat_a)

    chat_b = state_manager.create_workshop_chat(broadcast=False)["id"]
    state_manager.append_workshop_chat("Jon", "Let's study calculus", broadcast=False, chat_id=chat_b)

    context_a = mm.build_memory_context("How many joints did I say?", conversation_id=chat_a)
    context_b = mm.build_memory_context("How many joints did I say?", conversation_id=chat_b)

    a_text = " ".join(turn["text"] for turn in context_a["conversation"])
    b_text = " ".join(turn["text"] for turn in context_b["conversation"])

    check("chat A remembers its own conversation", "six joints" in a_text.lower(), a_text)
    check("chat B does NOT inherit chat A", "joints" not in b_text.lower(), b_text)
    check("chat B keeps its own topic", "calculus" in b_text.lower(), b_text)
    check(
        "returning to chat A still has the answer",
        "six joints" in " ".join(
            turn["text"] for turn in mm.build_memory_context("joints?", conversation_id=chat_a)["conversation"]
        ).lower()
    )

    # Long-term memory is shared by both, because it is about Jon, not about a chat.
    mm.remember("I prefer metric units", importance=3)
    shared = mm.build_memory_context("what units do I prefer", conversation_id=chat_b)
    check(
        "long-term memory is shared across chats",
        any("metric" in record["text"] for record in shared["long_term"])
    )


# ==========================================
# 4. PROJECT ISOLATION
# ==========================================
def test_project_isolation():
    mm, _ = fresh_store()

    mm.set_active_project("FRIDAY-MK1")
    mm.remember(
        "FRIDAY-MK1 uses Gemini Live for the voice layer",
        scope=mm.SCOPE_PROJECT,
        importance=3
    )

    friday_context = mm.build_memory_context("how does the voice layer work")
    check(
        "project memory is retrieved for its own project",
        any("Gemini Live" in record["text"] for record in friday_context["project"]),
        str([record["text"] for record in friday_context["project"]])
    )

    mm.set_active_project("Project Sentinel")
    sentinel_context = mm.build_memory_context("how does the voice layer work")
    check(
        "project memory does NOT leak into another project",
        not sentinel_context["project"],
        str([record["text"] for record in sentinel_context["project"]])
    )
    check(
        "switching project does not delete the other project's memory",
        any("Gemini Live" in record["text"] for record in mm.all_memories(scope=mm.SCOPE_PROJECT))
    )

    # "for this project, ..." files against whichever project is active.
    mm.run_memory_command(mm.parse_memory_command("Remember that for this project, keep it simple"))
    sentinel_memories = mm.all_memories(
        scope=mm.SCOPE_PROJECT,
        project_id="project-sentinel",
        include_all_projects=False
    )
    check(
        "project-scoped command files against the active project",
        any("keep it simple" in record["text"].lower() for record in sentinel_memories),
        str([record["text"] for record in sentinel_memories])
    )


# ==========================================
# 5. PINNING
# ==========================================
def test_pinning():
    mm, directory = fresh_store()

    record = mm.remember("I prefer Graphite mode", importance=4, category="preference")
    mm.remember("Graphite mode was mentioned in passing once", importance=1)
    mm.set_pinned(record["id"], True)

    after_restart = restart_and_query(directory)
    check(
        "pin survives restart",
        any("I prefer Graphite mode" == text for text in after_restart["pinned"]),
        str(after_restart["pinned"])
    )
    check(
        "pinned memory ranks above an unpinned one on the same topic",
        after_restart["top"] == "I prefer Graphite mode",
        after_restart["top"]
    )

    # A pinned core identity memory is offered even when the request does not
    # obviously name it — capped at three, and only for identity/preference.
    identity = mm.identity_prompt_block()
    check("core identity block includes the pinned preference", "Graphite" in identity, identity)
    check(
        "the identity block stays capped",
        len([line for line in identity.splitlines() if line.startswith("- ")]) <= 3,
        identity
    )

    # A stated identity fact is always-on without needing a pin; a migrated one
    # is not, because it was never deliberately said to this system.
    mm.remember("My name is Jon", source="explicit")
    mm.remember("Jon's old nickname was recorded once", source="migration", category="identity", importance=2)
    block = mm.identity_prompt_block()
    check("a stated identity fact reaches the voice layer", "My name is Jon" in block, block)
    check("a migrated fragment does not", "nickname" not in block, block)

    # Ordinary facts never ride along unasked.
    mm.remember("The build script lives in run_friday.sh", importance=3)
    check("an ordinary fact stays out of the always-on block", "run_friday.sh" not in mm.identity_prompt_block())


# ==========================================
# 6. DUPLICATES
# ==========================================
def test_duplicates():
    mm, _ = fresh_store()

    first = mm.remember("I prefer Graphite mode", importance=3)
    second = mm.remember("I prefer Graphite mode", importance=3)

    check("second identical store does not create a record", second["created"] is False)
    check("the same id is reused", first["id"] == second["id"])
    check("only one record exists", len(mm.all_memories()) == 1, str(mm.all_memories()))

    # A single-valued fact is corrected rather than duplicated.
    mm.remember("My favourite IDE is VS Code")
    mm.remember("My favourite IDE is Vim")
    ide = [record["text"] for record in mm.all_memories() if "IDE" in record["text"]]
    check("a single-valued fact is updated, not duplicated", len(ide) == 1, str(ide))
    check("the newer value wins", ide and "Vim" in ide[0], str(ide))

    # Two genuinely different preferences must BOTH survive.
    mm.remember("I prefer dark themes")
    mm.remember("I prefer short answers")
    preferences = [record["text"] for record in mm.all_memories() if "prefer" in record["text"]]
    check("different preferences are not merged", len(preferences) >= 3, str(preferences))

    # Exact duplicates that reached the store some other way are merged by maintenance.
    report = mm.maintain()
    check("maintenance runs cleanly", isinstance(report, dict) and "merged_duplicates" in report)


# ==========================================
# 7. MIGRATION
# ==========================================
def test_migration():
    mm, directory = fresh_store()

    legacy = Path(directory) / "memory.txt"
    legacy.write_text(
        "[2026-05-14] Jon's birthday is August 20th.\n"
        "[2026-08-06] Jon's full name is Jon Holden.\n"
        "[2026-08-06] test note\n"
    )

    result = mm.migrate_memory_txt(legacy)
    check("memory.txt migrated every line", result.get("migrated") == 3, str(result))

    texts = [record["text"] for record in mm.all_memories()]
    check("a real fact survived migration", any("Jon Holden" in text for text in texts), str(texts))
    check("scratch lines are kept, not discarded", any("test note" in text for text in texts))

    junk = next(record for record in mm.all_memories() if "test note" in record["text"])
    real = next(record for record in mm.all_memories() if "Jon Holden" in record["text"])
    check("scratch lines rank below real facts", junk["importance"] < real["importance"])
    check("the original date is preserved", real["created_at"].startswith("2026-08-06"), real["created_at"])

    backups = list((Path(directory) / "backups").glob("memory_txt_*.bak"))
    check("a backup of memory.txt was written", len(backups) == 1, str(backups))
    check("the original memory.txt still exists", legacy.exists())

    again = mm.migrate_memory_txt(legacy)
    check("migration is idempotent", again.get("migrated") == 0, str(again))

    workshop = mm.migrate_workshop_memory([
        {"text": "Workshop windows must not create separate voice sessions", "pinned": True},
        {"text": "Silent Operator uses gemini-2.5-flash", "pinned": False}
    ])
    check("workshop notes migrated", workshop.get("migrated") == 2, str(workshop))

    project_memories = mm.all_memories(scope=mm.SCOPE_PROJECT)
    check("workshop notes became project memory", len(project_memories) == 2)
    check(
        "a pinned workshop note stays pinned",
        any(record["pinned"] for record in project_memories)
    )

    workshop_again = mm.migrate_workshop_memory([{"text": "Something else", "pinned": False}])
    check("workshop migration is idempotent", workshop_again.get("migrated") == 0, str(workshop_again))


# ==========================================
# 8. COMMAND PRECEDENCE
# ==========================================
def test_command_precedence():
    mm, _ = fresh_store()

    check(
        "'remember to ...' stays a task",
        mm.parse_memory_command("remember to call Sam at four") is None
    )
    check(
        "'don't forget to ...' stays a task",
        mm.parse_memory_command("don't forget to buy milk") is None
    )
    check(
        "bare 'remember this' does not store when there is nothing to store",
        mm.run_memory_command({"action": "store_previous"}, conversation_id="empty_chat") == ""
    )
    check(
        "'remember that ...' is a memory",
        (mm.parse_memory_command("remember that I use a MacBook") or {}).get("action") == "store"
    )
    check(
        "'what do you remember about X' is a recall",
        (mm.parse_memory_command("What do you remember about me?") or {}).get("action") == "recall"
    )
    check(
        "a bulk wipe is refused rather than obeyed",
        (mm.parse_memory_command("forget everything") or {}).get("action") == "forget_all_refused"
    )
    check(
        "ordinary conversation is not a memory command",
        mm.parse_memory_command("what is the weather tomorrow") is None
    )

    # Capitalisation must survive the parse.
    intent = mm.parse_memory_command("Remember that I prefer Graphite mode")
    check("stored text keeps its capitalisation", intent["text"] == "I prefer Graphite mode", intent["text"])

    # Conservative capture: preferences yes, passing moods no. Extraction moved
    # into the Memory v2.5 learning pipeline; these assert the contract that has
    # to hold either way, and test_memory_v25.py covers the pipeline itself.
    from Core_Cognition import memory_learning

    check(
        "a preference is recognised automatically",
        len(memory_learning.extract("My favourite IDE is VS Code")) == 1
    )
    check(
        "a mood is not recorded as a durable fact",
        all(item.scope == memory_learning.SCOPE_TEMPORARY for item in memory_learning.extract("I'm tired today"))
    )
    check("a question is not captured", memory_learning.extract("What's the weather?") == [])
    check("a command is not captured", memory_learning.extract("Open music") == [])


# ==========================================
# 9. WORKING MEMORY
# ==========================================
def test_working_memory():
    mm, _ = fresh_store()

    mm.capture_working_references("take a look at renderer.js and tell me what it does", "chat_x")
    entries = mm.get_working_entries("chat_x")
    check("a named file becomes a working reference", any("renderer.js" in item["text"] for item in entries), str(entries))

    other = mm.get_working_entries("chat_y")
    check("working memory is per-conversation", not other, str(other))

    mm.note_working("This one expires immediately", "chat_x", ttl_seconds=1)
    # Timestamps are second-resolution, so wait past the stamped second.
    time.sleep(2.2)
    remaining = mm.get_working_entries("chat_x")
    check(
        "expired working memory disappears on its own",
        not any("expires immediately" in item["text"] for item in remaining),
        str(remaining)
    )
    check("unexpired working memory is still there", any("renderer.js" in item["text"] for item in remaining))


# ==========================================
# 10. PERFORMANCE
# ==========================================
def test_performance():
    mm, _ = fresh_store()

    for index in range(200):
        mm.remember(f"Reference fact {index} concerning subsystem {index} and component alpha{index}", importance=2)

    started = time.time()

    for _ in range(50):
        mm.build_memory_context("tell me about subsystem 137")

    per_call_ms = (time.time() - started) * 1000 / 50
    check(
        f"retrieval stays fast ({per_call_ms:.2f} ms per call over 200 memories)",
        per_call_ms < 25,
        f"{per_call_ms:.2f} ms"
    )

    check("a trivial command skips retrieval entirely", mm.should_retrieve("open music") is False)
    check("another trivial command skips retrieval", mm.should_retrieve("play") is False)
    check("a real question does not skip retrieval", mm.should_retrieve("what theme do I prefer") is True)

    started = time.time()

    for _ in range(50):
        mm.build_memory_context("open music")

    trivial_ms = (time.time() - started) * 1000 / 50
    check(
        f"skipped retrieval costs almost nothing ({trivial_ms:.3f} ms per call)",
        trivial_ms < 1.0,
        f"{trivial_ms:.3f} ms"
    )


def main() -> int:
    tests = (
        ("long-term persistence", test_long_term_persistence),
        ("forgetting", test_forget),
        ("chat separation", test_chat_separation),
        ("project isolation", test_project_isolation),
        ("pinning", test_pinning),
        ("duplicates", test_duplicates),
        ("migration", test_migration),
        ("command precedence", test_command_precedence),
        ("working memory", test_working_memory),
        ("performance", test_performance)
    )

    for label, test in tests:
        print(f"\n== {label} ==")

        try:
            test()
        except Exception as error:  # noqa: BLE001 - a crashed test is a failed test
            FAILED.append(label)
            print(f"[FAIL] {label} raised {type(error).__name__}: {error}")

    print("\n" + "=" * 52)
    print(f"Memory v2: {len(PASSED)} passed, {len(FAILED)} failed")

    if FAILED:
        for name in FAILED:
            print(f"  failed: {name}")

    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
