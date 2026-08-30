from __future__ import annotations

import contextlib
import io
import time
from typing import Any


def run_http_transport_checks(
    server: Any,
    session_manager_class: Any,
    in_flight_ttl_seconds: int,
    session_ttl_seconds: int,
) -> None:
    class FakeRuntime:
        serial = 0

        def __init__(self) -> None:
            type(self).serial += 1
            self.http_session_id = f"selfcheck-{self.serial}"
            self.state_owner = None

        def close(self) -> None:
            return None

    sessions = session_manager_class(
        FakeRuntime,
        max_sessions=2,
        session_ttl_seconds=30,
        in_flight_ttl_seconds=30,
        max_sessions_per_owner=1,
    )
    sessions.create("owner")
    sessions.create("owner")
    stats = sessions.stats()
    stuck = sessions.create("stuck", acquire=True)
    stuck_record = sessions._sessions[stuck.session_id]
    stuck_record.last_seen -= 31
    stuck_record.in_flight_since = (stuck_record.in_flight_since or time.time()) - 31
    stale_stats = sessions.stats()
    sessions.close()
    if stats.get("capacity_evicted") != 1 or "expired" not in stats:
        raise RuntimeError("HTTP session diagnostics do not distinguish expiration from capacity eviction")
    if stale_stats.get("stale_in_flight_evicted") != 1 or stale_stats.get("in_flight") != 0:
        raise RuntimeError("stale HTTP in-flight lease watchdog did not evict a stuck lease")
    if in_flight_ttl_seconds != 90:
        raise RuntimeError("HTTP in-flight lease TTL must not exceed the 90-second request lifetime")
    if session_ttl_seconds != 300:
        raise RuntimeError("idle HTTP sessions must survive normal five-minute tool gaps")

    class DisconnectingWriter:
        def write(self, _body: bytes) -> None:
            raise ConnectionAbortedError("selfcheck disconnect")

    class DisconnectingHandler:
        def __init__(self) -> None:
            self.wfile = DisconnectingWriter()
            self.close_connection = False

    disconnect = DisconnectingHandler()
    if server._write_http_body_safely(disconnect, b"test") or not disconnect.close_connection:
        raise RuntimeError("client disconnects are not handled as normal response termination")

    quiet_server = object.__new__(server.RuntimeHTTPServer)
    stderr = io.StringIO()
    with contextlib.redirect_stderr(stderr):
        try:
            raise ConnectionResetError("selfcheck disconnect")
        except ConnectionResetError:
            quiet_server.handle_error(None, ("127.0.0.1", 1))
    if stderr.getvalue():
        raise RuntimeError("ordinary client disconnects still print server tracebacks")
