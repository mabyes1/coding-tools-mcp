# Coding Tools skills

This directory contains model-facing operating guidance for capabilities that are implemented by Coding Tools MCP.

The split is intentional:

- MCP tools provide the executable capability.
- `SKILL.md` files describe when to use that capability, the safe workflow, and recovery behavior.
- Skills must describe the Coding Tools implementation that actually ships in this repository. Do not leave dependencies on external Codex-only runtimes such as `node_repl`, `@oai/sky`, or `browser-client.mjs` unless those runtimes are also bundled here.

`server_info` discovers `*/SKILL.md` files in this directory and exposes their paths to the model.

