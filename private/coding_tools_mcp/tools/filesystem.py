from __future__ import annotations

import fnmatch
import json
import os
import re
import subprocess
import threading
from collections.abc import Callable, Iterator
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

from ..errors import ToolFailure
from ..textutils import DEFAULT_MAX_LINES, TextTruncation, truncate_text_head
from ..workspace import DEFAULT_EXCLUDED_NAMES, ResolvedPath, Workspace, normalize_rel_display


GREP_MAX_LINE_CHARS = 500
ResolveExisting = Callable[[str], ResolvedPath]
CachedWhich = Callable[..., str | None]


def truncate_line_chars(line: str, max_chars: int = GREP_MAX_LINE_CHARS) -> tuple[str, bool]:
    if len(line) <= max_chars:
        return line, False
    suffix = " ... [truncated]"
    keep = max(0, max_chars - len(suffix))
    return line[:keep] + suffix, True


def matches_any_glob(rel: str, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatch(rel, pattern) or PurePosixPath(rel).match(pattern) for pattern in patterns)


def file_entry(path: Path, rel: str, path_stat: os.stat_result) -> dict[str, Any]:
    return {
        "path": rel,
        "type": "symlink" if path.is_symlink() else "file",
        "size_bytes": path_stat.st_size,
        "modified": datetime.fromtimestamp(path_stat.st_mtime, timezone.utc).isoformat().replace("+00:00", "Z"),
    }


def search_match_item(
    rel: str,
    line_number: int,
    column: int,
    line: str,
    before: list[str],
    after: list[str],
    max_preview_bytes: int,
) -> dict[str, Any]:
    preview, line_truncated = truncate_line_chars(line)
    preview_truncation = truncate_text_head(preview, max_lines=1, max_bytes=max_preview_bytes)
    item: dict[str, Any] = {
        "path": rel,
        "line": line_number,
        "column": column,
        "preview": preview_truncation.content,
        "before": before,
        "after": after,
    }
    if line_truncated or preview_truncation.truncated:
        item["preview_truncated"] = True
        item["preview_truncated_by"] = "chars" if line_truncated else preview_truncation.truncated_by
    return item


def truncation_fields(truncation: TextTruncation) -> dict[str, Any]:
    return {
        "truncated": truncation.truncated,
        "truncated_by": truncation.truncated_by,
        "output_lines": truncation.output_lines,
        "output_bytes": truncation.output_bytes,
    }


def walk_files(root: Path) -> Iterator[Path]:
    if root.is_file() or root.is_symlink():
        yield root
        return
    for current, dirs, files in os.walk(root, followlinks=False):
        dirs[:] = [name for name in dirs if name not in DEFAULT_EXCLUDED_NAMES]
        current_path = Path(current)
        for name in files:
            yield current_path / name


def path_batches(paths: Iterator[Path], size: int) -> Iterator[list[Path]]:
    batch: list[Path] = []
    for path in paths:
        batch.append(path)
        if len(batch) >= size:
            yield batch
            batch = []
    if batch:
        yield batch


def find_literal(line: str, needle: str, case_sensitive: bool) -> int:
    """Return the match index of a pre-normalized needle (lowered unless
    case_sensitive) in line, or -1."""
    haystack = line if case_sensitive else line.lower()
    return haystack.find(needle)


def entry_for_path(path: Path, root: Path) -> dict[str, Any]:
    stat = path.lstat()
    if path.is_symlink():
        kind = "symlink"
    elif path.is_dir():
        kind = "directory"
    elif path.is_file():
        kind = "file"
    else:
        kind = "other"
    item: dict[str, Any] = {
        "name": path.name,
        "path": normalize_rel_display(path, root),
        "type": kind,
        "size_bytes": stat.st_size,
        "modified": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat().replace("+00:00", "Z"),
        "is_hidden": path.name.startswith("."),
        "is_ignored": False,
    }
    if path.is_symlink():
        try:
            item["symlink_target"] = os.readlink(path)
        except OSError:
            pass
    return item


def sort_value(item: dict[str, Any], sort_key: str) -> Any:
    if sort_key == "type":
        return (item.get("type", ""), item.get("name", ""))
    if sort_key == "modified":
        return (item.get("modified", ""), item.get("name", ""))
    return item.get("name", "")


def read_file_tool(
    args: dict[str, Any],
    *,
    resolve_existing: ResolveExisting,
) -> dict[str, Any]:
    requested_path = str(args.get("path", ""))
    resolved = resolve_existing(requested_path)
    if resolved.path.is_dir():
        raise ToolFailure("IS_DIRECTORY", "Path is a directory.", category="validation")
    max_bytes = int(args.get("max_bytes", 131072))
    start_line = int(args.get("start_line", 1))
    end_line = args.get("end_line")
    max_lines = args.get("max_lines")
    if end_line is not None and max_lines is not None:
        calculated_end_line = start_line + int(max_lines) - 1
        end_line = min(int(end_line), calculated_end_line)
    if end_line is None and max_lines is not None:
        end_line = start_line + int(max_lines) - 1
    encoding = args.get("encoding", "utf-8")
    if encoding != "utf-8":
        raise ToolFailure("UNSUPPORTED_ENCODING", "Only utf-8 is supported.", category="validation")
    total_bytes = resolved.path.stat().st_size
    with resolved.path.open("rb") as raw_handle:
        if b"\x00" in raw_handle.read(4096):
            raise ToolFailure("BINARY_FILE", "Binary file read blocked for text tool.", category="validation")
    if start_line < 1:
        raise ToolFailure("INVALID_ARGUMENT", "start_line must be >= 1.", category="validation")
    requested_end = int(end_line) if end_line is not None else None
    selected_parts: list[str] = []
    selected_bytes = 0
    total_lines = 0
    selection_complete = False
    try:
        with resolved.path.open("r", encoding="utf-8", errors="strict", newline="") as handle:
            for total_lines, line in enumerate(handle, start=1):
                if total_lines < start_line:
                    continue
                if requested_end is not None and total_lines > requested_end:
                    continue
                if selection_complete:
                    continue
                selected_parts.append(line)
                selected_bytes += len(line.encode("utf-8"))
                if len(selected_parts) > DEFAULT_MAX_LINES or selected_bytes > max_bytes:
                    selection_complete = True
    except UnicodeDecodeError as exc:
        raise ToolFailure("UNSUPPORTED_ENCODING", "File is not valid utf-8.", category="validation") from exc
    selected = "".join(selected_parts)
    truncation = truncate_text_head(selected, max_lines=DEFAULT_MAX_LINES, max_bytes=max_bytes)
    selected = truncation.content
    truncated = truncation.truncated or selection_complete
    end = requested_end if requested_end is not None else total_lines
    if end < start_line:
        selected = ""
    actual_end = min(end, total_lines)
    if truncated and truncation.output_lines > 0:
        actual_end = min(total_lines, start_line + truncation.output_lines - 1)
    next_start_line = actual_end + 1 if truncated and actual_end < total_lines else None
    warnings = []
    if truncated:
        warnings.append("content truncated")
    if truncation.first_line_exceeds_limit:
        warnings.append("first selected line exceeds max_bytes")
    result = {
        "path": resolved.display,
        "content": selected,
        "encoding": "utf-8",
        "max_bytes": max_bytes,
        "start_line": start_line,
        "end_line": actual_end,
        "total_lines": total_lines,
        "total_bytes": total_bytes,
        "bytes_read": len(selected.encode("utf-8")),
        "truncated": truncated,
        "truncated_by": truncation.truncated_by or ("bytes" if selection_complete else None),
        "first_line_exceeds_limit": truncation.first_line_exceeds_limit,
        "output_lines": truncation.output_lines,
        "output_bytes": truncation.output_bytes,
        "next_start_line": next_start_line,
        "warnings": warnings,
    }
    if next_start_line is not None:
        result["next_action"] = {
            "tool": "read_file",
            "arguments": {
                "path": requested_path,
                "start_line": next_start_line,
                "max_bytes": max_bytes,
            },
        }
    return result


def list_dir_tool(
    args: dict[str, Any],
    *,
    resolve_existing: ResolveExisting,
    workspace: Workspace,
) -> dict[str, Any]:
    resolved = resolve_existing(str(args.get("path", ".")))
    if not resolved.path.is_dir():
        raise ToolFailure("NOT_A_DIRECTORY", "Path is not a directory.", category="validation")
    recursive = bool(args.get("recursive", False))
    max_depth = int(args.get("max_depth", 1))
    max_entries = int(args.get("max_entries", 1000))
    include_hidden = bool(args.get("include_hidden", False))
    include_ignored = bool(args.get("include_ignored", False))
    sort_key = args.get("sort", "name")
    entries: list[dict[str, Any]] = []
    truncated = False

    def visit(directory: Path, depth: int) -> None:
        nonlocal truncated
        if truncated:
            return
        try:
            children = list(directory.iterdir())
        except OSError:
            return
        child_rel_paths = [normalize_rel_display(child, workspace.root) for child in children]
        ignored = set() if include_ignored else workspace.git_ignored_paths(child_rel_paths)
        for child in children:
            if workspace.is_ignored_path(
                child,
                include_hidden=include_hidden,
                include_ignored=include_ignored,
                git_ignored=ignored,
            ):
                continue
            entries.append(entry_for_path(child, workspace.root))
            if len(entries) >= max_entries:
                truncated = True
                return
            if recursive and depth < max_depth and child.is_dir() and not child.is_symlink():
                visit(child, depth + 1)

    visit(resolved.path, 1)
    entries.sort(key=lambda item: sort_value(item, sort_key))
    return {
        "path": resolved.display,
        "entries": entries,
        "truncated": truncated,
        "warnings": ["entry limit reached"] if truncated else [],
    }


def list_files_tool(
    args: dict[str, Any],
    *,
    resolve_existing: ResolveExisting,
    workspace: Workspace,
    cached_which: CachedWhich,
) -> dict[str, Any]:
    resolved = resolve_existing(str(args.get("path", ".")))
    if not resolved.path.is_dir():
        raise ToolFailure("NOT_A_DIRECTORY", "Path is not a directory.", category="validation")
    patterns_arg = args.get("patterns")
    glob_arg = args.get("glob")
    if isinstance(patterns_arg, list) and patterns_arg:
        patterns = [str(item) for item in patterns_arg]
    elif isinstance(glob_arg, str) and glob_arg:
        patterns = [glob_arg]
    else:
        patterns = ["**/*"]
    exclude_patterns = [str(item) for item in args.get("exclude_patterns", [])]
    include_hidden = bool(args.get("include_hidden", False))
    include_ignored = bool(args.get("include_ignored", False))
    max_results = int(args.get("max_results", 5000))
    fast_result = _list_files_with_fd(
        resolved,
        patterns,
        exclude_patterns,
        include_hidden=include_hidden,
        include_ignored=include_ignored,
        max_results=max_results,
        sort_key=str(args.get("sort", "path")),
        workspace=workspace,
        cached_which=cached_which,
    )
    if fast_result is not None:
        return fast_result
    files: list[dict[str, Any]] = []
    truncated = False
    for batch in path_batches(walk_files(resolved.path), 256):
        candidates = [
            (path, rel)
            for path, rel in ((path, normalize_rel_display(path, workspace.root)) for path in batch)
            if matches_any_glob(rel, patterns) and not matches_any_glob(rel, exclude_patterns)
        ]
        ignored = set() if include_ignored else workspace.git_ignored_paths([rel for _, rel in candidates])
        for path, rel in candidates:
            if path.is_symlink() and not workspace.is_safe_existing_path(path):
                continue
            if workspace.is_ignored_path(
                path,
                include_hidden=include_hidden,
                include_ignored=include_ignored,
                git_ignored=ignored,
            ):
                continue
            files.append(file_entry(path, rel, path.lstat()))
            if len(files) >= max_results:
                truncated = True
                break
        if truncated:
            break
    files.sort(key=lambda item: item["modified"] if args.get("sort") == "modified" else item["path"])
    return {
        "path": resolved.display,
        "files": files,
        "truncated": truncated,
        "warnings": ["result limit reached"] if truncated else [],
    }


def _list_files_with_fd(
    resolved: ResolvedPath,
    patterns: list[str],
    exclude_patterns: list[str],
    *,
    include_hidden: bool,
    include_ignored: bool,
    max_results: int,
    sort_key: str,
    workspace: Workspace,
    cached_which: CachedWhich,
) -> dict[str, Any] | None:
    fd = cached_which("fd", "fdfind")
    if not fd or not resolved.path.is_dir():
        return None
    args_base = [
        fd,
        "--glob",
        "--color=never",
        "--type",
        "f",
        "--type",
        "l",
        "--max-results",
        str(max_results),
        "--no-require-git",
    ]
    if include_hidden:
        args_base.append("--hidden")
    if include_ignored:
        args_base.append("--no-ignore")
    else:
        for name in sorted(DEFAULT_EXCLUDED_NAMES):
            args_base.extend(["--exclude", name])
    for pattern in exclude_patterns:
        args_base.extend(["--exclude", pattern])

    paths: dict[str, Path] = {}
    for pattern in patterns:
        effective = pattern
        args = list(args_base)
        if "/" in pattern:
            args.append("--full-path")
            if not pattern.startswith("/") and not pattern.startswith("**/") and pattern != "**":
                effective = f"**/{pattern}"
        args.extend(["--", effective, "."])
        try:
            completed = subprocess.run(
                args,
                cwd=str(resolved.path),
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=10,
            )
        except Exception:
            return None
        if completed.returncode not in {0, 1}:
            return None
        for raw in completed.stdout.splitlines():
            rel_to_search = raw.strip().removeprefix("./")
            if not rel_to_search:
                continue
            path = resolved.path / rel_to_search
            if path.is_symlink() and not workspace.is_safe_existing_path(path):
                continue
            rel = normalize_rel_display(path, workspace.root)
            if matches_any_glob(rel, exclude_patterns):
                continue
            paths[rel] = path
            if len(paths) >= max_results:
                break
        if len(paths) >= max_results:
            break
    ignored = set() if include_ignored else workspace.git_ignored_paths(list(paths))
    files: list[dict[str, Any]] = []
    for rel, path in paths.items():
        if workspace.is_ignored_path(
            path,
            include_hidden=include_hidden,
            include_ignored=include_ignored,
            git_ignored=ignored,
        ):
            continue
        try:
            stat = path.lstat()
        except OSError:
            continue
        files.append(file_entry(path, rel, stat))
    files.sort(key=lambda item: item["modified"] if sort_key == "modified" else item["path"])
    truncated = len(paths) >= max_results
    return {
        "path": resolved.display,
        "files": files,
        "truncated": truncated,
        "engine": "fd",
        "warnings": ["result limit reached"] if truncated else [],
    }


def search_text_tool(
    args: dict[str, Any],
    *,
    resolve_existing: ResolveExisting,
    workspace: Workspace,
    cached_which: CachedWhich,
) -> dict[str, Any]:
    query = str(args.get("query", ""))
    if not query:
        raise ToolFailure("INVALID_ARGUMENT", "query is required.", category="validation")
    resolved = resolve_existing(str(args.get("path", ".")))
    regex = bool(args.get("regex", False))
    case_sensitive = bool(args.get("case_sensitive", False))
    include_globs = [str(item) for item in args.get("include_globs", [])]
    if isinstance(args.get("glob"), str):
        include_globs.append(str(args["glob"]))
    exclude_globs = [str(item) for item in args.get("exclude_globs", [])]
    context_lines = int(args.get("context_lines", 0))
    max_results = int(args.get("max_results", 1000))
    max_preview_bytes = int(args.get("max_preview_bytes", 512))
    fast_result = _search_text_with_rg(
        resolved,
        query,
        regex=regex,
        case_sensitive=case_sensitive,
        include_globs=include_globs,
        exclude_globs=exclude_globs,
        context_lines=context_lines,
        max_results=max_results,
        max_preview_bytes=max_preview_bytes,
        workspace=workspace,
        cached_which=cached_which,
    )
    if fast_result is not None:
        return fast_result
    matches: list[dict[str, Any]] = []
    total = 0
    flags = 0 if case_sensitive else re.IGNORECASE
    try:
        compiled = re.compile(query, flags) if regex else None
    except re.error as exc:
        raise ToolFailure("INVALID_ARGUMENT", f"Invalid regex: {exc}", category="validation") from exc
    needle = query if case_sensitive else query.lower()

    roots = [resolved.path] if resolved.path.is_file() else walk_files(resolved.path)
    for batch in path_batches(roots, 256):
        candidates = []
        for path in batch:
            if path.is_dir():
                continue
            if path.is_symlink() and not workspace.is_safe_existing_path(path):
                continue
            rel = normalize_rel_display(path, workspace.root)
            if include_globs and not matches_any_glob(rel, include_globs):
                continue
            if matches_any_glob(rel, exclude_globs):
                continue
            candidates.append((path, rel))
        ignored = workspace.git_ignored_paths([rel for _, rel in candidates])
        for path, rel in candidates:
            if workspace.is_ignored_path(path, git_ignored=ignored):
                continue
            try:
                data = path.read_bytes()
            except OSError:
                continue
            if b"\x00" in data[:4096]:
                continue
            try:
                lines = data.decode("utf-8").splitlines()
            except UnicodeDecodeError:
                continue
            for index, line in enumerate(lines):
                if compiled:
                    found = compiled.search(line)
                    if not found:
                        continue
                    column = found.start() + 1
                else:
                    literal_index = find_literal(line, needle, case_sensitive)
                    if literal_index < 0:
                        continue
                    column = literal_index + 1
                total += 1
                if len(matches) >= max_results:
                    continue
                before = lines[max(0, index - context_lines) : index]
                after = lines[index + 1 : index + 1 + context_lines]
                matches.append(search_match_item(rel, index + 1, column, line, before, after, max_preview_bytes))
    return {
        "query": query,
        "matches": matches,
        "total_matches": total,
        "truncated": total > len(matches),
        "warnings": ["result limit reached"] if total > len(matches) else [],
    }


def _search_text_with_rg(
    resolved: ResolvedPath,
    query: str,
    *,
    regex: bool,
    case_sensitive: bool,
    include_globs: list[str],
    exclude_globs: list[str],
    context_lines: int,
    max_results: int,
    max_preview_bytes: int,
    workspace: Workspace,
    cached_which: CachedWhich,
) -> dict[str, Any] | None:
    rg = cached_which("rg")
    if not rg:
        return None
    args = [rg, "--json", "--line-number", "--color=never"]
    if not case_sensitive:
        args.append("--ignore-case")
    if not regex:
        args.append("--fixed-strings")
    for name in sorted(DEFAULT_EXCLUDED_NAMES):
        args.extend(["--glob", f"!{name}/**"])
    for pattern in include_globs:
        args.extend(["--glob", pattern])
    for pattern in exclude_globs:
        args.extend(["--glob", f"!{pattern}"])
    search_path = resolved.display if resolved.display != "." else "."
    args.extend(["--", query, search_path])
    try:
        process = subprocess.Popen(
            args,
            cwd=str(workspace.root),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
    except OSError:
        return None
    timed_out = threading.Event()

    def stop_timed_out_search() -> None:
        timed_out.set()
        try:
            process.kill()
        except OSError:
            pass

    timeout = threading.Timer(10, stop_timed_out_search)
    timeout.daemon = True
    timeout.start()
    matches: list[dict[str, Any]] = []
    total = 0
    truncated = False
    file_cache: dict[str, list[str]] = {}
    assert process.stdout is not None
    try:
        for raw in process.stdout:
            try:
                event = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if event.get("type") != "match":
                continue
            data = event.get("data") if isinstance(event.get("data"), dict) else {}
            path_text = data.get("path", {}).get("text") if isinstance(data.get("path"), dict) else None
            line_number = data.get("line_number")
            line_text = data.get("lines", {}).get("text") if isinstance(data.get("lines"), dict) else ""
            if not isinstance(path_text, str) or not isinstance(line_number, int):
                continue
            total += 1
            if len(matches) >= max_results:
                truncated = True
                process.terminate()
                break
            rel = normalize_rel_display((workspace.root / path_text).resolve(), workspace.root)
            submatches = data.get("submatches") if isinstance(data.get("submatches"), list) else []
            first_submatch = submatches[0] if submatches and isinstance(submatches[0], dict) else {}
            column = int(first_submatch.get("start", 0)) + 1
            sanitized = str(line_text).replace("\r\n", "\n").replace("\r", "").rstrip("\n")
            lines: list[str] = []
            if context_lines > 0:
                lines = file_cache.get(rel, [])
                if rel not in file_cache:
                    try:
                        lines = (workspace.root / rel).read_text(encoding="utf-8").splitlines()
                    except OSError:
                        lines = []
                    file_cache[rel] = lines
            index = line_number - 1
            before = lines[max(0, index - context_lines) : index] if lines else []
            after = lines[index + 1 : index + 1 + context_lines] if lines else []
            matches.append(search_match_item(rel, line_number, column, sanitized, before, after, max_preview_bytes))
    finally:
        timeout.cancel()
        try:
            process.stdout.close()
        except OSError:
            pass
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=1)
    if timed_out.is_set():
        return None
    if not truncated and process.returncode not in {0, 1}:
        return None
    return {
        "query": query,
        "matches": matches,
        "total_matches": total,
        "total_matches_exact": not truncated,
        "truncated": truncated,
        "engine": "rg",
        "warnings": ["result limit reached; search stopped early"] if truncated else [],
    }


__all__ = [
    "GREP_MAX_LINE_CHARS",
    "entry_for_path",
    "file_entry",
    "find_literal",
    "list_dir_tool",
    "list_files_tool",
    "matches_any_glob",
    "path_batches",
    "read_file_tool",
    "search_match_item",
    "search_text_tool",
    "sort_value",
    "truncate_line_chars",
    "truncation_fields",
    "walk_files",
]
