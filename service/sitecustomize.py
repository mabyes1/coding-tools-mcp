"""Local service policy for the Streamable HTTP session manager.

The upstream defaults retain disconnected HTTP sessions for one hour. A
ChatGPT connector can reconnect several times without sending DELETE, so the
fixed 128-session cap is reached even though the server is healthy. Keep the
cap bounded, but reap abandoned sessions quickly for this single-user tunnel.
"""

from coding_tools_mcp import transport_http

transport_http.MAX_HTTP_SESSIONS = 256
transport_http.HTTP_SESSION_TTL_SECONDS = 300
