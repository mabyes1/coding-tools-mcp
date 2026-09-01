# Human Help MCP

A standalone MCP server that lets an AI agent deliberately hand one small step back to a human.

This first extraction intentionally **does not modify `coding-tools-mcp`**. It reuses the existing Windows interactive broker / Web Console queue so the current Human Help UI, Web Console focus mode, desktop fallback, and typing-based timeout extension keep working unchanged.

## Tool

`human_help_me`

Use it when the agent is blocked by something that genuinely needs a human: a physical action, a decision, missing information, a GUI-only step, or a permission boundary.

The input contract is intentionally kept compatible with the current `coding-tools-mcp` implementation:

- `reason`: `permission_blocked | gui_required | physical_action | faster_by_human | need_information | need_decision | other`
- `request`: focused human request
- `expected_result`: optional success condition
- `return_to_agent`: optional instruction for what the agent should do next
- `mode`: `prefer_human | blocking`
- `fallback`: `continue_best_effort | wait_for_human`
- `delivery`: `auto | desktop_only | chat_only`
- `timeout_seconds`: `5..300`

## Run

No third-party Python dependency is required.

```powershell
python human_help_mcp.py
```

Or install the local package:

```powershell
pip install -e .
human-help-mcp
```

Generic MCP stdio configuration:

```json
{
  "mcpServers": {
    "human-help": {
      "command": "python",
      "args": ["D:\\coding-tools-mcp\\human-help-mcp\\human_help_mcp.py"]
    }
  }
}
```

## Current bridge

By default the server writes Human Help requests to the same broker queue used by the existing Coding Tools installation:

```text
C:\ProgramData\WebGPTCodingToolsMCPService\interactive-requests
```

Queue override precedence:

1. `HUMAN_HELP_MCP_INTERACTIVE_QUEUE`
2. `CODING_TOOLS_MCP_INTERACTIVE_QUEUE`
3. the default path above

This means the standalone MCP is already a separate **agent-facing MCP surface**, while the presentation/broker layer is temporarily shared.

After the hackathon, the broker, Web Console bridge, and UI can be moved into this project too. That later migration can happen without changing the MCP tool contract.

## Design boundary

This project intentionally contains no shell execution, Git, filesystem, workspace, or Computer Use tools.

Its job is one sentence:

> When an agent should not or cannot do one small step itself, give the human a clean way to take over and return the result.

## Smoke test

```powershell
python -m unittest discover -s tests -v
```

The tests use `delivery=chat_only`, so they do not require the Windows broker.
