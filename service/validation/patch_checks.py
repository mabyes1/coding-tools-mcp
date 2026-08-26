from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any, Callable


def run_patch_checks(server: Any, find_subsequence_all: Callable[[list[str], list[str]], list[int]]) -> None:
    if find_subsequence_all(["x"] * 12_000, ["x"] * 6_000) != list(range(6_001)):
        raise RuntimeError("linear patch hunk matcher did not find overlapping matches correctly")

    with tempfile.TemporaryDirectory(prefix="coding-tools-patch-check-") as temporary:
        patch_workspace = Path(temporary)
        patch_project = patch_workspace / "project"
        patch_project.mkdir()
        patch_runtime = server.Runtime(patch_workspace, enable_view_image=False)
        try:
            patch_runtime.state_owner = "patch-check-owner"
            selected = patch_runtime.set_default_cwd({"path": "project"})
            if selected.get("default_cwd") != "project":
                raise RuntimeError("apply_patch characterization could not select project cwd")

            added = patch_runtime.apply_patch(
                {
                    "patch": "\n".join(
                        [
                            "*** Begin Patch",
                            "*** Add File: new.txt",
                            "+alpha",
                            "+beta",
                            "*** End Patch",
                        ]
                    )
                }
            )
            new_file = patch_project / "new.txt"
            if new_file.read_text(encoding="utf-8") != "alpha\nbeta\n":
                raise RuntimeError("apply_patch add-file content contract drifted")
            if added.get("base") != "project" or added.get("additions") != 2:
                raise RuntimeError("apply_patch add-file result contract drifted")
            baseline_key = "project/new.txt"
            if baseline_key not in patch_runtime.patch_baselines or patch_runtime.patch_baselines[baseline_key] is not None:
                raise RuntimeError("apply_patch add-file baseline contract drifted")

            baselines_before_dry_run = dict(patch_runtime.patch_baselines)
            dry_run = patch_runtime.apply_patch(
                {
                    "dry_run": True,
                    "patch": "\n".join(
                        [
                            "*** Begin Patch",
                            "*** Update File: new.txt",
                            "@@",
                            "-alpha",
                            "+dry-run-change",
                            " beta",
                            "*** End Patch",
                        ]
                    ),
                }
            )
            if not dry_run.get("dry_run") or new_file.read_text(encoding="utf-8") != "alpha\nbeta\n":
                raise RuntimeError("apply_patch dry-run mutation contract drifted")
            if patch_runtime.patch_baselines != baselines_before_dry_run:
                raise RuntimeError("apply_patch dry-run baseline contract drifted")

            ambiguous = patch_project / "ambiguous.txt"
            ambiguous.write_text("head\ntarget\nomega\nmiddle\ntarget\nomega\ntail\n", encoding="utf-8")
            try:
                patch_runtime.apply_patch(
                    {
                        "patch": "\n".join(
                            [
                                "*** Begin Patch",
                                "*** Update File: ambiguous.txt",
                                "@@",
                                "-target",
                                "+changed",
                                " omega",
                                "*** End Patch",
                            ]
                        )
                    }
                )
            except server.ToolFailure as exc:
                if exc.code != "PATCH_CONTEXT_AMBIGUOUS":
                    raise RuntimeError("ambiguous patch context returned the wrong error") from exc
                if exc.details.get("candidate_lines") != [2, 5]:
                    raise RuntimeError("ambiguous patch context stopped reporting candidate line numbers") from exc
                if "@@ -120,3 +120,3 @@" not in str(exc.details.get("retry_hint") or ""):
                    raise RuntimeError("ambiguous patch context stopped teaching the agent to use a location hint") from exc
            else:
                raise RuntimeError("ambiguous patch context was silently guessed without a location hint")

            located = patch_runtime.apply_patch(
                {
                    "patch": "\n".join(
                        [
                            "*** Begin Patch",
                            "*** Update File: ambiguous.txt",
                            "@@ -5,2 +5,2 @@",
                            "-target",
                            "+changed",
                            " omega",
                            "*** End Patch",
                        ]
                    )
                }
            )
            if ambiguous.read_text(encoding="utf-8") != "head\ntarget\nomega\nmiddle\nchanged\nomega\ntail\n":
                raise RuntimeError("unified hunk line hint did not select the nearest exact context match")
            if located.get("additions") != 1 or located.get("removals") != 1:
                raise RuntimeError("location-hinted patch result accounting drifted")

            tied = patch_project / "tied.txt"
            tied.write_text("head\ntarget\nomega\na\nb\ntarget\nomega\ntail\n", encoding="utf-8")
            try:
                patch_runtime.apply_patch(
                    {
                        "patch": "\n".join(
                            [
                                "*** Begin Patch",
                                "*** Update File: tied.txt",
                                "@@ -4,2 +4,2 @@",
                                "-target",
                                "+changed",
                                " omega",
                                "*** End Patch",
                            ]
                        )
                    }
                )
            except server.ToolFailure as exc:
                if exc.code != "PATCH_CONTEXT_AMBIGUOUS" or exc.details.get("candidate_lines") != [2, 6]:
                    raise RuntimeError("equidistant location hint stopped failing safely with candidate lines") from exc
            else:
                raise RuntimeError("equidistant location hint guessed between identical patch contexts")

            moved = patch_runtime.apply_patch(
                {
                    "patch": "\n".join(
                        [
                            "*** Begin Patch",
                            "*** Update File: new.txt",
                            "*** Move to: moved.txt",
                            "@@",
                            "-alpha",
                            "+gamma",
                            " beta",
                            "*** End Patch",
                        ]
                    )
                }
            )
            moved_file = patch_project / "moved.txt"
            if new_file.exists() or moved_file.read_text(encoding="utf-8") != "gamma\nbeta\n":
                raise RuntimeError("apply_patch move/update filesystem contract drifted")
            moved_entries = moved.get("affected_files", [])
            if not any(
                item.get("path") == "project/moved.txt"
                and item.get("old_path") == "project/new.txt"
                and item.get("operation") == "move"
                for item in moved_entries
            ):
                raise RuntimeError("apply_patch move result metadata contract drifted")
            if "project/moved.txt" not in patch_runtime.patch_baselines:
                raise RuntimeError("apply_patch move destination baseline contract drifted")

            existing = patch_project / "existing.txt"
            existing.write_text("keep\n", encoding="utf-8")
            partial = patch_project / "should-not-exist.txt"
            try:
                patch_runtime.apply_patch(
                    {
                        "patch": "\n".join(
                            [
                                "*** Begin Patch",
                                "*** Add File: should-not-exist.txt",
                                "+temporary",
                                "*** Add File: existing.txt",
                                "+replacement",
                                "*** End Patch",
                            ]
                        )
                    }
                )
            except server.ToolFailure as exc:
                if exc.code != "PATCH_FAILED":
                    raise RuntimeError("apply_patch staged-failure error contract drifted") from exc
            else:
                raise RuntimeError("apply_patch staged-failure contract stopped rejecting existing add target")
            if partial.exists() or existing.read_text(encoding="utf-8") != "keep\n":
                raise RuntimeError("apply_patch staged validation failure partially committed files")

            deleted = patch_runtime.apply_patch(
                {
                    "patch": "\n".join(
                        [
                            "*** Begin Patch",
                            "*** Delete File: moved.txt",
                            "*** End Patch",
                        ]
                    )
                }
            )
            if moved_file.exists() or deleted.get("removals") != 2:
                raise RuntimeError("apply_patch delete-file contract drifted")
        finally:
            patch_runtime.close()
