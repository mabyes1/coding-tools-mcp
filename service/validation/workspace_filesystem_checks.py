from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any


def run_workspace_filesystem_checks(server: Any) -> None:
    original_workspace_allowlist = os.environ.get(server.WORKSPACE_ALLOWLIST_ENV)
    try:
        with tempfile.TemporaryDirectory(prefix="coding-tools-workspace-contract-") as temporary:
            contract_root = Path(temporary)
            alpha = contract_root / "alpha"
            beta = contract_root / "beta"
            outside = contract_root / "outside"
            nested = alpha / "nested"
            alpha.mkdir()
            beta.mkdir()
            outside.mkdir()
            nested.mkdir()
            marker = nested / "marker.txt"
            marker.write_text("workspace-contract\n", encoding="utf-8")
            os.environ[server.WORKSPACE_ALLOWLIST_ENV] = f"Alpha={alpha}{os.pathsep}Beta={beta}"

            catalog = server.workspace_catalog_from_env()
            if [entry.name for entry in catalog] != ["Alpha", "Beta"]:
                raise RuntimeError("workspace allowlist selector order/names drifted")
            if [entry.path for entry in catalog] != [alpha.resolve(), beta.resolve()]:
                raise RuntimeError("workspace allowlist path normalization drifted")
            if server.workspace_entry_for_selector("alpha").path != alpha.resolve():
                raise RuntimeError("workspace selector matching stopped being case-insensitive")
            if server.workspace_entry_for_selector(str(beta)).name != "Beta":
                raise RuntimeError("workspace selector stopped accepting an exact allowlisted path")
            allowed = server.validate_workspace_selection(alpha)
            if allowed != (alpha.resolve(), beta.resolve()):
                raise RuntimeError("workspace selection validation no longer returns the configured roots")
            try:
                server.validate_workspace_selection(outside)
            except server.ToolFailure as exc:
                if exc.code != "WORKSPACE_NOT_ALLOWED":
                    raise
            else:
                raise RuntimeError("workspace selection accepted a root outside the private allowlist")

            workspace_contract = server.Workspace(alpha)
            inside_absolute = workspace_contract.resolve_existing(str(marker.resolve()))
            if inside_absolute.path != marker.resolve() or inside_absolute.display != "nested/marker.txt":
                raise RuntimeError("absolute path inside the workspace no longer normalizes to a relative display")
            if workspace_contract.resolve_existing("nested/marker.txt").path != marker.resolve():
                raise RuntimeError("relative workspace path resolution drifted")
            try:
                workspace_contract.resolve_existing("../outside")
            except server.ToolFailure as exc:
                if exc.code != "PATH_OUTSIDE_WORKSPACE":
                    raise
            else:
                raise RuntimeError("workspace traversal guard accepted '..'")
            try:
                workspace_contract.resolve_existing(str(outside.resolve()))
            except server.ToolFailure as exc:
                if exc.code != "ABSOLUTE_PATH_DENIED":
                    raise
            else:
                raise RuntimeError("workspace accepted an absolute path outside its root")
            pending = workspace_contract.resolve_for_write("nested/new-file.txt")
            if pending.existed or pending.path != (nested / "new-file.txt").resolve(strict=False):
                raise RuntimeError("workspace write-target resolution drifted for a new file")
            if server.normalize_rel_display(alpha, alpha) != ".":
                raise RuntimeError("workspace relative display for root drifted")

            docs = alpha / "docs"
            docs_nested = docs / "nested"
            docs_nested.mkdir(parents=True)
            (docs / "a.txt").write_text("alpha\nbeta\n", encoding="utf-8")
            (docs_nested / "c.txt").write_text("gamma beta\n", encoding="utf-8")
            (docs / "binary.bin").write_bytes(b"abc\x00def")
            filesystem_runtime = server.Runtime(alpha, enable_view_image=False)
            try:
                read_result = filesystem_runtime.read_file(
                    {"path": "docs/a.txt", "start_line": 2, "max_lines": 1}
                )
                read_content = str(read_result.get("content") or "").replace("\r\n", "\n")
                if read_content != "beta\n" or read_result.get("end_line") != 2:
                    raise RuntimeError("read_file line-selection contract drifted")
                try:
                    filesystem_runtime.read_file({"path": "docs/binary.bin"})
                except server.ToolFailure as exc:
                    if exc.code != "BINARY_FILE":
                        raise
                else:
                    raise RuntimeError("read_file binary guard drifted")

                listed = filesystem_runtime.list_dir(
                    {"path": "docs", "recursive": True, "max_depth": 3, "sort": "name"}
                )
                listed_paths = {str(item.get("path")) for item in listed.get("entries", [])}
                if not {"docs/a.txt", "docs/nested", "docs/nested/c.txt"}.issubset(listed_paths):
                    raise RuntimeError("list_dir recursive path contract drifted")

                files = filesystem_runtime.list_files(
                    {"path": "docs", "patterns": ["*.txt"], "sort": "path"}
                )
                file_paths = {str(item.get("path")) for item in files.get("files", [])}
                if not {"docs/a.txt", "docs/nested/c.txt"}.issubset(file_paths):
                    raise RuntimeError("list_files glob contract drifted")

                searched = filesystem_runtime.search_text(
                    {"query": "beta", "path": "docs", "case_sensitive": True}
                )
                match_paths = {str(item.get("path")) for item in searched.get("matches", [])}
                if match_paths != {"docs/a.txt", "docs/nested/c.txt"}:
                    raise RuntimeError("search_text literal-match contract drifted")
                if int(searched.get("total_matches", -1)) != 2:
                    raise RuntimeError("search_text total-match contract drifted")
            finally:
                filesystem_runtime.close()
    finally:
        if original_workspace_allowlist is None:
            os.environ.pop(server.WORKSPACE_ALLOWLIST_ENV, None)
        else:
            os.environ[server.WORKSPACE_ALLOWLIST_ENV] = original_workspace_allowlist
