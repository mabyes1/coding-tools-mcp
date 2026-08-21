# Coding Tools MCP 鬼之重構總計畫

> 性質：Living document。這份文件不是重構完成後補寫的墓誌銘，而是每一階段開始前、完成後都必須更新的施工圖。
>
> 核心規則：**先鎖行為，再搬結構；一次只拆一個責任；每刀都要能驗證、能回退、能從這份文件續接。**

## 0. 重構基線

- Stable baseline commit：`29121c40813a8fe9b3af05113c2f9ebcfe9882ec` (`29121c4`)
- Baseline subject：`feat: harden coding tools tunnel and workspace routing`
- Branch：`main`
- 重構開始前 repo 狀態：tracked files clean；`ADHD_ASSESSMENT_NOTES_2026-08-21.md` 為既有 untracked 個人筆記，**排除於本次所有修改與 commit**。
- `private/coding_tools_mcp/server.py`：8353 lines / 348,943 bytes。
- `Runtime`：約 1827–5060，單一 class 約 3200+ lines。
- `MCPHandler`：約 6807–7719，HTTP / MCP / OAuth 路由仍高度集中。
- `input_schemas()`：約 6401–6755，tool schema 集中在單一大型函式。
- `service/validate-private-source.py`：614 lines，目前是最重要的 regression contract 集合。

### 已經完成，這次只准保護、不准倒退的地基

1. **Privilege boundary**
   - Windows MCP / Tunnel service 使用 `LocalService`，不是 `SYSTEM`。
   - Elevated request queue 已移除 signed-in user write access。
   - Elevated broker privileged files 對 signed-in user 僅 `RX`；`SYSTEM` / Administrators 保有完整權限；LocalService 僅必要讀取權。
   - Elevated Broker 維持固定 action + manifest/hash 驗證，不得退化成 generic administrator shell。

2. **Release / update transaction**
   - deploy 有 staging、source validation、broker artifact build、build identity。
   - 正式替換前建立完整 app / runner / service-component bundle backup。
   - 安裝後 health check；中途失敗可 rollback。
   - release backup retention 已有限制，預設保留 20 份。

3. **Workspace / owner state boundary**
   - workspace path traversal、absolute escape、symlink write escape 必須維持防護。
   - default cwd 可依 owner + workspace 保存，且不可跨 owner 洩漏。
   - Web Project name 只解析 workspace 第一層同名 project；找不到時 fallback workspace root。

以上三組直接視為 regression contract，不重新設計，除非後續有獨立 issue 證明現有 contract 本身錯誤。

---

## 1. 這次到底要解決什麼

這次不是「把 8353 行切成十個 800 行檔案」的美容工程。目標是讓責任邊界真的存在，降低下列風險：

- `server.py` 同時知道 transport、workspace、permission、process/session、filesystem、git、schema、tool dispatch、HTTP、OAuth、diagnostics、image，任何修改都容易跨區污染。
- `Runtime` 是實質 God Object。tool handler、policy、state、session registry、telemetry 與 transport-facing state 混在一起。
- tool catalog、schema、handler dispatch 三者雖已有 registry 概念，但仍與 `Runtime` method name 強綁。
- HTTP/MCP handler 同時處理 protocol lifecycle、session owner、auth、OAuth endpoints、CORS 與 response formatting。
- installation / update / broker scripts已有 transaction 化，但仍存在多處 ACL、artifact build、service lifecycle 的重複知識，後續應收斂為共享 primitive。
- regression contracts 多集中在一個 600+ 行 validator，長期會再次變成第二個 God Script。

### 成功條件

重構完成不是看 `server.py` 剩幾行，而是看以下條件：

1. 修改 filesystem tool 不需要理解 HTTP / OAuth。
2. 修改 HTTP session lifecycle 不需要碰 exec / git tool 實作。
3. 修改 permission policy 有單一入口，不需要追 `Runtime` 內散落分支。
4. tool schema、tool metadata、handler registration 有清楚 source of truth，不靠名稱魔法暗中同步。
5. process/session lifecycle 能獨立測試。
6. install/update/repair 共用相同 deployment primitives，不各自複製 ACL / artifact / service 規則。
7. 每個公開 capability 都能從 contract 文件或 automated check 找到其 I/O、failure、permission、state/lifecycle invariants。
8. 任一階段中斷後，新 session 只讀本文件 + git diff/status 就能準確續工。

---

## 2. 絕對不可破壞的 Behavior Contracts

在任何 module extraction 之前，先把現有 validator 中的契約視為 API。

### 2.1 Public tool surface

- connector public tool budget：`<= 20`。
- 必須保留的 public tools 至少包含：
  - `get_default_cwd`
  - `set_default_cwd`
  - `request_permissions`
  - `human_help_me`
  - `computer_use`
  - `browser_use`
- 已移除的舊 public tools 不可偷偷復活，例如：
  - `list_sessions`
  - `request_elevated_action`
  - `git_blame`
- unknown / removed tool 必須乾淨回傳 JSON-RPC `-32602 Unknown tool`。

### 2.2 Exec / session

- `exec_command.execution_context` 僅 `service` / `active_user`。
- active-user PowerShell 必須先做 syntax validation，不能把語法錯誤直接丟給 child process。
- session capacity / output retention / cancellation / stdin / watchdog 行為不可因拆 module 產生 race。
- server_info / health 必須繼續能看 running / starting / retained / available slots。
- Windows command env 的 developer-tool profile vars 與 runtime isolation 必須維持。

### 2.3 Permission / privilege

- `dangerous` mode、one-shot grant、session grant 的語義不變。
- `privileged_executable` approval **永遠不等於 Administrator elevation**。
- 真正需要 admin 的操作只能走 Elevated Action contract。
- permission grant 必須綁 owner / workspace / tool / permission / arguments digest（依 scope）。

### 2.4 HUMAN HELP / Computer Use

- `human_help_me` 仍是 blocking handoff contract，不可被重構成普通 informational result。
- Computer Use action schema 必須與 `computer-use-actions.json` 同步。
- advertised action 必須真的有 backend branch。
- `inspect` / screenshot 不可搶 foreground。
- click 不可因為只有 focus 成功就假裝 click 成功。
- overlay 維持 per-operation leases。

### 2.5 HTTP / MCP

- MCP protocol negotiation、initialize、tools/list、tools/call、cancel 流程不變。
- HTTP session TTL = 300s。
- HTTP in-flight TTL = 90s，stale in-flight 必須能被 watchdog 回收。
- client disconnect 是正常 transport termination，不可升格成 opaque internal error。
- ExceptionGroup / TaskGroup 類錯誤必須保留 leaf diagnostics，不能再只顯示 `TaskGroup`。

### 2.6 Workspace / filesystem / git

- path boundary、symlink write boundary、default cwd semantics 不變。
- directory-only `cd` 必須持久化 default cwd。
- git scope 必須跟 default cwd 一致。
- patch relative path 必須跟 default cwd 一致，且 atomic patch commit semantics 不變。

---

## 3. 目標架構

這是責任邊界，不要求第一天就長成完全相同的檔名；若實際 dependency graph 顯示需要調整，先改本文件再改 code。

```text
coding_tools_mcp/
  server.py                 # 最後只保留 composition / public entry compatibility
  runtime.py                # Runtime composition、request context、shared state wiring
  tool_catalog.py           # ToolSpec、public catalog、annotations、definition generation
  tool_schemas.py           # input/output schema + schema validation

  workspace.py              # WorkspaceEntry / Workspace / cwd / path boundary
  command_policy.py         # command parsing、path candidate、env/shell policy checks
  execution.py              # exec_command orchestration + execution/session facade
  session_store.py          # ExecutionRegistry、retention、session lookup/lifecycle

  tools/
    filesystem.py           # read/list/search/apply_patch tool handlers
    git.py                  # git_status/diff/log/... handlers
    permissions.py          # request_permissions + privilege-facing tool logic
    desktop.py              # human_help / computer_use / browser_use facade
    diagnostics.py          # server_info/check_exec_environment
    images.py               # view_image + image sniff/resize helpers

  activity.py               # trace/activity log sanitization + persistence
  http_server.py            # RuntimeHTTPServer + health + MCP HTTP routing
  oauth_http.py             # OAuth HTTP endpoint glue；oauth.py 保留 domain logic
  bootstrap.py              # build_runtime / run_http / run_stdio / CLI parser / signals
```

### Dependency direction

原則上只能往下依賴：

```text
bootstrap / http_server
        ↓
runtime / tool catalog
        ↓
tool handlers / execution / workspace / policy
        ↓
processes / patching / oauth / protocol / transport_* 等既有低階 modules
```

禁止低階 module 反向 import `server.py`。`server.py` 在最終形態應接近 compatibility facade，不再是所有東西的宇宙中心。

---

## 4. 執行策略：一刀一責任，不做 Big Bang

### Phase 0 — Contract Freeze & Characterization

**目的：先把地板畫出來，之後才准拆牆。**

- [x] 把 `validate-private-source.py` 現有 contract 依既有區塊與新 characterization 區塊固定下來；後續 validator decomposition 留在 Phase 8，避免 Phase 0 為了排版先製造額外 churn。
- [x] 補上目前缺少但重構最容易踩到的 characterization checks：
  - [x] tool registry → handler resolution 一致性
  - [x] Runtime close ownership / shared ExecutionRegistry semantics
  - [x] request context cleanup，即使 handler error 也不殘留 request id / permission claim
  - [x] HTTP handler 與 Runtime lifecycle ownership
  - [x] server_info payload 關鍵欄位穩定性
- [x] 建立 baseline validation 記錄。
  - Source behavior：`PRIVATE_MCP_SOURCE_CHECK_OK tools=20 context_files=1`
  - Public tool canonical definition：20 tools / 24,739 bytes
  - Public tool contract SHA-256：`10a6219c4dd9a739f3ad6d05572f449d0800f8ad9bce16184851d10413b65392`

**Exit gate：** 不改 production behavior，只新增/整理 contract；baseline 全綠才進 Phase 1。

**Phase 0 result：PASS。** 此階段只修改 validator 與本文件，未修改 production runtime behavior。

### Phase 1 — Extract Pure / Low-Risk Foundations

先拆幾乎不碰 process lifecycle 的純邏輯，建立重構節奏。

- [x] `workspace.py`
  - [x] WorkspaceEntry catalog
  - [x] workspace selector / allowlist
  - [x] `ResolvedPath` / `Workspace`
  - [x] cwd/path boundary helpers
  - Characterization commit：`71e54d8` (`test: freeze workspace boundary behavior`)
  - Extraction commit：`7040c7c` (`refactor: extract workspace domain`)
  - Verification：compile + full source validator PASS；11 extracted symbols/constants AST-identical to pre-extraction HEAD。
  - `server.py`：8353 → 7960 lines。
- [x] `tool_schemas.py`
  - [x] schema builders
  - [x] `input_schemas`
  - [x] argument/schema validation
- [x] `tool_catalog.py`
  - [x] `ToolSpec`
  - [x] registry / public names / annotations / definitions
  - Extraction commit：`062f3cc` (`refactor: extract tool catalog and schemas`)
  - Verification：compile + full source validator PASS；14 extracted function/class definitions + 4 core assignments AST-identical to pre-extraction HEAD。
  - Public tool contract remained exactly 20 tools / 24,739 bytes / SHA-256 `10a6219c4dd9a739f3ad6d05572f449d0800f8ad9bce16184851d10413b65392`。
  - `tool_catalog.py` / `tool_schemas.py` do not import `server.py`。
  - `server.py`：7960 → 7165 lines（baseline 8353 → 7165）。

**要求：** 對外 import 暫時可由 `server.py` re-export，讓現有 validator 和其他 module 不必同時大改。

**Exit gate：** source validator 全綠；public tool JSON schema 與 baseline 等價。

### Phase 2 — Split Tool Handler Domains

把 Runtime 內最容易獨立的 handler 先移出，但 Runtime 仍當 composition root。

- [ ] filesystem tools
- [ ] git tools
- [x] image tool
  - Characterization commit：`7fbf68f` (`test: freeze image tool behavior`)
  - Extraction commit：`01e2b5c` (`refactor: extract image tool domain`)
  - `Runtime.view_image()` is now a one-line delegation into `tools/images.py` with explicit `resolve_existing` dependency injection.
  - PNG identification / dimensions / MCP image data / unsupported-binary / max-bytes contracts PASS.
  - Five pure image helpers AST-identical to pre-extraction HEAD.
  - Full source validator PASS；`server.py`：7165 → 7000 lines。
- [ ] diagnostics / server_info helpers
- [ ] human_help / desktop facade

優先採「service object / plain functions + explicit dependencies」，避免把一個 God Class 切成六個互相拿整個 Runtime 的小 God Class。

**Exit gate：** 每組搬完單獨 commit + validation；不得一口氣搬全部。

### Phase 3 — Execution / Session Lifecycle

這是高風險區，獨立成自己的工程。

- [ ] `ExecutionRegistry` 從 server.py 移出。
- [ ] session retention / output snapshot / prune / cancel / stdin lifecycle 收斂成單一 session service。
- [ ] exec orchestration 與 command policy 分離：
  - `command_policy.py`：解析與 allow/deny 判斷
  - `execution.py`：spawn / active_user delegation / output formatting
- [ ] Runtime 只保留 request-to-service wiring。
- [ ] 明確定義 registry ownership：誰 create、誰 close、HTTP reconnect 如何 share。

**Exit gate：** session concurrency、kill、retention、read_output、active_user、server_info pressure 全部 regression checks 通過；實際 service smoke test 通過。

### Phase 4 — Permission / Elevated / Interactive Boundaries

這階段不是重寫 privilege architecture，而是把已經安全的 contract 從 Runtime 中抽出，並避免未來散掉。

- [ ] permission grants / digest / owner binding 抽成 permission service。
- [ ] normal approval 與 true elevation 的 API 名稱與型別保持清楚區隔。
- [ ] Elevated Action 仍只能透過 manifest-defined actions。
- [ ] Interactive broker 與 Elevated broker 的 queue / identity / ACL regression checks 補齊。

**Exit gate：** ACL、broker identity、grant semantics、dangerous mode 都有 automated or scripted verification。

### Phase 5 — HTTP / MCP / OAuth Transport Split

- [ ] `MCPHandler` 拆掉 OAuth endpoint glue。
- [ ] MCP HTTP request parsing / session acquisition / dispatch / response lifecycle 收斂到 `http_server.py`。
- [ ] OAuth HTTP glue 移到 `oauth_http.py`，domain logic 繼續使用既有 `oauth.py`。
- [ ] `RuntimeHTTPServer` / health handler / tool-list notification lifecycle 整理。
- [ ] 保持 stdio transport 與 HTTP transport 共用同一 Runtime contract。

**Exit gate：** OAuth on/off、auth token、CORS、metadata、MCP session reconnect、disconnect、TTL/watchdog 全綠。

### Phase 6 — Bootstrap / server.py Reduction

- [ ] `build_runtime`
- [ ] `run_http`
- [ ] `run_stdio`
- [ ] parser / signal handling

移到 `bootstrap.py` 後，`server.py` 只留下 compatibility exports 與極薄 entry facade。

**目標不是硬性行數 KPI**，但若完成後 `server.py` 仍然需要數千行，代表責任邊界沒有真的拆完，必須重新 review。

### Phase 7 — Deployment Script Consolidation

Python server 穩定後再碰 installer/update，避免兩個高風險面同時移動。

- [ ] ACL primitives 共用化。
- [ ] broker artifact build 共用化。
- [ ] service stop/start/health 共用化。
- [ ] install / deploy / repair 不再複製同一 policy。
- [ ] rollback path 與 normal deploy path 使用同一 bundle model。

**Exit gate：** ValidateOnly、normal update、rollback、repair、fresh-install relevant checks 全通過。

### Phase 8 — Validator Decomposition & Architecture Guardrails

- [ ] 把 600+ 行 validator 拆成按 domain 的 checks，但保留單一入口。
- [ ] 加 architecture guard：禁止低階 module import `server.py`。
- [ ] 加 tool catalog consistency guard。
- [ ] 加容易再次膨脹的 size / dependency warning（警告即可，不以行數粗暴 fail build）。
- [ ] 最終 full regression + installed service smoke + tunnel smoke。

---

## 5. 每一刀的固定 SOP

每個 phase / sub-phase 都照這個順序，不准跳：

1. **READ**：先讀本文件「Current Checkpoint」和該 phase。
2. **STATUS**：`git status`，確認沒有把別人的／使用者的檔案捲進來。
3. **MAP**：列出這一刀要搬的 symbol、caller、import dependency、contract。
4. **TEST FIRST**：必要時先補 characterization check。
5. **MOVE ONLY**：第一個 commit 優先只做 relocation / dependency injection，不順手改行為。
6. **VALIDATE**：至少跑 source validator；碰 runtime/Windows broker/transport 則跑對應 smoke。
7. **DIFF REVIEW**：確認沒有 unrelated rewrite / formatting blast。
8. **UPDATE THIS FILE**：勾 checklist，寫結果、發現、下一刀。
9. **COMMIT**：一個責任一個 commit，commit message 說明 extraction domain。
10. **RESUME TEST**：假設現在 context 全失憶，只看本文件 + git log/status 能不能知道下一步。不能就補文件。

---

## 6. 禁止事項

- 禁止一次重寫整個 `server.py`。
- 禁止「既然都碰到了」順便加 feature。
- 禁止在 extraction commit 同時大量改 public payload/schema。
- 禁止為了漂亮 abstraction 改掉已知 production behavior。
- 禁止 low-level module 依賴整個 Runtime 只為拿一兩個欄位；改成 explicit dependency。
- 禁止把一個 3000 行 God Class 變成一個 3000 行 `utils.py`。
- 禁止修改或 commit `ADHD_ASSESSMENT_NOTES_2026-08-21.md`。
- 禁止 validation 紅燈仍繼續下一 phase。

---

## 7. Validation Ladder

依風險逐級跑，不是每個純 helper extraction 都重啟整台服務，但高風險區不能只做 syntax check。

### L0 — Static / import

- package compile/import
- catalog/schema invariants

### L1 — Source behavior validator

- `service/validate-private-source.py`
- 必須持續覆蓋目前既有 production regression contracts。

### L2 — Domain smoke

- filesystem / patch
- git
- exec/session
- Computer Use
- permission/broker
- HTTP/OAuth

依本次修改 domain 選擇。

### L3 — Installed service smoke

- deploy staging validation
- service restart
- `/healthz`
- `server_info`
- 真實 connector tool call

### L4 — Release / rollback smoke

只在 deployment lifecycle 有修改時執行；需要驗證正常 update 和 rollback 都能回到 healthy state。

---

## 8. Commit / Rollback Strategy

- baseline：`29121c4`
- 不做一個超大型「refactor everything」commit。
- 每個 extraction domain 一個可單獨 revert 的 commit。
- 純搬移與 behavior change 分開 commit。
- 發現 architecture assumption 錯誤時，先改本計畫，再調整 implementation；不要靠腦內版本偷偷轉彎。
- 任一 phase 若 regression 無法在合理範圍內定位，直接回到上一個 green commit，而不是在紅燈上繼續堆 patch。

---

## 9. Current Checkpoint

**狀態：PLAN / PRE-CODE**

### 2026-08-21 initial reconnaissance

- [x] 確認 stable baseline commit = `29121c4`。
- [x] 確認 tracked repo clean；ADHD markdown 為既有 untracked exclusion。
- [x] 發現目前 live Tunnel `server_info.build_identity` 仍回報 `0.2.2-private.36-dev+57f020cdd9e0` / `dirty=true`；repo source baseline 與 installed build identity 尚未對齊。已明確記錄此 ambiguity，source-only extraction 不以 live build 作判準，第一次 installed-service smoke 前必須對齊。
- [x] 重新盤點 live `server.py`，確認目前 8353 lines，`Runtime` 仍是最大責任聚合點。
- [x] 重新讀 live source validator，整理現有 regression contracts。
- [x] 驗證舊 review 的兩項 P0 已經落地：privileged ACL hardening、bundle deploy/rollback transaction。
- [x] 建立本重構總計畫。
- [x] Phase 0 Contract Freeze & Characterization 完成；source validator 全綠，public tool contract fingerprint 已鎖定。
- [ ] Live installed build identity 對齊延後到第一個**安全部署 checkpoint**：目前重啟 MCP 會中斷正在施工的 Tunnel，所以 source-only extraction 階段以 `29121c4` + validator baseline 為 authoritative baseline；在任何 installed-service smoke 前必須先完成對齊。
- [x] Phase 1 / Workspace domain extraction 完成。
- [x] Phase 1 / Tool schema + catalog extraction 完成。
- [ ] **NEXT：Phase 2 / split low-risk tool handler domains。先從 diagnostics / image 等低耦合區開始，不碰 Execution / Session。**

### 下一個 session / context 壓縮後應從這裡開始

1. 讀 `REFACTOR_MASTER_PLAN.md`。
2. `git status`，確認只有預期變更。
3. Phase 0 已完成；source baseline validation 必須保持全綠。
4. 第一個真正 extraction 是 **Workspace domain**，不是 Execution / HTTP。
5. 在第一次 installed-service smoke 前，先把 live build identity 對齊當時的 green commit。

---

## 10. 完成定義

鬼之重構完成時，必須同時滿足：

- [ ] `server.py` 不再承載多個獨立 domain implementation。
- [ ] Runtime 不再是 tool implementation God Object。
- [ ] transport / runtime / tool / execution / permission / workspace dependency direction 清楚。
- [ ] 每個公開 capability 有可找到的 contract + regression coverage。
- [ ] installed MCP、Tunnel、Computer Use、HUMAN HELP、active_user exec、permission approval、elevated action 全部仍可用。
- [ ] update / rollback 仍可回到 healthy service。
- [ ] 沒有以「功能看起來能跑」掩蓋 permission/security regression。
- [ ] 本文件最後 checkpoint 能清楚描述新架構與後續維護方式。

到這裡才算真的把鬼抓完，不是把鬼從 `server.py` 搬去別的房間。
