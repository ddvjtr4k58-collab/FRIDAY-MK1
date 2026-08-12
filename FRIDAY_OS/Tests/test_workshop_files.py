"""
Workshop Files test suite.

Offline and deterministic: no Gemini key, no network, no microphone, no
Electron. It exercises the REAL Virtual Finder, because the whole point of the
Workshop Files panel is that it is a second view of the existing store and not a
second store — a stubbed filesystem would prove nothing about that.

It works inside one scratch folder of its own at the Virtual Finder root,
created and removed through the tool's own operations, and it never touches the
five protected default folders or anything already filed in them.

Run from FRIDAY_OS:

    python3 -m Tests.test_workshop_files

Covered:
  1. the root lists the default folders the Workshop panel shows
  2. nested folders list, and report a parent to navigate back to
  3. text and code files come back through preview, read-only
  4. formats FRIDAY cannot render are marked, not guessed at
  5. path traversal and absolute paths are refused
  6. every virtual_finder_* event the renderer emits has a handler
"""

import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
FRIDAY_ROOT = HERE.parent

if str(FRIDAY_ROOT) not in sys.path:
    sys.path.insert(0, str(FRIDAY_ROOT))

PASSED = []
FAILED = []

SCRATCH = f"Workshop Files Suite {os.getpid()}"


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        PASSED.append(name)
        print(f"[PASS] {name}")
    else:
        FAILED.append(name)
        print(f"[FAIL] {name}{(' — ' + detail) if detail else ''}")


def run(operation: str, **payload) -> dict:
    from Sensory_Array.file_tools import perform_virtual_finder_operation

    return perform_virtual_finder_operation(operation, payload)


def data_of(result: dict) -> dict:
    value = result.get("data")
    return value if isinstance(value, dict) else {}


def names(result: dict) -> set:
    return {str(item.get("name") or "") for item in data_of(result).get("items", [])}


# ==========================================================
# 1. THE ROOT
# ==========================================================
def test_root_listing() -> None:
    from Sensory_Array.file_tools import DEFAULT_FOLDERS

    result = run("list", path="")
    check("the root folder lists", result.get("ok") is True, str(result.get("message")))

    listed = names(result)
    check(
        "the folders the Workshop panel shows are really there",
        set(DEFAULT_FOLDERS) <= listed,
        f"missing {sorted(set(DEFAULT_FOLDERS) - listed)}"
    )

    folders = {
        str(item.get("name")): item
        for item in data_of(result).get("items", [])
        if item.get("type") == "folder"
    }
    check(
        "every default folder is typed as a folder, so it is clickable",
        all(name in folders for name in DEFAULT_FOLDERS),
        str(sorted(set(DEFAULT_FOLDERS) - set(folders)))
    )
    check(
        "each carries the path the panel navigates by",
        all(folders[name].get("path") == name for name in DEFAULT_FOLDERS if name in folders)
    )
    check("the root reports no parent to go up to", data_of(result).get("parent_path") == "")


# ==========================================================
# 2. NESTED FOLDERS AND NAVIGATION
# ==========================================================
def test_nested_navigation() -> None:
    created = run("create_folder", name=SCRATCH, parent="")
    check("a folder can be created to browse into", created.get("ok") is True, str(created.get("message")))

    nested = run("create_folder", name="Inner", parent=SCRATCH)
    check("a folder can be created inside another", nested.get("ok") is True, str(nested.get("message")))

    deeper = run("create_folder", name="Deeper", parent=f"{SCRATCH}/Inner")
    check("nesting continues past one level", deeper.get("ok") is True, str(deeper.get("message")))

    level_one = run("list", path=SCRATCH)
    check("a nested folder lists", level_one.get("ok") is True)
    check("and shows its children", "Inner" in names(level_one), str(names(level_one)))
    check(
        "a folder one level down reports the root as its parent",
        data_of(level_one).get("parent_path") == "",
        repr(data_of(level_one).get("parent_path"))
    )

    level_two = run("list", path=f"{SCRATCH}/Inner")
    check("a folder two levels down lists", level_two.get("ok") is True)
    check("and shows its children", "Deeper" in names(level_two), str(names(level_two)))
    check(
        "and reports the folder above it, which is what Up navigates to",
        data_of(level_two).get("parent_path") == SCRATCH,
        repr(data_of(level_two).get("parent_path"))
    )
    check(
        "the breadcrumb describes where the panel is",
        data_of(level_two).get("breadcrumb") == [SCRATCH, "Inner"],
        str(data_of(level_two).get("breadcrumb"))
    )

    missing = run("list", path=f"{SCRATCH}/Nowhere")
    check("a folder that does not exist is refused, not invented", missing.get("ok") is False)


# ==========================================================
# 3. THE READ-ONLY VIEWER
# ==========================================================
def test_file_viewer() -> None:
    from Sensory_Array.file_tools import _resolve_existing_virtual_path

    samples = {
        "notes.md": "# Plan\n\nWire the Files panel to the real finder.\n",
        "engine.py": "def run():\n    return 'friday'\n",
        "log.txt": "boot ok\n"
    }

    for filename, body in samples.items():
        stem, extension = filename.rsplit(".", 1)
        created = run("create_file", name=stem, parent=SCRATCH, file_type=extension)

        if not created.get("ok"):
            check(f"{filename} can be created", False, str(created.get("message")))
            continue

        # Written directly because the Virtual Finder has no write operation —
        # what is under test here is the READ path the viewer uses.
        path = _resolve_existing_virtual_path(f"{SCRATCH}/{filename}", expected_type="file")
        path.write_text(body, encoding="utf-8")

    listed = {
        str(item.get("name")): item
        for item in data_of(run("list", path=SCRATCH)).get("items", [])
    }
    check(
        "text and code files appear in the listing",
        set(samples) <= set(listed),
        f"missing {sorted(set(samples) - set(listed))}"
    )
    check(
        "and are marked as something the viewer can show",
        all(listed[name].get("previewable") is True for name in samples if name in listed)
    )

    for filename, body in samples.items():
        preview = run("preview", path=f"{SCRATCH}/{filename}")

        if not preview.get("ok"):
            check(f"{filename} opens in the viewer", False, str(preview.get("message")))
            continue

        # The payload nests the reading under `preview`, alongside the item it
        # came from. The viewer unwraps exactly this shape.
        payload = data_of(preview).get("preview") or data_of(preview)
        text = payload.get("text", payload.get("content"))
        check(f"{filename} opens in the viewer with its real contents", text == body, repr(text)[:80])
        check(f"{filename} is reported as text, so the viewer renders it", payload.get("kind") == "text")

    meta = run("metadata", path=f"{SCRATCH}/notes.md")
    item = data_of(meta).get("item", {})
    check("metadata is available for the viewer header", meta.get("ok") is True)
    check("metadata reports the name", item.get("name") == "notes.md", str(item.get("name")))
    check("metadata reports a real size", int(item.get("size") or 0) > 0, str(item.get("size")))


# ==========================================================
# 4. FORMATS FRIDAY CANNOT RENDER
# ==========================================================
def test_unsupported_files() -> None:
    from Sensory_Array.file_tools import _resolve_existing_virtual_path

    created = run("create_file", name="archive", parent=SCRATCH, file_type="empty")
    check("a file with no known type can exist", created.get("ok") is True, str(created.get("message")))

    folder = _resolve_existing_virtual_path(SCRATCH, expected_type="folder")
    binary = folder / "payload.bin"
    binary.write_bytes(b"\x00\x01\x02\x03")

    listed = {
        str(item.get("name")): item
        for item in data_of(run("list", path=SCRATCH)).get("items", [])
    }
    check("an unsupported file still appears in the listing", "payload.bin" in listed, str(sorted(listed)))
    check(
        "and is marked as one the viewer cannot show, rather than being hidden",
        listed.get("payload.bin", {}).get("previewable") is False,
        str(listed.get("payload.bin", {}).get("previewable"))
    )

    meta = run("metadata", path=f"{SCRATCH}/payload.bin")
    check(
        "its metadata is still readable, which is what the panel falls back to",
        meta.get("ok") is True and data_of(meta).get("item", {}).get("name") == "payload.bin"
    )


# ==========================================================
# 5. THE PANEL CANNOT ESCAPE THE VIRTUAL FINDER
# ==========================================================
def test_containment() -> None:
    escapes = (
        "../../../etc",
        "..",
        f"{SCRATCH}/../../..",
        "/etc",
        "/etc/passwd",
        "~/Desktop"
    )
    allowed = [path for path in escapes if run("list", path=path).get("ok") is True]

    check(
        f"{len(escapes)} attempts to browse outside the Virtual Finder are refused",
        not allowed,
        "; ".join(allowed)
    )

    previewed = [
        path for path in ("../../../etc/passwd", "/etc/passwd")
        if run("preview", path=path).get("ok") is True
    ]
    check("and nothing outside it can be read into the viewer", not previewed, "; ".join(previewed))


# ==========================================================
# 6. THE RENDERER'S EVENTS ALL EXIST
# ==========================================================
def test_socket_contract() -> None:
    import re

    from Core_Cognition import state_manager

    for handler in ("handle_virtual_finder_list", "handle_virtual_finder_metadata"):
        check(f"{handler} is registered", hasattr(state_manager, handler))

    renderer = (FRIDAY_ROOT / "Visual_Interface" / "renderer.js").read_text(encoding="utf-8")
    emitted = set(re.findall(r"""socket\.emit\(\s*['"](virtual_finder_[a-z_]+)['"]""", renderer))
    source = (FRIDAY_ROOT / "Core_Cognition" / "state_manager.py").read_text(encoding="utf-8")
    handled = set(re.findall(r"""@socketio\.on\(\s*["'](virtual_finder_[a-z_]+)["']""", source))

    check(
        "every Virtual Finder event the interface emits has a handler",
        emitted <= handled,
        f"unhandled: {sorted(emitted - handled)}"
    )


def cleanup() -> None:
    # Both confirmations are required by design: deletion is not something the
    # Virtual Finder will do on a single unqualified request, and the scratch
    # folder has contents by the time the suite finishes.
    result = run("delete", paths=[SCRATCH], confirmed=True, confirm_non_empty=True)

    if not result.get("ok"):
        print(f"[WARN] scratch folder {SCRATCH!r} left behind: {result.get('message')}")


def main() -> int:
    tests = (
        ("root listing", test_root_listing),
        ("nested navigation", test_nested_navigation),
        ("read-only viewer", test_file_viewer),
        ("unsupported files", test_unsupported_files),
        ("containment", test_containment),
        ("socket contract", test_socket_contract),
    )

    try:
        for label, test in tests:
            print(f"\n== {label} ==")

            try:
                test()
            except Exception as error:  # noqa: BLE001 - a crashed test is a failed test
                FAILED.append(label)
                print(f"[FAIL] {label} raised {type(error).__name__}: {error}")
    finally:
        cleanup()

    print("\n" + "=" * 52)
    print(f"Workshop Files: {len(PASSED)} passed, {len(FAILED)} failed")

    if FAILED:
        for name in FAILED:
            print(f"  failed: {name}")

    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
