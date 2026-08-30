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

## Browser intent and tab ownership

Before any browser action, classify the intent into exactly one of these modes:

1. **User-directed page operation**
   - The user explicitly asked to operate the page they are currently using, such as filling a form, clicking controls, editing content, or completing a workflow on that site.
   - In this mode, it is acceptable to inspect and interact with the user's existing target tab when that tab is clearly the requested surface.
   - Do not navigate that tab away from its current site unless the user explicitly asked for navigation as part of the workflow.

2. **Assistant-initiated validation / research / debugging**
   - The browser is being used to verify behavior, inspect an extension, test a local UI, debug, search, compare results, or perform any other supporting work that the user did not ask to happen in their current tab.
   - Treat the user's current tab as **owned by the user and non-destructive**.
   - Never navigate, replace, reuse, or repurpose the user's current tab for this work.
   - Open a new tab first (`Ctrl+T`) and perform the work there. Use a new window instead when isolation is useful or the task is lengthy.
   - Prefer keeping assistant-created tabs/windows separate and close only those assistant-created surfaces when cleanup is useful.

When intent is ambiguous, default to **assistant-initiated validation / research / debugging** and preserve the user's current tab.

Opening a new tab or window may briefly change browser focus, but the assistant must not overwrite or navigate the user's existing page unless the user explicitly requested that page to be operated.

For page controls, prefer accessibility `element_index` when the target is clearly identified. Use coordinates only when the accessibility tree is insufficient.

## Authentication

Do not inspect cookies, local storage, browser password databases, saved passwords, or profile files to recover credentials.

If a requested page requires sign-in and the browser does not already have a usable session, ask the user to perform the credential step. Resume from the same browser window after they finish.

## Safety

Follow the Computer Use confirmation policy in `../computer-use/docs/confirmations.md` for state-changing browser actions.

Never use browser automation to bypass HTTPS/security interstitials, paywalls, CAPTCHAs, or permission boundaries.

## Current implementation note

Coding Tools Browser Use currently uses the Windows interactive browser surface plus a compiled .NET UI Automation helper rather than Codex's external `browser-client` runtime. This is deliberate: the skill and backend are self-contained in Coding Tools MCP and do not depend on `node_repl`, `@oai/sky`, or a missing Chrome helper package.

