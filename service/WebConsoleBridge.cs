using System;
using System.Collections;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Net;
using System.Net.Sockets;
using System.ServiceProcess;
using System.Text;
using System.Text.RegularExpressions;
using System.Threading;
using System.Web.Script.Serialization;
using System.Windows.Forms;

internal sealed class WebConsoleBridge
{
    private readonly string _logPath;
    private readonly string _queueRoot;
    private readonly string _permissionModePath;
    private readonly string _repoRoot;
    private readonly string _serviceRoot;
    private readonly string _pidPath;
    private readonly int _port;
    private readonly JavaScriptSerializer _json = new JavaScriptSerializer { MaxJsonLength = 1024 * 1024 };
    private readonly object _fileLock = new object();
    private readonly object _workspacePickerLock = new object();
    private volatile bool _running = true;

    public WebConsoleBridge(string logPath, string queueRoot, string permissionModePath, string repoRoot, string pidPath, int port)
    {
        _logPath = logPath;
        _queueRoot = queueRoot;
        _permissionModePath = permissionModePath;
        _repoRoot = repoRoot;
        _serviceRoot = Path.GetDirectoryName(permissionModePath) ?? AppDomain.CurrentDomain.BaseDirectory;
        _pidPath = pidPath;
        _port = port;
    }

    public void Run()
    {
        Directory.CreateDirectory(_queueRoot);
        WriteTextAtomic(_pidPath, System.Diagnostics.Process.GetCurrentProcess().Id.ToString());
        var listener = new TcpListener(IPAddress.Loopback, _port);
        listener.Start(16);
        try
        {
            while (_running)
            {
                var client = listener.AcceptTcpClient();
                ThreadPool.QueueUserWorkItem(delegate { HandleClient(client); });
            }
        }
        finally
        {
            listener.Stop();
            try { File.Delete(_pidPath); } catch { }
        }
    }

    private void HandleClient(TcpClient client)
    {
        using (client)
        {
            client.ReceiveTimeout = 5000;
            client.SendTimeout = 5000;
            try
            {
                using (var stream = client.GetStream())
                {
                    var request = ReadRequest(stream);
                    if (request == null) return;
                    HandleRequest(stream, request);
                }
            }
            catch { }
        }
    }

    private void HandleRequest(NetworkStream stream, HttpRequest request)
    {
        var origin = Header(request, "Origin");
        if (request.Method == "OPTIONS")
        {
            if (!IsAllowedOrigin(origin))
            {
                WriteJson(stream, 403, new Dictionary<string, object> { { "ok", false }, { "error", "origin_not_allowed" } }, "");
                return;
            }
            WriteResponse(stream, 204, "application/json; charset=utf-8", new byte[0], origin);
            return;
        }

        var consoleClient = String.Equals(Header(request, "X-Coding-Tools-Console"), "1", StringComparison.Ordinal);
        var extensionClient = String.Equals(Header(request, "X-Coding-Tools-Extension"), "1", StringComparison.Ordinal);
        // Chromium extension service-worker fetches may omit Origin entirely,
        // even when host_permissions allow the loopback request. Keep normal
        // browser callers origin-checked, and only admit the originless case
        // when the dedicated extension transport marker is also present.
        var originlessExtensionClient = String.IsNullOrWhiteSpace(origin) && extensionClient;
        if ((!IsAllowedOrigin(origin) && !originlessExtensionClient) || !consoleClient)
        {
            WriteJson(stream, 403, new Dictionary<string, object> { { "ok", false }, { "error", "console_client_required" } }, "");
            return;
        }

        // Any authenticated console request proves that the browser-side
        // console is alive. Do not restrict liveness to state/health because
        // preference and HUMAN_HELP response requests are valid activity too.
        TouchHeartbeat();

        if (request.Method == "GET" && request.Path == "/v1/health")
        {
            WriteJson(stream, 200, new Dictionary<string, object> { { "ok", true }, { "service", "coding-tools-web-console" } }, origin);
            return;
        }
        if (request.Method == "GET" && request.Path == "/v1/state")
        {
            WriteJson(stream, 200, BuildState(), origin);
            return;
        }
        if (request.Method == "POST" && request.Path == "/v1/workspace/pick")
        {
            var picked = PickWorkspaceFolder();
            WriteJson(stream, Convert.ToBoolean(picked["ok"]) ? 200 : 409, picked, origin);
            return;
        }
        if (request.Method == "POST" && request.Path == "/v1/workspace/add")
        {
            var body = DeserializeBody(request);
            var selectedPath = body.ContainsKey("path") ? Convert.ToString(body["path"]).Trim() : "";
            if (String.IsNullOrWhiteSpace(selectedPath) || !Directory.Exists(selectedPath))
            {
                WriteJson(stream, 400, new Dictionary<string, object>
                {
                    { "ok", false },
                    { "error", "workspace_path_invalid" }
                }, origin);
                return;
            }
            try { selectedPath = new DirectoryInfo(selectedPath).FullName; }
            catch
            {
                WriteJson(stream, 400, new Dictionary<string, object>
                {
                    { "ok", false },
                    { "error", "workspace_path_invalid" }
                }, origin);
                return;
            }
            var launched = LaunchAdminAction("add_workspace", selectedPath);
            if (launched == null)
            {
                WriteJson(stream, 400, new Dictionary<string, object> { { "ok", false }, { "error", "invalid_workspace_action" } }, origin);
                return;
            }
            launched["workspace_path"] = selectedPath;
            WriteJson(stream, Convert.ToBoolean(launched["ok"]) ? 200 : 409, launched, origin);
            return;
        }
        if (request.Method == "POST" && request.Path == "/v1/workspace/switch")
        {
            var body = DeserializeBody(request);
            var selector = body.ContainsKey("workspace") ? Convert.ToString(body["workspace"]).Trim() : "";
            if (String.IsNullOrWhiteSpace(selector))
            {
                WriteJson(stream, 400, new Dictionary<string, object> { { "ok", false }, { "error", "workspace_required" } }, origin);
                return;
            }
            var health = InvokeLoopbackJson("GET", "http://127.0.0.1:8766/healthz");
            if (!Convert.ToBoolean(health["ok"]))
            {
                WriteJson(stream, 409, new Dictionary<string, object>
                {
                    { "ok", false },
                    { "error", "mcp_health_unavailable" },
                    { "detail", health.ContainsKey("error") ? health["error"] : "MCP health endpoint is unavailable." }
                }, origin);
                return;
            }
            var workspaceConfig = ReadWorkspaceConfiguration();
            if (!Convert.ToBoolean(workspaceConfig["ok"]))
            {
                WriteJson(stream, 409, workspaceConfig, origin);
                return;
            }
            var selectedEntry = FindWorkspaceEntry(workspaceConfig.ContainsKey("workspaces") ? workspaceConfig["workspaces"] : null, selector);
            if (selectedEntry == null)
            {
                WriteJson(stream, 400, new Dictionary<string, object>
                {
                    { "ok", false },
                    { "error", "workspace_not_allowed" },
                    { "workspace", selector }
                }, origin);
                return;
            }
            var selectedName = Convert.ToString(selectedEntry["name"]);
            var selectedPath = Convert.ToString(selectedEntry["path"]);
            var currentPath = health.ContainsKey("workspace") ? Convert.ToString(health["workspace"]) : "";
            if (String.Equals(currentPath, selectedPath, StringComparison.OrdinalIgnoreCase))
            {
                WriteJson(stream, 200, new Dictionary<string, object>
                {
                    { "ok", true },
                    { "changed", false },
                    { "workspace", selectedName },
                    { "workspace_path", selectedPath }
                }, origin);
                return;
            }
            var launched = LaunchAdminAction("switch_workspace", selectedName);
            if (launched == null)
            {
                WriteJson(stream, 400, new Dictionary<string, object> { { "ok", false }, { "error", "invalid_workspace_action" } }, origin);
                return;
            }
            launched["workspace"] = selectedName;
            launched["workspace_path"] = selectedPath;
            WriteJson(stream, Convert.ToBoolean(launched["ok"]) ? 200 : 409, launched, origin);
            return;
        }
        if (request.Method == "GET" && request.Path == "/v1/assets/human-help-mascot")
        {
            var mascotPath = Path.Combine(_serviceRoot, "assets", "human-help-mascot-256.png");
            if (!File.Exists(mascotPath))
            {
                WriteJson(stream, 404, new Dictionary<string, object> { { "ok", false }, { "error", "mascot_not_found" } }, origin);
                return;
            }
            WriteResponse(stream, 200, "image/png", File.ReadAllBytes(mascotPath), origin);
            return;
        }
        if (request.Method == "GET" && request.Path == "/v1/browser/next")
        {
            var pending = TakeNextBrowserRequest(3500);
            if (pending == null)
            {
                WriteResponse(stream, 204, "application/json; charset=utf-8", new byte[0], origin);
                return;
            }
            WriteJson(stream, 200, pending, origin);
            return;
        }
        if (request.Method == "POST" && request.Path == "/v1/browser/respond")
        {
            var body = DeserializeBody(request);
            var requestId = body.ContainsKey("request_id") ? Convert.ToString(body["request_id"]) : "";
            var response = body.ContainsKey("response") ? body["response"] as Dictionary<string, object> : null;
            if (!Regex.IsMatch(requestId, "^[A-Za-z0-9_-]{8,80}$") || response == null)
            {
                WriteJson(stream, 400, new Dictionary<string, object> { { "ok", false }, { "error", "invalid_browser_response" } }, origin);
                return;
            }
            var processingPath = Path.Combine(_queueRoot, requestId + ".browser-extension.processing");
            if (!File.Exists(processingPath))
            {
                WriteJson(stream, 409, new Dictionary<string, object> { { "ok", false }, { "error", "browser_request_not_pending" } }, origin);
                return;
            }
            response["request_id"] = requestId;
            WriteTextAtomic(Path.Combine(_queueRoot, requestId + ".browser-extension.response"), _json.Serialize(response));
            try { File.Delete(processingPath); } catch { }
            WriteTextAtomic(Path.Combine(_queueRoot, "browser-extension.heartbeat"), DateTimeOffset.UtcNow.ToUnixTimeMilliseconds().ToString());
            WriteJson(stream, 200, new Dictionary<string, object> { { "ok", true } }, origin);
            return;
        }
        if (request.Method == "POST" && request.Path == "/v1/human-help/seen")
        {
            var body = DeserializeBody(request);
            var requestId = body.ContainsKey("request_id") ? Convert.ToString(body["request_id"]) : "";
            if (!Regex.IsMatch(requestId, "^[A-Za-z0-9_-]{8,80}$"))
            {
                WriteJson(stream, 400, new Dictionary<string, object> { { "ok", false }, { "error", "invalid_request_id" } }, origin);
                return;
            }
            var pendingPath = Path.Combine(_queueRoot, requestId + ".web-human-help.json");
            if (!File.Exists(pendingPath))
            {
                WriteJson(stream, 409, new Dictionary<string, object> { { "ok", false }, { "error", "request_not_pending" } }, origin);
                return;
            }
            WriteTextAtomic(Path.Combine(_queueRoot, requestId + ".web-human-help.seen"), DateTimeOffset.UtcNow.ToUnixTimeSeconds().ToString());
            WriteJson(stream, 200, new Dictionary<string, object> { { "ok", true } }, origin);
            return;
        }
        if (request.Method == "POST" && request.Path == "/v1/human-help/activity")
        {
            var body = DeserializeBody(request);
            var requestId = body.ContainsKey("request_id") ? Convert.ToString(body["request_id"]) : "";
            if (!Regex.IsMatch(requestId, "^[A-Za-z0-9_-]{8,80}$"))
            {
                WriteJson(stream, 400, new Dictionary<string, object> { { "ok", false }, { "error", "invalid_request_id" } }, origin);
                return;
            }
            var pendingPath = Path.Combine(_queueRoot, requestId + ".web-human-help.json");
            if (!File.Exists(pendingPath))
            {
                WriteJson(stream, 409, new Dictionary<string, object> { { "ok", false }, { "error", "request_not_pending" } }, origin);
                return;
            }
            WriteTextAtomic(Path.Combine(_queueRoot, requestId + ".web-human-help.activity"), DateTimeOffset.UtcNow.ToUnixTimeMilliseconds().ToString());
            WriteJson(stream, 200, new Dictionary<string, object> { { "ok", true } }, origin);
            return;
        }
        if (request.Method == "POST" && request.Path == "/v1/preferences")
        {
            var body = DeserializeBody(request);
            SetDnd(body.ContainsKey("dnd") && Convert.ToBoolean(body["dnd"]));
            WriteJson(stream, 200, new Dictionary<string, object> { { "ok", true }, { "dnd", IsDnd() } }, origin);
            return;
        }
        if (request.Method == "POST" && request.Path == "/v1/system/action")
        {
            var body = DeserializeBody(request);
            var action = body.ContainsKey("action") ? Convert.ToString(body["action"]).Trim().ToLowerInvariant() : "";
            if (action == "health")
            {
                var result = InvokeLoopbackJson("GET", "http://127.0.0.1:8766/healthz");
                WriteJson(stream, Convert.ToBoolean(result["ok"]) ? 200 : 409, result, origin);
                return;
            }
            if (action == "prune")
            {
                var result = InvokeLoopbackJson("POST", "http://127.0.0.1:8766/prune");
                WriteJson(stream, Convert.ToBoolean(result["ok"]) ? 200 : 409, result, origin);
                return;
            }
            var launched = LaunchAdminAction(action, null);
            if (launched == null)
            {
                WriteJson(stream, 400, new Dictionary<string, object> { { "ok", false }, { "error", "invalid_system_action" } }, origin);
                return;
            }
            WriteJson(stream, Convert.ToBoolean(launched["ok"]) ? 200 : 409, launched, origin);
            return;
        }
        if (request.Method == "POST" && request.Path == "/v1/activity/clear")
        {
            lock (_fileLock)
            {
                var directory = Path.GetDirectoryName(_logPath);
                if (!String.IsNullOrEmpty(directory)) Directory.CreateDirectory(directory);
                File.WriteAllText(_logPath, "", new UTF8Encoding(false));
            }
            WriteJson(stream, 200, new Dictionary<string, object> { { "ok", true } }, origin);
            return;
        }
        if (request.Method == "POST" && request.Path == "/v1/human-help/respond")
        {
            var body = DeserializeBody(request);
            var requestId = body.ContainsKey("request_id") ? Convert.ToString(body["request_id"]) : "";
            if (!Regex.IsMatch(requestId, "^[A-Za-z0-9_-]{8,80}$"))
            {
                WriteJson(stream, 400, new Dictionary<string, object> { { "ok", false }, { "error", "invalid_request_id" } }, origin);
                return;
            }
            var pendingPath = Path.Combine(_queueRoot, requestId + ".web-human-help.json");
            if (!File.Exists(pendingPath))
            {
                WriteJson(stream, 409, new Dictionary<string, object> { { "ok", false }, { "error", "request_not_pending" } }, origin);
                return;
            }
            var response = new Dictionary<string, object>
            {
                { "request_id", requestId },
                { "outcome", body.ContainsKey("outcome") ? Convert.ToString(body["outcome"]) : "completed" },
                { "answer", body.ContainsKey("answer") ? Convert.ToString(body["answer"]) : "" },
                { "responded_at", DateTimeOffset.UtcNow.ToUnixTimeSeconds() }
            };
            WriteTextAtomic(Path.Combine(_queueRoot, requestId + ".web-human-help.response"), _json.Serialize(response));
            WriteJson(stream, 200, new Dictionary<string, object> { { "ok", true } }, origin);
            return;
        }

        WriteJson(stream, 404, new Dictionary<string, object> { { "ok", false }, { "error", "not_found" } }, origin);
    }

    private Dictionary<string, object> TakeNextBrowserRequest(int waitMilliseconds)
    {
        var deadline = DateTime.UtcNow.AddMilliseconds(waitMilliseconds);
        do
        {
            try
            {
                string selected = null;
                string processing = null;
                lock (_fileLock)
                {
                    var files = Directory.GetFiles(_queueRoot, "*.browser-extension.pending");
                    Array.Sort(files, delegate(string left, string right) { return File.GetCreationTimeUtc(left).CompareTo(File.GetCreationTimeUtc(right)); });
                    if (files.Length > 0)
                    {
                        selected = files[0];
                        var name = Path.GetFileName(selected);
                        processing = Path.Combine(_queueRoot, name.Substring(0, name.Length - ".pending".Length) + ".processing");
                        File.Move(selected, processing);
                    }
                }
                if (!String.IsNullOrWhiteSpace(processing))
                {
                    var requestId = Path.GetFileName(processing).Split('.')[0];
                    if (!Regex.IsMatch(requestId, "^[A-Za-z0-9_-]{8,80}$"))
                    {
                        try { File.Delete(processing); } catch { }
                        continue;
                    }
                    var payload = _json.DeserializeObject(File.ReadAllText(processing, Encoding.UTF8)) as Dictionary<string, object>;
                    if (payload == null)
                    {
                        try { File.Delete(processing); } catch { }
                        continue;
                    }
                    WriteTextAtomic(Path.Combine(_queueRoot, "browser-extension.heartbeat"), DateTimeOffset.UtcNow.ToUnixTimeMilliseconds().ToString());
                    return new Dictionary<string, object>
                    {
                        { "ok", true },
                        { "request_id", requestId },
                        { "request", payload }
                    };
                }
            }
            catch { }
            Thread.Sleep(100);
        } while (DateTime.UtcNow < deadline);
        return null;
    }

    private Dictionary<string, object> BuildState()
    {
        var mcpHealth = InvokeLoopbackJson("GET", "http://127.0.0.1:8766/healthz");
        var workspaceConfig = ReadWorkspaceConfiguration();
        var state = new Dictionary<string, object>
        {
            { "ok", true },
            { "connected_at", DateTimeOffset.UtcNow.ToUnixTimeMilliseconds() },
            { "activity", ReadTail(_logPath, 192 * 1024) },
            { "dnd", IsDnd() },
            { "permission_mode", ReadSmallText(_permissionModePath, "safe") },
            { "services", ReadServiceStates() },
            { "human_help", ReadPendingHumanHelp() }
        };
        if (Convert.ToBoolean(mcpHealth["ok"]))
        {
            state["workspace"] = mcpHealth.ContainsKey("workspace") ? mcpHealth["workspace"] : "";
            state["workspace_allowlist"] = Convert.ToBoolean(workspaceConfig["ok"])
                ? workspaceConfig["workspaces"]
                : (mcpHealth.ContainsKey("workspace_allowlist") ? mcpHealth["workspace_allowlist"] : new object[0]);
        }
        else
        {
            state["workspace"] = "";
            state["workspace_allowlist"] = new object[0];
            state["workspace_error"] = mcpHealth.ContainsKey("error") ? mcpHealth["error"] : "MCP health endpoint is unavailable.";
        }
        return state;
    }

    private Dictionary<string, object> ReadWorkspaceConfiguration()
    {
        var configPath = Path.Combine(_serviceRoot, "data", "workspace-config.json");
        try
        {
            if (!File.Exists(configPath))
            {
                return new Dictionary<string, object> { { "ok", false }, { "error", "workspace_config_missing" } };
            }
            var config = _json.DeserializeObject(File.ReadAllText(configPath, Encoding.UTF8)) as Dictionary<string, object>;
            if (config == null || !config.ContainsKey("workspaces") || !(config["workspaces"] is IEnumerable))
            {
                return new Dictionary<string, object> { { "ok", false }, { "error", "workspace_config_invalid" } };
            }
            config["ok"] = true;
            return config;
        }
        catch (Exception exc)
        {
            return new Dictionary<string, object> { { "ok", false }, { "error", exc.Message } };
        }
    }

    private Dictionary<string, object> PickWorkspaceFolder()
    {
        if (!Monitor.TryEnter(_workspacePickerLock))
        {
            return new Dictionary<string, object>
            {
                { "ok", false },
                { "error", "workspace_picker_busy" }
            };
        }
        try
        {
            var selectedPath = "";
            var cancelled = true;
            Exception failure = null;
            var dialogThread = new Thread(new ThreadStart(delegate
            {
                try
                {
                    using (var owner = new Form())
                    using (var dialog = new FolderBrowserDialog())
                    {
                        owner.Text = "Coding Tools 工作區";
                        owner.ShowInTaskbar = false;
                        owner.StartPosition = FormStartPosition.CenterScreen;
                        owner.FormBorderStyle = FormBorderStyle.FixedToolWindow;
                        owner.Width = 1;
                        owner.Height = 1;
                        owner.Opacity = 0;
                        owner.TopMost = true;
                        owner.Show();
                        owner.Activate();
                        owner.BringToFront();
                        dialog.Description = "選擇要加入 Coding Tools 的工作區資料夾";
                        dialog.ShowNewFolderButton = false;
                        dialog.RootFolder = Environment.SpecialFolder.MyComputer;
                        var health = InvokeLoopbackJson("GET", "http://127.0.0.1:8766/healthz");
                        if (Convert.ToBoolean(health["ok"]) && health.ContainsKey("workspace"))
                        {
                            var current = Convert.ToString(health["workspace"]);
                            if (Directory.Exists(current)) dialog.SelectedPath = current;
                        }
                        if (dialog.ShowDialog(owner) == DialogResult.OK && !String.IsNullOrWhiteSpace(dialog.SelectedPath))
                        {
                            selectedPath = dialog.SelectedPath;
                            cancelled = false;
                        }
                    }
                }
                catch (Exception exc) { failure = exc; }
            }));
            dialogThread.SetApartmentState(ApartmentState.STA);
            dialogThread.Start();
            dialogThread.Join();
            if (failure != null)
            {
                return new Dictionary<string, object>
                {
                    { "ok", false },
                    { "error", failure.Message }
                };
            }
            return new Dictionary<string, object>
            {
                { "ok", true },
                { "cancelled", cancelled },
                { "path", cancelled ? "" : new DirectoryInfo(selectedPath).FullName }
            };
        }
        finally { Monitor.Exit(_workspacePickerLock); }
    }

    private static IDictionary FindWorkspaceEntry(object rawAllowlist, string selector)
    {
        var entries = rawAllowlist as IEnumerable;
        if (entries == null) return null;
        IDictionary pathMatch = null;
        foreach (var rawEntry in entries)
        {
            var entry = rawEntry as IDictionary;
            if (entry == null) continue;
            var name = entry.Contains("name") ? Convert.ToString(entry["name"]) : "";
            var path = entry.Contains("path") ? Convert.ToString(entry["path"]) : "";
            if (String.Equals(name, selector, StringComparison.OrdinalIgnoreCase)) return entry;
            if (String.Equals(path, selector, StringComparison.OrdinalIgnoreCase)) pathMatch = entry;
        }
        return pathMatch;
    }

    private Dictionary<string, object> ReadServiceStates()
    {
        return new Dictionary<string, object>
        {
            { "mcp", ReadServiceState("WebGPTCodingToolsMCP") },
            { "secure_tunnel", ReadServiceState("OpenAITunnelClient") },
            { "legacy_tunnel", ReadServiceState("WebGPTCloudflareTunnel") }
        };
    }

    private static Dictionary<string, object> ReadServiceState(string name)
    {
        try
        {
            using (var service = new ServiceController(name))
            {
                return new Dictionary<string, object>
                {
                    { "name", name },
                    { "installed", true },
                    { "status", service.Status.ToString().ToLowerInvariant() }
                };
            }
        }
        catch
        {
            return new Dictionary<string, object>
            {
                { "name", name },
                { "installed", false },
                { "status", "missing" }
            };
        }
    }

    private Dictionary<string, object> InvokeLoopbackJson(string method, string url)
    {
        try
        {
            var request = (HttpWebRequest)WebRequest.Create(url);
            request.Method = method;
            request.Timeout = 3000;
            request.ReadWriteTimeout = 3000;
            if (String.Equals(method, "POST", StringComparison.OrdinalIgnoreCase)) request.ContentLength = 0;
            using (var response = (HttpWebResponse)request.GetResponse())
            using (var reader = new StreamReader(response.GetResponseStream(), Encoding.UTF8))
            {
                var text = reader.ReadToEnd();
                var payload = _json.DeserializeObject(text) as Dictionary<string, object>;
                if (payload == null) payload = new Dictionary<string, object> { { "raw", text } };
                payload["ok"] = true;
                return payload;
            }
        }
        catch (Exception exc)
        {
            return new Dictionary<string, object>
            {
                { "ok", false },
                { "error", exc.Message }
            };
        }
    }

    private Dictionary<string, object> LaunchAdminAction(string action, string workspace)
    {
        var actionMap = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase)
        {
            { "start_all", "StartAll" },
            { "stop_all", "StopAll" },
            { "restart_all", "RestartAll" },
            { "restart_tunnel", "RestartTunnel" },
            { "update", "Update" },
            { "rollback", "Rollback" },
            { "safe", "Safe" },
            { "trusted", "Trusted" },
            { "yolo", "Yolo" },
            { "switch_workspace", "SwitchWorkspace" },
            { "add_workspace", "AddWorkspace" }
        };
        string mapped;
        if (!actionMap.TryGetValue(action, out mapped)) return null;

        var script = Path.Combine(_serviceRoot, "manage-web-console-system.ps1");
        if (!File.Exists(script))
        {
            return new Dictionary<string, object> { { "ok", false }, { "error", "web_console_admin_helper_missing" } };
        }
        try
        {
            var windows = Environment.GetFolderPath(Environment.SpecialFolder.Windows);
            var powershell = Path.Combine(windows, @"System32\WindowsPowerShell\v1.0\powershell.exe");
            var arguments = "-NoLogo -NoProfile -ExecutionPolicy Bypass -File " + QuoteArgument(script)
                + " -Action " + QuoteArgument(mapped)
                + " -RepoRoot " + QuoteArgument(_repoRoot);
            if (String.Equals(mapped, "SwitchWorkspace", StringComparison.OrdinalIgnoreCase)
                || String.Equals(mapped, "AddWorkspace", StringComparison.OrdinalIgnoreCase))
            {
                if (String.IsNullOrWhiteSpace(workspace))
                {
                    return new Dictionary<string, object> { { "ok", false }, { "error", "workspace_required" } };
                }
                arguments += " -Workspace " + QuoteArgument(workspace);
            }
            Process.Start(new ProcessStartInfo
            {
                FileName = powershell,
                Arguments = arguments,
                WorkingDirectory = _serviceRoot,
                UseShellExecute = true,
                Verb = "runas",
                WindowStyle = ProcessWindowStyle.Normal
            });
            return new Dictionary<string, object>
            {
                { "ok", true },
                { "accepted", true },
                { "requires_uac", true },
                { "action", action }
            };
        }
        catch (Exception exc)
        {
            return new Dictionary<string, object>
            {
                { "ok", false },
                { "error", exc.Message },
                { "action", action }
            };
        }
    }

    private static string QuoteArgument(string value)
    {
        return "\"" + (value ?? "").Replace("\"", "") + "\"";
    }

    private object ReadPendingHumanHelp()
    {
        try
        {
            var files = Directory.GetFiles(_queueRoot, "*.web-human-help.json");
            if (files.Length == 0) return null;
            Array.Sort(files, delegate(string left, string right) { return File.GetCreationTimeUtc(left).CompareTo(File.GetCreationTimeUtc(right)); });
            return _json.DeserializeObject(File.ReadAllText(files[0], Encoding.UTF8));
        }
        catch { return null; }
    }

    private void TouchHeartbeat()
    {
        try { WriteTextAtomic(Path.Combine(_queueRoot, "web-console.heartbeat"), DateTimeOffset.UtcNow.ToUnixTimeSeconds().ToString()); }
        catch { }
    }

    private bool IsDnd()
    {
        try { return File.Exists(Path.Combine(_queueRoot, "activity-log-viewer.dnd")); }
        catch { return false; }
    }

    private void SetDnd(bool enabled)
    {
        var path = Path.Combine(_queueRoot, "activity-log-viewer.dnd");
        lock (_fileLock)
        {
            if (enabled) WriteTextAtomic(path, DateTimeOffset.Now.ToString("o"));
            else try { File.Delete(path); } catch { }
        }
    }

    private static string ReadSmallText(string path, string fallback)
    {
        try { return File.Exists(path) ? File.ReadAllText(path).Trim() : fallback; }
        catch { return fallback; }
    }

    private static string ReadTail(string path, int maxBytes)
    {
        try
        {
            if (!File.Exists(path)) return "";
            using (var stream = new FileStream(path, FileMode.Open, FileAccess.Read, FileShare.ReadWrite | FileShare.Delete))
            {
                var start = Math.Max(0, stream.Length - maxBytes);
                stream.Position = start;
                var bytes = new byte[stream.Length - start];
                var count = stream.Read(bytes, 0, bytes.Length);
                var text = Encoding.UTF8.GetString(bytes, 0, count);
                if (start > 0)
                {
                    var newline = text.IndexOf('\n');
                    if (newline >= 0) text = text.Substring(newline + 1);
                }
                return text;
            }
        }
        catch { return ""; }
    }

    private Dictionary<string, object> DeserializeBody(HttpRequest request)
    {
        if (String.IsNullOrWhiteSpace(request.Body)) return new Dictionary<string, object>();
        var value = _json.DeserializeObject(request.Body) as Dictionary<string, object>;
        return value ?? new Dictionary<string, object>();
    }

    private static bool IsAllowedOrigin(string origin)
    {
        if (String.IsNullOrWhiteSpace(origin)) return false;
        return origin.StartsWith("chrome-extension://", StringComparison.OrdinalIgnoreCase)
            || origin.StartsWith("edge-extension://", StringComparison.OrdinalIgnoreCase)
            || origin.StartsWith("moz-extension://", StringComparison.OrdinalIgnoreCase)
            || String.Equals(origin, "https://chatgpt.com", StringComparison.OrdinalIgnoreCase)
            || String.Equals(origin, "https://chat.openai.com", StringComparison.OrdinalIgnoreCase);
    }

    private static string Header(HttpRequest request, string name)
    {
        string value;
        return request.Headers.TryGetValue(name, out value) ? value : "";
    }

    private static HttpRequest ReadRequest(NetworkStream stream)
    {
        var headerBytes = new List<byte>();
        var state = 0;
        while (headerBytes.Count < 64 * 1024)
        {
            var current = stream.ReadByte();
            if (current < 0) return null;
            headerBytes.Add((byte)current);
            state = current == (state == 0 || state == 2 ? 13 : 10) ? state + 1 : (current == 13 ? 1 : 0);
            if (state == 4) break;
        }
        if (state != 4) return null;
        var headerText = Encoding.ASCII.GetString(headerBytes.ToArray());
        var lines = headerText.Split(new[] { "\r\n" }, StringSplitOptions.None);
        var first = lines[0].Split(' ');
        if (first.Length < 2) return null;
        var request = new HttpRequest { Method = first[0].ToUpperInvariant(), Path = first[1].Split('?')[0] };
        for (var i = 1; i < lines.Length; i++)
        {
            var colon = lines[i].IndexOf(':');
            if (colon <= 0) continue;
            request.Headers[lines[i].Substring(0, colon).Trim()] = lines[i].Substring(colon + 1).Trim();
        }
        var contentLength = 0;
        int.TryParse(Header(request, "Content-Length"), out contentLength);
        if (contentLength > 1024 * 1024) return null;
        if (contentLength > 0)
        {
            var body = new byte[contentLength];
            var offset = 0;
            while (offset < body.Length)
            {
                var read = stream.Read(body, offset, body.Length - offset);
                if (read <= 0) break;
                offset += read;
            }
            request.Body = Encoding.UTF8.GetString(body, 0, offset);
        }
        return request;
    }

    private static void WriteJson(NetworkStream stream, int status, object payload, string origin)
    {
        var serializer = new JavaScriptSerializer { MaxJsonLength = 1024 * 1024 };
        WriteResponse(stream, status, "application/json; charset=utf-8", Encoding.UTF8.GetBytes(serializer.Serialize(payload)), origin);
    }

    private static void WriteResponse(NetworkStream stream, int status, string contentType, byte[] body, string origin)
    {
        var statusText = status == 200 ? "OK" : status == 204 ? "No Content" : status == 400 ? "Bad Request" : status == 403 ? "Forbidden" : status == 409 ? "Conflict" : "Not Found";
        var headers = new StringBuilder();
        headers.Append("HTTP/1.1 ").Append(status).Append(' ').Append(statusText).Append("\r\n");
        headers.Append("Content-Type: ").Append(contentType).Append("\r\n");
        headers.Append("Content-Length: ").Append(body.Length).Append("\r\n");
        headers.Append("Cache-Control: no-store\r\n");
        headers.Append("Access-Control-Allow-Methods: GET, POST, OPTIONS\r\n");
        headers.Append("Access-Control-Allow-Headers: Content-Type, X-Coding-Tools-Console, X-Coding-Tools-Extension\r\n");
        headers.Append("Access-Control-Allow-Private-Network: true\r\n");
        if (!String.IsNullOrWhiteSpace(origin)) headers.Append("Access-Control-Allow-Origin: ").Append(origin).Append("\r\nVary: Origin\r\n");
        headers.Append("Connection: close\r\n\r\n");
        var header = Encoding.ASCII.GetBytes(headers.ToString());
        stream.Write(header, 0, header.Length);
        if (body.Length > 0) stream.Write(body, 0, body.Length);
    }

    private static void WriteTextAtomic(string path, string text)
    {
        var directory = Path.GetDirectoryName(path);
        if (!String.IsNullOrWhiteSpace(directory)) Directory.CreateDirectory(directory);
        var temporary = path + "." + Guid.NewGuid().ToString("N") + ".tmp";
        File.WriteAllText(temporary, text, new UTF8Encoding(false));
        if (File.Exists(path)) File.Replace(temporary, path, null);
        else File.Move(temporary, path);
    }

    private sealed class HttpRequest
    {
        public string Method = "";
        public string Path = "";
        public string Body = "";
        public readonly Dictionary<string, string> Headers = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);
    }

    public static int Main(string[] args)
    {
        try
        {
            var values = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);
            for (var i = 0; i + 1 < args.Length; i += 2) values[args[i]] = args[i + 1];
            var queue = values.ContainsKey("--queue") ? values["--queue"] : @"C:\ProgramData\WebGPTCodingToolsMCPService\interactive-requests";
            var log = values.ContainsKey("--log") ? values["--log"] : @"C:\ProgramData\WebGPTCodingToolsMCPService\logs\ai-activity.log";
            var mode = values.ContainsKey("--permission-mode") ? values["--permission-mode"] : @"C:\ProgramData\WebGPTCodingToolsMCPService\permission-mode.txt";
            var repo = values.ContainsKey("--repo") ? values["--repo"] : @"D:\coding-tools-mcp\coding-tools-mcp";
            var pid = values.ContainsKey("--pid") ? values["--pid"] : Path.Combine(queue, "web-console.pid");
            var port = values.ContainsKey("--port") ? Int32.Parse(values["--port"]) : 8768;
            new WebConsoleBridge(log, queue, mode, repo, pid, port).Run();
            return 0;
        }
        catch (Exception exc)
        {
            try { File.AppendAllText(@"C:\ProgramData\WebGPTCodingToolsMCPService\interactive-requests\web-console.log", DateTimeOffset.Now.ToString("o") + " " + exc + Environment.NewLine); } catch { }
            return 1;
        }
    }
}
