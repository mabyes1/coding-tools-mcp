(() => {
  if (window.top !== window || document.getElementById("coding-tools-console-host")) return;

  const host = document.createElement("div");
  host.id = "coding-tools-console-host";
  host.style.cssText = "all:initial!important;position:fixed!important;inset:0!important;z-index:2147483647!important;pointer-events:none!important;";
  (document.documentElement || document.body).appendChild(host);
  const root = host.attachShadow({ mode: "open" });
  root.innerHTML = `
    <style>
      :host{all:initial;color-scheme:light dark;font-family:"Segoe UI","Microsoft JhengHei UI","Noto Sans TC",sans-serif;--fg:#111827;--muted:#596673;--muted-2:#74808b;--glass-bg:rgba(255,255,255,.11);--glass:rgba(255,255,255,.14);--glass-hi:rgba(255,255,255,.52);--glass-mid:rgba(255,255,255,.22);--glass-low:rgba(255,255,255,.05);--line:rgba(17,24,39,.13);--line-strong:rgba(255,255,255,.56);--good:#159a6f;--warn:#a76500;--bad:#c43d4d;--shadow:0 28px 90px rgba(15,23,42,.18)}
      :host([data-theme="dark"]){--fg:#f1f5f9;--muted:#a7b2bd;--muted-2:#83909c;--glass-bg:rgba(10,14,18,.24);--glass:rgba(255,255,255,.07);--glass-hi:rgba(255,255,255,.34);--glass-mid:rgba(255,255,255,.13);--glass-low:rgba(255,255,255,.025);--line:rgba(255,255,255,.13);--line-strong:rgba(255,255,255,.42);--good:#62d6a6;--warn:#ffd27a;--bad:#ff8f8f;--shadow:0 28px 90px rgba(0,0,0,.40)}
      *{box-sizing:border-box}button,input,textarea{font:inherit}button{cursor:pointer}
      .edge,.shell{font-family:"Segoe UI","Microsoft JhengHei UI","Noto Sans TC",sans-serif}
      .focusMask{position:fixed;background:#07101882;backdrop-filter:blur(9px) saturate(.7);-webkit-backdrop-filter:blur(9px) saturate(.7);pointer-events:auto;opacity:0;visibility:hidden;transition:opacity .16s ease;z-index:1}
      .focusMask.on{opacity:1;visibility:visible}.focusMask.top{left:0;right:0;top:0}.focusMask.left,.focusMask.right{top:0}.focusMask.bottom{left:0;right:0;bottom:0}
      .escapeHint{position:fixed;pointer-events:none;border:1px solid #79d7b04d;border-radius:18px;box-shadow:0 0 0 1px #07101866,0 0 34px #79d7b020;opacity:0;visibility:hidden;transition:opacity .16s ease;z-index:2}.escapeHint.on{opacity:1;visibility:visible}
      .edge{pointer-events:auto;position:fixed;right:10px;top:42%;width:36px;height:116px;border:1px solid var(--line-strong);border-radius:18px;background:linear-gradient(145deg,var(--glass-mid) 0%,var(--glass-low) 38%,transparent 64%),var(--glass-bg);backdrop-filter:blur(28px) saturate(170%);-webkit-backdrop-filter:blur(28px) saturate(170%);color:var(--fg);box-shadow:0 14px 42px rgba(0,0,0,.16),inset 0 1px 0 var(--glass-hi),inset 1px 0 0 var(--glass-mid),inset 0 -1px 0 var(--glass-low);display:grid;place-items:center;transition:transform .22s ease,opacity .22s ease}
      .edge:hover{background:var(--glass);border-color:var(--line-strong)}.edge span{writing-mode:vertical-rl;letter-spacing:.16em;font-size:11px}.edge i{position:absolute;top:13px;width:7px;height:7px;border-radius:50%;background:#7d8995}.edge i.live{background:#55d6a3;box-shadow:0 0 0 4px #55d6a31c}.edge i.attn{background:#ffb45e;animation:pulse 1.4s infinite}
      .shell{pointer-events:auto;position:fixed;right:14px;top:14px;bottom:14px;height:auto;width:min(450px,calc(100vw - 28px));background:linear-gradient(145deg,var(--glass-mid) 0%,var(--glass-low) 20%,transparent 46%),linear-gradient(325deg,var(--glass-low) 0%,transparent 34%),var(--glass-bg);backdrop-filter:blur(34px) saturate(175%) contrast(102%);-webkit-backdrop-filter:blur(34px) saturate(175%) contrast(102%);border:1px solid var(--line-strong);border-radius:26px;box-shadow:var(--shadow),0 1px 0 rgba(255,255,255,.10),inset 0 1px 0 var(--glass-hi),inset 1px 0 0 var(--glass-mid),inset -1px 0 0 rgba(255,255,255,.04),inset 0 -1px 0 var(--glass-low);transform:translateX(calc(100% + 36px));transition:transform .25s cubic-bezier(.2,.8,.2,1);display:grid;grid-template-rows:auto auto 1fr auto;color:var(--fg);z-index:3;overflow:hidden;isolation:isolate}
      .shell::before{content:"";position:absolute;inset:0;border-radius:inherit;pointer-events:none;z-index:0;background:radial-gradient(120% 52% at 6% -4%,var(--glass-hi) 0%,var(--glass-mid) 18%,transparent 48%),radial-gradient(70% 32% at 104% 96%,var(--glass-mid) 0%,transparent 58%),linear-gradient(112deg,transparent 0 34%,rgba(255,255,255,.10) 44%,rgba(255,255,255,.025) 54%,transparent 64%);opacity:.78;mix-blend-mode:screen}
      .shell::after{content:"";position:absolute;inset:1px;border-radius:25px;pointer-events:none;z-index:0;box-shadow:inset 0 0 0 1px rgba(255,255,255,.07),inset 0 18px 34px rgba(255,255,255,.035),inset 0 -20px 30px rgba(0,0,0,.025)}
      .shell>*{position:relative;z-index:1}
      .shell.open{transform:translateX(0)}.shell.open~.edge{transform:translateX(110%);opacity:0}
      .header{padding:18px 18px 13px;border-bottom:1px solid var(--line);background:transparent}.titleRow{display:flex;align-items:center;gap:10px}.mark{width:24px;height:24px;border:1px solid var(--line-strong);border-radius:7px;background:var(--glass);display:grid;place-items:center;font:700 10px/1 Consolas,monospace;color:var(--good);box-shadow:inset 0 1px 0 rgba(255,255,255,.20)}.title{font-size:15px;font-weight:700;letter-spacing:.04em;color:var(--fg)}.spacer{flex:1}.iconBtn{width:32px;height:32px;border:0;border-radius:10px;background:transparent;color:var(--muted);font-size:19px}.iconBtn:hover{background:var(--glass);color:var(--fg)}
      .sub{display:flex;align-items:center;gap:8px;margin-top:10px;color:var(--muted);font-size:11px}.statusDot{width:7px;height:7px;border-radius:50%;background:#697783}.statusDot.live{background:#55d6a3}.mode{margin-left:auto;padding:3px 7px;border-radius:999px;border:1px solid var(--line);background:var(--glass);font:700 9px/1.2 Consolas,monospace;letter-spacing:.08em}.mode[data-mode="safe"]{color:#159a6f}.mode[data-mode="trusted"]{color:#3d78a8}.mode[data-mode="dangerous"]{color:#d14b59;border-color:rgba(209,75,89,.28);background:rgba(209,75,89,.08)}.mode:empty{display:none}
      .tabs{display:flex;gap:5px;padding:9px 13px;border-bottom:1px solid var(--line);background:transparent}.tab{border:1px solid transparent;background:transparent;color:var(--muted);border-radius:10px;padding:8px 11px;font-size:12px}.tab:hover{color:var(--fg);background:var(--glass)}.tab.active{border-color:var(--line-strong);background:var(--glass);color:var(--fg);box-shadow:inset 0 1px 0 rgba(255,255,255,.20),0 5px 16px rgba(0,0,0,.06)}.badge{display:inline-grid;min-width:18px;height:18px;padding:0 5px;margin-left:5px;place-items:center;border-radius:9px;background:#ad6638;color:#fff;font-size:10px}.badge[hidden]{display:none}
      .body{min-height:0;overflow:auto;padding:14px 15px 22px;overscroll-behavior:contain;scrollbar-color:var(--muted-2) transparent;scrollbar-width:thin}.empty{margin:54px 22px;color:var(--muted);text-align:center;line-height:1.7;font-size:13px}.empty strong{display:block;color:var(--fg);margin-bottom:5px}
      .event{display:grid;grid-template-columns:52px 1fr;gap:8px;padding:10px 2px;border-bottom:1px solid var(--line)}.eventTime{color:var(--muted-2);font:11px/1.6 Consolas,monospace}.eventMain{min-width:0}.eventTitle{font-size:12px;color:var(--fg);line-height:1.45}.event.start .eventTitle{color:#2f7fbd}.event.done .eventTitle{color:#159a6f}.event.fail .eventTitle{color:#c43d4d}.eventDetail{margin-top:5px;color:var(--muted);font:11px/1.55 Consolas,"Microsoft JhengHei UI",monospace;white-space:pre-wrap;overflow-wrap:anywhere}.eventDetail[hidden]{display:none}
      .currentWork{margin:0 0 14px;padding:12px;border:1px solid rgba(21,154,111,.22);border-radius:12px;background:var(--glass);box-shadow:var(--shadow),inset 0 1px 0 rgba(255,255,255,.10);backdrop-filter:blur(18px) saturate(112%);-webkit-backdrop-filter:blur(18px) saturate(112%)}.currentLabel{font-size:10px;font-weight:700;letter-spacing:.12em;color:var(--good);margin-bottom:7px}.currentItem{padding:8px 0;border-top:1px solid var(--line)}.currentItem:first-of-type{border-top:0}.currentTitle{font-size:12px;color:var(--fg);line-height:1.5}.currentDetail{margin-top:4px;color:var(--muted);font:10px/1.45 Consolas,monospace;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.sessionBadge{display:inline-block;margin-right:7px;padding:2px 5px;border:1px solid var(--line);border-radius:5px;color:var(--muted);background:transparent;font:9px/1.2 Consolas,monospace;vertical-align:1px}
      .help{border:1px solid rgba(167,101,0,.24);border-radius:12px;background:var(--glass);box-shadow:var(--shadow),inset 0 1px 0 rgba(255,255,255,.10);backdrop-filter:blur(18px) saturate(112%);-webkit-backdrop-filter:blur(18px) saturate(112%);padding:15px;margin-bottom:12px}.helpEyebrow{color:var(--warn);font-size:10px;font-weight:700;letter-spacing:.14em}.help h2{font-size:18px;line-height:1.45;margin:9px 0 7px;color:var(--fg)}.help p{font-size:12px;line-height:1.65;color:var(--muted);margin:0}.help textarea{width:100%;min-height:102px;margin-top:14px;resize:vertical;border:1px solid var(--line);border-radius:9px;background:transparent;color:var(--fg);padding:11px 12px;outline:0}.help textarea:focus{border-color:var(--good);box-shadow:0 0 0 3px rgba(21,154,111,.08)}.helpActions{display:flex;gap:8px;margin-top:10px}.primary,.secondary{border:1px solid transparent;border-radius:8px;padding:9px 13px;font-size:12px}.primary{background:var(--fg);color:var(--glass-bg);font-weight:700}.secondary{background:transparent;border-color:var(--line);color:var(--fg)}.primary:hover{opacity:.88}.secondary:hover{border-color:var(--line-strong);background:var(--glass)}
      .settings{display:grid;gap:12px}.glassCard{border:1px solid var(--line-strong);border-radius:13px;background:linear-gradient(145deg,var(--glass-mid) 0%,var(--glass-low) 34%,transparent 70%),var(--glass);box-shadow:0 12px 34px rgba(0,0,0,.08),inset 0 1px 0 var(--glass-hi),inset 1px 0 0 var(--glass-low);backdrop-filter:blur(20px) saturate(135%);-webkit-backdrop-filter:blur(20px) saturate(135%);overflow:hidden}.cardHead{display:flex;align-items:flex-start;gap:12px;padding:13px 14px 11px;border-bottom:1px solid var(--line)}.cardHeadText{min-width:0}.cardTitle{font-size:12px;font-weight:800;color:var(--fg);letter-spacing:.03em}.cardHint{margin-top:3px;color:var(--muted);font-size:10px;line-height:1.45}.cardBody{padding:12px 14px}.serviceList{display:grid;gap:8px}.serviceRow{display:grid;grid-template-columns:1fr auto;gap:10px;align-items:center;padding:9px 10px;border:1px solid var(--line);border-radius:9px;background:linear-gradient(145deg,var(--glass-low),transparent 65%)}.serviceName{font-size:11px;color:var(--fg)}.serviceTech{display:block;margin-top:2px;color:var(--muted);font:9px/1.3 Consolas,monospace}.statusPill{padding:3px 7px;border:1px solid var(--line);border-radius:999px;font:700 9px/1 Consolas,monospace;text-transform:uppercase;color:var(--muted)}.statusPill.running{color:#159a6f;border-color:rgba(21,154,111,.26);background:rgba(21,154,111,.07)}.statusPill.stopped{color:#c43d4d;border-color:rgba(196,61,77,.24);background:rgba(196,61,77,.06)}.statusPill.missing{color:var(--muted-2)}.actionGrid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:8px}.actionBtn{min-height:38px;border:1px solid var(--line);border-radius:9px;background:linear-gradient(145deg,var(--glass-low),transparent 68%);color:var(--fg);padding:8px 10px;text-align:left;font-size:11px;transition:.14s ease}.actionBtn:hover{background:linear-gradient(145deg,var(--glass-mid),var(--glass-low));border-color:var(--line-strong);transform:translateY(-1px);box-shadow:inset 0 1px 0 var(--glass-hi)}.actionBtn:disabled{opacity:.46;cursor:wait;transform:none}.actionBtn strong{display:block;font-size:11px}.actionBtn small{display:block;margin-top:2px;color:var(--muted);font-size:9px;line-height:1.3}.actionBtn.danger{border-color:rgba(196,61,77,.18)}.actionBtn.danger:hover{background:rgba(196,61,77,.06);border-color:rgba(196,61,77,.32)}.permissionGrid{display:grid;grid-template-columns:repeat(3,1fr);gap:7px}.permissionBtn{border:1px solid var(--line);border-radius:9px;background:linear-gradient(145deg,var(--glass-low),transparent 68%);color:var(--muted);padding:9px 6px;font:700 10px/1 Consolas,monospace}.permissionBtn:hover{color:var(--fg);border-color:var(--line-strong)}.permissionBtn.active{color:var(--fg);background:linear-gradient(145deg,var(--glass-mid),var(--glass-low));border-color:var(--line-strong);box-shadow:inset 0 1px 0 var(--glass-hi)}.permissionBtn.yolo.active{color:#c43d4d;border-color:rgba(196,61,77,.32);background:rgba(196,61,77,.07)}.healthBox{margin-top:9px;padding:9px 10px;border:1px solid var(--line);border-radius:8px;background:linear-gradient(145deg,var(--glass-low),transparent 72%);color:var(--muted);font:9px/1.5 Consolas,monospace;white-space:pre-wrap;overflow-wrap:anywhere}.healthBox strong{color:var(--fg)}.settingsBusy{display:inline-block;margin-left:auto;color:var(--warn);font:9px/1.3 Consolas,monospace}
      .footer{display:flex;align-items:center;gap:10px;padding:10px 14px;border-top:1px solid var(--line);background:transparent;color:var(--muted);font-size:11px}.toggle{display:inline-flex;align-items:center;gap:6px}.toggle input{accent-color:#159a6f}.clear{margin-left:auto;border:0;background:transparent;color:var(--muted);font-size:11px}.clear:hover{color:var(--bad)}.toast{position:absolute;left:16px;right:16px;bottom:54px;padding:10px 12px;border-radius:14px;background:var(--glass-bg);backdrop-filter:blur(24px) saturate(150%);-webkit-backdrop-filter:blur(24px) saturate(150%);border:1px solid var(--line-strong);color:var(--fg);font-size:12px;box-shadow:0 12px 34px rgba(0,0,0,.16),inset 0 1px 0 rgba(255,255,255,.18);opacity:0;transform:translateY(8px);transition:.2s;pointer-events:none}.toast.show{opacity:1;transform:none}
      @keyframes pulse{50%{box-shadow:0 0 0 7px #ffb45e20}}
      @media (max-width:520px){.shell{right:8px;top:8px;bottom:8px;width:calc(100vw - 16px);border-radius:22px}.edge{top:auto;bottom:18%;height:92px}}
      @media (prefers-reduced-motion:reduce){.shell,.edge,.toast{transition:none}.edge i.attn{animation:none}}
    </style>
    <div class="focusMask top" aria-hidden="true"></div>
    <div class="focusMask left" aria-hidden="true"></div>
    <div class="focusMask right" aria-hidden="true"></div>
    <div class="focusMask bottom" aria-hidden="true"></div>
    <div class="escapeHint" aria-hidden="true"></div>
    <aside class="shell" aria-label="CODING MCP 主控台">
      <header class="header">
        <div class="titleRow"><div class="mark">MCP</div><div class="title">CODING MCP 主控台</div><div class="spacer"></div><button class="iconBtn close" title="收合">×</button></div>
        <div class="sub"><span class="statusDot"></span><span class="statusText">正在連線…</span><span class="mode"></span></div>
      </header>
      <nav class="tabs"><button class="tab active" data-tab="activity">工作紀錄</button><button class="tab" data-tab="help">HUMAN_HELP<span class="badge" hidden>1</span></button><button class="tab" data-tab="settings">設定</button></nav>
      <main class="body"></main>
      <footer class="footer"><label class="toggle detailsToggle"><input class="details" type="checkbox">詳細輸出</label><label class="toggle"><input class="dnd" type="checkbox">免打擾</label><button class="clear">清除紀錄</button></footer>
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
  const focusMasks = Array.from(root.querySelectorAll(".focusMask"));
  const escapeHint = $(".escapeHint");
  let state = null;
  let activeTab = "activity";
  let connected = false;
  let lastPresentedHelpId = "";
  let renderedHelpId = "";
  let pollTimer = null;
  let connectionError = "";
  let allowHelpInputFocus = false;
  let focusMaskRaf = 0;
  let lastHelpActivitySentAt = 0;
  let settingsBusy = "";
  let lastHealth = null;
  const REQUEST_TIMEOUT_MS = 5000;
  const HELP_ACTIVITY_THROTTLE_MS = 300;
  const version = document.createElement("span");
  version.className = "version";
  version.textContent = `v${chrome.runtime.getManifest().version}`;
  version.style.cssText = "margin-left:4px;color:var(--muted);font:10px/1 Consolas,monospace";
  statusText.after(version);
  const CONSOLE_BASE = "http://127.0.0.1:8768";

  function colorLuminance(value) {
    const match = String(value || "").match(/rgba?\((\d+)[, ]+(\d+)[, ]+(\d+)/i);
    if (!match) return null;
    const rgb = [Number(match[1]), Number(match[2]), Number(match[3])].map((channel) => {
      const value = channel / 255;
      return value <= .03928 ? value / 12.92 : Math.pow((value + .055) / 1.055, 2.4);
    });
    return .2126 * rgb[0] + .7152 * rgb[1] + .0722 * rgb[2];
  }

  function syncPageTheme() {
    const page = document.body || document.documentElement;
    const htmlStyle = getComputedStyle(document.documentElement);
    const bodyStyle = getComputedStyle(page);
    const classHint = `${document.documentElement.className || ""} ${page.className || ""}`.toLowerCase();
    let dark = /(^|\s)dark(\s|$)/.test(classHint) || String(htmlStyle.colorScheme || "").includes("dark");
    const backgroundLuminance = colorLuminance(bodyStyle.backgroundColor) ?? colorLuminance(htmlStyle.backgroundColor);
    if (backgroundLuminance !== null) dark = backgroundLuminance < .35;
    else {
      const textLuminance = colorLuminance(bodyStyle.color);
      if (textLuminance !== null) dark = textLuminance > .55;
    }
    host.dataset.theme = dark ? "dark" : "light";
  }

  syncPageTheme();

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

  const saved = (() => { try { return JSON.parse(localStorage.getItem("coding-tools-console-ui") || "{}"); } catch (_) { return {}; } })();
  details.checked = Boolean(saved.details);
  if (saved.open) shell.classList.add("open");

  function persist() {
    try { localStorage.setItem("coding-tools-console-ui", JSON.stringify({ open: shell.classList.contains("open"), details: details.checked })); } catch (_) { }
  }

  function extensionRequest(path, options = {}) {
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

  async function directRequest(path, options = {}) {
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

  async function request(path, options = {}) {
    try {
      return await extensionRequest(path, options);
    } catch (extensionError) {
      try {
        return await directRequest(path, options);
      } catch (directError) {
        const extensionMessage = String(extensionError && extensionError.message || extensionError);
        const directMessage = String(directError && directError.message || directError);
        throw new Error(`extension: ${extensionMessage}; direct: ${directMessage}`);
      }
    }
  }

  function setOpen(open) {
    shell.classList.toggle("open", open);
    persist();
    if (open) setTimeout(() => { body.scrollTop = body.scrollHeight; }, 20);
  }

  function findEscapeComposer() {
    const candidates = Array.from(document.querySelectorAll(
      '#prompt-textarea, textarea, [contenteditable="true"], [role="textbox"]'
    ));
    let best = null;
    let bestScore = -Infinity;
    for (const node of candidates) {
      if (!(node instanceof HTMLElement) || host.contains(node)) continue;
      const rect = node.getBoundingClientRect();
      if (rect.width < 180 || rect.height < 24 || rect.bottom < window.innerHeight * .58) continue;
      const style = getComputedStyle(node);
      if (style.visibility === "hidden" || style.display === "none") continue;
      const score = rect.bottom * 3 + rect.width - Math.abs(window.innerWidth / 2 - (rect.left + rect.width / 2));
      if (score > bestScore) { best = node; bestScore = score; }
    }
    return best;
  }

  function updateFocusMask() {
    cancelAnimationFrame(focusMaskRaf);
    focusMaskRaf = requestAnimationFrame(() => {
      const enabled = Boolean(state && state.human_help);
      if (!enabled) {
        focusMasks.forEach((node) => node.classList.remove("on"));
        escapeHint.classList.remove("on");
        return;
      }
      const composer = findEscapeComposer();
      const rect = composer ? composer.getBoundingClientRect() : null;
      const padX = 22;
      const padY = 14;
      const hole = rect ? {
        left: Math.max(0, rect.left - padX),
        right: Math.min(window.innerWidth, rect.right + padX),
        top: Math.max(0, rect.top - padY),
        bottom: Math.min(window.innerHeight, rect.bottom + padY),
      } : {
        left: Math.max(12, window.innerWidth * .27),
        right: Math.min(window.innerWidth - 12, window.innerWidth * .73),
        top: Math.max(0, window.innerHeight - 132),
        bottom: window.innerHeight,
      };
      const top = $(".focusMask.top");
      const left = $(".focusMask.left");
      const right = $(".focusMask.right");
      const bottom = $(".focusMask.bottom");
      top.style.height = `${hole.top}px`;
      left.style.left = "0"; left.style.top = `${hole.top}px`; left.style.width = `${hole.left}px`; left.style.height = `${Math.max(0, hole.bottom - hole.top)}px`;
      right.style.left = `${hole.right}px`; right.style.top = `${hole.top}px`; right.style.right = "0"; right.style.height = `${Math.max(0, hole.bottom - hole.top)}px`;
      bottom.style.top = `${hole.bottom}px`;
      escapeHint.style.left = `${hole.left}px`; escapeHint.style.top = `${hole.top}px`; escapeHint.style.width = `${Math.max(0, hole.right - hole.left)}px`; escapeHint.style.height = `${Math.max(0, hole.bottom - hole.top)}px`;
      focusMasks.forEach((node) => node.classList.add("on"));
      escapeHint.classList.add("on");
    });
  }

  function toast(message) {
    const node = $(".toast");
    node.textContent = message;
    node.classList.add("show");
    clearTimeout(node._timer);
    node._timer = setTimeout(() => node.classList.remove("show"), 2200);
  }

  function selectTab(tabName) {
    activeTab = tabName;
    root.querySelectorAll(".tab").forEach((item) => item.classList.toggle("active", item.dataset.tab === tabName));
  }

  function humanizeTool(tool) {
    return ({ exec_command:"執行指令",apply_patch:"修改檔案",read_file:"讀取檔案",search_text:"搜尋程式碼",list_files:"瀏覽檔案",list_dir:"瀏覽資料夾",git_status:"檢查 Git 狀態",git_diff:"查看程式變更",git_log:"查看 Git 紀錄",browser_use:"操作瀏覽器",computer_use:"操作電腦",human_help_me:"需要你協助","HUMAN HELP":"需要你協助",view_image:"查看圖片" })[tool] || tool.replaceAll("_", " ");
  }

  function humanizePart(value) {
    const text = String(value || "").trim();
    if (text === "active_user") return "桌面";
    if (text === "service") return "背景";
    if (text === "permission_blocked") return "需要系統權限";
    if (text === "gui_required") return "需要你操作畫面";
    if (text === "physical_action") return "需要實體操作";
    if (text === "faster_by_human") return "這一步你做比較快";
    if (text === "need_information") return "需要你提供資訊";
    if (text === "need_decision") return "需要你決定";
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
      noteHumanHelpActivity(help.request_id);
      allowHelpInputFocus = true;
      textarea.tabIndex = 0;
    });
    for (const eventName of ["keydown", "input", "paste", "compositionupdate"]) {
      textarea.addEventListener(eventName, () => noteHumanHelpActivity(help.request_id));
    }
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

  function createSettingsCard(titleText, hintText) {
    const card = document.createElement("section"); card.className = "glassCard";
    const head = document.createElement("div"); head.className = "cardHead";
    const copy = document.createElement("div"); copy.className = "cardHeadText";
    const title = document.createElement("div"); title.className = "cardTitle"; title.textContent = titleText;
    const hint = document.createElement("div"); hint.className = "cardHint"; hint.textContent = hintText;
    copy.append(title, hint); head.appendChild(copy);
    const cardBody = document.createElement("div"); cardBody.className = "cardBody";
    card.append(head, cardBody);
    return { card, head, body: cardBody };
  }

  function appendServiceRow(container, label, tech, service) {
    const row = document.createElement("div"); row.className = "serviceRow";
    const name = document.createElement("div"); name.className = "serviceName"; name.textContent = label;
    const detail = document.createElement("span"); detail.className = "serviceTech"; detail.textContent = tech; name.appendChild(detail);
    const status = String(service && service.status || "unknown").toLowerCase();
    const pill = document.createElement("span"); pill.className = `statusPill ${status}`;
    pill.textContent = service && service.installed === false ? "MISSING" : status.toUpperCase();
    row.append(name, pill); container.appendChild(row);
  }

  function makeActionButton(title, hint, action, options = {}) {
    const button = document.createElement("button");
    button.className = `actionBtn${options.danger ? " danger" : ""}`;
    button.disabled = Boolean(settingsBusy);
    const strong = document.createElement("strong"); strong.textContent = title;
    const small = document.createElement("small"); small.textContent = hint;
    button.append(strong, small);
    button.onclick = () => systemAction(action, options.confirm || "");
    return button;
  }

  function renderSettings() {
    body.replaceChildren();
    const wrap = document.createElement("div"); wrap.className = "settings";
    const services = state && state.services || {};

    const statusCard = createSettingsCard("系統狀態", "MCP 與 Tunnel 的即時 Windows Service 狀態");
    if (settingsBusy) {
      const busy = document.createElement("span"); busy.className = "settingsBusy"; busy.textContent = `${settingsBusy}…`;
      statusCard.head.appendChild(busy);
    }
    const serviceList = document.createElement("div"); serviceList.className = "serviceList";
    appendServiceRow(serviceList, "Coding Tools MCP", "WebGPTCodingToolsMCP", services.mcp);
    appendServiceRow(serviceList, "Secure MCP Tunnel", "OpenAITunnelClient", services.secure_tunnel);
    appendServiceRow(serviceList, "Legacy Cloudflare", "WebGPTCloudflareTunnel", services.legacy_tunnel);
    statusCard.body.appendChild(serviceList);
    wrap.appendChild(statusCard.card);

    const serviceCard = createSettingsCard("服務控制", "對應原本 BAT 的 Start / Stop / Restart 核心功能");
    const serviceActions = document.createElement("div"); serviceActions.className = "actionGrid";
    serviceActions.append(
      makeActionButton("啟動全部", "MCP + Tunnel", "start_all"),
      makeActionButton("重新啟動", "重建服務連線", "restart_all", { confirm:"會短暫中斷目前 MCP / Tunnel 連線，確定重新啟動？" }),
      makeActionButton("重啟 Tunnel", "只重啟 OpenAI Tunnel", "restart_tunnel", { confirm:"只會重啟 Secure MCP Tunnel，MCP 本體會保持運行。確定？" }),
      makeActionButton("停止全部", "保留 Web Console", "stop_all", { danger:true, confirm:"這會停止 MCP 與 Tunnel，確定要繼續？" })
    );
    serviceCard.body.appendChild(serviceActions); wrap.appendChild(serviceCard.card);

    const permissionCard = createSettingsCard("Permission Mode", "切換後會重啟 MCP；YOLO 允許 exec_command 直接修改檔案系統");
    const permissionGrid = document.createElement("div"); permissionGrid.className = "permissionGrid";
    const currentMode = String(state && state.permission_mode || "safe").toLowerCase();
    for (const item of [
      ["SAFE", "safe", "safe"],
      ["TRUSTED", "trusted", "trusted"],
      ["YOLO", "yolo", "dangerous"]
    ]) {
      const button = document.createElement("button");
      button.className = `permissionBtn${item[1] === "yolo" ? " yolo" : ""}${currentMode === item[2] ? " active" : ""}`;
      button.textContent = item[0]; button.disabled = Boolean(settingsBusy);
      button.onclick = () => systemAction(item[1], item[1] === "yolo" ? "YOLO 會停用 MCP command permission gates，並允許 exec 直接 create / edit / move / delete 檔案。確定切換？" : "");
      permissionGrid.appendChild(button);
    }
    permissionCard.body.appendChild(permissionGrid); wrap.appendChild(permissionCard.card);

    const maintenanceCard = createSettingsCard("維護工具", "更新、回滾、健康檢查與 Session 清理");
    const maintenanceActions = document.createElement("div"); maintenanceActions.className = "actionGrid";
    maintenanceActions.append(
      makeActionButton("更新 MCP", "Deploy source + restart", "update", { confirm:"更新會重新部署 Coding Tools MCP，期間 Tunnel 會短暫斷線。確定更新？" }),
      makeActionButton("回滾上一版", "Rollback latest backup", "rollback", { danger:true, confirm:"要回滾到上一份 MCP deployment backup 嗎？" }),
      makeActionButton("健康檢查", "讀取 /healthz", "health"),
      makeActionButton("清理閒置 Session", "呼叫 /prune", "prune")
    );
    maintenanceCard.body.appendChild(maintenanceActions);
    if (lastHealth) {
      const health = document.createElement("div"); health.className = "healthBox";
      if (lastHealth.ok === false) {
        health.textContent = `health: FAILED\n${lastHealth.error || "unknown error"}`;
      } else {
        const build = lastHealth.build_identity || {};
        health.textContent = [
          `health: ${lastHealth.status || "ok"}`,
          `version: ${lastHealth.display_version || lastHealth.version || build.display_version || "unknown"}`,
          `mode: ${lastHealth.permission_mode || currentMode}`,
          `workspace: ${lastHealth.workspace || "unknown"}`
        ].join("\n");
      }
      maintenanceCard.body.appendChild(health);
    }
    wrap.appendChild(maintenanceCard.card);
    body.appendChild(wrap);
  }

  function render() {
    syncPageTheme();
    statusDot.classList.toggle("live", connected);
    statusText.textContent = connected
      ? (state && state.dnd ? "已連線 · 免打擾" : "已連線 · 即時")
      : (connectionError ? "請允許瀏覽器存取本機網路" : "本機主控台未連線");
    const permissionMode = String(state && state.permission_mode || "").toLowerCase();
    mode.dataset.mode = permissionMode;
    mode.textContent = permissionMode === "dangerous" ? "YOLO MODE" : permissionMode === "trusted" ? "TRUSTED" : permissionMode === "safe" ? "SAFE" : "";
    dnd.checked = Boolean(state && state.dnd);
    const help = state && state.human_help;
    updateFocusMask();
    badge.hidden = !help;
    edge.querySelector("i").className = help ? "attn" : connected ? "live" : "";
    $(".detailsToggle").hidden = activeTab !== "activity";
    $(".clear").hidden = activeTab !== "activity";
    if (activeTab === "help") renderHelp();
    else if (activeTab === "settings") renderSettings();
    else renderActivity();
  }

  async function systemAction(action, confirmation = "") {
    if (confirmation && !window.confirm(confirmation)) return;
    const labels = {
      start_all:"啟動服務", stop_all:"停止服務", restart_all:"重新啟動服務",
      restart_tunnel:"重啟 Tunnel",
      update:"更新 MCP", rollback:"回滾 MCP", safe:"切換 SAFE", trusted:"切換 TRUSTED",
      yolo:"切換 YOLO", health:"健康檢查", prune:"清理 Session"
    };
    settingsBusy = labels[action] || action;
    if (activeTab === "settings") renderSettings();
    try {
      const result = await request("/v1/system/action", { method:"POST", body:{ action } });
      if (action === "health") {
        lastHealth = result;
        toast("健康檢查完成");
      } else if (action === "prune") {
        toast("閒置 Session 清理完成");
      } else {
        toast(`${labels[action] || action} 已送出${result && result.requires_uac ? " · 請確認 UAC" : ""}`);
      }
    } catch (error) {
      toast(`${labels[action] || action}失敗：${error.message}`);
    } finally {
      settingsBusy = "";
      if (activeTab === "settings") renderSettings();
      setTimeout(poll, 900);
    }
  }

  async function respondHelp(requestId, outcome, answer) {
    try {
      await request("/v1/human-help/respond", { method:"POST", body:{ request_id:requestId, outcome, answer } });
      toast("已交還代理，工作會繼續進行");
      await poll();
    } catch (error) { toast(`送出失敗：${error.message}`); }
  }

  function noteHumanHelpActivity(requestId) {
    if (!requestId) return;
    const now = Date.now();
    if (now - lastHelpActivitySentAt < HELP_ACTIVITY_THROTTLE_MS) return;
    lastHelpActivitySentAt = now;
    request("/v1/human-help/activity", { method:"POST", body:{ request_id:requestId } }).catch(() => {});
  }

  function isEditableTarget(target) {
    if (!(target instanceof HTMLElement) || host.contains(target)) return false;
    return target.matches("textarea,input,[contenteditable='true'],[role='textbox']") || Boolean(target.closest("textarea,input,[contenteditable='true'],[role='textbox']"));
  }

  function noteEscapeComposerActivity(event) {
    const helpId = state && state.human_help && state.human_help.request_id || "";
    if (helpId && isEditableTarget(event.target)) noteHumanHelpActivity(helpId);
  }

  async function poll() {
    clearTimeout(pollTimer);
    let helpToAcknowledge = "";
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
      if (helpId && helpId !== lastPresentedHelpId && document.visibilityState === "visible") {
        helpToAcknowledge = helpId;
        selectTab("help");
        setOpen(true);
      }
      if (!helpId) lastPresentedHelpId = "";
    } catch (error) { connected = false; state = null; connectionError = String(error && error.message || error); }
    render();
    if (helpToAcknowledge) {
      try {
        await request("/v1/human-help/seen", { method:"POST", body:{ request_id:helpToAcknowledge } });
        lastPresentedHelpId = helpToAcknowledge;
      } catch (error) {
        connectionError = String(error && error.message || error);
      }
    }
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
    selectTab(tab.dataset.tab);
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
  for (const eventName of ["keydown", "input", "paste", "compositionupdate", "pointerdown"]) {
    document.addEventListener(eventName, noteEscapeComposerActivity, true);
  }
  window.addEventListener("resize", updateFocusMask);
  window.addEventListener("scroll", updateFocusMask, true);
  poll();
})();
