---
name: control-chrome
description: Inspect and control the user's Chrome or Edge browser while preserving the signed-in desktop browser state.
---

# Browser Use

Use the `browser_use` MCP tool when the task depends on the user's existing Chrome/Edge state, a local web UI, an authenticated browser session, or visual/interactive page behavior.

Prefer a purpose-built connector/API/CLI for semantic operations when one exists and the user did not specifically ask for browser interaction. Browser UI is the right surface for local router/admin pages, visual testing, extension-dependent state, and workflows that exist only in the browser.

## Workflow

1. Call `browser_use` with `action=list_windows`.
2. Choose exactly one Chrome/Edge window from the returned ids.
3. Call `action=inspect` and read the returned accessibility elements and screenshot.
4. Perform one UI-changing action.
5. Inspect again before the next state-derived action.

For direct navigation use `action=navigate` with `url`. It reuses the selected browser window and its existing profile/session.

For page controls, prefer accessibility `element_index` when the target is clearly identified. Use coordinates only when the accessibility tree is insufficient.

## Authentication

Do not inspect cookies, local storage, browser password databases, saved passwords, or profile files to recover credentials.

If a requested page requires sign-in and the browser does not already have a usable session, ask the user to perform the credential step. Resume from the same browser window after they finish.

## Safety

Follow the Computer Use confirmation policy in `../computer-use/docs/confirmations.md` for state-changing browser actions.

Never use browser automation to bypass HTTPS/security interstitials, paywalls, CAPTCHAs, or permission boundaries.

## Current implementation note

Coding Tools Browser Use currently uses the Windows interactive browser surface plus a compiled .NET UI Automation helper rather than Codex's external `browser-client` runtime. This is deliberate: the skill and backend are self-contained in Coding Tools MCP and do not depend on `node_repl`, `@oai/sky`, or a missing Chrome helper package.

