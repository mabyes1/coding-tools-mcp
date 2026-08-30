const BROWSER_AGENT_BASE = "http://127.0.0.1:8768";
const BROWSER_AGENT_STORAGE_KEY = "codingToolsBrowserAgentTabId";
const BROWSER_AGENT_GROUP_TITLE = "Coding Tools · Browser Use";
const BROWSER_AGENT_ALARM = "coding-tools-browser-agent-poll";
let browserAgentTickPromise = null;

async function browserAgentRequest(path, options = {}) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), options.timeout || 9000);
  try {
    const response = await fetch(`${BROWSER_AGENT_BASE}${path}`, {
      method: options.method || "GET",
      headers: {
        "X-Coding-Tools-Console": "1",
        "X-Coding-Tools-Extension": "1",
        ...(options.body ? { "Content-Type": "application/json" } : {})
      },
      body: options.body ? JSON.stringify(options.body) : undefined,
      cache: "no-store",
      targetAddressSpace: "loopback",
      signal: controller.signal
    });
    if (response.status === 204) return null;
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || `HTTP ${response.status}`);
    return payload;
  } finally {
    clearTimeout(timer);
  }
}

async function getStoredAgentTabId() {
  const stored = await chrome.storage.local.get(BROWSER_AGENT_STORAGE_KEY);
  const value = Number(stored[BROWSER_AGENT_STORAGE_KEY]);
  return Number.isInteger(value) && value > 0 ? value : null;
}

async function getTabOrNull(tabId) {
  if (!tabId) return null;
  try { return await chrome.tabs.get(tabId); } catch (_) { return null; }
}

async function ensureBrowserAgentTab() {
  let tab = await getTabOrNull(await getStoredAgentTabId());
  if (tab) return tab;
  tab = await chrome.tabs.create({ active: false, url: "about:blank" });
  if (!tab || !tab.id) throw new Error("browser_agent_tab_create_failed");
  try {
    const groupId = await chrome.tabs.group({ tabIds: [tab.id] });
    await chrome.tabGroups.update(groupId, { title: BROWSER_AGENT_GROUP_TITLE, color: "cyan", collapsed: false });
    tab = await chrome.tabs.get(tab.id);
  } catch (_) { }
  await chrome.storage.local.set({ [BROWSER_AGENT_STORAGE_KEY]: tab.id });
  return tab;
}

function browserWindowRow(tab) {
  return {
    id: tab.id,
    title: tab.title || "Coding Tools Browser Use",
    process_id: 0,
    process_name: "chrome",
    class_name: "browser_extension_agent_tab",
    url: tab.url || "",
    tab_group: BROWSER_AGENT_GROUP_TITLE,
    background: tab.active !== true
  };
}

async function waitForTabComplete(tabId, timeoutMs = 20000) {
  const current = await chrome.tabs.get(tabId);
  if (current.status === "complete") return current;
  return await new Promise((resolve, reject) => {
    const timer = setTimeout(() => {
      chrome.tabs.onUpdated.removeListener(listener);
      reject(new Error("browser_navigation_timeout"));
    }, timeoutMs);
    function listener(updatedId, changeInfo, tab) {
      if (updatedId !== tabId || changeInfo.status !== "complete") return;
      clearTimeout(timer);
      chrome.tabs.onUpdated.removeListener(listener);
      resolve(tab);
    }
    chrome.tabs.onUpdated.addListener(listener);
  });
}

async function executeInAgentTab(tabId, func, args = []) {
  return await withDebugger(tabId, async send => {
    const expression = `(${func.toString()})(...${JSON.stringify(args)})`;
    const evaluation = await send("Runtime.evaluate", {
      expression,
      awaitPromise: true,
      returnByValue: true,
      userGesture: true
    });
    if (evaluation.exceptionDetails) {
      const detail = evaluation.exceptionDetails.exception?.description || evaluation.exceptionDetails.text || "browser_script_failed";
      throw new Error(detail);
    }
    if (!evaluation.result || !("value" in evaluation.result)) throw new Error("browser_script_returned_no_result");
    return evaluation.result.value;
  });
}

async function inspectBrowserAgentTab(tabId) {
  return await executeInAgentTab(tabId, () => {
    const visible = element => {
      const style = getComputedStyle(element);
      const rect = element.getBoundingClientRect();
      return style.display !== "none" && style.visibility !== "hidden" && rect.width > 1 && rect.height > 1;
    };
    const nameOf = element => String(
      element.getAttribute("aria-label") ||
      (element.labels && element.labels[0] && element.labels[0].innerText) ||
      element.getAttribute("title") || element.getAttribute("placeholder") ||
      element.innerText || element.value || element.id || ""
    ).trim().replace(/\s+/g, " ").slice(0, 240);
    const typeOf = element => {
      const tag = element.tagName.toLowerCase();
      if (tag === "button" || element.getAttribute("role") === "button") return "Button";
      if (tag === "input" || tag === "textarea" || element.isContentEditable) return "Edit";
      if (tag === "a") return "Hyperlink";
      if (tag === "select") return "ComboBox";
      return element.getAttribute("role") || tag;
    };
    const candidates = Array.from(document.querySelectorAll("body *"))
      .filter(element => visible(element) && (nameOf(element) || /^(INPUT|TEXTAREA|BUTTON|A|SELECT)$/.test(element.tagName)))
      .slice(0, 350);
    const elements = candidates.map((element, index) => {
      const rect = element.getBoundingClientRect();
      return {
        index,
        type: typeOf(element),
        name: nameOf(element),
        automation_id: element.id || "",
        enabled: !element.disabled,
        offscreen: rect.bottom <= 0 || rect.right <= 0 || rect.top >= innerHeight || rect.left >= innerWidth,
        x: Math.round(rect.left), y: Math.round(rect.top),
        width: Math.round(rect.width), height: Math.round(rect.height)
      };
    });
    return { elements, width: innerWidth, height: innerHeight, title: document.title, url: location.href };
  });
}

async function targetPoint(tabId, request) {
  return await executeInAgentTab(tabId, req => {
    function ensureCursor() {
      let cursor = document.getElementById("__coding_tools_browser_cursor__");
      if (cursor) return cursor;
      cursor = document.createElement("div");
      cursor.id = "__coding_tools_browser_cursor__";
      cursor.setAttribute("aria-hidden", "true");
      cursor.style.cssText = "position:fixed;left:0;top:0;width:30px;height:36px;z-index:2147483647;pointer-events:none;filter:drop-shadow(0 1px 2px #0008);transition:transform .08s ease;";
      cursor.innerHTML = '<svg viewBox="0 0 30 36" width="30" height="36"><path d="M3 2v25l7-7 6 14 6-3-6-13h11z" fill="#4ac2ff" stroke="white" stroke-width="2.6" stroke-linejoin="round"/></svg>';
      (document.documentElement || document.body).appendChild(cursor);
      return cursor;
    }
    const visible = element => {
      const style = getComputedStyle(element); const rect = element.getBoundingClientRect();
      return style.display !== "none" && style.visibility !== "hidden" && rect.width > 1 && rect.height > 1;
    };
    const nameOf = element => String(element.getAttribute("aria-label") || (element.labels && element.labels[0] && element.labels[0].innerText) || element.getAttribute("title") || element.getAttribute("placeholder") || element.innerText || element.value || element.id || "").trim().replace(/\s+/g, " ").slice(0, 240);
    const candidates = Array.from(document.querySelectorAll("body *")).filter(element => visible(element) && (nameOf(element) || /^(INPUT|TEXTAREA|BUTTON|A|SELECT)$/.test(element.tagName))).slice(0, 350);
    let element = null;
    let x = Number(req.x);
    let y = Number(req.y);
    if (Number.isInteger(req.element_index)) {
      element = candidates[req.element_index] || null;
      if (!element) throw new Error("stale_element_index");
      const rect = element.getBoundingClientRect();
      x = rect.left + rect.width / 2; y = rect.top + rect.height / 2;
    } else {
      if (!Number.isFinite(x) || !Number.isFinite(y)) throw new Error("browser_target_required");
      element = document.elementFromPoint(x, y);
    }
    const cursor = ensureCursor();
    cursor.style.transform = `translate(${Math.round(x - 4)}px,${Math.round(y - 3)}px)`;
    cursor.style.opacity = "1";
    clearTimeout(window.__codingToolsCursorTimer);
    window.__codingToolsCursorTimer = setTimeout(() => { cursor.style.opacity = ".35"; }, 850);
    if (element && req.focus === true) {
      element.focus({ preventScroll: true });
      if (req.replace === true && typeof element.select === "function") element.select();
    }
    return { x: Math.round(x), y: Math.round(y) };
  }, [request]);
}

async function withDebugger(tabId, operation) {
  const target = { tabId };
  await chrome.debugger.attach(target, "1.3");
  try { return await operation((method, params = {}) => chrome.debugger.sendCommand(target, method, params)); }
  finally { try { await chrome.debugger.detach(target); } catch (_) { } }
}

async function captureAgentTab(tabId) {
  return await withDebugger(tabId, async send => {
    const metrics = await send("Page.getLayoutMetrics");
    const shot = await send("Page.captureScreenshot", { format: "jpeg", quality: 82, fromSurface: true });
    const viewport = metrics.cssVisualViewport || metrics.visualViewport || {};
    return { data: shot.data, width: Math.round(viewport.clientWidth || 0), height: Math.round(viewport.clientHeight || 0) };
  });
}

async function dispatchMouse(tabId, point, button, clickCount = 1) {
  await withDebugger(tabId, async send => {
    const cdpButton = button === "right" ? "right" : button === "middle" ? "middle" : "left";
    const buttons = cdpButton === "right" ? 2 : cdpButton === "middle" ? 4 : 1;
    await send("Input.dispatchMouseEvent", { type: "mouseMoved", x: point.x, y: point.y });
    await send("Input.dispatchMouseEvent", { type: "mousePressed", x: point.x, y: point.y, button: cdpButton, buttons, clickCount });
    await send("Input.dispatchMouseEvent", { type: "mouseReleased", x: point.x, y: point.y, button: cdpButton, buttons: 0, clickCount });
  });
}

function keyDescription(raw) {
  const parts = String(raw || "").split("+").map(value => value.trim().toUpperCase()).filter(Boolean);
  let modifiers = 0; let basis = "";
  for (const part of parts) {
    if (["ALT", "ALT_L"].includes(part)) modifiers |= 1;
    else if (["CTRL", "CONTROL", "CONTROL_L"].includes(part)) modifiers |= 2;
    else if (["META", "COMMAND"].includes(part)) modifiers |= 4;
    else if (["SHIFT", "SHIFT_L"].includes(part)) modifiers |= 8;
    else basis = part;
  }
  const map = {
    ENTER: ["Enter", "Enter", 13], RETURN: ["Enter", "Enter", 13], TAB: ["Tab", "Tab", 9],
    ESC: ["Escape", "Escape", 27], ESCAPE: ["Escape", "Escape", 27], SPACE: [" ", "Space", 32],
    BACKSPACE: ["Backspace", "Backspace", 8], DELETE: ["Delete", "Delete", 46],
    LEFT: ["ArrowLeft", "ArrowLeft", 37], UP: ["ArrowUp", "ArrowUp", 38], RIGHT: ["ArrowRight", "ArrowRight", 39], DOWN: ["ArrowDown", "ArrowDown", 40],
    HOME: ["Home", "Home", 36], END: ["End", "End", 35], PAGEUP: ["PageUp", "PageUp", 33], PAGEDOWN: ["PageDown", "PageDown", 34]
  };
  for (let i = 1; i <= 12; i++) map[`F${i}`] = [`F${i}`, `F${i}`, 111 + i];
  const mapped = map[basis] || [basis.length === 1 ? basis.toLowerCase() : basis, `Key${basis}`, basis.length === 1 ? basis.charCodeAt(0) : 0];
  if (!mapped[0]) throw new Error("unsupported_browser_key");
  return { key: mapped[0], code: mapped[1], windowsVirtualKeyCode: mapped[2], nativeVirtualKeyCode: mapped[2], modifiers };
}

async function buildBrowserResponse(tab, action, request) {
  const response = { ok: true, retryable: false, action, window: browserWindowRow(tab), background_tab: true, message: "Browser Use completed in its agent-created Chrome tab without redirecting the user's selected tab." };
  const includeText = request.include_text !== false && action !== "screenshot";
  const includeScreenshot = request.include_screenshot !== false || action === "screenshot";
  if (includeText) Object.assign(response, await inspectBrowserAgentTab(tab.id));
  if (includeScreenshot) {
    const shot = await captureAgentTab(tab.id);
    response.screenshot = { mime_type: "image/jpeg", width: shot.width, height: shot.height };
    response.screenshot_base64 = shot.data;
  }
  return response;
}

async function executeBrowserAgentRequest(request) {
  const action = String(request.action || "inspect").toLowerCase();
  let tab = await ensureBrowserAgentTab();
  if (action === "list_windows") return { ok: true, retryable: false, action, windows: [browserWindowRow(tab)], message: "Browser agent tab discovery completed." };
  if (request.window_id != null && Number(request.window_id) !== tab.id) throw new Error("browser_agent_tab_mismatch");

  if (action === "navigate") {
    const url = String(request.text || "").trim();
    if (!/^https?:\/\//i.test(url) && !/^about:blank$/i.test(url)) throw new Error("browser_navigation_url_not_allowed");
    await chrome.tabs.update(tab.id, { url, active: false });
    tab = await waitForTabComplete(tab.id);
  } else if (action === "click" || action === "right_click") {
    const point = await targetPoint(tab.id, request);
    await dispatchMouse(tab.id, point, action === "right_click" ? "right" : "left");
  } else if (action === "type_text") {
    const text = String(request.text || "");
    if (!text) throw new Error("browser_type_text_required");
    await targetPoint(tab.id, { ...request, focus: true, replace: true });
    await withDebugger(tab.id, send => send("Input.insertText", { text }));
  } else if (action === "press_key") {
    const key = keyDescription(request.key);
    await withDebugger(tab.id, async send => {
      await send("Input.dispatchKeyEvent", { type: "rawKeyDown", ...key });
      await send("Input.dispatchKeyEvent", { type: "keyUp", ...key });
    });
  } else if (action === "scroll") {
    const point = await targetPoint(tab.id, request.element_index != null || request.x != null ? request : { ...request, x: 40, y: 160 });
    await withDebugger(tab.id, send => send("Input.dispatchMouseEvent", { type: "mouseWheel", x: point.x, y: point.y, deltaX: 0, deltaY: Number(request.scroll_y || 0) }));
  } else if (!["inspect", "screenshot", "activate"].includes(action)) {
    throw new Error("unsupported_browser_action");
  }
  tab = await chrome.tabs.get(tab.id);
  return await buildBrowserResponse(tab, action, request);
}

async function browserAgentTick() {
  const pending = await browserAgentRequest("/v1/browser/next", { timeout: 10000 });
  if (!pending || !pending.request_id || !pending.request) return;
  let response;
  try { response = await executeBrowserAgentRequest(pending.request); }
  catch (error) {
    response = { ok: false, error: "BROWSER_EXTENSION_ACTION_FAILED", message: String(error && error.message || error), retryable: true };
  }
  await browserAgentRequest("/v1/browser/respond", { method: "POST", body: { request_id: pending.request_id, response }, timeout: 10000 });
}

function kickCodingToolsBrowserAgent() {
  if (browserAgentTickPromise) return browserAgentTickPromise;
  browserAgentTickPromise = browserAgentTick()
    .catch(() => null)
    .finally(() => { browserAgentTickPromise = null; });
  return browserAgentTickPromise;
}

chrome.alarms.onAlarm.addListener(alarm => {
  if (alarm && alarm.name === BROWSER_AGENT_ALARM) kickCodingToolsBrowserAgent();
});

function startCodingToolsBrowserAgent() {
  chrome.alarms.create(BROWSER_AGENT_ALARM, { periodInMinutes: 0.5 });
  kickCodingToolsBrowserAgent();
}
