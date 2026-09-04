# Kate Portable Coding MCP

This is the hackathon rescue path for running **a separate Coding MCP on Kate's own Windows PC**.
It intentionally does not reuse Ken's machine, OAuth secrets, Cloudflare credentials, workspace paths, or Secure MCP Tunnel profile.

## What this portable build gives you

- Local workspace-confined Coding MCP
- `read_file`, `list_files`, `search_text`, `apply_patch`, `exec_command`, git tools, etc.
- OAuth 2.1 + PKCE with a generated local authorization password
- A temporary HTTPS endpoint through Cloudflare Quick Tunnel
- `safe`, `trusted`, and `dangerous` permission modes

This first portable build is **Coding Core**. The Windows desktop broker stack used by `computer_use`, `human_help_me`, and other interactive desktop actions is not installed by this rescue package yet.

## Install once

Open PowerShell in the repository root and run:

```powershell
powershell -ExecutionPolicy Bypass -File .\portable\KATE-INSTALL.ps1 -Workspace "C:\path\to\kates\code"
```

Requirements:

- Windows x64
- Python 3.11 or newer available as `py` or `python`
- Internet access during first install

The installer creates only `portable\.runtime` inside this checkout. It does not copy Ken's credentials.

## Run

```powershell
powershell -ExecutionPolicy Bypass -File .\portable\KATE-RUN.ps1
```

The script prints two important values:

1. `Connector URL`, ending in `/mcp`
2. `OAuth password`

Use the Connector URL when creating Kate's custom MCP connector in ChatGPT. When the OAuth authorization page opens, paste the generated OAuth password.

The default permission mode is `trusted`. To use the full YOLO-style local development mode intentionally:

```powershell
powershell -ExecutionPolicy Bypass -File .\portable\KATE-RUN.ps1 -PermissionMode dangerous
```

## Important limitation

The `trycloudflare.com` URL is temporary. If the tunnel restarts, the URL changes and the ChatGPT connector needs to be updated/recreated. This is deliberate for the hackathon rescue package. A persistent per-user Secure MCP Tunnel installer can replace this later.

## Security model

The public temporary endpoint is protected by the MCP server's OAuth flow. The generated password and token secret stay under `portable\.runtime` and are excluded from the handoff archive.

