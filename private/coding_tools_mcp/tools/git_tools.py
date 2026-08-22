from __future__ import annotations

import difflib
import re
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ..errors import ToolFailure
from ..gitutils import git_command
from ..patching import read_text_preserve_newlines
from ..textutils import DEFAULT_MAX_LINES, truncate_text_head
from ..workspace import ResolvedPath, Workspace
from .filesystem import truncation_fields


ResolveExisting = Callable[[str], ResolvedPath]
ResolveForWrite = Callable[[str], ResolvedPath]
GitPathFilter = Callable[[str], str]
CommandEnv = Callable[[dict[str, str]], dict[str, str]]
RequireGit = Callable[[], str]
DefaultCwd = Callable[[], Path]


def parse_branch_line(line: str) -> tuple[str, str, int, int]:
    branch = line
    upstream = ""
    ahead = 0
    behind = 0
    if "..." in line:
        branch, rest = line.split("...", 1)
        upstream = rest.split(" ", 1)[0]
    if "[" in line and "]" in line:
        meta = line.split("[", 1)[1].split("]", 1)[0]
        ahead_match = re.search(r"ahead (\d+)", meta)
        behind_match = re.search(r"behind (\d+)", meta)
        ahead = int(ahead_match.group(1)) if ahead_match else 0
        behind = int(behind_match.group(1)) if behind_match else 0
    return branch.strip(), upstream.strip(), ahead, behind


def validate_git_ref(ref: str) -> str:
    if not ref or ref.startswith("-") or "\x00" in ref or "\n" in ref or "\r" in ref:
        raise ToolFailure("INVALID_ARGUMENT", "Invalid git revision.", category="validation")
    return ref


def parse_git_blame_porcelain(output: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    current: dict[str, Any] = {}
    for raw in output.splitlines():
        parts = raw.split()
        if len(parts) >= 3 and re.fullmatch(r"[0-9a-fA-F^]{40}", parts[0]):
            current = {
                "commit": parts[0].lstrip("^"),
                "original_line": int(parts[1]) if parts[1].isdigit() else None,
                "line": int(parts[2]) if parts[2].isdigit() else None,
            }
            continue
        if raw.startswith("author "):
            current["author"] = raw.removeprefix("author ")
            continue
        if raw.startswith("author-mail "):
            current["author_mail"] = raw.removeprefix("author-mail ").strip("<>")
            continue
        if raw.startswith("author-time "):
            value = raw.removeprefix("author-time ")
            current["author_time"] = int(value) if value.isdigit() else value
            continue
        if raw.startswith("summary "):
            current["summary"] = raw.removeprefix("summary ")
            continue
        if raw.startswith("\t"):
            row = dict(current)
            row["content"] = raw[1:]
            rows.append(row)
    return rows


def parse_diff_files(diff_text: str) -> list[dict[str, Any]]:
    files: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for line in diff_text.splitlines():
        if line.startswith("diff --git "):
            parts = line.split()
            if len(parts) >= 4:
                path = parts[3][2:] if parts[3].startswith("b/") else parts[3]
                current = {"path": path, "status": "modified", "binary": False}
                files.append(current)
        elif current is not None and line.startswith("new file mode"):
            current["status"] = "added"
        elif current is not None and line.startswith("deleted file mode"):
            current["status"] = "deleted"
        elif current is not None and line.startswith("Binary files"):
            current["binary"] = True
    return files


class GitTools:
    """Git-domain operations with Runtime state supplied explicitly."""

    def __init__(
        self,
        *,
        workspace: Workspace,
        default_cwd: DefaultCwd,
        resolve_existing: ResolveExisting,
        resolve_for_write: ResolveForWrite,
        git_path_filter: GitPathFilter,
        command_env: CommandEnv,
        require_git: RequireGit,
        patch_baselines: dict[str, str | None],
    ) -> None:
        self.workspace = workspace
        self.default_cwd = default_cwd
        self.resolve_existing = resolve_existing
        self.resolve_for_write = resolve_for_write
        self.git_path_filter = git_path_filter
        self.command_env = command_env
        self.require_git = require_git
        self.patch_baselines = patch_baselines

    def git_env(self) -> dict[str, str]:
        return self.command_env({})

    def git_safe_directory(self, cmd: list[str]) -> Path:
        command_cwd = self.default_cwd()
        for index, token in enumerate(cmd[:-1]):
            if token != "-C":
                continue
            candidate = Path(cmd[index + 1])
            if not candidate.is_absolute():
                candidate = self.workspace.root / candidate
            try:
                command_cwd = candidate.resolve(strict=True)
            except OSError:
                command_cwd = candidate.parent.resolve(strict=True)
            break
        repo = self.workspace.git_repository_for(command_cwd)
        return repo or self.workspace.root

    def run_git_text(
        self, cmd: list[str], *, timeout: int | None = None, env: dict[str, str] | None = None
    ) -> subprocess.CompletedProcess[str]:
        safe_cmd = git_command(cmd[0], self.git_safe_directory(cmd), *cmd[1:])
        return subprocess.run(
            safe_cmd,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            env=self.git_env() if env is None else env,
        )

    def run_git_bytes(
        self, cmd: list[str], *, timeout: int | None = None, env: dict[str, str] | None = None
    ) -> subprocess.CompletedProcess[bytes]:
        safe_cmd = git_command(cmd[0], self.git_safe_directory(cmd), *cmd[1:])
        return subprocess.run(
            safe_cmd,
            text=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            env=self.git_env() if env is None else env,
        )

    @staticmethod
    def git_status_not_repo(completed: subprocess.CompletedProcess[str]) -> dict[str, Any]:
        warnings = []
        stderr = completed.stderr.strip()
        if stderr:
            warnings.append(f"git rev-parse failed: {stderr}")
        return {"is_repo": False, "clean": True, "entries": [], "truncated": False, "warnings": warnings}

    def is_git_repo(self, path: Path, *, env: dict[str, str] | None = None) -> bool:
        completed = self.run_git_text(
            [self.require_git(), "-C", str(path), "rev-parse", "--is-inside-work-tree"], env=env
        )
        return completed.returncode == 0 and completed.stdout.strip() == "true"

    def git_rev_parse(self, path: Path, rev: str, *, env: dict[str, str] | None = None) -> str:
        completed = self.run_git_text([self.require_git(), "-C", str(path), "rev-parse", rev], env=env)
        return completed.stdout.strip() if completed.returncode == 0 else ""

    def git_path_filters(self, args: dict[str, Any]) -> list[str]:
        path_filters: list[str] = []
        if isinstance(args.get("path"), str):
            path_filters.append(str(args["path"]))
        if isinstance(args.get("paths"), list):
            path_filters.extend(str(item) for item in args["paths"])
        return [self.git_path_filter(path) for path in path_filters]

    def git_repo_scope(self, args: dict[str, Any]) -> tuple[Path | None, list[str]]:
        requested: list[str] = []
        if isinstance(args.get("path"), str):
            requested.append(str(args["path"]))
        if isinstance(args.get("paths"), list):
            requested.extend(str(item) for item in args["paths"])
        if not requested:
            requested = ["."]

        repo: Path | None = None
        filters: list[str] = []
        for raw_path in requested:
            try:
                resolved = self.resolve_existing(raw_path)
            except ToolFailure as exc:
                if exc.code != "NOT_FOUND":
                    raise
                resolved = self.resolve_for_write(raw_path)
            probe = resolved.path if resolved.existed else resolved.path.parent
            current_repo = self.workspace.git_repository_for(probe)
            if current_repo is None:
                return None, []
            if repo is None:
                repo = current_repo
            elif repo != current_repo:
                raise ToolFailure(
                    "INVALID_ARGUMENT",
                    "A single Git tool call cannot span multiple repositories.",
                    category="validation",
                    details={"paths": requested},
                )
            try:
                repo_rel = resolved.path.relative_to(current_repo).as_posix()
            except ValueError:
                return None, []
            if repo_rel not in {"", "."}:
                filters.append(repo_rel)
        return repo, filters

    def status(self, args: dict[str, Any]) -> dict[str, Any]:
        resolved = self.resolve_existing(str(args.get("path", ".")))
        max_entries = int(args.get("max_entries", 1000))
        include_untracked = bool(args.get("include_untracked", True))
        git = self.require_git()
        git_env = self.git_env()
        root_check = self.run_git_text(
            [git, "-C", str(resolved.path), "rev-parse", "--show-toplevel"], env=git_env
        )
        if root_check.returncode != 0:
            return self.git_status_not_repo(root_check)
        status_cmd = [git, "-C", str(resolved.path), "status", "--porcelain=v1", "-b"]
        if not include_untracked:
            status_cmd.append("--untracked-files=no")
        completed = self.run_git_text(status_cmd, timeout=10, env=git_env)
        if completed.returncode != 0:
            raise ToolFailure("GIT_ERROR", completed.stderr.strip() or "git status failed", category="runtime")
        lines = completed.stdout.splitlines()
        branch = ""
        upstream = ""
        ahead = 0
        behind = 0
        entries: list[dict[str, Any]] = []
        for line in lines:
            if line.startswith("## "):
                branch, upstream, ahead, behind = parse_branch_line(line[3:])
                continue
            if not line:
                continue
            path_text = line[3:]
            original = None
            if " -> " in path_text:
                original, path_text = path_text.split(" -> ", 1)
            entries.append(
                {
                    "path": path_text,
                    "original_path": original,
                    "index_status": line[0],
                    "worktree_status": line[1],
                }
            )
            if len(entries) >= max_entries:
                break
        return {
            "is_repo": True,
            "branch": branch,
            "head": self.git_rev_parse(resolved.path, "HEAD", env=git_env),
            "upstream": upstream,
            "ahead": ahead,
            "behind": behind,
            "clean": not entries,
            "entries": entries,
            "truncated": len(entries) >= max_entries and len(lines) > max_entries + 1,
        }

    def diff(self, args: dict[str, Any]) -> dict[str, Any]:
        git = self.require_git()
        git_env = self.git_env()
        staged = bool(args.get("staged", False))
        unstaged = bool(args.get("unstaged", True))
        context = int(args.get("context_lines", 3))
        max_bytes = int(args.get("max_bytes", 262144))
        fallback_filters = self.git_path_filters(args)
        repo, path_filters = self.git_repo_scope(args)
        if repo is None:
            return self.fallback_diff(fallback_filters, max_bytes)
        chunks: list[bytes] = []
        if unstaged:
            chunks.append(self.run_git_diff(git, repo, context, path_filters, cached=False, env=git_env))
        if staged:
            chunks.append(self.run_git_diff(git, repo, context, path_filters, cached=True, env=git_env))
        combined = b""
        for chunk in chunks:
            if combined and chunk and not combined.endswith(b"\n"):
                combined += b"\n"
            combined += chunk
        diff_truncation = truncate_text_head(
            combined.decode("utf-8", errors="replace"), max_lines=DEFAULT_MAX_LINES, max_bytes=max_bytes
        )
        diff_text = diff_truncation.content
        truncated = diff_truncation.truncated
        return {
            "diff": diff_text,
            "files": parse_diff_files(diff_text),
            **truncation_fields(diff_truncation),
            "warnings": ["diff truncated"] if truncated else [],
        }

    def run_git_diff(
        self,
        git: str,
        repo: Path,
        context: int,
        path_filters: list[str],
        *,
        cached: bool,
        env: dict[str, str] | None = None,
    ) -> bytes:
        cmd = [git, "-C", str(repo), "diff", f"--unified={context}"]
        if cached:
            cmd.append("--cached")
        if path_filters:
            cmd.append("--")
            cmd.extend(path_filters)
        completed = self.run_git_bytes(cmd, timeout=10, env=env)
        if completed.returncode not in {0, 1}:
            raise ToolFailure("GIT_ERROR", completed.stderr.decode("utf-8", errors="replace"), category="runtime")
        return completed.stdout

    def fallback_diff(self, path_filters: list[str], max_bytes: int) -> dict[str, Any]:
        selected = set(path_filters)
        chunks: list[str] = []
        files: list[dict[str, Any]] = []
        for rel, before in sorted(self.patch_baselines.items()):
            if selected and rel not in selected:
                continue
            current_path = self.workspace.resolve_for_write(rel).path
            after = (
                read_text_preserve_newlines(current_path)
                if current_path.exists() and not current_path.is_dir()
                else None
            )
            if before == after:
                continue
            before_lines = [] if before is None else before.splitlines(keepends=True)
            after_lines = [] if after is None else after.splitlines(keepends=True)
            chunks.extend(
                difflib.unified_diff(
                    before_lines,
                    after_lines,
                    fromfile=f"a/{rel}",
                    tofile=f"b/{rel}",
                    lineterm="",
                )
            )
            status = "added" if before is None else "deleted" if after is None else "modified"
            files.append({"path": rel, "status": status, "binary": False})
        diff = "\n".join(chunks)
        if diff and not diff.endswith("\n"):
            diff += "\n"
        diff_truncation = truncate_text_head(diff, max_lines=DEFAULT_MAX_LINES, max_bytes=max_bytes)
        diff_text = diff_truncation.content
        truncated = diff_truncation.truncated
        return {
            "diff": diff_text,
            "files": files,
            **truncation_fields(diff_truncation),
            "warnings": ["non-git diff fallback"] + (["diff truncated"] if truncated else []),
        }

    def log(self, args: dict[str, Any]) -> dict[str, Any]:
        git = self.require_git()
        git_env = self.git_env()
        requested_path = str(args.get("path", "."))
        resolved = self.resolve_existing(requested_path)
        repo = self.workspace.git_repository_for(resolved.path)
        if repo is None:
            return {"is_repo": False, "commits": [], "truncated": False, "warnings": []}
        ref = validate_git_ref(str(args.get("ref", "HEAD")))
        max_count = int(args.get("max_count", 20))
        skip = int(args.get("skip", 0))
        path_filter = resolved.path.relative_to(repo).as_posix()
        cmd = [
            git,
            "-C",
            str(repo),
            "log",
            f"--max-count={max_count + 1}",
            f"--skip={skip}",
            "--date=iso-strict",
            "--pretty=format:%H%x1f%h%x1f%an%x1f%ae%x1f%ad%x1f%s%x1e",
            ref,
        ]
        if path_filter not in {"", "."}:
            cmd.extend(["--", path_filter])
        completed = self.run_git_text(cmd, timeout=10, env=git_env)
        if completed.returncode != 0:
            raise ToolFailure("GIT_ERROR", completed.stderr.strip() or "git log failed", category="runtime")
        commits: list[dict[str, Any]] = []
        for record in completed.stdout.split("\x1e"):
            fields = record.strip("\n").split("\x1f")
            if len(fields) < 6 or not fields[0]:
                continue
            commits.append(
                {
                    "hash": fields[0],
                    "short_hash": fields[1],
                    "author_name": fields[2],
                    "author_email": fields[3],
                    "author_date": fields[4],
                    "subject": fields[5],
                }
            )
        truncated = len(commits) > max_count
        result = {
            "is_repo": True,
            "ref": ref,
            "path": resolved.display,
            "max_count": max_count,
            "skip": skip,
            "commits": commits[:max_count],
            "truncated": truncated,
            "warnings": ["commit limit reached"] if truncated else [],
        }
        if truncated:
            result["next_action"] = {
                "tool": "git_log",
                "arguments": {
                    "path": requested_path,
                    "ref": ref,
                    "max_count": max_count,
                    "skip": skip + max_count,
                },
            }
        return result

    def show(self, args: dict[str, Any]) -> dict[str, Any]:
        git = self.require_git()
        git_env = self.git_env()
        repo, normalized_filters = self.git_repo_scope(args)
        if repo is None:
            return {"is_repo": False, "content": "", "files": [], "truncated": False, "warnings": []}
        rev = validate_git_ref(str(args.get("rev", "HEAD")))
        context = int(args.get("context_lines", 3))
        max_bytes = int(args.get("max_bytes", 262144))
        include_diff = bool(args.get("include_diff", True))
        cmd = [
            git,
            "-C",
            str(repo),
            "show",
            "--no-ext-diff",
            "--format=fuller",
            f"--unified={context}",
        ]
        if not include_diff:
            cmd.append("--no-patch")
        cmd.append(rev)
        if normalized_filters:
            cmd.append("--")
            cmd.extend(normalized_filters)
        completed = self.run_git_bytes(cmd, timeout=10, env=git_env)
        if completed.returncode != 0:
            raise ToolFailure(
                "GIT_ERROR",
                completed.stderr.decode("utf-8", errors="replace").strip() or "git show failed",
                category="runtime",
            )
        truncation = truncate_text_head(
            completed.stdout.decode("utf-8", errors="replace"),
            max_lines=DEFAULT_MAX_LINES,
            max_bytes=max_bytes,
        )
        content = truncation.content
        return {
            "is_repo": True,
            "rev": rev,
            "content": content,
            "files": parse_diff_files(content),
            **truncation_fields(truncation),
            "warnings": ["output truncated"] if truncation.truncated else [],
        }

    def blame(self, args: dict[str, Any]) -> dict[str, Any]:
        git = self.require_git()
        git_env = self.git_env()
        requested_path = str(args.get("path", ""))
        resolved = self.resolve_existing(requested_path)
        if resolved.path.is_dir():
            raise ToolFailure("IS_DIRECTORY", "Path is a directory.", category="validation")
        repo = self.workspace.git_repository_for(resolved.path)
        if repo is None:
            return {"is_repo": False, "path": resolved.display, "lines": [], "truncated": False, "warnings": []}
        ref_arg = args.get("rev")
        ref = validate_git_ref(str(ref_arg)) if isinstance(ref_arg, str) and ref_arg else None
        start_line = int(args.get("start_line", 1))
        end_line = args.get("end_line")
        max_lines = int(args.get("max_lines", 200))
        if end_line is None:
            requested_final_line = start_line + max_lines - 1
        else:
            requested_final_line = int(end_line)
        if requested_final_line < start_line:
            raise ToolFailure("INVALID_ARGUMENT", "end_line must be >= start_line.", category="validation")
        requested_lines = requested_final_line - start_line + 1
        truncated = requested_lines > max_lines
        final_line = min(requested_final_line, start_line + max_lines - 1)
        cmd = [
            git,
            "-C",
            str(repo),
            "blame",
            "--line-porcelain",
            "-L",
            f"{start_line},{final_line}",
        ]
        if ref:
            cmd.append(ref)
        cmd.extend(["--", resolved.path.relative_to(repo).as_posix()])
        completed = self.run_git_text(cmd, timeout=10, env=git_env)
        if completed.returncode != 0:
            raise ToolFailure("GIT_ERROR", completed.stderr.strip() or "git blame failed", category="runtime")
        lines = parse_git_blame_porcelain(completed.stdout)
        if len(lines) > max_lines:
            lines = lines[:max_lines]
            truncated = True
        result = {
            "is_repo": True,
            "path": resolved.display,
            "rev": ref,
            "start_line": start_line,
            "end_line": final_line,
            "max_lines": max_lines,
            "lines": lines,
            "truncated": truncated,
            "warnings": ["line limit reached"] if truncated else [],
        }
        if truncated and final_line < requested_final_line:
            next_arguments: dict[str, Any] = {
                "path": requested_path,
                "start_line": final_line + 1,
                "end_line": requested_final_line,
                "max_lines": max_lines,
            }
            if ref:
                next_arguments["rev"] = ref
            result["next_action"] = {
                "tool": "git_blame",
                "arguments": next_arguments,
            }
        return result


__all__ = [
    "GitTools",
    "parse_branch_line",
    "parse_diff_files",
    "parse_git_blame_porcelain",
    "validate_git_ref",
]
