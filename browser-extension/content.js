(() => {
  if (window.top !== window || document.getElementById("coding-tools-console-host")) return;

  const host = document.createElement("div");
  host.id = "coding-tools-console-host";
  host.style.cssText = "all:initial!important;position:fixed!important;inset:0!important;z-index:2147483647!important;pointer-events:none!important;";
  (document.documentElement || document.body).appendChild(host);
  const root = host.attachShadow({ mode: "open" });
  root.innerHTML = `
    <style>
      :host{all:initial;color-scheme:dark;font-family:"Segoe UI","Microsoft JhengHei UI","Noto Sans TC",sans-serif}
      *{box-sizing:border-box}button,input,textarea{font:inherit}button{cursor:pointer}
      .edge,.shell{font-family:"Segoe UI","Microsoft JhengHei UI","Noto Sans TC",sans-serif}
      .edge{pointer-events:auto;position:fixed;right:0;top:42%;width:34px;height:116px;border:1px solid #344251;border-right:0;border-radius:12px 0 0 12px;background:#101820;color:#dce8f2;box-shadow:0 12px 35px #0008;display:grid;place-items:center;transition:transform .22s ease,opacity .22s ease}
      .edge:hover{background:#15212c}.edge span{writing-mode:vertical-rl;letter-spacing:.16em;font-size:11px}.edge i{position:absolute;top:13px;width:7px;height:7px;border-radius:50%;background:#7d8995}.edge i.live{background:#55d6a3;box-shadow:0 0 0 4px #55d6a31c}.edge i.attn{background:#ffb45e;animation:pulse 1.4s infinite}
      .shell{pointer-events:auto;position:fixed;right:0;top:0;height:100vh;width:min(430px,calc(100vw - 20px));background:#0c1218;border-left:1px solid #293744;box-shadow:-24px 0 64px #0009;transform:translateX(102%);transition:transform .25s cubic-bezier(.2,.8,.2,1);display:grid;grid-template-rows:auto auto 1fr auto;color:#e8eef4}
      .shell.open{transform:translateX(0)}.shell.open~.edge{transform:translateX(110%);opacity:0}
      .header{padding:18px 18px 13px;border-bottom:1px solid #26333e;background:#101820}.titleRow{display:flex;align-items:center;gap:10px}.mark{width:24px;height:24px;border:1px solid #526679;display:grid;place-items:center;font:700 10px/1 Consolas,monospace;color:#71d9b1}.title{font-size:15px;font-weight:700;letter-spacing:.04em}.spacer{flex:1}.iconBtn{width:32px;height:32px;border:0;border-radius:7px;background:transparent;color:#94a6b7;font-size:19px}.iconBtn:hover{background:#1b2833;color:#fff}
      .sub{display:flex;align-items:center;gap:8px;margin-top:10px;color:#8496a8;font-size:11px}.statusDot{width:7px;height:7px;border-radius:50%;background:#697783}.statusDot.live{background:#55d6a3}.mode{margin-left:auto;color:#ff8f8f;font-weight:700}.mode:empty{display:none}
      .tabs{display:flex;gap:4px;padding:9px 13px;border-bottom:1px solid #26333e;background:#0f171e}.tab{border:0;background:transparent;color:#8295a6;border-radius:6px;padding:8px 11px;font-size:12px}.tab.active{background:#1a2731;color:#eef5fa}.badge{display:inline-grid;min-width:18px;height:18px;padding:0 5px;margin-left:5px;place-items:center;border-radius:9px;background:#ad6638;color:#fff;font-size:10px}.badge[hidden]{display:none}
      .body{min-height:0;overflow:auto;padding:14px 15px 22px;overscroll-behavior:contain;scrollbar-color:#536170 transparent;scrollbar-width:thin}.empty{margin:54px 22px;color:#7e909f;text-align:center;line-height:1.7;font-size:13px}.empty strong{display:block;color:#c9d5df;margin-bottom:5px}
      .event{display:grid;grid-template-columns:52px 1fr;gap:8px;padding:10px 2px;border-bottom:1px solid #17232d}.eventTime{color:#637789;font:11px/1.6 Consolas,monospace}.eventMain{min-width:0}.eventTitle{font-size:12px;color:#dbe7ef;line-height:1.45}.event.start .eventTitle{color:#77b9ed}.event.done .eventTitle{color:#5bddad}.event.fail .eventTitle{color:#ff8a8a}.eventDetail{margin-top:5px;color:#93a4b3;font:11px/1.55 Consolas,"Microsoft JhengHei UI",monospace;white-space:pre-wrap;overflow-wrap:anywhere}.eventDetail[hidden]{display:none}
      .currentWork{margin:0 0 14px;padding:12px;border:1px solid #315246;background:#101b18}.currentLabel{font-size:10px;font-weight:700;letter-spacing:.12em;color:#67d5aa;margin-bottom:7px}.currentItem{padding:8px 0;border-top:1px solid #1f332d}.currentItem:first-of-type{border-top:0}.currentTitle{font-size:12px;color:#e4f2ec;line-height:1.5}.currentDetail{margin-top:4px;color:#8fa79d;font:10px/1.45 Consolas,monospace;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.sessionBadge{display:inline-block;margin-right:7px;padding:2px 5px;border:1px solid #3d5668;border-radius:4px;color:#9fc8e4;background:#101b23;font:9px/1.2 Consolas,monospace;vertical-align:1px}
      .help{border:1px solid #594932;background:#171712;padding:15px;margin-bottom:12px}.helpEyebrow{color:#f0b867;font-size:10px;font-weight:700;letter-spacing:.14em}.help h2{font-size:18px;line-height:1.45;margin:9px 0 7px;color:#f6f0e7}.help p{font-size:12px;line-height:1.65;color:#bcb5a8;margin:0}.help textarea{width:100%;min-height:102px;margin-top:14px;resize:vertical;border:1px solid #3b4650;background:#0c1218;color:#eef4f8;padding:11px 12px;outline:0}.help textarea:focus{border-color:#6aa88d}.helpActions{display:flex;gap:8px;margin-top:10px}.primary,.secondary{border:1px solid transparent;padding:9px 13px;font-size:12px}.primary{background:#dcebe4;color:#102019;font-weight:700}.secondary{background:transparent;border-color:#46535e;color:#b9c5ce}.primary:hover{background:#fff}.secondary:hover{border-color:#80909d;color:#fff}
      .footer{display:flex;align-items:center;gap:10px;padding:10px 14px;border-top:1px solid #26333e;background:#101820;color:#8395a5;font-size:11px}.toggle{display:inline-flex;align-items:center;gap:6px}.toggle input{accent-color:#5ed1a5}.clear{margin-left:auto;border:0;background:transparent;color:#8293a2;font-size:11px}.clear:hover{color:#f29b9b}.toast{position:absolute;left:16px;right:16px;bottom:54px;padding:10px 12px;background:#253340;border:1px solid #43586a;color:#e8f0f5;font-size:12px;box-shadow:0 8px 25px #0008;opacity:0;transform:translateY(8px);transition:.2s;pointer-events:none}.toast.show{opacity:1;transform:none}
      @keyframes pulse{50%{box-shadow:0 0 0 7px #ffb45e20}}
      @media (max-width:520px){.shell{width:100vw}.edge{top:auto;bottom:18%;height:92px}}
      @media (prefers-reduced-motion:reduce){.shell,.edge,.toast{transition:none}.edge i.attn{animation:none}}
    </style>
    <aside class="shell" aria-label="CODING MCP 主控台">
      <header class="header">
        <div class="titleRow"><div class="mark">MCP</div><div class="title">CODING MCP 主控台</div><div class="spacer"></div><button class="iconBtn close" title="收合">×</button></div>
        <div class="sub"><span class="statusDot"></span><span class="statusText">正在連線…</span><span class="mode"></span></div>
      </header>
      <nav class="tabs"><button class="tab active" data-tab="activity">工作紀錄</button><button class="tab" data-tab="help">HUMAN_HELP<span class="badge" hidden>1</span></button></nav>
      <main class="body"></main>
      <footer class="footer"><label class="toggle"><input class="details" type="checkbox">詳細輸出</label><label class="toggle"><input class="dnd" type="checkbox">免打擾</label><button class="clear">清除紀錄</button></footer>
      <div class="toast"></div>
    </aside>
    <button class="edge" title="開啟 CODING MCP 主控台"><i></i><span>CODING MCP</span></button>`;

  const $ = (selector) => root.querySelector(selector);
  const shell = $(".shell");
  const body = $(".body");
  const edge = $(".edge");
  const statusDot = $(".statusDot");
  const statusText = $(".statusText");
  const mode = $(".mode");
  const details = $(".details");
  const dnd = $(".dnd");
  const badge = $(".badge");
  let state = null;
  let activeTab = "activity";
  let connected = false;
  let lastHelpId = "";
  let renderedHelpId = "";
  let pollTimer = null;
  let connectionError = "";
  let allowHelpInputFocus = false;
  const REQUEST_TIMEOUT_MS = 5000;
  const version = document.createElement("span");
  version.className = "version";
  version.textContent = `v${chrome.runtime.getManifest().version}`;
  version.style.cssText = "margin-left:4px;color:#65788a;font:10px/1 Consolas,monospace";
  statusText.after(version);
  const CONSOLE_BASE = "http://127.0.0.1:8768";

  if (false) {
  // Chrome 142+ requires a document from the extension origin to obtain Local
  // Network Access before its service worker can call a loopback service. Keep
  // the frame rendered (not display:none) so Chrome can surface the one-time
  // permission prompt, while making it visually inert.
  const bridgeFrame = document.createElement("iframe");
  bridgeFrame.src = chrome.runtime.getURL("bridge-frame.html");
  bridgeFrame.allow = "local-network-access; local-network; loopback-network";
  bridgeFrame.title = "CODING MCP 本機連線";
  bridgeFrame.style.cssText = "position:fixed;width:1px;height:1px;left:-10px;top:-10px;opacity:0;pointer-events:none;border:0";
  root.appendChild(bridgeFrame);
  window.addEventListener("message", (event) => {
    if (event.source !== bridgeFrame.contentWindow || !event.data || event.data.source !== "coding-tools-bridge-frame") return;
    connectionError = event.data.ok ? "" : String(event.data.error || "本機網路權限尚未允許");
    if (event.data.ok) poll(); else render();
  });
  }

  const saved = (() => { try { return JSON.parse(localStorage.getItem("coding-tools-console-ui") || "{}"); } catch (_) { return {}; } })();
  details.checked = Boolean(saved.details);
  if (saved.open) shell.classList.add("open");

  function persist() {
    try { localStorage.setItem("coding-tools-console-ui", JSON.stringify({ open: shell.classList.contains("open"), details: details.checked })); } catch (_) { }
  }

  function legacyRequest(path, options) {
    return new Promise((resolve, reject) => {
      let settled = false;
      const timeout = setTimeout(() => {
        if (settled) return;
        settled = true;
        reject(new Error("主控台請求逾時"));
      }, REQUEST_TIMEOUT_MS);
      chrome.runtime.sendMessage(
        { type: "coding-tools-console-request", path, options },
        (response) => {
          if (settled) return;
          settled = true;
          clearTimeout(timeout);
          if (chrome.runtime.lastError) return reject(chrome.runtime.lastError);
          if (!response || !response.ok) return reject(new Error(response && response.error || "主控台無回應"));
          resolve(response.payload);
        }
      );
    });
  }

  async function request(path, options = {}) {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS);
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

  function setOpen(open) {
    shell.classList.toggle("open", open);
    persist();
    if (open) setTimeout(() => { body.scrollTop = body.scrollHeight; }, 20);
  }

  function toast(message) {
    const node = $(".toast");
    node.textContent = message;
    node.classList.add("show");
    clearTimeout(node._timer);
    node._timer = setTimeout(() => node.classList.remove("show"), 2200);
  }

  function humanizeTool(tool) {
    return ({ exec_command:"執行指令",apply_patch:"修改檔案",read_file:"讀取檔案",search_text:"搜尋程式碼",list_files:"瀏覽檔案",list_dir:"瀏覽資料夾",git_status:"檢查 Git 狀態",git_diff:"查看程式變更",git_log:"查看 Git 紀錄",browser_use:"操作瀏覽器",computer_use:"操作電腦",human_help_me:"需要你協助","HUMAN HELP":"需要你協助",view_image:"查看圖片" })[tool] || tool.replaceAll("_", " ");
  }

  function humanizePart(value) {
    const text = String(value || "").trim();
    if (text === "active_user") return "桌面";
    if (text === "service") return "背景";
    if (text === "ok") return "完成";
    if (text === "failed") return "失敗";
    if (text === "human_action_required") return "等待你操作";
    if (text === "human_completed") return "你已完成";
    const duration = text.match(/^(\d+) ms$/);
    if (duration && Number(duration[1]) >= 1000) return `${(Number(duration[1]) / 1000).toFixed(1)} 秒`;
    return text;
  }

  function extractActivityIdentity(value) {
    const text = String(value || "");
    const match = text.match(/\s+\{(S-[A-Z0-9_-]+)(?:;(R-[A-Z0-9_-]+))?\}\s*$/i);
    if (!match) return { text, session:"", request:"" };
    return {
      text: text.slice(0, match.index).trimEnd(),
      session: match[1].toUpperCase(),
      request: String(match[2] || "").toUpperCase()
    };
  }

  function parseActivity(raw) {
    const events = [];
    for (const source of String(raw || "").split(/\r?\n/)) {
      const line = source.replace(/\s+$/, "");
      let match = line.match(/^\[(\d{2}:\d{2}:\d{2})\]\s+▶\s+(.+)$/);
      if (match) {
        const identity = extractActivityIdentity(match[2]);
        const parts = identity.text.split(" · ");
        events.push({ kind:"start", time:match[1], tool:parts[0].trim(), session:identity.session, request:identity.request, title:`${humanizeTool(parts[0].trim())}${parts.length > 1 ? " · " + parts.slice(1).map(humanizePart).join(" · ") : ""}`, detail:"" });
        continue;
      }
      match = line.match(/^\[(\d{2}:\d{2}:\d{2})\]\s+([✓✗])\s+(.+)$/);
      if (match) {
        const identity = extractActivityIdentity(match[3]);
        const parts = identity.text.split(" · ");
        const summary = parts.slice(1).map(humanizePart).filter(Boolean).join(" · ");
        events.push({ kind:match[2] === "✓" ? "done" : "fail", time:match[1], tool:parts[0].trim(), session:identity.session, request:identity.request, title:`${match[2]} ${humanizeTool(parts[0].trim())}${summary ? " · " + summary : ""}`, detail:"" });
        continue;
      }
      if ((line.startsWith("> ") || line.startsWith("  ")) && events.length) {
        events[events.length - 1].detail += (events[events.length - 1].detail ? "\n" : "") + line.trim();
      }
    }
    return events.slice(-120);
  }

  function renderActivity() {
    const events = parseActivity(state && state.activity);
    const runningByRequest = new Map();
    for (const event of events) {
      if (!event.request) continue;
      const requestKey = `${event.session || "NO-SESSION"}|${event.request}`;
      if (event.kind === "start") runningByRequest.set(requestKey, event);
      else runningByRequest.delete(requestKey);
    }
    const running = Array.from(runningByRequest.values()).slice(-6);
    body.replaceChildren();
    if (!events.length) {
      const empty = document.createElement("div"); empty.className = "empty";
      empty.innerHTML = "<strong>還沒有工作紀錄</strong>接下來的 MCP 操作會即時顯示在這裡。";
      body.appendChild(empty); return;
    }
    if (running.length) {
      const current = document.createElement("section"); current.className = "currentWork";
      const label = document.createElement("div"); label.className = "currentLabel"; label.textContent = running.length > 1 ? `目前工作 · ${running.length} 個 Session` : "目前工作";
      current.appendChild(label);
      for (const event of running) {
        const item = document.createElement("div"); item.className = "currentItem";
        const title = document.createElement("div"); title.className = "currentTitle";
        if (event.session) { const session = document.createElement("span"); session.className = "sessionBadge"; session.textContent = event.session; title.appendChild(session); }
        title.append(document.createTextNode(event.title)); item.appendChild(title);
        if (event.detail) { const detail = document.createElement("div"); detail.className = "currentDetail"; detail.textContent = event.detail.split("\n")[0].replace(/^>\s*/, ""); item.appendChild(detail); }
        current.appendChild(item);
      }
      body.appendChild(current);
    }
    for (const event of events) {
      const row = document.createElement("article"); row.className = `event ${event.kind}`;
      const time = document.createElement("div"); time.className = "eventTime"; time.textContent = event.time;
      const main = document.createElement("div"); main.className = "eventMain";
      const title = document.createElement("div"); title.className = "eventTitle";
      if (event.session) { const session = document.createElement("span"); session.className = "sessionBadge"; session.textContent = event.session; title.appendChild(session); }
      title.append(document.createTextNode(event.title));
      const detail = document.createElement("div"); detail.className = "eventDetail"; detail.textContent = event.detail; detail.hidden = !details.checked || !event.detail;
      main.append(title, detail); row.append(time, main); body.appendChild(row);
    }
    if (shell.classList.contains("open")) body.scrollTop = body.scrollHeight;
  }

  function renderHelp() {
    const help = state && state.human_help;
    const helpId = help && help.request_id || "";
    if (help && helpId === renderedHelpId && body.querySelector(".help")) return;
    body.replaceChildren();
    renderedHelpId = helpId;
    if (!help) {
      const empty = document.createElement("div"); empty.className = "empty";
      empty.innerHTML = "<strong>目前不需要你介入</strong>代理遇到需要人工判斷或操作的步驟時，問題會出現在這裡。";
      body.appendChild(empty); return;
    }
    const card = document.createElement("section"); card.className = "help";
    const eyebrow = document.createElement("div"); eyebrow.className = "helpEyebrow"; eyebrow.textContent = "HUMAN HELP · 等待你的回覆";
    const title = document.createElement("h2"); title.textContent = help.request || "需要你協助";
    const expected = document.createElement("p"); expected.textContent = help.expected_result ? `完成標準：${help.expected_result}` : "請完成這一步後告訴代理。";
    const textarea = document.createElement("textarea"); textarea.placeholder = "輸入結果、補充資訊，或描述你完成了什麼…";
    textarea.tabIndex = -1;
    textarea.addEventListener("pointerdown", () => {
      allowHelpInputFocus = true;
      textarea.tabIndex = 0;
    });
    textarea.addEventListener("focus", () => {
      if (allowHelpInputFocus) return;
      textarea.blur();
    });
    textarea.addEventListener("blur", () => {
      allowHelpInputFocus = false;
      textarea.tabIndex = -1;
    });
    const actions = document.createElement("div"); actions.className = "helpActions";
    const done = document.createElement("button"); done.className = "primary"; done.textContent = "完成並交還代理";
    const cancel = document.createElement("button"); cancel.className = "secondary"; cancel.textContent = "目前無法完成";
    done.onclick = () => respondHelp(help.request_id, "completed", textarea.value);
    cancel.onclick = () => respondHelp(help.request_id, "cancelled", textarea.value);
    actions.append(done, cancel); card.append(eyebrow, title, expected, textarea, actions); body.appendChild(card);
  }

  function render() {
    statusDot.classList.toggle("live", connected);
    statusText.textContent = connected
      ? (state && state.dnd ? "已連線 · 免打擾" : "已連線 · 即時")
      : (connectionError ? "請允許瀏覽器存取本機網路" : "本機主控台未連線");
    mode.textContent = state && state.permission_mode === "dangerous" ? "YOLO MODE" : "";
    dnd.checked = Boolean(state && state.dnd);
    const help = state && state.human_help;
    badge.hidden = !help;
    edge.querySelector("i").className = help ? "attn" : connected ? "live" : "";
    if (activeTab === "help") renderHelp(); else renderActivity();
  }

  async function respondHelp(requestId, outcome, answer) {
    try {
      await request("/v1/human-help/respond", { method:"POST", body:{ request_id:requestId, outcome, answer } });
      toast("已交還代理，工作會繼續進行");
      await poll();
    } catch (error) { toast(`送出失敗：${error.message}`); }
  }

  async function poll() {
    clearTimeout(pollTimer);
    const activeBeforePoll = document.activeElement;
    let focusToRestore = (
      activeBeforePoll &&
      activeBeforePoll !== document.body &&
      activeBeforePoll !== host &&
      !host.contains(activeBeforePoll)
    ) ? activeBeforePoll : null;
    try {
      state = await request("/v1/state"); connected = true;
      const helpId = state.human_help && state.human_help.request_id || "";
      if (helpId && helpId !== lastHelpId && !state.dnd) {
        activeTab = "help";
        setOpen(true);
      }
      lastHelpId = helpId;
    } catch (error) { connected = false; state = null; connectionError = String(error && error.message || error); }
    render();
    if (focusToRestore && focusToRestore.isConnected) {
      const restoreFocus = () => {
        if (!focusToRestore.isConnected || document.activeElement === focusToRestore) return;
        try { focusToRestore.focus({ preventScroll: true }); }
        catch (_) { try { focusToRestore.focus(); } catch (_) { } }
      };
      queueMicrotask(restoreFocus);
      setTimeout(restoreFocus, 60);
    }
    pollTimer = setTimeout(poll, shell.classList.contains("open") ? 700 : 1800);
  }

  edge.onclick = () => setOpen(true);
  $(".close").onclick = () => setOpen(false);
  root.querySelectorAll(".tab").forEach((tab) => tab.onclick = () => {
    activeTab = tab.dataset.tab;
    root.querySelectorAll(".tab").forEach((item) => item.classList.toggle("active", item === tab));
    render();
  });
  details.onchange = () => { persist(); render(); };
  dnd.onchange = async () => {
    try { await request("/v1/preferences", { method:"POST", body:{ dnd:dnd.checked } }); await poll(); }
    catch (error) { toast(`設定失敗：${error.message}`); }
  };
  $(".clear").onclick = async () => {
    try { await request("/v1/activity/clear", { method:"POST" }); await poll(); toast("工作紀錄已清除"); }
    catch (error) { toast(`清除失敗：${error.message}`); }
  };
  chrome.runtime.onMessage.addListener((message) => { if (message && message.type === "coding-tools-console-toggle") setOpen(!shell.classList.contains("open")); });
  poll();
})();
