# Coding Tools MCP 測試

所有測試都從 repo 根目錄執行。測試會建立臨時工作區，核心檔案與 MCP
整合測試不會修改目前專案。

```powershell
# 每次修改或編譯後執行；速度最快
.\test-coding-tools.ps1 -Mode Quick

# 包含隔離 MCP HTTP 服務、初始化、工具列表、工具呼叫和 Session 流程
.\test-coding-tools.ps1 -Mode Full

# 另外檢查目前已安裝服務的版本與 broker 自我測試
.\test-coding-tools.ps1 -Mode System

# 使用專用測試視窗與隔離 Chrome，逐項跑完 Computer Use / Browser Use
.\test-coding-tools.ps1 -Mode Interactive
```

`Quick` 會執行既有的 `service\validate-private-source.py`，並補上編譯、
公開工具、內部相容工具、Git 與執行工作階段的逐項檢查。`Full` 會另外
啟動一個臨時的 loopback MCP 服務；不會碰正式的 8765、8766 或 8767
連接埠。部署前驗證固定使用 `Full`，把 HTTP/OAuth 流程也納入編譯閘門。

`System` 預設讀取 `http://127.0.0.1:8766/healthz`，也可以明確指定：

```powershell
.\test-coding-tools.ps1 -Mode System `
  -HealthUrl http://127.0.0.1:8766/healthz `
  -ServiceRoot C:\ProgramData\WebGPTCodingToolsMCPService
```

`Interactive` 會短暫開啟專用 Windows 測試視窗及全新的臨時 Chrome 使用者
目錄，實際驗證視窗列舉、檢查、截圖、啟用、左右鍵、文字輸入、按鍵、
捲動與瀏覽器導覽。它不會碰目前開著的應用程式或私人分頁，結束後會
自動關閉測試視窗並刪除臨時資料。

測試輸出分成四種狀態：`PASS`（自動通過）、`FAIL`/`ERROR`（需要修正）、
`MANUAL`（必須由人操作或會改變正式系統）、`PAUSED`（目前明確不執行）。
`Quick`、`Full`、`System` 不會自行移動滑鼠，所以會把 Computer Use 與
Browser Use 列成 `PAUSED`；要實際執行時明確使用 `Interactive`。Web Console
的原始碼契約仍會自動驗證，但實際畫面與設定按鈕列為 `MANUAL`。

測試輸出格式是：

```text
[PASS] source.compile
[FAIL] mcp.http — failure detail
[MANUAL] manual.web_console.visual - requires opening the live Web Console
[PAUSED] paused.computer_use - paused by Ken; no desktop-control action is invoked
TEST_SUMMARY {"counts": {"MANUAL": 7, "PASS": 43, "PAUSED": 3}, "mode": "full"}
```

只要出現 `FAIL` 或 `ERROR`，程序就會以非零結束碼離開，方便接到編譯和
部署流程。
