const CONSOLE_BASE = "http://127.0.0.1:8768";
const CONSOLE_REQUEST_TIMEOUT_MS = 4000;

async function consoleRequest(path, options = {}) {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), CONSOLE_REQUEST_TIMEOUT_MS);
  try {
    const response = await fetch(`${CONSOLE_BASE}${path}`, {
      method: options.method || "GET",
      headers: {
        "X-Coding-Tools-Console": "1",
        ...(options.body ? { "Content-Type": "application/json" } : {})
      },
      body: options.body ? JSON.stringify(options.body) : undefined,
      cache: "no-store",
      targetAddressSpace: "loopback",
      signal: controller.signal
    });
    const payload = response.status === 204 ? {} : await response.json();
    if (!response.ok) throw new Error(payload.error || `HTTP ${response.status}`);
    return payload;
  } catch (error) {
    if (error && error.name === "AbortError") throw new Error("console_request_timeout");
    throw error;
  } finally {
    clearTimeout(timeout);
  }
}

chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  if (!message || message.type !== "coding-tools-console-request") return false;
  consoleRequest(message.path, message.options)
    .then((payload) => sendResponse({ ok: true, payload }))
    .catch((error) => sendResponse({ ok: false, error: String(error && error.message || error) }));
  return true;
});

chrome.action.onClicked.addListener(async (tab) => {
  if (!tab.id) return;
  try { await chrome.tabs.sendMessage(tab.id, { type: "coding-tools-console-toggle" }); } catch (_) { }
});
