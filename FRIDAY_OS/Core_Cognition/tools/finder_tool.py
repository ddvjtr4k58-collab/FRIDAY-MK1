"""Finder — the Virtual Finder, FRIDAY's own file surface."""

from __future__ import annotations

from Core_Cognition.tools import (
    KIND_ACTION,
    KIND_OPEN,
    Capability,
    Slot,
    Tool,
    ToolResult,
    register,
)

_QUERY = r"(?:search|find|look for)\s+(?:my |the )?(?:files?|documents?)?\s*(?:for|called|named)?\s*(?P<value>[\w .'-]+)$"
_FOLDER = r"(?:folder|directory)\s+(?:called|named|for)\s+(?P<value>[\w .'-]+)$"


def _open(_slots) -> ToolResult:
    return ToolResult(speech="", action="open_files", event="acknowledge")


def _search(slots) -> ToolResult:
    from Sensory_Array.file_tools import create_virtual_finder_widget

    query = str(slots.get("query") or "").strip()

    if len(query) < 2:
        return ToolResult(ok=False, speech="")

    create_virtual_finder_widget(search_query=query)

    return ToolResult(
        speech=f"Searching files for {query}.",
        data={"query": query},
        event="acknowledge"
    )


def _create_folder(slots) -> ToolResult:
    from Sensory_Array.file_tools import create_virtual_finder_widget, create_virtual_folder

    name = str(slots.get("name") or "").strip()

    if len(name) < 1:
        return ToolResult(ok=False, speech="")

    result = create_virtual_folder(name)
    create_virtual_finder_widget()

    return ToolResult(speech=str(result or f"Created {name}."), data={"folder": name}, event="acknowledge")


register(Tool(
    name="finder",
    description="The Virtual Finder: browsing, searching and organising FRIDAY's file surface.",
    category="files",
    # No default capability: "files" appears in navigation commands the brain loop
    # already owns ("go back in files", "all files"), and opening the Finder in
    # answer to one of those would be a wrong answer rather than a missing one.
    anchors=("file", "files", "folder", "folders", "finder", "document", "documents", "directory"),
    inputs=("search query", "folder name"),
    outputs=("Virtual Finder widget", "search results"),
    capabilities=(
        Capability(
            name="open_files",
            description="Open the Virtual Finder.",
            handler=_open,
            kind=KIND_OPEN,
            priority=1,
            phrases=(
                "open files",
                "show files",
                "open my files",
                "open the finder",
                "open virtual finder",
                "pull up my files",
                "show me my files",
            ),
            patterns=(r"^(?:open|show|pull up|bring up|launch) (?:me )?(?:the |my )?(?:files?|finder|file manager)\b",),
            cues=("open", "show", "pull up", "bring up", "launch"),
            outputs=("Virtual Finder widget",),
        ),
        Capability(
            name="search_files",
            description="Search the Virtual Finder.",
            handler=_search,
            kind=KIND_ACTION,
            priority=3,
            patterns=(r"^(?:search|find|look for)\b.*\b(?:files?|documents?)\b", r"^search files for\b"),
            cues=("search", "find", "look for"),
            slots=(Slot("query", _QUERY, description="What to search for"),),
            inputs=("search query",),
            outputs=("Virtual Finder search results",),
        ),
        Capability(
            name="create_folder",
            description="Create a folder in the Virtual Finder.",
            handler=_create_folder,
            kind=KIND_ACTION,
            priority=3,
            patterns=(r"^(?:make|create|add)\b.*\b(?:folder|directory)\b",),
            cues=("make", "create", "new folder"),
            slots=(Slot("name", _FOLDER, description="Folder name"),),
            inputs=("folder name",),
            outputs=("created folder",),
        ),
    ),
))
