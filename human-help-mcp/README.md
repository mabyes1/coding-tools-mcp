# Human Help MCP

Standalone Human Help MCP extraction for the hackathon.

This branch includes the actual Human Help presentation sources as well as the MCP server: browser overlay UI, Windows desktop broker UI, Web Console bridge source, and the existing mascot assets.

Use `human_help_mcp.py` as the stdio MCP entry point. For the current live test, it intentionally reuses the already-installed broker queue at `C:\ProgramData\WebGPTCodingToolsMCPService\interactive-requests`, so another CLI on the same Windows machine can call the independent MCP and get the same Human Help UI.

Only `human_help_me` is exposed by this MCP.
