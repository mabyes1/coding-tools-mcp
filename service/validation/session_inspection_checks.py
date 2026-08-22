from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any


class _ExitedProcess:
    pid = 0

    @staticmethod
    def poll() -> int:
        return 0


def run_session_inspection_checks(server: Any) -> None:
    with tempfile.TemporaryDirectory(prefix="coding-tools-session-inspect-") as temporary:
        inspect_runtime = server.Runtime(Path(temporary), enable_view_image=False)
        try:
            inspected = server.ExecSession(
                "inspect-completed",
                _ExitedProcess(),
                command_preview="echo inspect",
                cwd=str(temporary),
            )
            inspected.append_stdout(b"first line\nneedle line\nlast line\n")
            inspected.refresh_status()
            inspect_runtime.output_sessions[inspected.session_id] = inspected

            listed = inspect_runtime.list_sessions({"include_completed": True})
            if listed.get("completed") != 1 or listed.get("active") != 0:
                raise RuntimeError("hidden list_sessions count contract drifted")
            listed_rows = listed.get("sessions", [])
            if not listed_rows or listed_rows[0].get("session_id") != inspected.session_id:
                raise RuntimeError("hidden list_sessions metadata contract drifted")

            tailed = inspect_runtime.tail_output(
                {"session_id": inspected.session_id, "stream": "stdout", "lines": 1}
            )
            if "last line" not in str(tailed.get("content") or ""):
                raise RuntimeError("hidden tail_output contract drifted")

            found = inspect_runtime.find_output(
                {"session_id": inspected.session_id, "query": "needle", "stream": "stdout"}
            )
            matches = found.get("matches", [])
            if len(matches) != 1 or matches[0].get("line") != 2 or matches[0].get("column") != 1:
                raise RuntimeError("hidden find_output contract drifted")

            tree = inspect_runtime.process_tree({"session_id": inspected.session_id})
            if tree.get("session_id") != inspected.session_id or tree.get("process_tree") != []:
                raise RuntimeError("hidden process_tree contract drifted")
        finally:
            inspect_runtime.close()
