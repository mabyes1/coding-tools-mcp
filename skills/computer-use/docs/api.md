# `computer_use` API

The public MCP tool exposes one bounded Windows UI action per call.

## Actions

- `list_windows`: discover targetable top-level windows.
- `inspect`: return accessibility elements and, by default, a screenshot.
- `screenshot`: capture the selected window without accessibility enumeration.
- `activate`: restore/foreground the selected window.
- `click`: click by `element_index` or window-relative `x`/`y`.
- `right_click`: right-click by `element_index` or window-relative `x`/`y`.
- `type_text`: send literal Unicode text to the current focus.
- `press_key`: send a supported key or chord such as `ENTER`, `TAB`, `ESC`, `CTRL+A`, or `ALT+LEFT`.
- `scroll`: mouse-wheel from window-relative `x`/`y` using `scroll_y`.

## Target selection

Use one or more of:

- `window_id`: opaque id returned by `list_windows` or `inspect`.
- `process_name`: optional process filter.
- `title`: optional case-insensitive substring filter.

If selection matches zero or multiple windows, stop and select from a fresh `list_windows` result.

## Observation fields

`inspect` can return `elements`, each containing:

- `index`
- `type`
- `name`
- `automation_id`
- `enabled`
- `offscreen`
- window-relative `x`, `y`, `width`, `height` when bounds are available

Element indexes are not durable identifiers. Re-observe after UI changes.

