from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any


class _ExitedProcess:
    pid = 0

    @staticmethod
    def poll() -> int:
        return 0


def run_session_output_checks(server: Any) -> None:
    with tempfile.TemporaryDirectory(prefix="coding-tools-output-check-") as temporary:
        output_runtime = server.Runtime(Path(temporary), enable_view_image=False)
        try:
            delta_session = server.ExecSession("output-delta-contract", _ExitedProcess())
            delta_session.append_stdout(b"abcdef")
            delta_session.append_stderr(b"XYZ")
            delta = output_runtime._snapshot_session(
                delta_session,
                {"output_mode": "delta", "after_cursor": {"stdout": 2, "stderr": 1}},
                65536,
            )
            if delta.get("stdout") != "cdef" or delta.get("stderr") != "YZ":
                raise RuntimeError("explicit-cursor session delta contract drifted")
            if delta.get("cursor") != {"stdout": 6, "stderr": 3}:
                raise RuntimeError("session snapshot cursor contract drifted")
            try:
                output_runtime._snapshot_session(delta_session, {"output_mode": "definitely-invalid"}, 65536)
            except server.ToolFailure as exc:
                if exc.code != "INVALID_ARGUMENT":
                    raise RuntimeError("invalid output-mode error contract drifted") from exc
            else:
                raise RuntimeError("invalid output mode stopped being rejected")

            paged_session = server.ExecSession("output-page-contract", _ExitedProcess())
            paged_session.append_stdout(b"abcdef")
            truncated_payload = output_runtime._snapshot_session(paged_session, {"output_mode": "full"}, 3)
            formatted = output_runtime._format_session_output(paged_session, truncated_payload, {})
            expected_ref = "session:output-page-contract:stdout"
            if not formatted.get("output_truncated") or formatted.get("output_ref") != expected_ref:
                raise RuntimeError("truncated output_ref formatting contract drifted")
            next_action = formatted.get("next_action")
            if not isinstance(next_action, dict) or next_action.get("tool") != "read_output":
                raise RuntimeError("truncated terminal output lost read_output next_action")
            if next_action.get("arguments", {}).get("output_ref") != expected_ref:
                raise RuntimeError("truncated terminal output next_action ref drifted")
            page = output_runtime.read_output({"output_ref": expected_ref, "offset": 1, "limit": 2})
            if page.get("content") != "bc" or page.get("next_offset") != 3:
                raise RuntimeError("read_output byte paging contract drifted")
            page_next = page.get("next_action")
            if not isinstance(page_next, dict) or page_next.get("arguments", {}).get("offset") != 3:
                raise RuntimeError("read_output next-page action contract drifted")
            try:
                output_runtime.read_output({"output_ref": expected_ref, "stream": "stderr"})
            except server.ToolFailure as exc:
                if exc.code != "INVALID_ARGUMENT":
                    raise RuntimeError("read_output stream-mismatch error contract drifted") from exc
            else:
                raise RuntimeError("read_output stopped rejecting stream/output_ref mismatch")
        finally:
            output_runtime.close()
