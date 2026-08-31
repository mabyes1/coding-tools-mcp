# Coding Tools MCP 功能清單

這份清單是測試執行器的範圍說明。每次執行會在終端機逐項列出
`[PASS]`、`[FAIL]`、`[MANUAL]` 或 `[PAUSED]`。

## 自動驗證

- 公開工具：`server_info`、`check_exec_environment`、`get_default_cwd`、`set_default_cwd`、`read_file`、`list_files`、`search_text`、`apply_patch`、`exec_command`、`write_stdin`、`kill_session`、`read_output`、`git_status`、`git_diff`、`git_log`、`view_image`
- 安全路徑：`human_help_me` 的 `chat_only` 可見性與回傳格式、`request_permissions` 參數邊界
- 內部/相容工具：`which_tools`、`list_workspaces`、`switch_workspace`、`list_dir`、`list_sessions`、`process_tree`、`poll_session`、`tail_output`、`find_output`、`git_show`、`git_blame`、`kill_tree`
- 原始碼契約：工具目錄、工作區與檔案邊界、搜尋與修補、Git、命令權限、執行工作階段、輸出保留、程序控制、映像檔、HTTP 生命週期/傳輸、Windows 服務部署契約
- 隔離 HTTP：健康檢查、Server Card、MCP 初始化、工具列表、工具呼叫、錯誤回應、工作階段輪詢/輸出/終止
- OAuth：狀態資料庫、PKCE、JWT bearer、授權碼一次性使用、refresh token 輪替/重放拒絕，以及隔離 HTTP 授權流程
- 系統唯讀檢查：已安裝服務版本與 Git 身分、兩個 broker 的 `--self-test`

## 人工驗證

- Web Console 實際畫面、分頁、設定和主控台動作
- 真實 HUMAN HELP 的 Web Console/桌面送達與人工回覆
- `request_permissions` 的 Windows 核准視窗
- `request_elevated_action`、`exec_command(execution_context=active_user)`
- 更新、回滾、重啟和 UAC 等會改變機器或已安裝服務的操作

## 明確啟動的互動驗證

- Computer Use：`list_windows`、`inspect`、`screenshot`、`activate`、`click`、`right_click`、`type_text`、`press_key`、`scroll`
- Browser Use：上述動作加上 `navigate`
- 測試目標：專用 Windows 測試視窗、localhost 測試頁、沿用目前 Profile 的 Coding Tools 背景代理分頁
- 可見提示：Computer Use 使用 HUMAN HELP 機器人與橘色游標；Browser Use 使用 HUMAN HELP 機器人與藍色頁面游標
- 執行方式：`.\test-coding-tools.ps1 -Mode Interactive`

## 一般模式下暫停

- Computer Use：`Quick`、`Full`、`System` 不會自動移動滑鼠
- Browser Use：`Quick`、`Full`、`System` 不會自動開啟瀏覽器

`Full`/`System` 會列出人工與暫停項目，但不會把它們算成失敗；只有
`FAIL` 或 `ERROR` 會讓命令回傳非零狀態。
