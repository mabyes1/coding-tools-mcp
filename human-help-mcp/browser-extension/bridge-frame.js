(async () => {
  try {
    const response = await fetch("http://127.0.0.1:8768/v1/health", {
      headers: { "X-Coding-Tools-Console": "1" },
      cache: "no-store",
      targetAddressSpace: "loopback"
    });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    window.parent.postMessage({ source: "coding-tools-bridge-frame", ok: true }, "*");
  } catch (error) {
    window.parent.postMessage({
      source: "coding-tools-bridge-frame",
      ok: false,
      error: String(error && error.message || error)
    }, "*");
  }
})();
