# Human Help MCP

Standalone Human Help MCP extraction for the hackathon.

## Included now

This branch contains the full Human Help source surface, not only the MCP protocol shim:

- `human_help_mcp.py` — standalone stdio MCP exposing only `human_help_me`
- `browser-extension/` — the existing browser drawer / Human Help overlay UI source copied unchanged
- `assets/` — the existing Human Help mascot images copied unchanged
- `windows/interactive-broker.ps1` — the current signed-in Windows broker source, including the desktop Human Help form and mascot rendering
- `windows/WebConsoleBridge.cs` — the current browser bridge source used by the Human Help overlay
- Windows broker launcher/install/manage source

The copied UI/broker sources are intentionally byte-for-byte references to the currently working Coding Tools implementation. They are present here so the Human Help project has its actual face, not a rewritten imitation.

## Current test architecture

For now, the standalone MCP can reuse the already-installed Coding Tools broker queue:

```text
Agent / CLI
  -> Human Help MCP
  -> C:\ProgramData\WebGPTCodingToolsMCPService\interactive-requests
  -> existing signed-in broker
  -> Web Console Human Help overlay OR desktop Human Help form + mascot
  -> human answer
  -> MCP
  -> agent resumes
```

This means another MCP-capable CLI on the same Windows machine can test the independent MCP immediately without replacing the stable Coding Tools installation.

## MCP tool

Only one tool is exposed:

`human_help_me`

Required arguments:

- `reason`: `permission_blocked | gui_required | physical_action | faster_by_human | need_information | need_decision | other`
- `request`: focused human request

Optional:

- `expected_result`
- `return_to_agent`
- `mode`: `prefer_human | blocking`
- `fallback`: `continue_best_effort | wait_for_human`
- `delivery`: `auto | desktop_only | chat_only`
- `timeout_seconds`: `5..300`

## Run

No third-party Python dependency is required.

```powershell
python human_help_mcp.py
```

Generic MCP stdio configuration:

```json
{
  "mcpServers": {
    "Human-Help": {
      "command": "python",
      "args": ["D:\\coding-tools-mcp\\human-help-mcp\\human_help_mcp.py"]
    }
  }
}
```

## Suggested live test

Ask the CLI:

> Use `human_help_me` to ask me whether you should choose option A or option B.

Expected result: the same Human Help UI you already know appears, including the existing browser overlay behavior or desktop mascot fallback, and the answer returns to that CLI.

## Important boundary

The MCP surface itself contains no shell, Git, filesystem, workspace, Browser Use, or Computer Use tools.

The copied `windows/interactive-broker.ps1` is currently the original shared broker source, so it still contains legacy Coding Tools handlers internally. That is deliberate for this first extraction because the goal is to preserve the proven UI behavior while testing the independent MCP. After the hackathon, the broker can be pruned into a Human-Help-only implementation without changing the MCP contract.

## Smoke tests

```powershell
python -m unittest discover -s tests -v
```
