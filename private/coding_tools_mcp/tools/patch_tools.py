from __future__ import annotations

from collections.abc import Callable
from typing import Any

from ..errors import ToolFailure
from ..patching import (
    AtomicPatchCommitter,
    FileBaseline,
    StagedFile,
    apply_update_hunks,
    parse_patch,
)
from ..workspace import ResolvedPath, Workspace


ResolveExisting = Callable[[str], ResolvedPath]
ResolveForWrite = Callable[[str], ResolvedPath]
DefaultCwdDisplay = Callable[[], str]


def normalize_patch_path(
    raw_path: str,
    *,
    require_existing: bool,
    resolve_existing: ResolveExisting,
    resolve_for_write: ResolveForWrite,
) -> str:
    """Return a workspace-relative patch path resolved from the default cwd."""
    resolved = resolve_existing(raw_path) if require_existing else resolve_for_write(raw_path)
    return resolved.display


def commit_staged_files(
    staged: list[StagedFile],
    *,
    patch_committer: AtomicPatchCommitter,
    patch_baselines: dict[str, str | None],
) -> None:
    patch_committer.commit(staged)
    for change in staged:
        if change.display in patch_baselines:
            continue
        patch_baselines[change.display] = (
            None if change.baseline.data is None else change.baseline.data.decode("utf-8", errors="replace")
        )


def apply_patch_tool(
    args: dict[str, Any],
    *,
    workspace: Workspace,
    resolve_existing: ResolveExisting,
    resolve_for_write: ResolveForWrite,
    default_cwd_display: DefaultCwdDisplay,
    patch_lock: Any,
    patch_committer: AtomicPatchCommitter,
    patch_baselines: dict[str, str | None],
) -> dict[str, Any]:
    patch = str(args.get("patch", ""))
    dry_run = bool(args.get("dry_run", False))
    with patch_lock:
        operations = parse_patch(patch)
        for op in operations:
            op.path = normalize_patch_path(
                op.path,
                require_existing=op.kind in {"update", "delete"},
                resolve_existing=resolve_existing,
                resolve_for_write=resolve_for_write,
            )
            if op.move_to:
                op.move_to = normalize_patch_path(
                    op.move_to,
                    require_existing=False,
                    resolve_existing=resolve_existing,
                    resolve_for_write=resolve_for_write,
                )
        staged: dict[str, StagedFile] = {}
        summaries: list[str] = []
        affected: list[dict[str, str]] = []
        additions = 0
        removals = 0
        for op in operations:
            if op.kind in {"add", "update", "delete"}:
                workspace.reject_write_symlink(op.path)
            if op.move_to:
                workspace.reject_write_symlink(op.move_to)
            if op.kind == "add":
                target = workspace.resolve_for_write(op.path)
                if target.existed:
                    raise ToolFailure("PATCH_FAILED", "Cannot add file that already exists.", category="validation")
                baseline = FileBaseline.capture(target.path)
                staged[target.display] = StagedFile(
                    target.display,
                    target.path,
                    op.add_content or "",
                    baseline,
                    None,
                )
                affected.append({"path": target.display, "operation": "add"})
                summaries.append(f"A {target.display}")
                additions += len((op.add_content or "").splitlines())
            elif op.kind == "delete":
                target = workspace.resolve_existing(op.path)
                if target.path.is_dir():
                    raise ToolFailure("PATCH_FAILED", "Cannot delete a directory.", category="validation")
                prior = staged.get(target.display)
                baseline = prior.baseline if prior is not None else FileBaseline.capture(target.path)
                staged[target.display] = StagedFile(target.display, target.path, None, baseline, baseline.mode)
                affected.append({"path": target.display, "operation": "delete"})
                summaries.append(f"D {target.display}")
                removals += len((baseline.data or b"").splitlines())
            elif op.kind == "update":
                source = workspace.resolve_existing(op.path)
                if source.path.is_dir():
                    raise ToolFailure("PATCH_FAILED", "Cannot update a directory.", category="validation")
                prior = staged.get(source.display)
                if prior is not None and prior.content is None:
                    raise ToolFailure("PATCH_FAILED", "Cannot update a deleted file.", category="validation")
                baseline = prior.baseline if prior is not None else FileBaseline.capture(source.path)
                content = prior.content if prior is not None else baseline.text(source.display)
                assert content is not None
                updated = apply_update_hunks(content, op.hunks, op.path)
                for hunk in op.hunks:
                    for line in hunk:
                        additions += line.startswith("+")
                        removals += line.startswith("-")
                source_mode = prior.mode if prior is not None else baseline.mode
                if op.move_to:
                    dest = workspace.resolve_for_write(op.move_to)
                    if dest.existed and dest.display != source.display:
                        raise ToolFailure("PATCH_FAILED", "Cannot move over an existing file.", category="validation")
                    dest_baseline = baseline if dest.display == source.display else FileBaseline.capture(dest.path)
                    staged[source.display] = StagedFile(
                        source.display,
                        source.path,
                        None,
                        baseline,
                        source_mode,
                    )
                    staged[dest.display] = StagedFile(
                        dest.display,
                        dest.path,
                        updated,
                        dest_baseline,
                        source_mode,
                    )
                    affected.append({"path": dest.display, "old_path": source.display, "operation": "move"})
                    summaries.append(f"R {source.display} -> {dest.display}")
                else:
                    staged[source.display] = StagedFile(
                        source.display,
                        source.path,
                        updated,
                        baseline,
                        source_mode,
                    )
                    affected.append({"path": source.display, "operation": "update"})
                    summaries.append(f"M {source.display}")
        if not affected:
            raise ToolFailure("PATCH_FAILED", "No files were modified.", category="validation")
        if not dry_run:
            commit_staged_files(
                list(staged.values()),
                patch_committer=patch_committer,
                patch_baselines=patch_baselines,
            )
    return {
        "dry_run": dry_run,
        "clean": True,
        "base": default_cwd_display(),
        "summary": "\n".join(summaries),
        "affected_files": affected,
        "additions": additions,
        "removals": removals,
        "warnings": [],
    }


__all__ = [
    "apply_patch_tool",
    "commit_staged_files",
    "normalize_patch_path",
]
