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

- [x] filesystem tools
  - Characterization commit：`1a9f2ae` (`test: freeze filesystem read and search behavior`)
  - Extraction commit：`4453e9e` (`refactor: extract filesystem read and search tools`)
  - `read_file` / `list_dir` / `list_files` / `search_text` moved to `tools/filesystem.py` with explicit `Workspace` / resolver / executable-discovery dependencies.
  - `fd` and `rg` fast paths moved together with their fallback implementations so engine selection does not split domain ownership.
  - Temp-workspace contracts cover line selection, binary rejection, directory listing, glob discovery, search context/column semantics, and cross-platform line endings.
  - Ten extracted filesystem helpers AST-identical to pre-extraction HEAD.
  - Full source validator PASS after the interrupted shutdown attempt；`server.py`：6853 → 6273 lines（baseline 8353 → 6273）。
- [x] git tools
  - Characterization commit：`9849017` (`test: freeze git tool behavior`)
  - Extraction commit：`b178501` (`refactor: extract git tool domain`)
  - Implementation moved into `tools/git_tools.py` with explicit workspace / cwd / resolver / command-env / git-discovery / patch-baseline dependencies.
  - `Runtime.git_status` / `git_diff` / `git_log` / `git_show` / `git_blame` are now one-line delegations；legacy `_git_repo_scope` remains as a thin compatibility bridge because the frozen validator exercises it directly.
  - Duplicate Git parser implementations were removed from `server.py`; `server.py` now re-exports the extracted helpers.
  - Compile + full source validator PASS；four pure helpers (`parse_branch_line`, `validate_git_ref`, `parse_git_blame_porcelain`, `parse_diff_files`) are AST-identical to pre-extraction HEAD.
  - Final review：dead `_git_*` wrappers removed except `_git_repo_scope`, which remains only because the frozen validator calls it directly；staged `git diff --check` PASS.
  - `server.py`：6273 → 5802 lines（baseline 8353 → 5802）。
- [x] apply_patch tool
  - 2026-08-22 dependency map：the orchestration is localized to workspace/default-cwd resolution, symlink-write rejection, `patch_lock`, `AtomicPatchCommitter`, `patch_baselines`, and result formatting. It does **not** directly own permission-grant logic.
  - Decision：treat this as the final Phase 2 tool-domain extraction rather than carrying it into Phase 3. Keep low-level parsing/atomic commit primitives in existing `patching.py`; extract Runtime orchestration only.
  - Existing characterization only proves relative-path update from persistent default cwd, so add add/dry-run/move/delete + baseline semantics before production relocation.
  - Characterization expanded on 2026-08-22：add result/content, dry-run no-mutation/no-baseline-change, update+move metadata/content, delete counts, patch-baseline registration, and staged validation failure all-or-nothing behavior. Full validator PASS; ready for a tests-only freeze commit.
  - Characterization commit：`027ad52` (`test: freeze apply patch orchestration`).
  - Production extraction now starts from a green checkpoint; target module is `tools/patch_tools.py`, while `patching.py` remains the low-level parser/atomic-commit layer.
  - Production orchestration moved to `tools/patch_tools.py`; `Runtime.apply_patch()` is now a thin explicit-dependency delegation. `patching.py` remained untouched.
  - Compile + expanded full validator PASS；repository search confirms no stale Runtime-only patch helpers and no reverse import of `server.py` from the new module；`git diff --check` PASS.
  - Current pre-commit line count：`server.py` 5802 → 5686（baseline 8353 → 5686）。
  - Extraction commit：`514542a` (`refactor: extract patch tool orchestration`).
- [x] image tool
  - Characterization commit：`7fbf68f` (`test: freeze image tool behavior`)
  - Extraction commit：`01e2b5c` (`refactor: extract image tool domain`)
  - `Runtime.view_image()` is now a one-line delegation into `tools/images.py` with explicit `resolve_existing` dependency injection.
  - PNG identification / dimensions / MCP image data / unsupported-binary / max-bytes contracts PASS.
  - Five pure image helpers AST-identical to pre-extraction HEAD.
  - Full source validator PASS；`server.py`：7165 → 7000 lines。
- [ ] diagnostics / server_info helpers
  - [x] pure diagnostics helpers extracted to `tools/diagnostics.py`
  - Characterization commit：`17ac139` (`test: freeze diagnostics helper behavior`)
  - Extraction commit：`10c6830` (`refactor: extract diagnostics helpers`)
  - Covered contracts：fresh execution pressure、exec-environment summary、workspace SKILL metadata/path、missing executable discovery。
  - `server_info_payload()` intentionally remains in Runtime as composition root until session ownership moves in Phase 3。
- [x] human_help / desktop facade
  - Characterization commit：`be361ad` (`test: freeze desktop tool facade behavior`)
  - Extraction commit：`4260c49` (`refactor: extract desktop tool facade`)
  - `human_help_me` / computer/browser argument mapping moved to `tools/desktop.py` with explicit interactive-broker callbacks；broker protocol unchanged。
  - Full source validator PASS；old Runtime bodies and extracted helper bodies AST-identical after signature/docstring normalization。

Current `server.py`：5641 lines（baseline 8353 → 5641）。

優先採「service object / plain functions + explicit dependencies」，避免把一個 God Class 切成六個互相拿整個 Runtime 的小 God Class。

**Exit gate：** 每組搬完單獨 commit + validation；不得一口氣搬全部。

### Phase 3 — Execution / Session Lifecycle

這是高風險區，獨立成自己的工程。

- [x] `ExecutionRegistry` 從 server.py 移出。
  - 2026-08-22 map：registry currently owns active/completed execution maps and locks, **plus** reconnect-shared owner cwd / permission grants / runtime-dir metadata / HTTP session stats provider.
  - Relocation rule：move the registry as-is to `session_store.py`; do not split those historical shared-state fields during this extraction. Move `PermissionGrant` with it only as a data type so `session_store.py` does not import `server.py`; Phase 4 may later separate permission ownership.
  - Existing Phase 0 characterization already freezes owning-vs-shared Runtime close semantics and HTTP reconnect sharing. Add one missing close characterization for a live child process: registry close must clear maps, hard-kill the child, drain readers, and remain idempotent.
  - Live-child close characterization added on 2026-08-22 and full validator PASS：a spawned 60-second child is terminated by `ExecutionRegistry.close()`, maps are cleared, `closed=True`, and repeated close is harmless. Ready for a tests-only freeze commit.
  - Characterization commit：`e63d563` (`test: freeze execution registry close`).
  - Production relocation：`PermissionGrant` + `ExecutionRegistry` moved verbatim to `session_store.py`; `server.py` re-exports them via import. AST equivalence PASS for both classes versus pre-relocation HEAD.
  - Compile + full validator PASS；`session_store.py` has no reverse import of `server.py`；`git diff --check` PASS.
  - Extraction commit：`f1dad6b` (`refactor: extract execution registry`).
  - `server.py`：5686 → 5641（baseline 8353 → 5641）。
- [ ] session retention / output snapshot / prune / cancel / stdin lifecycle 收斂成單一 session service。
  - 2026-08-22 boundary decision：**do not move the whole remaining session block at once**. First move only retention/store lifecycle into `ExecutionRegistry`: completed-session remember, scratch cleanup, retained-byte accounting, eviction, completion, TTL prune, and active/completed lookup.
  - Keep `_make_session` with later execution/spawn orchestration. Keep output formatting/snapshot/read-output and stdin/kill/poll as separate later sub-phases. Keep `cancel_request` in Runtime as request-id → session-id glue even after session cancellation moves.
  - Existing validator does not directly freeze retention eviction/TTL behavior. Add characterization for completed-session promotion, oldest eviction + scratch cleanup, TTL prune + scratch cleanup, and missing-session lookup errors before moving these methods.
  - Retention/store characterization added：completed active session promotes to retained output；retained count evicts the oldest entry at `MAX_RETAINED_OUTPUT_SESSIONS` and removes its scratch dir；TTL prune removes expired retained output + scratch dir；missing active/completed lookups preserve `SESSION_NOT_FOUND` with their existing distinct categories. Full validator PASS; ready for tests-only freeze commit.
  - Characterization commit：`c55f22f` (`test: freeze session retention lifecycle`).
  - Production relocation in worktree：`_remember_output_session`, `_cleanup_session_scratch`, `_retained_output_bytes_locked`, `_evict_retained_locked`, `_complete_session`, `_prune_sessions`, `_get_output_session`, `_get_session` moved verbatim onto `ExecutionRegistry`; Runtime keeps thin compatibility wrappers.
  - Retention constants `MAX_RETAINED_OUTPUT_SESSIONS`, `COMPLETED_SESSION_TTL_SECONDS`, `MAX_RUNTIME_OUTPUT_BYTES` moved to `session_store.py` and are re-exported by `server.py`.
  - All eight moved methods are AST-identical to pre-relocation HEAD；compile + full validator + reverse-import check + `git diff --check` PASS. Current `server.py`：5641 → 5598（baseline 8353 → 5598）。
  - Retention/store extraction commit：`feadb6d` (`refactor: move session retention into registry`).
  - Next sub-phase boundary：move output snapshot/format/read paging only (`_snapshot_session`, `_session_output_summary`, `_format_session_output`, `read_output`) plus their pure output-ref helpers into the same session service. Keep stdin/poll/kill and metadata/list/process-tree out of this commit.
  - Existing validator has no direct output cursor/paging characterization. Before relocation, freeze explicit-cursor delta snapshot, truncated output-ref/next-action formatting, read-output paging, invalid output mode, and stream/output-ref mismatch rejection.
  - Output characterization added：explicit byte-cursor delta/cursor totals, invalid output-mode rejection, truncated terminal `output_ref` + `read_output` continuation action, byte-offset paging + next offset/action, and stream/output-ref mismatch rejection. Full validator PASS; ready for tests-only freeze commit.
  - Characterization commit：`a188a67` (`test: freeze session output paging`).
  - Production output layer relocated onto `ExecutionRegistry`：`_format_session_output`, `_snapshot_session`, `_session_output_summary`, `read_output`; pure `truncate_bytes` / `read_output_action` and `EXEC_PREVIEW_BYTES` moved to `session_store.py` and remain re-exported by `server.py` for compatibility.
  - All four methods + two helpers are AST-identical to pre-relocation HEAD；compile + full validator + reverse-import check + `git diff --check` PASS. Current `server.py`：5598 → 5372（baseline 8353 → 5372）。
  - Output extraction commit：`d883250` (`refactor: move session output paging into registry`).
  - Next process-control boundary：move `poll_session`, `write_stdin`, `_session_has_new_output`, `_wait_for_session_exit`, `kill_session`, `cancel_session` onto `ExecutionRegistry`. Keep `cancel_request` in Runtime because request-id → session-id mapping belongs to MCP request wiring; keep `_make_session` with later spawn orchestration.
  - Existing validator has no direct process-control characterization. Freeze completed-session poll, stdin-write rejection after exit, explicit-cursor new-output detection, forced live-child kill/eviction, and cancel-session registry removal before relocation.
  - Process-control characterization added：completed-session poll output, closed-session stdin rejection (`SESSION_CLOSED` / runtime), explicit-cursor new-output detection, real live-child forced `SIGKILL` + eviction, and cancel-session active-map removal. Full validator PASS; ready for tests-only freeze commit.
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

**狀態：PHASE 2 COMPLETE / PHASE 3 STARTING / GREEN CHECKPOINT**

### 2026-08-22 checkpoint — Git extraction complete

- Resumed from `ec31adb` (`docs: finalize refactor handoff`) with the Git characterization commit `9849017` already present.
- Found an interrupted/uncommitted Git extraction: modified `server.py` + new `tools/git_tools.py`; no unrelated tracked changes were present. Existing `ADHD_ASSESSMENT_NOTES_2026-08-21.md` remains excluded.
- Finished routing `git_status` / `git_diff` / `git_log` / `git_show` / `git_blame` through `GitTools`; removed duplicate Git parser bodies from `server.py` while preserving re-export compatibility.
- Compile + full source validator：**PASS** (`PRIVATE_MCP_SOURCE_CHECK_OK tools=20 context_files=1`).
- Pure helper AST equivalence：**PASS** for `parse_branch_line`, `validate_git_ref`, `parse_git_blame_porcelain`, `parse_diff_files` versus pre-extraction `HEAD`.
- LocalService cannot directly `git show` this repo without safe-directory configuration; verification used one-shot `git -c safe.directory=D:/coding-tools-mcp/coding-tools-mcp ...` only. No global Git config was changed.
- Dead `_git_*` wrappers were removed after repository-wide caller search; `_git_repo_scope` is intentionally retained as the one compatibility bridge exercised by the frozen validator.
- Final pre-commit checks：`git diff --check` **PASS**；no `server.py` reverse import exists in `tools/git_tools.py`；full source validator remains **PASS** after wrapper removal.
- `server.py` current line count：**5802**（baseline 8353 → 5802）。
- Extraction commit：`b178501` (`refactor: extract git tool domain`)；post-commit worktree returned to only the excluded `ADHD_ASSESSMENT_NOTES_2026-08-21.md` untracked file.
- `apply_patch` dependency map completed after the Git commit. It is sufficiently localized to remain a Phase 2 extraction; permission grants are not implemented inside the patch orchestrator.
- Characterization now covers add/dry-run/move/delete, patch-baseline registration, and staged validation failure without partial commit；full validator PASS before any production relocation.
- Characterization freeze commit：`027ad52` (`test: freeze apply patch orchestration`).
- Production orchestration is now in `tools/patch_tools.py`; `Runtime.apply_patch()` is a thin delegation and `patching.py` remains unchanged. Expanded validator + `git diff --check` PASS.
- Extraction commit：`514542a` (`refactor: extract patch tool orchestration`)；post-commit worktree again contains only the excluded ADHD note.
- **Phase 2 close decision：** low-risk tool-domain extraction is complete. `server_info_payload()` remains intentionally in Runtime because it composes execution/session ownership; that remaining work is reclassified into Phase 3 rather than keeping Phase 2 artificially open.
- Phase 3 map confirms `ExecutionRegistry` also carries owner cwd / permission grant shared state. This impurity is preserved intentionally for the relocation; splitting it now would combine architecture change with movement.
- Live-child registry close characterization now passes in the full validator.
- Registry close characterization commit：`e63d563` (`test: freeze execution registry close`).
- `PermissionGrant` + `ExecutionRegistry` relocation commit：`f1dad6b` (`refactor: extract execution registry`)；AST equivalence + full validator + reverse-import check all PASS；post-commit worktree returned to only the excluded ADHD note.
- Session lifecycle map completed. The next extraction is deliberately narrower than the phase headline: retention/store primitives only. Output formatting, stdin/poll/kill, request cancellation, and spawn orchestration remain separate.
- Retention/store characterization now passes in the full validator: promotion, FIFO count eviction + scratch cleanup, TTL prune + scratch cleanup, and missing lookup error semantics are frozen.
- Retention/store characterization commit：`c55f22f` (`test: freeze session retention lifecycle`).
- Eight retention/store methods are now relocated verbatim onto `ExecutionRegistry`; Runtime keeps thin wrappers for compatibility. AST equivalence + full validator + reverse-import check + `git diff --check` PASS；`server.py` is 5598 lines.
- Retention/store extraction commit：`feadb6d` (`refactor: move session retention into registry`)；post-commit worktree returned to only the excluded ADHD note.
- Output snapshot/read-output is the next isolated session sub-phase. It will not include stdin/poll/kill or session metadata/list/process-tree.
- Output snapshot/read-output characterization now passes in the full validator, including reconnect-safe explicit cursors and continuation refs/actions.
- Output characterization commit：`a188a67` (`test: freeze session output paging`).
- Output snapshot/format/read paging is now relocated verbatim onto `ExecutionRegistry`; Runtime keeps thin wrappers and `server.py` is 5372 lines. AST equivalence + full validator + reverse-import check + `git diff --check` PASS.
- Output extraction commit：`d883250` (`refactor: move session output paging into registry`)；post-commit worktree returned to only the excluded ADHD note.
- Process-control map is complete: session-id control moves to registry, request-id cancellation stays in Runtime, session construction stays with later spawn orchestration.
- Process-control characterization now passes in the full validator, including a real 60-second child terminated through `kill_session`.
- **Next：** commit this process-control freeze separately, then relocate only the mapped methods.

### 2026-08-21 end-of-session handoff

- Stable project baseline remains `29121c4`；所有重構都建立在 Phase 0 frozen contracts 上。
- Latest verified production extraction：`4453e9e` (`refactor: extract filesystem read and search tools`)。
- Latest checkpoint before this handoff update：`78408cc` (`docs: checkpoint filesystem extraction`)。
- `server.py`：**8353 → 6273 lines**。
- Full source validator：**PASS**，最後一次結果 `PRIVATE_MCP_SOURCE_CHECK_OK tools=20 context_files=1`。
- Public MCP contract 仍為 **20 tools / 24,739 canonical bytes / SHA-256 `10a6219c4dd9a739f3ad6d05572f449d0800f8ad9bce16184851d10413b65392`**。
- 新抽出的低階 modules 目前沒有反向 import `server.py`。
- Worktree 收尾時只有既有 `ADHD_ASSESSMENT_NOTES_2026-08-21.md` untracked；它是使用者私人筆記，**禁止修改、stage、commit**。

### 已完成

- [x] Phase 0：Contract Freeze & Characterization。
- [x] Phase 1：`workspace.py` extraction。
- [x] Phase 1：`tool_catalog.py` / `tool_schemas.py` extraction。
- [x] Phase 2：image domain → `tools/images.py`。
- [x] Phase 2：human-help / computer/browser facade → `tools/desktop.py`。
- [x] Phase 2：pure diagnostics helpers → `tools/diagnostics.py`。
- [x] Phase 2：filesystem read/list/search + `fd` / `rg` fast paths → `tools/filesystem.py`。
- [x] Phase 2：git tools。
  - Characterization commit：`9849017` (`test: freeze git tool behavior`)。
  - Extraction commit：`b178501` (`refactor: extract git tool domain`)。
  - 已鎖定：repo/default-cwd scope、git status worktree state、diff content/file metadata、log metadata、show metadata-only、blame attribution/content、option-like ref rejection。
  - Production implementation 已移至 `tools/git_tools.py`；Runtime public handlers 為薄 delegation；full validator PASS。
- [ ] Phase 2：剩餘 diagnostics / `server_info_payload()`；**刻意延後**，因為它仍與 execution/session ownership 相連，應與 Phase 3 邊界一起處理。

### 重要：今晚的 shutdown interruption

使用者曾誤按關機後取消。結果：

- Tunnel connector endpoint 一度回 `ExceptionGroup / TaskGroup`，該 Tunnel session 可視為被中斷。
- signed-in interactive broker 也被關掉，因此 `active_user` Git command 暫時不可用。
- 另一條 `coding-tools` MCP service 仍正常，已用它重新跑 compile + full validator 並完成 filesystem commit。
- 不需要為這次中斷做 source rollback；Git 已回到 clean green checkpoint。
- 下次電腦重新開機後，**先用 `server_info` / `git status` 確認服務與 repo 狀態即可**，不要假設今晚的 connector session 還存在。

### Live build identity 尚未對齊

目前 installed service 最後觀察到的 build identity 仍是舊的：

`0.2.2-private.36-dev+57f020cdd9e0` / `git_sha=57f020cdd9e0` / `dirty=true`

這在目前 source-only extraction 階段是**已知且接受的 ambiguity**。不要為了對齊版本在每一刀之間 deploy/restart，否則會一直切斷施工 connector。第一次真正進入 **installed-service smoke checkpoint** 前，再把 live build 對齊當時的 green commit，並驗證 `/healthz`、`server_info`、connector tool calls。

### 下一個新 Session 必做順序

1. **先讀完整 `REFACTOR_MASTER_PLAN.md`，尤其 Phase 2、SOP、Current Checkpoint。**
2. `server_info`，確認 MCP service 可用；若 Tunnel 不可用但 `coding-tools` 可用，可直接用後者，不需要先修 Tunnel 才能讀 repo。
3. `set_default_cwd` 到 `D:\\coding-tools-mcp\\coding-tools-mcp`（若 default cwd 尚未在此）。
4. `git status`：預期只有 `ADHD_ASSESSMENT_NOTES_2026-08-21.md` untracked。若有任何其他修改，先釐清，不要直接覆蓋。
5. `git log -n 10`，確認 filesystem extraction / handoff commits 存在。
6. 先跑一次 compile + `service/validate-private-source.py --package-parent private --workspace ..`，確認 green baseline。2026-08-21 收尾時此 validator 已在 commit `9849017` 後重新跑綠。
7. **NEXT IMPLEMENTATION：Phase 2 / Git tools extraction。**
   - Git characterization 已完成並 commit，不要重寫；先讀 `9849017` 的 tests 理解 frozen behavior。
   - map `git_status` / `git_diff` / `git_log` / `git_show` / `git_blame` 與 internal git helpers 的 dependency graph。
   - 再抽到 `tools/git.py`，Runtime 保留薄 delegation。
   - pure helper relocation 優先做 AST equivalence check。
   - extraction 完成後 full validator + diff review + independent commit + 更新本文件。
8. **暫時不要碰：**
   - `apply_patch` extraction：它與 patch transaction / write permission / workspace write boundary 較重，filesystem read/search 已拆，但 patch 應獨立處理。
   - ExecutionRegistry / exec/session lifecycle：這是 Phase 3，高風險，不得在 Git extraction 順手處理。
   - HTTP/OAuth：Phase 5。
   - deployment scripts：Phase 7。
9. Git domain 完成後，再重新評估 Phase 2 是否先拆 `apply_patch` domain，或直接宣告低風險 handler extraction 完成並進 Phase 3。**先更新計畫，再做決定。**

### 新 Session 可直接貼給 AI 的一句話

> 繼續 `D:\coding-tools-mcp\coding-tools-mcp\REFACTOR_MASTER_PLAN.md` 的鬼之重構；先讀 Current Checkpoint、確認 git/validator green，然後照 handoff 從 Phase 2 Git tools extraction 開始，不要重做已完成的 Workspace / schema / image / desktop / diagnostics / filesystem，也不要提前碰 Execution/HTTP/deployment。

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
