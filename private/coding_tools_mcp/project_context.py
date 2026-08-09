from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .gitutils import git_command


CONTEXT_FILE_NAMES = frozenset({"AGENTS.md", "AGENTS.MD", "CLAUDE.md", "CLAUDE.MD"})
SKIPPED_CONTEXT_DIRS = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
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
)
MAX_ROOT_CONTEXT_BYTES = 32 * 1024
MAX_CONTEXT_FILE_BYTES = 16 * 1024
MAX_NESTED_CONTEXT_FILES = 64
MAX_CONTEXT_SCAN_DIRS = 5_000
MAX_CONTEXT_SCAN_DEPTH = 12


@dataclass(frozen=True)
class LoadedContextFile:
    path: str
    content: str
    truncated: bool


@dataclass(frozen=True)
class ProjectContext:
    root_files: tuple[LoadedContextFile, ...]
    nested_files: tuple[str, ...]
    warnings: tuple[str, ...]

    def server_instructions(self) -> str:
        sections = [
            "Use these tools only for coding operations inside the configured workspace.",
            "Use apply_patch as the only direct file-modification tool; do not modify files through exec_command.",
            "Relative paths use the persistent default cwd. Use set_default_cwd when entering a project; a directory-only cd/chdir/Set-Location exec is also persisted automatically.",
        ]
        for item in self.root_files:
            suffix = " [truncated]" if item.truncated else ""
            sections.append(f"Project instructions from {item.path}{suffix}:\n{item.content}")
        if self.nested_files:
            paths = "\n".join(f"- {path}" for path in self.nested_files)
            sections.append(
                "Nested project instruction files are available below. Before modifying files under one of their "
                f"directories, read the applicable instruction file with read_file:\n{paths}"
            )
        if self.warnings:
            sections.append("Project-context warnings:\n" + "\n".join(f"- {warning}" for warning in self.warnings))
        return "\n\n".join(sections)


def load_project_context(root: Path) -> ProjectContext:
    resolved_root = root.expanduser().resolve(strict=True)
    loaded: list[LoadedContextFile] = []
    warnings: list[str] = []
    remaining = MAX_ROOT_CONTEXT_BYTES
    for name in sorted(CONTEXT_FILE_NAMES):
        path = resolved_root / name
        if not path.is_file():
            continue
        try:
            resolved = path.resolve(strict=True)
            resolved.relative_to(resolved_root)
        except (OSError, ValueError):
            warnings.append(f"Skipped unsafe root instruction path: {name}")
            continue
        if remaining <= 0:
            warnings.append("Root instruction byte limit reached.")
            break
        budget = min(MAX_CONTEXT_FILE_BYTES, remaining)
        try:
            with resolved.open("rb") as handle:
                data = handle.read(budget + 1)
            content = _decode_utf8_prefix(data[:budget])
        except UnicodeDecodeError:
            warnings.append(f"Skipped non-UTF-8 instruction file: {name}")
            continue
        except OSError as exc:
            warnings.append(f"Could not read {name}: {exc}")
            continue
        truncated = len(data) > budget
        loaded.append(LoadedContextFile(name, content, truncated))
        remaining -= len(content.encode("utf-8"))

    loaded_names = {item.path for item in loaded}
    nested = [path for path in _discover_context_files(resolved_root, warnings) if path not in loaded_names]
    if len(nested) > MAX_NESTED_CONTEXT_FILES:
        nested = nested[:MAX_NESTED_CONTEXT_FILES]
        warnings.append(f"Nested instruction list truncated to {MAX_NESTED_CONTEXT_FILES} files.")
    return ProjectContext(tuple(loaded), tuple(nested), tuple(warnings))


def _discover_context_files(root: Path, warnings: list[str]) -> list[str]:
    git_paths = _git_context_files(root)
    if git_paths is not None:
        return git_paths

    # The configured workspace is often an umbrella directory containing
    # several independent repositories. Probe immediate child repositories
    # with git first so large dependency/build trees never enter the Python
    # directory walk merely to discover AGENTS.md / CLAUDE.md files.
    discovered: list[str] = []
    git_scanned_children: set[str] = set()
    try:
        children = sorted(root.iterdir(), key=lambda path: path.name.casefold())
    except OSError:
        children = []
    for child in children:
        if not child.is_dir() or child.is_symlink() or child.name in SKIPPED_CONTEXT_DIRS:
            continue
        child_paths = _git_context_files(child)
        if child_paths is None:
            continue
        git_scanned_children.add(child.name)
        discovered.extend(f"{child.name}/{path}" for path in child_paths)
        for worktree in _git_worktree_roots(child):
            try:
                worktree_relative = worktree.relative_to(root)
            except ValueError:
                continue
            if worktree_relative == Path(child.name):
                continue
            worktree_paths = _git_context_files(worktree)
            if worktree_paths is None:
                continue
            prefix = worktree_relative.as_posix()
            discovered.extend(f"{prefix}/{path}" for path in worktree_paths)
        if len(discovered) > MAX_NESTED_CONTEXT_FILES:
            return sorted(set(discovered))

    # When the workspace is an umbrella containing real child repositories,
    # those repositories define the project boundaries. Do not recursively
    # walk unrelated non-repository trees such as SDKs, device dumps, caches,
    # or tool installations merely to look for instruction filenames. We
    # still honor instruction files placed directly at a non-repository
    # child's root.
    if git_scanned_children:
        for child in children:
            if (
                not child.is_dir()
                or child.is_symlink()
                or child.name in SKIPPED_CONTEXT_DIRS
                or child.name in git_scanned_children
            ):
                continue
            for name in sorted(CONTEXT_FILE_NAMES):
                candidate = child / name
                if candidate.is_file() and not candidate.is_symlink():
                    discovered.append(candidate.relative_to(root).as_posix())
        return sorted(set(discovered))

    scanned_dirs = 0
    for current, dirs, files in os.walk(root, followlinks=False):
        scanned_dirs += 1
        if scanned_dirs > MAX_CONTEXT_SCAN_DIRS:
            warnings.append(f"Project-context scan stopped after {MAX_CONTEXT_SCAN_DIRS} directories.")
            return discovered
        current_path = Path(current)
        depth = len(current_path.relative_to(root).parts)
        dirs[:] = sorted(
            name
            for name in dirs
            if name not in SKIPPED_CONTEXT_DIRS
            and not (depth == 0 and name in git_scanned_children)
            and depth < MAX_CONTEXT_SCAN_DEPTH
        )
        file_names = set(files)
        for name in sorted(CONTEXT_FILE_NAMES):
            if name not in file_names:
                continue
            path = current_path / name
            if path.is_symlink():
                continue
            discovered.append(path.relative_to(root).as_posix())
    return discovered


def _git_context_files(root: Path) -> list[str] | None:
    pathspecs = sorted(CONTEXT_FILE_NAMES) + [
        f":(glob)**/{name}" for name in sorted(CONTEXT_FILE_NAMES)
    ]
    try:
        completed = subprocess.run(
            git_command(
                "git",
                root,
                "-C",
                str(root),
                "ls-files",
                "-co",
                "--exclude-standard",
                "-z",
                "--",
                *pathspecs,
            ),
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    paths: list[str] = []
    for raw in completed.stdout.split(b"\0"):
        if not raw:
            continue
        try:
            path = raw.decode("utf-8")
        except UnicodeDecodeError:
            continue
        parts = Path(path).parts
        if len(parts) > MAX_CONTEXT_SCAN_DEPTH + 1 or any(part in SKIPPED_CONTEXT_DIRS for part in parts[:-1]):
            continue
        if parts and parts[-1] in CONTEXT_FILE_NAMES:
            paths.append(Path(path).as_posix())
        if len(paths) >= MAX_NESTED_CONTEXT_FILES + 1:
            break
    return sorted(set(paths))


def _git_worktree_roots(repo_root: Path) -> list[Path]:
    try:
        completed = subprocess.run(
            git_command(
                "git",
                repo_root,
                "-C",
                str(repo_root),
                "worktree",
                "list",
                "--porcelain",
            ),
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if completed.returncode != 0:
        return []

    roots: list[Path] = []
    marker = b"worktree "
    for raw_line in completed.stdout.splitlines():
        if not raw_line.startswith(marker):
            continue
        try:
            candidate = Path(raw_line[len(marker) :].decode("utf-8")).resolve(strict=True)
        except (OSError, UnicodeDecodeError):
            continue
        roots.append(candidate)
    return roots


def _decode_utf8_prefix(data: bytes) -> str:
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        if exc.reason != "unexpected end of data":
            raise
        return data[: exc.start].decode("utf-8")
