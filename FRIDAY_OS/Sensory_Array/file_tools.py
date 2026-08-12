import base64
import ctypes
import errno
import json
import os
import re
import secrets
import shutil
import stat
import threading
import unicodedata
from pathlib import Path, PurePosixPath

from Core_Cognition.state_manager import (
    add_hud_card,
    broadcast_to_hud,
    broadcast_state,
    set_workshop_file_manager_open
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
APPROVED_DATA_ROOT = PROJECT_ROOT / "Data"
VIRTUAL_FINDER_ROOT = PROJECT_ROOT / "Data" / "Virtual_Finder"
DEFAULT_FOLDERS = (
    "Private Folder",
    "Meholli Industries",
    "Projects",
    "School",
    "FRIDAY Logs"
)

# Protected root folders that were created under an older name. Renamed in place on
# startup rather than replaced, so nothing already filed away is orphaned into a
# folder the Virtual Finder no longer protects.
RENAMED_DEFAULT_FOLDERS = (
    ("JARVIS Logs", "FRIDAY Logs"),
)
PROTECTED_ROOT_NAME_KEYS = frozenset(
    unicodedata.normalize("NFC", name).casefold()
    for name in DEFAULT_FOLDERS
)

FILE_OPERATION_LOCK = threading.RLock()
MAX_PREVIEW_BYTES = 512 * 1024
MAX_SEARCH_RESULTS = 200
MAX_TREE_DEPTH = 8
MAX_TREE_NODES = 800
MAX_TRANSFER_ITEMS = 5000
MAX_TRANSFER_BYTES = 1024 * 1024 * 1024
MAX_OPERATION_PATHS = 50

CREATE_FILE_TYPES = {
    "txt": (".txt", ""),
    "md": (".md", ""),
    "json": (".json", "{}\n"),
    "py": (".py", ""),
    "js": (".js", ""),
    "empty": ("", "")
}
TEXT_PREVIEW_EXTENSIONS = {
    ".txt",
    ".md",
    ".json",
    ".py",
    ".js",
    ".css",
    ".csv",
    ".log",
    ".toml",
    ".yaml",
    ".yml"
}
IMAGE_PREVIEW_MIME_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp"
}
MIME_TYPES = {
    **{extension: "text/plain" for extension in TEXT_PREVIEW_EXTENSIONS},
    ".json": "application/json",
    ".md": "text/markdown",
    ".py": "text/x-python",
    ".js": "text/javascript",
    ".css": "text/css",
    ".csv": "text/csv",
    **IMAGE_PREVIEW_MIME_TYPES
}
BLOCKED_SECRET_EXTENSIONS = {
    ".key",
    ".pem",
    ".p12",
    ".pfx",
    ".cer",
    ".crt",
    ".mobileprovision"
}
BLOCKED_NAME_TOKENS = {
    "credential",
    "credentials",
    "env",
    "secret",
    "secrets",
    "token",
    "tokens"
}
CONTROL_OR_BIDI_RE = re.compile(r"[\x00-\x1f\x7f-\x9f\u202a-\u202e\u2066-\u2069]")


class VirtualFinderError(Exception):
    def __init__(self, code: str, message: str, data=None):
        super().__init__(message)
        self.code = str(code or "operation_failed")
        self.message = str(message or "Virtual Finder operation failed.")
        self.data = data if isinstance(data, dict) else {}

    def as_result(self) -> dict:
        return _result(False, self.code, self.message, self.data)


def _result(ok: bool, code: str, message: str, data=None) -> dict:
    return {
        "ok": bool(ok),
        "code": str(code or ("ok" if ok else "operation_failed")),
        "message": str(message or ""),
        "data": data if isinstance(data, dict) else {}
    }


def _fail(code: str, message: str, data=None):
    raise VirtualFinderError(code, message, data)


def _is_sensitive_name(name: str) -> bool:
    value = unicodedata.normalize("NFC", str(name or "")).strip()
    lowered = value.casefold()

    if not lowered or lowered.startswith("."):
        return True

    if Path(lowered).suffix in BLOCKED_SECRET_EXTENSIONS:
        return True

    normalized = re.sub(r"[^a-z0-9]+", "_", lowered).strip("_")
    tokens = {token for token in normalized.split("_") if token}

    if tokens.intersection(BLOCKED_NAME_TOKENS):
        return True

    if "private" in tokens and "key" in tokens:
        return True

    if "api" in tokens and "key" in tokens:
        return True

    if tokens.intersection({"apikey", "privatekey"}):
        return True

    if normalized in {"apikey", "privatekey", "id_rsa", "id_ed25519", "known_hosts"}:
        return True

    return False


def _validate_entry_name(name: str) -> str:
    value = unicodedata.normalize("NFC", str(name or ""))

    if not value or value != value.strip():
        _fail("invalid_name", "Enter a name without leading or trailing spaces.")

    if value in {".", ".."} or value.startswith("."):
        _fail("invalid_name", "Hidden and relative names are not available.")

    if "/" in value or "\\" in value or ":" in value:
        _fail("invalid_name", "Names cannot contain path separators or colons.")

    if CONTROL_OR_BIDI_RE.search(value):
        _fail("invalid_name", "Names cannot contain control characters.")

    if value.endswith((" ", ".")):
        _fail("invalid_name", "Names cannot end with a space or period.")

    if len(value.encode("utf-8")) > 255:
        _fail("invalid_name", "That name is too long.")

    if _is_sensitive_name(value):
        _fail("sensitive_name", "Sensitive credential and secret names are blocked.")

    return value


def _name_key(name: str) -> str:
    return unicodedata.normalize("NFC", str(name or "")).casefold()


def _matching_child_names(parent: Path, requested_name: str) -> list:
    requested_key = _name_key(requested_name)

    try:
        with os.scandir(parent) as entries:
            return [entry.name for entry in entries if _name_key(entry.name) == requested_key]
    except OSError:
        _fail("unavailable", "That virtual folder cannot be read.")


def _resolved_child_component(parent: Path, requested_name: str) -> Path:
    matches = _matching_child_names(parent, requested_name)

    if requested_name in matches:
        return parent / requested_name

    if len(matches) == 1:
        return parent / matches[0]

    if len(matches) > 1:
        _fail("ambiguous_path", "That virtual path has ambiguous name casing.")

    return parent / requested_name


def _stat_identity(item_stat) -> tuple:
    return (
        int(item_stat.st_dev),
        int(item_stat.st_ino),
        int(stat.S_IFMT(item_stat.st_mode))
    )


def _path_identity(path: Path) -> tuple:
    try:
        return _stat_identity(os.lstat(path))
    except OSError:
        _fail("unavailable", "That virtual item is unavailable.")


def _path_matches_identity(path: Path, expected_identity: tuple) -> bool:
    try:
        return _stat_identity(os.lstat(path)) == expected_identity
    except OSError:
        return False


def _require_path_identity(path: Path, expected_identity: tuple) -> None:
    if not _path_matches_identity(path, expected_identity):
        _fail("source_changed", "A virtual item changed during the operation.")


def _open_anchored_directory(path: Path) -> tuple:
    try:
        path_stat = os.lstat(path)
    except OSError:
        _fail("unavailable", "A virtual folder is unavailable.")

    if stat.S_ISLNK(path_stat.st_mode) or not stat.S_ISDIR(path_stat.st_mode):
        _fail("symlink_blocked", "Linked filesystem locations are blocked.")

    flags = os.O_RDONLY

    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC

    try:
        descriptor = os.open(path, flags)
    except OSError:
        _fail("unavailable", "A virtual folder is unavailable.")

    if _stat_identity(os.fstat(descriptor)) != _stat_identity(path_stat):
        os.close(descriptor)
        _fail("source_changed", "A virtual folder changed during the operation.")

    return descriptor, _stat_identity(path_stat)


def _rename_noreplace(source: Path, target: Path) -> None:
    """Atomically rename one item without following or replacing target paths."""
    source_parent_fd, _ = _open_anchored_directory(source.parent)
    target_parent_fd = None

    try:
        target_parent_fd, _ = _open_anchored_directory(target.parent)
        libc = ctypes.CDLL(None, use_errno=True)
        result = -1

        if hasattr(libc, "renameatx_np"):
            renameatx_np = libc.renameatx_np
            renameatx_np.argtypes = [
                ctypes.c_int,
                ctypes.c_char_p,
                ctypes.c_int,
                ctypes.c_char_p,
                ctypes.c_uint
            ]
            renameatx_np.restype = ctypes.c_int
            rename_exclusive = 0x00000004
            rename_nofollow_any = 0x00000010
            rename_resolve_beneath = 0x00000020
            result = renameatx_np(
                source_parent_fd,
                os.fsencode(source.name),
                target_parent_fd,
                os.fsencode(target.name),
                rename_exclusive | rename_nofollow_any | rename_resolve_beneath
            )
        elif hasattr(libc, "renameat2"):
            renameat2 = libc.renameat2
            renameat2.argtypes = [
                ctypes.c_int,
                ctypes.c_char_p,
                ctypes.c_int,
                ctypes.c_char_p,
                ctypes.c_uint
            ]
            renameat2.restype = ctypes.c_int
            result = renameat2(
                source_parent_fd,
                os.fsencode(source.name),
                target_parent_fd,
                os.fsencode(target.name),
                0x00000001
            )
        else:
            _fail(
                "operation_unavailable",
                "Safe no-replace filesystem operations are unavailable on this Mac."
            )

        if result != 0:
            error_number = ctypes.get_errno()

            if error_number in {errno.EEXIST, errno.ENOTEMPTY}:
                _fail("already_exists", "An item with that name already exists.")
            if error_number in {errno.ELOOP}:
                _fail("symlink_blocked", "Linked filesystem locations are blocked.")
            if error_number in {errno.ENOENT}:
                _fail("source_changed", "A virtual item changed during the operation.")
            if error_number in {errno.EXDEV}:
                _fail("path_blocked", "Mounted or external locations are blocked.")
            if error_number in {errno.EINVAL, errno.ENOSYS, errno.ENOTSUP}:
                _fail(
                    "operation_unavailable",
                    "Safe no-replace filesystem operations are unavailable on this Mac."
                )

            _fail("unavailable", "The virtual item could not be moved safely.")
    finally:
        if target_parent_fd is not None:
            os.close(target_parent_fd)
        os.close(source_parent_fd)


def ensure_virtual_finder_root() -> None:
    try:
        project_root = PROJECT_ROOT.resolve(strict=True)
    except Exception:
        _fail("path_blocked", "Virtual storage is unavailable.")

    if os.path.lexists(APPROVED_DATA_ROOT):
        if APPROVED_DATA_ROOT.is_symlink() or not APPROVED_DATA_ROOT.is_dir():
            _fail("path_blocked", "Virtual storage is unavailable.")
    else:
        APPROVED_DATA_ROOT.mkdir(mode=0o700)

    try:
        approved_data_root = APPROVED_DATA_ROOT.resolve(strict=True)
        approved_data_root.relative_to(project_root)
    except Exception:
        _fail("path_blocked", "Virtual storage is unavailable.")

    if os.lstat(approved_data_root).st_dev != os.lstat(project_root).st_dev:
        _fail("path_blocked", "Mounted or external storage roots are blocked.")

    try:
        VIRTUAL_FINDER_ROOT.relative_to(APPROVED_DATA_ROOT)
    except Exception:
        _fail("path_blocked", "Virtual storage is unavailable.")

    if os.path.lexists(VIRTUAL_FINDER_ROOT) and VIRTUAL_FINDER_ROOT.is_symlink():
        _fail("path_blocked", "Virtual storage is unavailable.")

    VIRTUAL_FINDER_ROOT.mkdir(parents=True, exist_ok=True)

    if not VIRTUAL_FINDER_ROOT.is_dir():
        _fail("path_blocked", "Virtual storage is unavailable.")

    for old_name, new_name in RENAMED_DEFAULT_FOLDERS:
        old_path = VIRTUAL_FINDER_ROOT / old_name
        new_path = VIRTUAL_FINDER_ROOT / new_name

        # Only ever a rename onto free ground: if both exist the two are left alone
        # for the user to reconcile, since merging them could lose a file.
        if (
            os.path.lexists(old_path)
            and not os.path.lexists(new_path)
            and not old_path.is_symlink()
            and old_path.is_dir()
        ):
            try:
                old_path.rename(new_path)
                print(f"[Virtual Finder: renamed '{old_name}' to '{new_name}']")
            except Exception as error:
                print(f"[Virtual Finder: could not rename '{old_name}': {error}]")

    for folder in DEFAULT_FOLDERS:
        target = VIRTUAL_FINDER_ROOT / folder

        if os.path.lexists(target):
            if target.is_symlink() or not target.is_dir():
                _fail("path_blocked", "A protected virtual location is unavailable.")
            continue

        target.mkdir(mode=0o700)


def _virtual_root() -> Path:
    ensure_virtual_finder_root()

    try:
        root = VIRTUAL_FINDER_ROOT.resolve(strict=True)
    except Exception:
        _fail("path_blocked", "Virtual storage is unavailable.")

    if VIRTUAL_FINDER_ROOT.is_symlink() or not root.is_dir():
        _fail("path_blocked", "Virtual storage is unavailable.")

    try:
        approved_data_root = APPROVED_DATA_ROOT.resolve(strict=True)
        root.relative_to(approved_data_root)
    except Exception:
        _fail("path_blocked", "Virtual storage is unavailable.")

    if os.lstat(root).st_dev != os.lstat(approved_data_root).st_dev:
        _fail("path_blocked", "Mounted or external storage roots are blocked.")

    return root


def _parse_virtual_path(path=None, allow_root: bool = True) -> tuple:
    raw_path = "" if path is None else str(path)

    if not raw_path:
        if allow_root:
            return ()
        _fail("path_required", "A virtual path is required.")

    if raw_path != raw_path.strip():
        _fail("invalid_path", "That virtual path is invalid.")

    if "\\" in raw_path or CONTROL_OR_BIDI_RE.search(raw_path):
        _fail("invalid_path", "That virtual path is invalid.")

    pure_path = PurePosixPath(raw_path)

    if pure_path.is_absolute() or raw_path.startswith("/"):
        _fail("path_blocked", "Only virtual storage paths are available.")

    parts = raw_path.split("/")

    if any(not part or part in {".", ".."} for part in parts):
        _fail("path_blocked", "Relative and traversal paths are blocked.")

    return tuple(_validate_entry_name(part) for part in parts)


def _assert_contained(path: Path, root: Path) -> None:
    try:
        path.relative_to(root)
    except Exception:
        _fail("path_blocked", "That path is outside virtual storage.")


def _resolve_existing_virtual_path(
    path=None,
    *,
    allow_root: bool = True,
    expected_type: str = "any"
) -> Path:
    root = _virtual_root()
    parts = _parse_virtual_path(path, allow_root=allow_root)
    candidate = root
    root_device = os.lstat(root).st_dev

    for part in parts:
        candidate = _resolved_child_component(candidate, part)

        try:
            item_stat = os.lstat(candidate)
        except FileNotFoundError:
            _fail("not_found", "That virtual item no longer exists.")
        except OSError:
            _fail("unavailable", "That virtual item is unavailable.")

        if stat.S_ISLNK(item_stat.st_mode):
            _fail("symlink_blocked", "Linked filesystem items are blocked.")

        if item_stat.st_dev != root_device:
            _fail("path_blocked", "Mounted or external locations are blocked.")

        try:
            resolved = candidate.resolve(strict=True)
        except Exception:
            _fail("unavailable", "That virtual item is unavailable.")

        _assert_contained(resolved, root)

    try:
        item_stat = os.lstat(candidate)
    except OSError:
        _fail("unavailable", "That virtual item is unavailable.")

    if stat.S_ISLNK(item_stat.st_mode):
        _fail("symlink_blocked", "Linked filesystem items are blocked.")

    if expected_type == "folder" and not stat.S_ISDIR(item_stat.st_mode):
        _fail("not_directory", "That virtual path is not a folder.")

    if expected_type == "file" and not stat.S_ISREG(item_stat.st_mode):
        _fail("not_file", "That virtual path is not a regular file.")

    if expected_type == "any" and not (
        stat.S_ISDIR(item_stat.st_mode) or stat.S_ISREG(item_stat.st_mode)
    ):
        _fail("unsupported_item", "That filesystem item is not supported.")

    return candidate


def _resolve_new_virtual_target(parent=None, name: str = "") -> tuple:
    parent_path = _resolve_existing_virtual_path(parent, expected_type="folder")
    clean_name = _validate_entry_name(name)
    target = parent_path / clean_name
    root = _virtual_root()
    _assert_contained(target, root)

    if parent_path == root and _name_key(clean_name) in PROTECTED_ROOT_NAME_KEYS:
        _fail("protected_item", "Protected virtual locations cannot be replaced.")

    if _matching_child_names(parent_path, clean_name):
        _fail("already_exists", "An item with that name already exists.")

    return parent_path, target, clean_name


def _relative_display_path(path: Path) -> str:
    root = _virtual_root()
    _assert_contained(path, root)
    relative = path.relative_to(root).as_posix()
    return "" if relative == "." else relative


def _is_protected_path(path: Path) -> bool:
    relative_path = _relative_display_path(path)
    parts = relative_path.split("/") if relative_path else []
    return not parts or (
        len(parts) == 1
        and _name_key(parts[0]) in PROTECTED_ROOT_NAME_KEYS
    )


def _safe_child_paths(folder: Path) -> list:
    root = _virtual_root()
    root_device = os.lstat(root).st_dev
    children = []

    try:
        candidates = list(folder.iterdir())
    except OSError:
        _fail("unavailable", "That virtual folder cannot be read.")

    for child in candidates:
        try:
            _validate_entry_name(child.name)
            item_stat = os.lstat(child)
        except VirtualFinderError:
            continue
        except OSError:
            continue

        if stat.S_ISLNK(item_stat.st_mode) or item_stat.st_dev != root_device:
            continue

        if not (stat.S_ISDIR(item_stat.st_mode) or stat.S_ISREG(item_stat.st_mode)):
            continue

        try:
            _assert_contained(child.resolve(strict=True), root)
        except VirtualFinderError:
            continue
        except Exception:
            continue

        children.append(child)

    return sorted(
        children,
        key=lambda entry: (
            not stat.S_ISDIR(os.lstat(entry).st_mode),
            entry.name.casefold()
        )
    )


def _item_mime_type(path: Path) -> str:
    if path.is_dir():
        return "inode/directory"

    return MIME_TYPES.get(path.suffix.casefold(), "application/octet-stream")


def _serialize_virtual_item(path: Path) -> dict:
    try:
        item_stat = os.lstat(path)
    except OSError:
        _fail("metadata_unavailable", "Item metadata is unavailable.")

    if stat.S_ISLNK(item_stat.st_mode):
        _fail("symlink_blocked", "Linked filesystem items are blocked.")

    is_folder = stat.S_ISDIR(item_stat.st_mode)
    is_file = stat.S_ISREG(item_stat.st_mode)

    if not (is_folder or is_file):
        _fail("unsupported_item", "That filesystem item is not supported.")

    extension = "" if is_folder else path.suffix.casefold()
    previewable = bool(
        is_file
        and item_stat.st_size <= MAX_PREVIEW_BYTES
        and extension in TEXT_PREVIEW_EXTENSIONS.union(IMAGE_PREVIEW_MIME_TYPES)
        and not _is_sensitive_name(path.name)
    )
    item_count = len(_safe_child_paths(path)) if is_folder else 0

    if is_folder:
        kind = "folder"
    elif extension in IMAGE_PREVIEW_MIME_TYPES:
        kind = "image"
    elif extension == ".json":
        kind = "json"
    elif extension == ".md":
        kind = "markdown"
    elif extension == ".py":
        kind = "python"
    elif extension == ".js":
        kind = "javascript"
    elif extension == ".log":
        kind = "log"
    elif extension in TEXT_PREVIEW_EXTENSIONS:
        kind = "text"
    else:
        kind = "file"

    return {
        "name": path.name,
        "path": _relative_display_path(path),
        "type": "folder" if is_folder else "file",
        "kind": kind,
        "extension": extension,
        "mime_type": _item_mime_type(path),
        "size": item_stat.st_size if is_file else 0,
        "modified": item_stat.st_mtime,
        "created": getattr(item_stat, "st_birthtime", item_stat.st_ctime),
        "item_count": item_count,
        "previewable": previewable,
        "protected": _is_protected_path(path)
    }


def list_virtual_items(path=None) -> list:
    with FILE_OPERATION_LOCK:
        folder = _resolve_existing_virtual_path(path, expected_type="folder")
        items = []

        for child in _safe_child_paths(folder):
            try:
                items.append(_serialize_virtual_item(child))
            except VirtualFinderError:
                continue

        return items


def _storage_summary() -> tuple:
    root = _virtual_root()
    category_totals = {
        folder: {
            "item_count": 0,
            "file_count": 0,
            "folder_count": 0,
            "total_bytes": 0
        }
        for folder in DEFAULT_FOLDERS
    }
    totals = {
        "item_count": 0,
        "file_count": 0,
        "folder_count": 0,
        "total_bytes": 0
    }
    stack = [root]

    while stack:
        folder = stack.pop()

        for child in _safe_child_paths(folder):
            item_stat = os.lstat(child)
            is_folder = stat.S_ISDIR(item_stat.st_mode)
            relative_parts = _relative_display_path(child).split("/")
            category = relative_parts[0] if relative_parts else ""
            totals["item_count"] += 1

            if is_folder:
                totals["folder_count"] += 1
                stack.append(child)
            else:
                totals["file_count"] += 1
                totals["total_bytes"] += item_stat.st_size

            category_total = category_totals.get(category)

            if category_total is None or len(relative_parts) == 1:
                continue

            category_total["item_count"] += 1

            if is_folder:
                category_total["folder_count"] += 1
            else:
                category_total["file_count"] += 1
                category_total["total_bytes"] += item_stat.st_size

    return totals, category_totals


def _folder_tree(path=None, max_depth: int = MAX_TREE_DEPTH, max_nodes: int = MAX_TREE_NODES) -> tuple:
    root_folder = _resolve_existing_virtual_path(path, expected_type="folder")
    node_count = 0
    truncated = False

    def build(folder: Path, depth: int):
        nonlocal node_count, truncated
        nodes = []

        if depth >= max_depth:
            return nodes

        for child in _safe_child_paths(folder):
            if not stat.S_ISDIR(os.lstat(child).st_mode):
                continue

            if node_count >= max_nodes:
                truncated = True
                break

            node_count += 1
            children = _safe_child_paths(child)
            folder_children = [
                item for item in children
                if stat.S_ISDIR(os.lstat(item).st_mode)
            ]
            nodes.append({
                "name": child.name,
                "path": _relative_display_path(child),
                "item_count": len(children),
                "has_children": bool(folder_children),
                "protected": _is_protected_path(child),
                "children": build(child, depth + 1)
            })

            if truncated:
                break

        return nodes

    return build(root_folder, 0), truncated


def get_virtual_finder_payload(path=None) -> dict:
    with FILE_OPERATION_LOCK:
        current = _resolve_existing_virtual_path(path, expected_type="folder")
        relative_path = _relative_display_path(current)
        parent_path = ""

        if relative_path:
            parent_path = _relative_display_path(current.parent)

        totals, category_totals = _storage_summary()
        tree, tree_truncated = _folder_tree("", MAX_TREE_DEPTH, MAX_TREE_NODES)

        return {
            "root": "Virtual Finder",
            "current_path": relative_path,
            "parent_path": parent_path,
            "breadcrumb": [part for part in relative_path.split("/") if part],
            "sidebar": [
                {
                    "name": folder,
                    "path": folder,
                    "count": category_totals[folder]["item_count"],
                    "protected": True,
                    **category_totals[folder]
                }
                for folder in DEFAULT_FOLDERS
            ],
            "items": list_virtual_items(relative_path),
            "folder_tree": tree,
            "folder_tree_truncated": tree_truncated,
            "storage": totals
        }


def get_virtual_desktop_payload() -> dict:
    with FILE_OPERATION_LOCK:
        ensure_virtual_finder_root()
        totals, category_totals = _storage_summary()

        return {
            "root": "Virtual Finder",
            "current_path": "",
            "items": list_virtual_items(""),
            "default_folders": list(DEFAULT_FOLDERS),
            "storage": totals,
            "sidebar_counts": {
                folder: category_totals[folder]["item_count"]
                for folder in DEFAULT_FOLDERS
            }
        }


def broadcast_virtual_desktop_payload() -> None:
    broadcast_to_hud("virtual_desktop_update", get_virtual_desktop_payload())


def search_virtual_items(query: str, path=None) -> list:
    with FILE_OPERATION_LOCK:
        base_folder = _resolve_existing_virtual_path(path, expected_type="folder")
        needle = re.sub(r"\s+", " ", str(query or "").casefold()).strip()

        if not needle:
            return []

        results = []
        stack = [base_folder]

        while stack and len(results) < MAX_SEARCH_RESULTS:
            folder = stack.pop()

            for child in _safe_child_paths(folder):
                item_stat = os.lstat(child)
                is_folder = stat.S_ISDIR(item_stat.st_mode)

                if is_folder:
                    stack.append(child)

                relative_path = _relative_display_path(child)
                haystack = f"{child.name} {relative_path}".casefold()

                if needle not in haystack:
                    continue

                results.append(_serialize_virtual_item(child))

                if len(results) >= MAX_SEARCH_RESULTS:
                    break

        return sorted(
            results,
            key=lambda entry: (
                entry.get("type") != "folder",
                entry.get("name", "").casefold()
            )
        )


def _assert_safe_transfer_tree(path: Path) -> dict:
    root = _virtual_root()
    root_device = os.lstat(root).st_dev
    totals = {"item_count": 0, "total_bytes": 0}
    stack = [path]

    while stack:
        current = stack.pop()

        try:
            item_stat = os.lstat(current)
        except OSError:
            _fail("unavailable", "A selected virtual item is unavailable.")

        if stat.S_ISLNK(item_stat.st_mode):
            _fail("symlink_blocked", "Linked filesystem items cannot be transferred.")

        if item_stat.st_dev != root_device:
            _fail("path_blocked", "Mounted or external locations cannot be transferred.")

        try:
            resolved_current = current.resolve(strict=True)
        except OSError:
            _fail("unavailable", "A selected virtual item is unavailable.")

        _assert_contained(resolved_current, root)

        if current != path:
            _validate_entry_name(current.name)

        totals["item_count"] += 1

        if totals["item_count"] > MAX_TRANSFER_ITEMS:
            _fail("transfer_too_large", "That selection contains too many items.")

        if stat.S_ISDIR(item_stat.st_mode):
            try:
                stack.extend(current.iterdir())
            except OSError:
                _fail("unavailable", "A selected virtual folder cannot be read.")
        elif stat.S_ISREG(item_stat.st_mode):
            totals["total_bytes"] += item_stat.st_size

            if totals["total_bytes"] > MAX_TRANSFER_BYTES:
                _fail("transfer_too_large", "That selection is too large to transfer.")
        else:
            _fail("unsupported_item", "That selection contains an unsupported item.")

    return totals


def _operation_paths(payload: dict, *, protect: bool = True) -> list:
    raw_paths = payload.get("paths") if isinstance(payload, dict) else []

    if isinstance(raw_paths, str):
        raw_paths = [raw_paths]

    if not isinstance(raw_paths, list) or not raw_paths:
        _fail("selection_required", "Select at least one virtual item.")

    if len(raw_paths) > MAX_OPERATION_PATHS:
        _fail("selection_too_large", "Too many items are selected.")

    resolved = []
    seen = set()

    for raw_path in raw_paths:
        path = _resolve_existing_virtual_path(raw_path, allow_root=False)
        relative_path = _relative_display_path(path)
        relative_key = _name_key(relative_path)

        if relative_key in seen:
            continue

        if protect and _is_protected_path(path):
            _fail("protected_item", "Protected virtual locations cannot be changed.")

        seen.add(relative_key)
        resolved.append(path)

    return resolved


def _existing_refresh_path(value, fallback: Path) -> str:
    if value is not None:
        try:
            current = _resolve_existing_virtual_path(value, expected_type="folder")
            return _relative_display_path(current)
        except VirtualFinderError:
            pass

    return _relative_display_path(fallback)


def _op_create_folder(payload: dict) -> tuple:
    parent, target, _ = _resolve_new_virtual_target(
        payload.get("parent") or "",
        payload.get("name") or ""
    )
    target.mkdir(mode=0o700, exist_ok=False)
    return "created", "Folder created.", {
        "item": _serialize_virtual_item(target),
        "refresh_path": _relative_display_path(parent)
    }


def _op_create_file(payload: dict) -> tuple:
    file_type = str(payload.get("file_type") or "empty").strip().casefold()

    if file_type not in CREATE_FILE_TYPES:
        _fail("unsupported_file_type", "Choose a supported safe file type.")

    requested_name = _validate_entry_name(payload.get("name") or "")
    extension, initial_content = CREATE_FILE_TYPES[file_type]

    if extension and not requested_name.casefold().endswith(extension):
        requested_name = _validate_entry_name(f"{requested_name}{extension}")

    parent, target, _ = _resolve_new_virtual_target(
        payload.get("parent") or "",
        requested_name
    )
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL

    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW

    descriptor = os.open(target, flags, 0o600)
    created_identity = _stat_identity(os.fstat(descriptor))

    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as file_handle:
            file_handle.write(initial_content)
    except Exception:
        try:
            if _path_matches_identity(target, created_identity):
                target.unlink()
        except Exception:
            pass
        raise

    return "created", "File created.", {
        "item": _serialize_virtual_item(target),
        "refresh_path": _relative_display_path(parent)
    }


def _op_rename(payload: dict) -> tuple:
    source = _resolve_existing_virtual_path(payload.get("path"), allow_root=False)

    if _is_protected_path(source):
        _fail("protected_item", "Protected virtual locations cannot be renamed.")

    _assert_safe_transfer_tree(source)
    new_name = _validate_entry_name(payload.get("new_name") or "")

    if new_name == source.name:
        _fail("same_name", "Choose a different name.")

    target = source.parent / new_name
    root = _virtual_root()
    old_path = _relative_display_path(source)
    source_identity = _path_identity(source)

    if source.parent == root and _name_key(new_name) in PROTECTED_ROOT_NAME_KEYS:
        _fail("protected_item", "Protected virtual locations cannot be replaced.")

    if _matching_child_names(source.parent, new_name):
        _fail("already_exists", "An item with that name already exists.")

    renamed = False

    try:
        _rename_noreplace(source, target)
        renamed = True
        _require_path_identity(target, source_identity)
        _require_unique_destination_name(target.parent, target.name)
        _assert_safe_transfer_tree(target)
    except Exception:
        if (
            renamed
            and _path_matches_identity(target, source_identity)
            and not os.path.lexists(source)
        ):
            try:
                _rename_noreplace(target, source)
            except Exception:
                pass
        raise

    refresh_path = _existing_refresh_path(payload.get("current_path"), target.parent)
    return "renamed", "Item renamed.", {
        "old_path": old_path,
        "item": _serialize_virtual_item(target),
        "refresh_path": refresh_path
    }


def _collapse_delete_paths(paths: list) -> list:
    selected = []

    for path in sorted(paths, key=lambda item: len(item.parts)):
        if any(parent == path or parent in path.parents for parent in selected):
            continue
        selected.append(path)

    return selected


def _op_delete(payload: dict) -> tuple:
    paths = _collapse_delete_paths(_operation_paths(payload))
    preflight = []
    path_identities = []
    contains_non_empty = False

    for path in paths:
        _assert_safe_transfer_tree(path)
        path_identity = _path_identity(path)
        item = _serialize_virtual_item(path)
        _require_path_identity(path, path_identity)
        is_non_empty = item["type"] == "folder" and item["item_count"] > 0
        contains_non_empty = contains_non_empty or is_non_empty
        path_identities.append((path, path_identity))
        preflight.append({
            "name": item["name"],
            "path": item["path"],
            "type": item["type"],
            "item_count": item["item_count"],
            "non_empty": is_non_empty
        })

    if payload.get("confirmed") is not True:
        _fail(
            "confirmation_required",
            "Confirm deletion before removing selected items.",
            {"items": preflight, "contains_non_empty": contains_non_empty}
        )

    if contains_non_empty and payload.get("confirm_non_empty") is not True:
        _fail(
            "non_empty_confirmation_required",
            "Confirm deletion of non-empty folders.",
            {"items": preflight, "contains_non_empty": True}
        )

    fallback = paths[0].parent
    deleted_paths = [_relative_display_path(path) for path in paths]
    staging_path, staging_identity = _create_private_staging_directory("delete")
    quarantined = []

    try:
        for index, (path, expected_identity) in enumerate(path_identities):
            _require_path_identity(path, expected_identity)
            quarantine_target = staging_path / f"item-{index}"
            _rename_noreplace(path, quarantine_target)
            moved_identity = _path_identity(quarantine_target)
            quarantined.append((path, quarantine_target, moved_identity))

            if moved_identity != expected_identity:
                _fail("source_changed", "A selected item changed before deletion.")

            _assert_safe_transfer_tree(quarantine_target)

        for _, quarantine_target, expected_identity in quarantined:
            _remove_owned_transfer_target(quarantine_target, expected_identity)

            if os.path.lexists(quarantine_target):
                _fail("source_changed", "A selected item changed during deletion.")
    except Exception:
        for original_path, quarantine_target, _ in reversed(quarantined):
            try:
                if os.path.lexists(quarantine_target) and not os.path.lexists(original_path):
                    _rename_noreplace(quarantine_target, original_path)
            except Exception:
                pass

        _remove_empty_owned_staging(staging_path, staging_identity)
        raise

    _remove_empty_owned_staging(staging_path, staging_identity)

    refresh_path = _existing_refresh_path(payload.get("current_path"), fallback)
    return "deleted", "Selected items deleted.", {
        "deleted": deleted_paths,
        "refresh_path": refresh_path
    }


def _remove_owned_transfer_target(path: Path, expected_identity: tuple) -> None:
    if not _path_matches_identity(path, expected_identity):
        return

    item_stat = os.lstat(path)

    if stat.S_ISLNK(item_stat.st_mode):
        path.unlink()
    elif stat.S_ISDIR(item_stat.st_mode):
        shutil.rmtree(path)
    else:
        path.unlink()


def _strip_file_execute_bits(path: Path) -> None:
    stack = [path]

    while stack:
        current = stack.pop()
        item_stat = os.lstat(current)

        if stat.S_ISDIR(item_stat.st_mode):
            stack.extend(current.iterdir())
        elif stat.S_ISREG(item_stat.st_mode):
            os.chmod(current, stat.S_IMODE(item_stat.st_mode) & ~0o111)


def _assert_destination_name_available(destination: Path, name: str) -> None:
    root = _virtual_root()

    if destination == root and _name_key(name) in PROTECTED_ROOT_NAME_KEYS:
        _fail("protected_item", "Protected virtual locations cannot be replaced.")

    if _matching_child_names(destination, name):
        _fail("already_exists", "The destination already contains one of those names.")


def _require_unique_destination_name(destination: Path, name: str) -> None:
    if len(_matching_child_names(destination, name)) != 1:
        _fail("already_exists", "The destination name changed during the operation.")


def _create_private_staging_directory(purpose: str) -> tuple:
    root = _virtual_root()
    root_fd, root_identity = _open_anchored_directory(root)
    staging_name = None
    staging_identity = None

    try:
        for _ in range(16):
            candidate_name = f".vf-{purpose}-{secrets.token_hex(12)}"

            try:
                os.mkdir(candidate_name, mode=0o700, dir_fd=root_fd)
                staging_name = candidate_name
                break
            except FileExistsError:
                continue
            except OSError:
                _fail("unavailable", "A safe operation staging area could not be created.")

        if staging_name is None:
            _fail("unavailable", "A safe operation staging area could not be created.")

        anchored_stat = os.stat(staging_name, dir_fd=root_fd, follow_symlinks=False)
        staging_identity = _stat_identity(anchored_stat)
        staging_path = root / staging_name

        if (
            not stat.S_ISDIR(anchored_stat.st_mode)
            or not _path_matches_identity(root, root_identity)
            or not _path_matches_identity(staging_path, staging_identity)
        ):
            try:
                os.rmdir(staging_name, dir_fd=root_fd)
            except OSError:
                pass
            _fail("source_changed", "Virtual storage changed during the operation.")

        try:
            resolved_staging = staging_path.resolve(strict=True)
        except OSError:
            _fail("unavailable", "A safe operation staging area is unavailable.")

        _assert_contained(resolved_staging, root)
    except Exception:
        if staging_name is not None and staging_identity is not None:
            try:
                anchored_stat = os.stat(
                    staging_name,
                    dir_fd=root_fd,
                    follow_symlinks=False
                )

                if _stat_identity(anchored_stat) == staging_identity:
                    os.rmdir(staging_name, dir_fd=root_fd)
            except OSError:
                pass
        raise
    finally:
        os.close(root_fd)

    return staging_path, staging_identity


def _remove_empty_owned_staging(path: Path, expected_identity: tuple) -> None:
    if not _path_matches_identity(path, expected_identity):
        return

    try:
        path.rmdir()
    except OSError:
        pass


def _op_transfer(payload: dict) -> tuple:
    mode = str(payload.get("mode") or "copy").strip().casefold()

    if mode not in {"copy", "move"}:
        _fail("invalid_transfer_mode", "Transfer mode must be copy or move.")

    sources = _operation_paths(payload)
    destination = _resolve_existing_virtual_path(
        payload.get("destination") or "",
        expected_type="folder"
    )
    destination_identity = _path_identity(destination)
    source_set = set(sources)
    destination_names = set()

    for source in sources:
        _require_path_identity(destination, destination_identity)

        if any(other != source and (source in other.parents or other in source.parents) for other in source_set):
            _fail("overlapping_selection", "Select parent folders or their contents, not both.")

        _assert_safe_transfer_tree(source)

        if stat.S_ISDIR(os.lstat(source).st_mode) and (
            destination == source or source in destination.parents
        ):
            _fail("recursive_transfer", "A folder cannot be transferred into itself.")

        normalized_target_name = source.name.casefold()

        if normalized_target_name in destination_names:
            _fail("duplicate_destination", "Selected items contain duplicate destination names.")

        destination_names.add(normalized_target_name)

        _assert_destination_name_available(destination, source.name)

    completed = []
    staged_items = []
    staging_path = None
    staging_identity = None

    try:
        if mode == "copy":
            staging_path, staging_identity = _create_private_staging_directory("transfer")

            for source in sources:
                _require_path_identity(destination, destination_identity)
                _require_path_identity(staging_path, staging_identity)
                source_identity = _path_identity(source)
                staged_target = staging_path / source.name

                try:
                    if stat.S_ISDIR(os.lstat(source).st_mode):
                        shutil.copytree(source, staged_target, symlinks=True)
                    else:
                        shutil.copy2(source, staged_target, follow_symlinks=False)
                except Exception:
                    if os.path.lexists(staged_target):
                        staged_items.append((staged_target, _path_identity(staged_target)))
                    raise

                staged_items.append((staged_target, _path_identity(staged_target)))

                _strip_file_execute_bits(staged_target)
                _require_path_identity(source, source_identity)
                _require_path_identity(staging_path, staging_identity)
                _assert_safe_transfer_tree(source)
                _assert_safe_transfer_tree(staged_target)

            for source in sources:
                _require_path_identity(destination, destination_identity)
                _require_path_identity(staging_path, staging_identity)
                staged_target = staging_path / source.name
                staged_identity = _path_identity(staged_target)
                target = destination / source.name
                _assert_destination_name_available(destination, source.name)
                _rename_noreplace(staged_target, target)
                completed.append((source, target, staged_identity))
                _require_path_identity(target, staged_identity)
                _require_path_identity(destination, destination_identity)
                _require_unique_destination_name(destination, source.name)
                _assert_safe_transfer_tree(target)
        else:
            for source in sources:
                _require_path_identity(destination, destination_identity)
                source_identity = _path_identity(source)
                target = destination / source.name
                _assert_destination_name_available(destination, source.name)
                _rename_noreplace(source, target)
                completed.append((source, target, source_identity))
                _require_path_identity(target, source_identity)
                _require_path_identity(destination, destination_identity)
                _require_unique_destination_name(destination, source.name)
                _assert_safe_transfer_tree(target)
    except Exception:
        if mode == "move":
            for source, target, target_identity in reversed(completed):
                try:
                    if (
                        _path_matches_identity(target, target_identity)
                        and not os.path.lexists(source)
                    ):
                        _rename_noreplace(target, source)
                except Exception:
                    pass
        else:
            for _, target, target_identity in reversed(completed):
                try:
                    _remove_owned_transfer_target(target, target_identity)
                except Exception:
                    pass

        if staging_path is not None and staging_identity is not None:
            for staged_target, staged_target_identity in reversed(staged_items):
                try:
                    _remove_owned_transfer_target(staged_target, staged_target_identity)
                except Exception:
                    pass

            _remove_empty_owned_staging(staging_path, staging_identity)
        raise

    if staging_path is not None and staging_identity is not None:
        _remove_empty_owned_staging(staging_path, staging_identity)

    items = [_serialize_virtual_item(target) for _, target, _ in completed]
    fallback = sources[0].parent if mode == "move" else destination
    refresh_path = _existing_refresh_path(payload.get("current_path"), fallback)
    action_label = "copied" if mode == "copy" else "moved"
    return "transferred", f"Selected items {action_label}.", {
        "mode": mode,
        "destination": _relative_display_path(destination),
        "items": items,
        "refresh_path": refresh_path
    }


def _valid_image_signature(extension: str, content: bytes) -> bool:
    if extension == ".png":
        return content.startswith(b"\x89PNG\r\n\x1a\n")
    if extension in {".jpg", ".jpeg"}:
        return content.startswith(b"\xff\xd8\xff")
    if extension == ".gif":
        return content.startswith((b"GIF87a", b"GIF89a"))
    if extension == ".webp":
        return len(content) >= 12 and content.startswith(b"RIFF") and content[8:12] == b"WEBP"
    return False


def _op_preview(payload: dict) -> tuple:
    path = _resolve_existing_virtual_path(
        payload.get("path"),
        allow_root=False,
        expected_type="file"
    )
    item = _serialize_virtual_item(path)

    if not item["previewable"] or item["size"] > MAX_PREVIEW_BYTES:
        _fail(
            "preview_unsupported",
            "Preview is unavailable for that safe file type.",
            {"item": item}
        )

    flags = os.O_RDONLY
    path_stat = os.lstat(path)

    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW

    descriptor = os.open(path, flags)

    try:
        descriptor_stat = os.fstat(descriptor)

        if (
            descriptor_stat.st_dev != path_stat.st_dev
            or descriptor_stat.st_ino != path_stat.st_ino
            or not stat.S_ISREG(descriptor_stat.st_mode)
        ):
            _fail("source_changed", "That file changed before it could be previewed.")

        with os.fdopen(descriptor, "rb") as file_handle:
            descriptor = -1
            content = file_handle.read(MAX_PREVIEW_BYTES + 1)
    except Exception:
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except Exception:
                pass
        raise

    if len(content) > MAX_PREVIEW_BYTES:
        _fail("preview_too_large", "That file is too large to preview.", {"item": item})

    extension = path.suffix.casefold()

    if extension in IMAGE_PREVIEW_MIME_TYPES:
        if not _valid_image_signature(extension, content):
            _fail("preview_unsupported", "That image preview is invalid.", {"item": item})

        mime_type = IMAGE_PREVIEW_MIME_TYPES[extension]
        preview = {
            "kind": "image",
            "mime_type": mime_type,
            "data_url": f"data:{mime_type};base64,{base64.b64encode(content).decode('ascii')}"
        }
    else:
        if b"\x00" in content:
            _fail("preview_unsupported", "Binary content cannot be previewed.", {"item": item})

        try:
            text_content = content.decode("utf-8")
        except UnicodeDecodeError:
            _fail("preview_unsupported", "Only UTF-8 text can be previewed.", {"item": item})

        if extension == ".json":
            try:
                formatted_json = json.dumps(json.loads(text_content), indent=2, ensure_ascii=False)

                if len(formatted_json.encode("utf-8")) <= MAX_PREVIEW_BYTES:
                    text_content = formatted_json
            except Exception:
                pass

        preview = {
            "kind": "text",
            "format": "json" if extension == ".json" else "plain",
            "content": text_content,
            "truncated": False
        }

    return "preview_ready", "Preview ready.", {
        "item": item,
        "preview": preview
    }


def _op_metadata(payload: dict) -> tuple:
    path = _resolve_existing_virtual_path(payload.get("path"), allow_root=False)
    return "metadata_ready", "Metadata ready.", {
        "item": _serialize_virtual_item(path)
    }


def _op_list(payload: dict) -> tuple:
    result = get_virtual_finder_payload(payload.get("path") or "")
    return "folder_loaded", "Folder loaded.", result


def _op_search(payload: dict) -> tuple:
    path = payload.get("path") or ""
    query = str(payload.get("query") or "").strip()
    data = get_virtual_finder_payload(path)
    data["search_query"] = query
    data["items"] = search_virtual_items(query, path) if query else data["items"]
    return "search_complete", "Search complete.", data


def _op_folder_tree(payload: dict) -> tuple:
    requested_depth = payload.get("depth", MAX_TREE_DEPTH)

    try:
        depth = max(1, min(MAX_TREE_DEPTH, int(requested_depth)))
    except Exception:
        depth = MAX_TREE_DEPTH

    tree, truncated = _folder_tree(payload.get("path") or "", depth, MAX_TREE_NODES)
    return "tree_loaded", "Folder tree loaded.", {
        "folder_tree": tree,
        "folder_tree_truncated": truncated
    }


VIRTUAL_FINDER_OPERATIONS = {
    "list": _op_list,
    "search": _op_search,
    "folder_tree": _op_folder_tree,
    "metadata": _op_metadata,
    "preview": _op_preview,
    "create_file": _op_create_file,
    "create_folder": _op_create_folder,
    "rename": _op_rename,
    "delete": _op_delete,
    "copy": lambda payload: _op_transfer({**payload, "mode": "copy"}),
    "move": lambda payload: _op_transfer({**payload, "mode": "move"}),
    "transfer": _op_transfer
}


def perform_virtual_finder_operation(operation: str, payload=None) -> dict:
    normalized_operation = str(operation or "").strip().casefold()
    handler = VIRTUAL_FINDER_OPERATIONS.get(normalized_operation)

    if handler is None:
        return _result(False, "unsupported_operation", "That Virtual Finder operation is unavailable.")

    safe_payload = payload if isinstance(payload, dict) else {}

    try:
        with FILE_OPERATION_LOCK:
            code, message, data = handler(safe_payload)
        return _result(True, code, message, data)
    except VirtualFinderError as error:
        return error.as_result()
    except Exception as error:
        print(f"[VirtualFinder] {normalized_operation or 'operation'} failed ({type(error).__name__})")
        return _result(False, "operation_failed", "Virtual Finder operation failed safely.")


def _voice_result(result: dict, success_message: str) -> str:
    if result.get("ok"):
        return success_message

    message = str(result.get("message") or "Virtual Finder operation failed").rstrip(". ")
    return message


def create_virtual_folder(name: str, parent=None) -> str:
    result = perform_virtual_finder_operation(
        "create_folder",
        {"name": name, "parent": parent or ""}
    )
    return _voice_result(result, "Folder created")


def rename_virtual_item(old_name: str, new_name: str, parent=None) -> str:
    try:
        old_clean = _validate_entry_name(old_name)
        parent_parts = _parse_virtual_path(parent, allow_root=True)
        source_path = "/".join((*parent_parts, old_clean))
    except VirtualFinderError as error:
        return _voice_result(error.as_result(), "Item renamed")

    result = perform_virtual_finder_operation(
        "rename",
        {
            "path": source_path,
            "new_name": new_name,
            "current_path": "/".join(parent_parts)
        }
    )
    return _voice_result(result, "Item renamed")


def create_virtual_finder_widget(path=None, search_query=None, preferred_workspace=None) -> dict:
    payload = get_virtual_finder_payload(path)

    if search_query:
        payload["search_query"] = str(search_query or "").strip()
        payload["items"] = search_virtual_items(search_query, payload["current_path"])

    add_hud_card(
        card_id="virtual_finder",
        title="VIRTUAL FINDER",
        card_type="virtual_finder",
        x=92,
        y=98,
        width=760,
        height=520,
        broadcast=False,
        data=payload,
        preferred_workspace=preferred_workspace
    )
    set_workshop_file_manager_open(True, broadcast=False)
    broadcast_virtual_desktop_payload()
    broadcast_state()
    return payload
