$ErrorActionPreference = "Stop"

Write-Host "The private MCP now contains the session manager natively; no sitecustomize patch is required."
Write-Host "Use manage-web-gpt-mcp.bat for health/prune/restart, or service\update-private-mcp.ps1 for a controlled update."
Write-Host "No service or Python environment was changed."
