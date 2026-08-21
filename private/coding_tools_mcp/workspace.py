from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from .envutils import ENV_PREFIX
from .errors import ToolFailure
from .gitutils import git_command


DEFAULT_EXCLUDED_NAMES = {
    ".git",
    ".reference",
    "node_modules",
    "target",
    "dist",
    "build",
    ".venv",
    "venv",
    ".tox",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    "__pycache__",
}
WORKSPACE_ALLOWLIST_ENV = f"{ENV_PREFIX}_WORKSPACE_ALLOWLIST"


def normalize_rel_display(path: Path, root: Path) -> str:
    try:
        rel = path.relative_to(root)
    except ValueError:
        return path.as_posix()
    text = rel.as_posix()
    return "." if text == "" else text


def is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


@dataclass(frozen=True)
class WorkspaceEntry:
    name: str
    path: Path


def workspace_catalog_from_env() -> tuple[WorkspaceEntry, ...]:
    """Return the named workspace roots this private instance may open.

    A semicolon-separated allowlist is intentionally opt-in.  When it is set,
    selecting any other root fails before a Runtime is created.  Exact roots
    keep the configured workspace boundary auditable and avoid silently
    granting access to a whole drive or parent tree.  Entries may be plain
    paths (the directory name becomes the selector) or ``name=path`` pairs.
    """
    raw = (os.environ.get(WORKSPACE_ALLOWLIST_ENV) or "").strip()
    if not raw:
        return ()
    entries: list[WorkspaceEntry] = []
    seen_paths: set[str] = set()
    seen_names: set[str] = set()
    for item in raw.split(os.pathsep):
        value = item.strip().strip('"')
        if not value:
            continue
        if "=" in value:
            name, path_text = value.split("=", 1)
            name = name.strip()
            path_text = path_text.strip()
        else:
            path_text = value
            name = Path(path_text).name or Path(path_text).drive
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", name):
            raise ToolFailure(
                "WORKSPACE_ALLOWLIST_INVALID",
                f"Workspace selector is invalid: {name!r}",
                category="security",
            )
        candidate = Path(path_text).expanduser()
        try:
            resolved = candidate.resolve(strict=True)
        except FileNotFoundError as exc:
            raise ToolFailure(
                "WORKSPACE_ALLOWLIST_INVALID",
                f"Workspace allowlist entry does not exist: {value}",
                category="security",
            ) from exc
        if not resolved.is_dir():
            raise ToolFailure(
                "WORKSPACE_ALLOWLIST_INVALID",
                f"Workspace allowlist entry is not a directory: {value}",
                category="security",
            )
        path_key = os.path.normcase(str(resolved))
        name_key = name.casefold()
        if path_key in seen_paths:
            raise ToolFailure(
                "WORKSPACE_ALLOWLIST_INVALID",
                f"Workspace allowlist contains a duplicate path: {resolved}",
                category="security",
            )
        if name_key in seen_names:
            raise ToolFailure(
                "WORKSPACE_ALLOWLIST_INVALID",
                f"Workspace allowlist contains a duplicate selector: {name}",
                category="security",
            )
        seen_paths.add(path_key)
        seen_names.add(name_key)
        entries.append(WorkspaceEntry(name=name, path=resolved))
    return tuple(entries)


def workspace_allowlist_from_env() -> tuple[Path, ...]:
    return tuple(entry.path for entry in workspace_catalog_from_env())


def workspace_entry_for_selector(selector: str) -> WorkspaceEntry:
    entries = workspace_catalog_from_env()
    if not entries:
        raise ToolFailure(
            "WORKSPACE_SWITCH_DISABLED",
            f"Set {WORKSPACE_ALLOWLIST_ENV} before switching workspaces.",
            category="security",
        )
    raw = selector.strip()
    for entry in entries:
        if raw.casefold() == entry.name.casefold():
            return entry
    try:
        selected = Path(raw).expanduser().resolve(strict=True)
    except FileNotFoundError as exc:
        raise ToolFailure(
            "WORKSPACE_NOT_ALLOWED",
            f"Unknown workspace selector: {selector}",
            category="security",
        ) from exc
    selected_key = os.path.normcase(str(selected))
    for entry in entries:
        if os.path.normcase(str(entry.path)) == selected_key:
            return entry
    raise ToolFailure(
        "WORKSPACE_NOT_ALLOWED",
        "Selected workspace is not in the configured private allowlist.",
        category="security",
        details={"selector": selector, "allowed": [entry.name for entry in entries]},
    )


def validate_workspace_selection(workspace: Path) -> tuple[Path, ...]:
    allowed = workspace_allowlist_from_env()
    if not allowed:
        return ()
    try:
        selected = workspace.expanduser().resolve(strict=True)
    except FileNotFoundError as exc:
        raise ToolFailure(
            "INVALID_ARGUMENT",
            f"Workspace root does not exist: {workspace}",
            category="validation",
        ) from exc
    selected_key = os.path.normcase(str(selected))
    if not any(os.path.normcase(str(root)) == selected_key for root in allowed):
        raise ToolFailure(
            "WORKSPACE_NOT_ALLOWED",
            "Selected workspace is not in the configured private allowlist.",
            category="security",
            details={"workspace": str(selected), "allowed": [str(root) for root in allowed]},
        )
    return allowed


@dataclass
class ResolvedPath:
    display: str
    path: Path
    existed: bool


class Workspace:
    def __init__(self, root: Path) -> None:
        self.root = root.expanduser().resolve(strict=True)
        if not self.root.is_dir():
            raise ToolFailure("INVALID_ARGUMENT", "Workspace root must be a directory.", category="validation")
        unsafe_roots = {"/"}
        try:
            unsafe_roots.add(str(Path.home().resolve()))
        except RuntimeError:
            pass
        if str(self.root) in unsafe_roots:
            raise ToolFailure("INVALID_ARGUMENT", "Unsafe workspace root rejected.", category="security")
        self.git_path = shutil.which("git")
        self._git_repo_cache: dict[Path, Path | None] = {}

    def _reject_unsafe_text(self, raw_path: str) -> PurePosixPath:
        if not isinstance(raw_path, str) or not raw_path:
            raise ToolFailure("INVALID_ARGUMENT", "Path must be a non-empty string.", category="validation")
        if "\x00" in raw_path:
            raise ToolFailure("INVALID_ARGUMENT", "Path contains a NUL byte.", category="validation")
        if raw_path.startswith("/") or re.match(r"^[A-Za-z]:[\\/]", raw_path):
            raise ToolFailure("ABSOLUTE_PATH_DENIED", "Absolute paths are denied.", category="security")
        pure = PurePosixPath(raw_path)
        if any(part == ".." for part in pure.parts):
            raise ToolFailure("PATH_OUTSIDE_WORKSPACE", "Path escapes the configured workspace.", category="security")
        return pure

    def _normalize_path_input(self, raw_path: str) -> tuple[PurePosixPath, bool]:
        """Normalize an absolute path that is already inside this workspace.

        Absolute paths outside the root remain denied.  Accepting an absolute
        path only after resolving it against the configured root lets clients
        pass ``D:\\workspace\\project`` without weakening the boundary.
        """
        if isinstance(raw_path, str):
            candidate_text = raw_path.strip()
            if Path(candidate_text).is_absolute() or re.match(r"^[A-Za-z]:[\\/]", candidate_text):
                candidate = Path(candidate_text).expanduser()
                try:
                    resolved = candidate.resolve(strict=False)
                except OSError as exc:
                    raise ToolFailure(
                        "INVALID_ARGUMENT",
                        "Absolute path could not be normalized safely.",
                        category="validation",
                    ) from exc
                if not is_relative_to(resolved, self.root):
                    raise ToolFailure(
                        "ABSOLUTE_PATH_DENIED",
                        "Absolute path escapes the configured workspace.",
                        category="security",
                    )
                relative = resolved.relative_to(self.root)
                return PurePosixPath(relative.as_posix() or "."), True
        return self._reject_unsafe_text(raw_path), False

    def resolve_existing(self, raw_path: str = ".") -> ResolvedPath:
        return self.resolve_existing_at(self.root, raw_path)

    def resolve_existing_at(self, base: Path, raw_path: str = ".") -> ResolvedPath:
        pure, was_absolute = self._normalize_path_input(raw_path or ".")
        base = self._validate_base(base)
        candidate = (self.root if was_absolute else base).joinpath(*pure.parts)
        try:
            resolved = candidate.resolve(strict=True)
        except FileNotFoundError as exc:
            raise ToolFailure(
                "NOT_FOUND",
                f"Path not found: {raw_path}",
                category="not_found",
                details={
                    "requested_path": raw_path,
                    "base": normalize_rel_display(base, self.root),
                    "attempted_path": normalize_rel_display(candidate, self.root),
                    "recovery_hint": "Check get_default_cwd or use the path relative to the reported base.",
                },
            ) from exc
        if not is_relative_to(resolved, self.root):
            code = "SYMLINK_ESCAPE" if candidate.is_symlink() else "PATH_OUTSIDE_WORKSPACE"
            raise ToolFailure(code, "Path escapes the configured workspace.", category="security")
        return ResolvedPath(normalize_rel_display(resolved, self.root), resolved, True)

    def resolve_for_write(self, raw_path: str) -> ResolvedPath:
        return self.resolve_for_write_at(self.root, raw_path)

    def resolve_for_write_at(self, base: Path, raw_path: str) -> ResolvedPath:
        pure, was_absolute = self._normalize_path_input(raw_path)
        if pure.name in {"", ".", ".."}:
            raise ToolFailure("INVALID_ARGUMENT", "Invalid write target.", category="validation")
        base = self._validate_base(base)
        candidate = (self.root if was_absolute else base).joinpath(*pure.parts)
        if candidate.exists() or candidate.is_symlink():
            resolved = candidate.resolve(strict=True)
            if not is_relative_to(resolved, self.root):
                raise ToolFailure("SYMLINK_ESCAPE", "Path escapes the configured workspace.", category="security")
            return ResolvedPath(normalize_rel_display(resolved, self.root), resolved, True)

        parent = candidate.parent
        missing: list[Path] = []
        while not parent.exists():
            missing.append(parent)
            if parent == self.root or parent.parent == parent:
                break
            parent = parent.parent
        try:
            resolved_parent = parent.resolve(strict=True)
        except FileNotFoundError as exc:
            raise ToolFailure("NOT_FOUND", f"Parent directory not found: {raw_path}", category="not_found") from exc
        if not is_relative_to(resolved_parent, self.root):
            raise ToolFailure("PATH_OUTSIDE_WORKSPACE", "Path escapes the configured workspace.", category="security")
        target = resolved_parent.joinpath(*reversed([p.name for p in missing]), candidate.name)
        return ResolvedPath(normalize_rel_display(target, self.root), target, False)

    def _validate_base(self, base: Path) -> Path:
        try:
            resolved = base.resolve(strict=True)
        except FileNotFoundError as exc:
            raise ToolFailure("NOT_FOUND", "Default cwd path no longer exists.", category="not_found") from exc
        if not resolved.is_dir():
            raise ToolFailure("NOT_A_DIRECTORY", "Default cwd is not a directory.", category="validation")
        if not is_relative_to(resolved, self.root):
            raise ToolFailure("PATH_OUTSIDE_WORKSPACE", "Default cwd escapes the configured workspace.", category="security")
        return resolved

    def reject_write_symlink(self, raw_path: str) -> None:
        pure, was_absolute = self._normalize_path_input(raw_path)
        candidate = self.root.joinpath(*pure.parts)
        if candidate.is_symlink():
            raise ToolFailure("SYMLINK_ESCAPE", "Writing through symlinks is denied.", category="security")

    def is_ignored_path(
        self,
        path: Path,
        *,
        include_hidden: bool = False,
        include_ignored: bool = False,
        git_ignored: set[str] | None = None,
    ) -> bool:
        try:
            rel = path.relative_to(self.root)
        except ValueError:
            return True
        parts = rel.parts
        if not include_hidden and any(part.startswith(".") for part in parts if part not in {".", ""}):
            return True
        if not include_ignored and any(part in DEFAULT_EXCLUDED_NAMES for part in parts):
            return True
        if include_ignored:
            return False
        rel_text = rel.as_posix()
        if rel_text in (git_ignored if git_ignored is not None else self.git_ignored_paths([rel_text])):
            return True
        return False

    def is_safe_existing_path(self, path: Path) -> bool:
        try:
            resolved = path.resolve(strict=True)
        except FileNotFoundError:
            return False
        return is_relative_to(resolved, self.root)

    def git_repository_for(self, path: Path) -> Path | None:
        """Return the nearest Git worktree root inside this workspace.

        The configured workspace may be an umbrella containing many unrelated
        repositories. Cache directory ancestry so large searches do not spawn
        failing ``git -C <umbrella>`` probes for every batch.
        """

        try:
            resolved = path.resolve(strict=True)
        except (FileNotFoundError, OSError):
            resolved = path.parent.resolve(strict=True)
        current = resolved if resolved.is_dir() else resolved.parent
        if not is_relative_to(current, self.root):
            return None
        visited: list[Path] = []
        repo: Path | None = None
        while True:
            if current in self._git_repo_cache:
                repo = self._git_repo_cache[current]
                break
            visited.append(current)
            if (current / ".git").exists():
                repo = current
                break
            if current == self.root:
                break
            current = current.parent
        for directory in visited:
            self._git_repo_cache[directory] = repo
        return repo

    def git_ignored_paths(self, rel_paths: list[str]) -> set[str]:
        if not rel_paths:
            return set()
        git = self.git_path
        if not git:
            return set()
        groups: dict[Path, list[tuple[str, str]]] = {}
        for workspace_rel in rel_paths:
            absolute = self.root / Path(workspace_rel)
            repo = self.git_repository_for(absolute)
            if repo is None:
                continue
            try:
                repo_rel = absolute.relative_to(repo).as_posix()
            except ValueError:
                continue
            groups.setdefault(repo, []).append((workspace_rel, repo_rel))
        ignored: set[str] = set()
        for repo, items in groups.items():
            reverse = {repo_rel: workspace_rel for workspace_rel, repo_rel in items}
            try:
                completed = subprocess.run(
                    git_command(git, repo, "-C", str(repo), "check-ignore", "--stdin", "-z"),
                    input="\0".join(reverse) + "\0",
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    timeout=2,
                )
            except (OSError, subprocess.SubprocessError):
                continue
            if completed.returncode not in {0, 1}:
                continue
            ignored.update(
                reverse[repo_rel]
                for repo_rel in completed.stdout.split("\0")
                if repo_rel in reverse
            )
        return ignored


__all__ = [
    "DEFAULT_EXCLUDED_NAMES",
    "WORKSPACE_ALLOWLIST_ENV",
    "ResolvedPath",
    "Workspace",
    "WorkspaceEntry",
    "is_relative_to",
    "normalize_rel_display",
    "validate_workspace_selection",
    "workspace_allowlist_from_env",
    "workspace_catalog_from_env",
    "workspace_entry_for_selector",
]
