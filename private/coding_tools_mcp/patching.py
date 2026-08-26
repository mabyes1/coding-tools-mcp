from __future__ import annotations

import hashlib
import os
import re
import stat
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from .errors import ToolFailure


PATCH_TEMP_PREFIX = ".coding-tools-patch-"
PATCH_BACKUP_PREFIX = ".coding-tools-backup-"


@dataclass
class PatchOperation:
    kind: str
    path: str
    add_content: str | None = None
    hunks: list[PatchHunk] = field(default_factory=list)
    move_to: str | None = None


@dataclass(frozen=True)
class PatchHunk:
    lines: list[str]
    old_start: int | None = None


@dataclass(frozen=True)
class ParsedHunk:
    old: list[str]
    new: list[str]
    old_start: int | None = None


@dataclass(frozen=True)
class MatchedHunk:
    hunk_index: int
    start: int
    end: int
    new: list[str]


@dataclass(frozen=True)
class FileBaseline:
    """Filesystem state captured while a patch is being staged."""

    data: bytes | None
    mode: int | None
    digest: str | None

    @classmethod
    def capture(cls, path: Path) -> FileBaseline:
        if not path.exists():
            return cls(data=None, mode=None, digest=None)
        if path.is_dir():
            raise ToolFailure("PATCH_FAILED", "Cannot patch a directory.", category="validation")
        data = path.read_bytes()
        return cls(data=data, mode=stat.S_IMODE(path.stat().st_mode), digest=hashlib.sha256(data).hexdigest())

    def matches(self, path: Path) -> bool:
        if self.data is None:
            return not path.exists()
        if not path.exists() or path.is_dir():
            return False
        current = path.read_bytes()
        return self.digest == hashlib.sha256(current).hexdigest() and self.mode == stat.S_IMODE(path.stat().st_mode)

    def text(self, path: str) -> str:
        if self.data is None:
            return ""
        try:
            return self.data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ToolFailure(
                "UNSUPPORTED_ENCODING",
                f"Patch target is not valid UTF-8: {path}",
                category="validation",
            ) from exc


@dataclass(frozen=True)
class StagedFile:
    display: str
    path: Path
    content: str | None
    baseline: FileBaseline
    mode: int | None


class AtomicPatchCommitter:
    """Commit staged paths with atomic renames and full-set rollback.

    Filesystems do not provide a portable transaction spanning unrelated paths.
    Every replacement here is atomic; originals are retained as same-directory
    backups until the complete set succeeds, then removed.
    """

    def commit(self, changes: list[StagedFile]) -> None:
        if not changes:
            return
        self._assert_unique_paths(changes)
        created_dirs: list[Path] = []
        prepared: dict[Path, Path] = {}
        backups: dict[Path, Path] = {}
        installed: set[Path] = set()
        preserve_backups = False
        try:
            for change in changes:
                created_dirs.extend(_ensure_parent(change.path.parent))
                if change.content is not None:
                    prepared[change.path] = _prepare_file(change)

            for change in changes:
                self._assert_baseline(change)

            for change in changes:
                # Recheck immediately before moving each original. If a later
                # path conflicts, earlier backups are restored as a set.
                self._assert_baseline(change)
                if change.path.exists():
                    backup = _reserve_backup_path(change.path.parent)
                    os.replace(change.path, backup)
                    backups[change.path] = backup
                    _fsync_directory(change.path.parent)

            for change in changes:
                prepared_path = prepared.get(change.path)
                if prepared_path is not None:
                    # A newly-created target has no backup. Do not silently
                    # overwrite a file created after patch staging.
                    if change.path not in backups:
                        self._assert_baseline(change)
                    os.replace(prepared_path, change.path)
                    installed.add(change.path)
                    _fsync_directory(change.path.parent)

        except Exception as exc:
            rollback_errors = self._rollback(changes, prepared, backups, installed, created_dirs)
            if rollback_errors:
                preserve_backups = True
                display_by_path = {change.path: change.display for change in changes}
                recovery_backups = {
                    display_by_path.get(path, str(path)): str(backup)
                    for path, backup in backups.items()
                    if backup.exists()
                }
                raise ToolFailure(
                    "PATCH_ROLLBACK_FAILED",
                    "Patch failed and one or more files could not be restored; recovery backups were preserved.",
                    category="internal",
                    details={
                        "rollback_errors": rollback_errors,
                        "recovery_backups": recovery_backups,
                        "cause": str(exc),
                    },
                ) from exc
            raise
        finally:
            for path in prepared.values():
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass
            if not preserve_backups:
                for path in backups.values():
                    try:
                        path.unlink(missing_ok=True)
                    except OSError:
                        # The new file is already installed (or rollback has
                        # restored the old one). A stale hidden backup is safer
                        # than turning cleanup failure into data loss.
                        continue
                    _fsync_directory(path.parent)

    @staticmethod
    def _assert_unique_paths(changes: list[StagedFile]) -> None:
        paths = [change.path for change in changes]
        if len(paths) != len(set(paths)):
            raise ToolFailure("PATCH_FAILED", "Patch staged the same path more than once.", category="validation")

    @staticmethod
    def _assert_baseline(change: StagedFile) -> None:
        if change.baseline.matches(change.path):
            return
        raise ToolFailure(
            "PATCH_CONFLICT",
            f"File changed while the patch was being prepared: {change.display}",
            category="conflict",
            retryable=True,
            details={
                "path": change.display,
                "retry_hint": "Read the current file and regenerate the patch.",
            },
        )

    @staticmethod
    def _rollback(
        changes: list[StagedFile],
        prepared: dict[Path, Path],
        backups: dict[Path, Path],
        installed: set[Path],
        created_dirs: list[Path],
    ) -> list[str]:
        errors: list[str] = []
        for path in reversed([change.path for change in changes]):
            try:
                if path in installed and path.exists() and not path.is_dir():
                    path.unlink()
                backup = backups.get(path)
                if backup is not None:
                    if not backup.exists():
                        errors.append(f"{path}: recovery backup is missing")
                        continue
                    os.replace(backup, path)
                    _fsync_directory(path.parent)
            except OSError as rollback_error:
                errors.append(f"{path}: {rollback_error}")
        for path in prepared.values():
            try:
                path.unlink(missing_ok=True)
            except OSError as cleanup_error:
                errors.append(f"{path}: {cleanup_error}")
        for directory in reversed(created_dirs):
            try:
                directory.rmdir()
            except OSError:
                pass
        return errors


def _ensure_parent(parent: Path) -> list[Path]:
    missing: list[Path] = []
    current = parent
    while not current.exists():
        missing.append(current)
        current = current.parent
    parent.mkdir(parents=True, exist_ok=True)
    return list(reversed(missing))


def _prepare_file(change: StagedFile) -> Path:
    assert change.content is not None
    fd, raw_path = tempfile.mkstemp(prefix=PATCH_TEMP_PREFIX, dir=change.path.parent)
    temp_path = Path(raw_path)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(change.content.encode("utf-8"))
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp_path, change.mode if change.mode is not None else 0o644)
        return temp_path
    except Exception:
        temp_path.unlink(missing_ok=True)
        raise


def _reserve_backup_path(parent: Path) -> Path:
    fd, raw_path = tempfile.mkstemp(prefix=PATCH_BACKUP_PREFIX, dir=parent)
    os.close(fd)
    path = Path(raw_path)
    path.unlink()
    return path


def _fsync_directory(directory: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        fd = os.open(directory, flags)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def parse_patch(patch: str) -> list[PatchOperation]:
    lines = patch.splitlines()
    if not lines or lines[0].strip() != "*** Begin Patch" or lines[-1].strip() != "*** End Patch":
        raise ToolFailure("PATCH_FAILED", "Patch must use *** Begin Patch / *** End Patch envelope.", category="validation")
    operations: list[PatchOperation] = []
    i = 1
    while i < len(lines) - 1:
        line = lines[i]
        if not line:
            i += 1
            continue
        if line.startswith("*** Add File: "):
            path = line.removeprefix("*** Add File: ").strip()
            i += 1
            content_lines: list[str] = []
            while i < len(lines) - 1 and not lines[i].startswith("*** "):
                if not lines[i].startswith("+"):
                    raise ToolFailure("PATCH_FAILED", "Add file lines must start with '+'.", category="validation")
                content_lines.append(lines[i][1:])
                i += 1
            operations.append(PatchOperation("add", path, add_content="\n".join(content_lines) + "\n"))
            continue
        if line.startswith("*** Delete File: "):
            path = line.removeprefix("*** Delete File: ").strip()
            operations.append(PatchOperation("delete", path))
            i += 1
            continue
        if line.startswith("*** Update File: "):
            path = line.removeprefix("*** Update File: ").strip()
            i += 1
            move_to: str | None = None
            if i < len(lines) - 1 and lines[i].startswith("*** Move to: "):
                move_to = lines[i].removeprefix("*** Move to: ").strip()
                i += 1
            hunks: list[PatchHunk] = []
            current: list[str] = []
            current_old_start: int | None = None
            while i < len(lines) - 1 and not lines[i].startswith("*** "):
                if lines[i].startswith("@@"):
                    if current:
                        hunks.append(PatchHunk(current, current_old_start))
                    current = []
                    current_old_start = parse_hunk_old_start(lines[i])
                else:
                    current.append(lines[i])
                i += 1
            if current:
                hunks.append(PatchHunk(current, current_old_start))
            operations.append(PatchOperation("update", path, hunks=hunks, move_to=move_to))
            continue
        raise ToolFailure("PATCH_FAILED", f"Unrecognized patch line: {line}", category="validation")
    return operations


def apply_update_hunks(content: str, hunks: list[PatchHunk], path: str = "<patch>") -> str:
    if not hunks:
        return content
    bom, text = strip_bom(content)
    line_ending = detect_line_ending(text)
    normalized = normalize_to_lf(text)
    had_trailing_newline = normalized.endswith("\n")
    lines = normalized.splitlines()
    parsed = [parse_update_hunk(hunk) for hunk in hunks]
    matched: list[MatchedHunk] = []
    for index, hunk in enumerate(parsed):
        matches = [0] if not hunk.old else find_subsequence_all(lines, hunk.old)
        if not matches:
            raise ToolFailure(
                "PATCH_CONTEXT_NOT_FOUND",
                f"Patch context did not match in {path}.",
                category="validation",
                retryable=True,
                details={
                    "path": path,
                    "hunk_index": index,
                    "match_count": 0,
                    "retry_hint": "Read the current file and regenerate this hunk with current context.",
                },
            )
        if len(matches) > 1 and hunk.old_start is not None:
            hint_index = max(0, hunk.old_start - 1)
            minimum_distance = min(abs(match - hint_index) for match in matches)
            closest = [match for match in matches if abs(match - hint_index) == minimum_distance]
            if len(closest) == 1:
                matches = closest
        if len(matches) > 1:
            candidate_lines = [match + 1 for match in matches[:50]]
            retry_hint = (
                "Adjust the unified hunk line hint or include additional unchanged context lines."
                if hunk.old_start is not None
                else "Use a standard unified hunk header such as @@ -120,3 +120,3 @@ or include additional unchanged context lines."
            )
            raise ToolFailure(
                "PATCH_CONTEXT_AMBIGUOUS",
                f"Patch context matched {len(matches)} locations in {path} at lines {candidate_lines}; add a location hint or more context.",
                category="validation",
                retryable=True,
                details={
                    "path": path,
                    "hunk_index": index,
                    "match_count": len(matches),
                    "candidate_lines": candidate_lines,
                    "candidate_lines_truncated": len(matches) > len(candidate_lines),
                    "location_hint_line": hunk.old_start,
                    "retry_hint": retry_hint,
                },
            )
        start = matches[0]
        matched.append(MatchedHunk(index, start, start + len(hunk.old), hunk.new))

    matched.sort(key=lambda item: item.start)
    for previous, current in zip(matched, matched[1:]):
        if previous.end > current.start:
            raise ToolFailure(
                "PATCH_HUNKS_OVERLAP",
                f"Patch hunks {previous.hunk_index} and {current.hunk_index} overlap in {path}.",
                category="validation",
                details={
                    "path": path,
                    "hunk_indexes": [previous.hunk_index, current.hunk_index],
                    "retry_hint": "Merge the overlapping hunks into one hunk.",
                },
            )

    # Apply in a single forward pass. Repeated slicing once per hunk makes a
    # large topology patch quadratic and can hold the HTTP server GIL long
    # enough for unrelated connector calls to time out.
    updated_lines: list[str] = []
    cursor = 0
    for matched_hunk in matched:
        updated_lines.extend(lines[cursor : matched_hunk.start])
        updated_lines.extend(matched_hunk.new)
        cursor = matched_hunk.end
    updated_lines.extend(lines[cursor:])
    updated = "\n".join(updated_lines)
    if had_trailing_newline and (updated_lines or updated == ""):
        updated += "\n"
    elif not text and updated_lines:
        updated += "\n"
    return bom + restore_line_endings(updated, line_ending)


UNIFIED_HUNK_HEADER_RE = re.compile(r"^@@\s+-(\d+)(?:,\d+)?\s+\+\d+(?:,\d+)?\s+@@(?:\s.*)?$")


def parse_hunk_old_start(header: str) -> int | None:
    match = UNIFIED_HUNK_HEADER_RE.match(header.strip())
    return int(match.group(1)) if match else None


def parse_update_hunk(hunk: PatchHunk) -> ParsedHunk:
    old: list[str] = []
    new: list[str] = []
    for raw in hunk.lines:
        if raw == "*** End of File":
            continue
        if not raw:
            raise ToolFailure("PATCH_FAILED", "Invalid empty patch line.", category="validation")
        marker = raw[0]
        value = raw[1:] if marker in {" ", "-", "+"} else raw
        if marker == " ":
            old.append(value)
            new.append(value)
        elif marker == "-":
            old.append(value)
        elif marker == "+":
            new.append(value)
        else:
            raise ToolFailure("PATCH_FAILED", "Update lines must start with space, '-' or '+'.", category="validation")
    return ParsedHunk(old=old, new=new, old_start=hunk.old_start)


def find_subsequence_all(lines: list[str], needle: list[str]) -> list[int]:
    if not needle:
        return [0]
    if len(needle) > len(lines):
        return []

    # Knuth-Morris-Pratt finds every (including overlapping) match in O(n+m).
    # The earlier slice comparison was O(n*m), which made a single large patch
    # capable of starving every HTTP worker under the GIL.
    prefix = [0] * len(needle)
    matched = 0
    for index in range(1, len(needle)):
        while matched and needle[index] != needle[matched]:
            matched = prefix[matched - 1]
        if needle[index] == needle[matched]:
            matched += 1
        prefix[index] = matched

    positions: list[int] = []
    matched = 0
    for index, line in enumerate(lines):
        while matched and line != needle[matched]:
            matched = prefix[matched - 1]
        if line == needle[matched]:
            matched += 1
        if matched == len(needle):
            positions.append(index - len(needle) + 1)
            matched = prefix[matched - 1]
    return positions


def strip_bom(text: str) -> tuple[str, str]:
    return ("\ufeff", text[1:]) if text.startswith("\ufeff") else ("", text)


def detect_line_ending(text: str) -> str:
    crlf = text.find("\r\n")
    lf = text.find("\n")
    if lf < 0 or crlf < 0:
        return "\n"
    return "\r\n" if crlf <= lf else "\n"


def normalize_to_lf(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def restore_line_endings(text: str, ending: str) -> str:
    return text.replace("\n", "\r\n") if ending == "\r\n" else text


def read_text_preserve_newlines(path: Path) -> str:
    with path.open("r", encoding="utf-8", newline="") as handle:
        return handle.read()
