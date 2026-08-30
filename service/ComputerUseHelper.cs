using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Drawing;
using System.Drawing.Drawing2D;
using System.Drawing.Imaging;
using System.IO;
using System.Linq;
using System.Runtime.InteropServices;
using System.Text;
using System.Threading;
using System.Web.Script.Serialization;
using System.Windows;
using System.Windows.Automation;
using System.Windows.Forms;

internal static class ComputerUseHelper
{
    private static readonly JavaScriptSerializer Json = new JavaScriptSerializer { MaxJsonLength = 16 * 1024 * 1024 };

    private const uint MouseEventLeftDown = 0x0002;
    private const uint MouseEventLeftUp = 0x0004;
    private const uint MouseEventRightDown = 0x0008;
    private const uint MouseEventRightUp = 0x0010;
    private const uint PrintWindowRenderFullContent = 0x00000002;

    [DllImport("user32.dll", SetLastError = true)]
    private static extern bool SetCursorPos(int x, int y);

    [DllImport("user32.dll")]
    private static extern void mouse_event(uint flags, uint dx, uint dy, uint data, UIntPtr extraInfo);

    [DllImport("user32.dll", SetLastError = true)]
    private static extern bool PrintWindow(IntPtr hwnd, IntPtr hdcBlt, uint flags);

    private sealed class ElementEntry
    {
        public int Index;
        public AutomationElement Element;
        public Dictionary<string, object> Row;
    }

    private static int Main(string[] args)
    {
        string responseFile = null;
        string errorFile = null;
        try
        {
            string encoded = null;
            for (int i = 0; i + 1 < args.Length; i++)
            {
                if (args[i] == "--request-base64") encoded = args[i + 1];
                else if (args[i] == "--response-file") responseFile = args[i + 1];
                else if (args[i] == "--error-file") errorFile = args[i + 1];
            }
            if (String.IsNullOrWhiteSpace(encoded)) throw new InvalidOperationException("--request-base64 is required.");
            var requestJson = Encoding.UTF8.GetString(Convert.FromBase64String(encoded));
            var request = Json.Deserialize<Dictionary<string, object>>(requestJson);
            var overlayLease = BeginComputerUseOverlay(request);
            Dictionary<string, object> response;
            try
            {
                response = Handle(request);
            }
            finally
            {
                EndComputerUseOverlay(overlayLease);
            }
            var serialized = Json.Serialize(response);
            if (!String.IsNullOrWhiteSpace(responseFile))
            {
                var directory = Path.GetDirectoryName(responseFile);
                if (!String.IsNullOrWhiteSpace(directory)) Directory.CreateDirectory(directory);
                File.WriteAllText(responseFile, serialized, new UTF8Encoding(false));
            }
            else
            {
                Console.OutputEncoding = new UTF8Encoding(false);
                Console.Write(serialized);
            }
            return 0;
        }
        catch (Exception ex)
        {
            var message = ex.GetType().FullName + ": " + ex.Message;
            if (!String.IsNullOrWhiteSpace(errorFile))
            {
                try
                {
                    var directory = Path.GetDirectoryName(errorFile);
                    if (!String.IsNullOrWhiteSpace(directory)) Directory.CreateDirectory(directory);
                    File.WriteAllText(errorFile, message, new UTF8Encoding(false));
                }
                catch { }
            }
            else
            {
                Console.Error.WriteLine(message);
            }
            return 2;
        }
    }

    private static string BeginComputerUseOverlay(Dictionary<string, object> request)
    {
        // Keep the visual safety indicator at the lowest shared execution layer.
        // This guarantees Browser Use / Computer Use still shows the mascot even
        // when a caller has to invoke the helper directly instead of going through
        // the interactive broker (for example while an MCP client has stale schemas).
        try
        {
            var serviceRoot = AppDomain.CurrentDomain.BaseDirectory.TrimEnd(Path.DirectorySeparatorChar, Path.AltDirectorySeparatorChar);
            var queueRoot = Path.Combine(serviceRoot, "interactive-requests");
            var overlayPath = Path.Combine(serviceRoot, "computer-use-overlay.exe");
            var leasesRoot = Path.Combine(queueRoot, "computer-use-overlay-leases");
            var pidPath = Path.Combine(queueRoot, "computer-use-overlay.pid");
            var mascotPath = Path.Combine(serviceRoot, "assets", "human-help-mascot.png");
            var browserOnly = GetBool(request, "browser_only", false);
            var mode = browserOnly ? "browser" : "computer";
            var action = GetString(request, "action", "inspect").Trim().ToLowerInvariant();

            Directory.CreateDirectory(leasesRoot);
            var leasePath = Path.Combine(
                leasesRoot,
                Process.GetCurrentProcess().Id.ToString() + "-" + Guid.NewGuid().ToString("N") + ".lease"
            );
            File.WriteAllText(
                leasePath,
                mode + "|" + action + "|" + DateTimeOffset.Now.ToString("o"),
                new UTF8Encoding(false)
            );
            if (!File.Exists(overlayPath)) return leasePath;

            if (File.Exists(pidPath))
            {
                int overlayPid;
                if (Int32.TryParse(File.ReadAllText(pidPath).Trim(), out overlayPid) && overlayPid > 0)
                {
                    try
                    {
                        var existing = Process.GetProcessById(overlayPid);
                        if (!existing.HasExited) return leasePath;
                    }
                    catch { }
                }
            }

            var startInfo = new ProcessStartInfo
            {
                FileName = overlayPath,
                Arguments = "--leases-dir " + QuoteArgument(leasesRoot)
                    + " --pid " + QuoteArgument(pidPath)
                    + " --mascot " + QuoteArgument(mascotPath),
                WorkingDirectory = serviceRoot,
                UseShellExecute = false,
                CreateNoWindow = true
            };
            Process.Start(startInfo);
            return leasePath;
        }
        catch
        {
            // The overlay is a user-visible indicator, never a reason to fail the
            // underlying bounded UI action.
            return null;
        }
    }

    private static void EndComputerUseOverlay(string leasePath)
    {
        if (String.IsNullOrWhiteSpace(leasePath)) return;
        try { if (File.Exists(leasePath)) File.Delete(leasePath); }
        catch { }
    }

    private static string QuoteArgument(string value)
    {
        return "\"" + (value ?? "").Replace("\"", "\\\"") + "\"";
    }

    private static Dictionary<string, object> Handle(Dictionary<string, object> request)
    {
        try
        {
            var action = GetString(request, "action", "inspect").Trim().ToLowerInvariant();
            bool browserOnly = GetBool(request, "browser_only", false);
            AssertActionSupported(action, browserOnly);
            if (action == "list_windows")
            {
                return Ok(new Dictionary<string, object>
                {
                    { "action", action },
                    { "windows", ListWindows(GetString(request, "process_name", ""), browserOnly) },
                    { "message", "Window discovery completed." }
                });
            }

            var window = ResolveWindow(request, browserOnly);
            AssertAllowedWindow(window);
            long windowId = Convert.ToInt64(window["id"]);

            if (action == "inspect") { }
            else if (action == "screenshot") { }
            else if (action == "activate") ActivateWindow(windowId);
            else if (action == "click")
            {
                ActivateWindow(windowId);
                ClickTarget(request, windowId, false);
                Thread.Sleep(120);
            }
            else if (action == "right_click")
            {
                ActivateWindow(windowId);
                ClickTarget(request, windowId, true);
                Thread.Sleep(120);
            }
            else if (action == "type_text")
            {
                ActivateWindow(windowId);
                var text = GetString(request, "text", "");
                if (String.IsNullOrEmpty(text)) throw new InvalidOperationException("text is required for type_text.");
                AutomationElement element;
                if (request.ContainsKey("element_index") && request["element_index"] != null)
                    element = ResolveElement(request, windowId);
                else
                    element = AutomationElement.FocusedElement;
                if (element == null) throw new InvalidOperationException("No focused editable element was found.");
                SetElementValue(element, text);
                Thread.Sleep(80);
            }
            else if (action == "press_key")
            {
                ActivateWindow(windowId);
                SendKey(GetString(request, "key", ""));
                Thread.Sleep(80);
            }
            else if (action == "scroll")
            {
                ActivateWindow(windowId);
                AutomationElement element;
                if ((request.ContainsKey("element_index") && request["element_index"] != null) ||
                    (request.ContainsKey("x") && request["x"] != null && request.ContainsKey("y") && request["y"] != null))
                    element = ResolveElement(request, windowId);
                else
                    element = GetRoot(windowId);
                ScrollElement(element, GetInt(request, "scroll_y", 0));
                Thread.Sleep(120);
            }
            else if (action == "navigate")
            {
                if (!browserOnly) throw new InvalidOperationException("navigate is available only through browser_use.");
                var url = GetString(request, "text", "");
                if (String.IsNullOrWhiteSpace(url)) throw new InvalidOperationException("A URL is required for navigate.");
                ActivateWindow(windowId);
                var address = FindBrowserAddressElement(windowId);
                SetElementValue(address, url);
                address.SetFocus();
                SendKey("ENTER");
                Thread.Sleep(700);
            }
            else
            {
                throw new InvalidOperationException("Unsupported Computer Use action: " + action);
            }

            bool includeScreenshot = GetBool(request, "include_screenshot", true);
            bool includeText = GetBool(request, "include_text", true);
            if (action == "screenshot") { includeScreenshot = true; includeText = false; }

            var payload = new Dictionary<string, object>
            {
                { "action", action },
                { "window", window },
                { "message", "Computer Use action completed. Re-observe after every state-changing action before reusing coordinates or element indexes." }
            };
            if (includeText) payload["elements"] = GetElementRows(windowId);
            if (includeScreenshot)
            {
                var shot = Capture(windowId);
                payload["screenshot"] = new Dictionary<string, object>
                {
                    { "mime_type", "image/jpeg" }, { "width", shot.Item2 }, { "height", shot.Item3 }
                };
                payload["screenshot_base64"] = Convert.ToBase64String(shot.Item1);
            }
            return Ok(payload);
        }
        catch (Exception ex)
        {
            return new Dictionary<string, object>
            {
                { "ok", false },
                { "error", "COMPUTER_USE_FAILED" },
                { "message", ex.Message },
                { "retryable", true }
            };
        }
    }

    private static void AssertActionSupported(string action, bool browserOnly)
    {
        var contractPath = Path.Combine(AppDomain.CurrentDomain.BaseDirectory, "computer-use-actions.json");
        if (!File.Exists(contractPath))
            throw new InvalidOperationException("Computer Use action contract is missing: " + contractPath);
        var contract = Json.Deserialize<Dictionary<string, string[]>>(File.ReadAllText(contractPath, Encoding.UTF8));
        var key = browserOnly ? "browser_use" : "computer_use";
        string[] actions;
        if (contract == null || !contract.TryGetValue(key, out actions) || actions == null)
            throw new InvalidOperationException("Computer Use action contract is invalid for " + key + ".");
        if (!actions.Contains(action, StringComparer.OrdinalIgnoreCase))
            throw new InvalidOperationException("Unsupported Computer Use action: " + action);
    }

    private static Dictionary<string, object> Ok(Dictionary<string, object> payload)
    {
        payload["ok"] = true;
        payload["retryable"] = false;
        return payload;
    }

    private static List<Dictionary<string, object>> ListWindows(string processFilter, bool browserOnly)
    {
        var result = new List<Dictionary<string, object>>();
        var seen = new HashSet<long>();
        var root = AutomationElement.RootElement;
        var children = root.FindAll(TreeScope.Children, Condition.TrueCondition);
        foreach (AutomationElement element in children)
        {
            try
            {
                long hwnd = element.Current.NativeWindowHandle;
                int pid = element.Current.ProcessId;
                if (hwnd == 0 || pid <= 0) continue;
                var process = Process.GetProcessById(pid);
                string name = process.ProcessName;
                if (!ProcessMatches(name, processFilter, browserOnly)) continue;
                seen.Add(hwnd);
                result.Add(WindowRow(hwnd, element.Current.Name, pid, name, element.Current.ClassName));
            }
            catch { }
        }
        foreach (var process in Process.GetProcesses())
        {
            try
            {
                long hwnd = process.MainWindowHandle.ToInt64();
                if (hwnd == 0 || seen.Contains(hwnd)) continue;
                string name = process.ProcessName;
                if (!ProcessMatches(name, processFilter, browserOnly)) continue;
                seen.Add(hwnd);
                result.Add(WindowRow(hwnd, process.MainWindowTitle, process.Id, name, ""));
            }
            catch { }
        }
        return result.OrderBy(r => Convert.ToInt64(r["id"])).ToList();
    }

    private static bool ProcessMatches(string name, string filter, bool browserOnly)
    {
        if (browserOnly && !name.Equals("chrome", StringComparison.OrdinalIgnoreCase) && !name.Equals("msedge", StringComparison.OrdinalIgnoreCase)) return false;
        if (!String.IsNullOrWhiteSpace(filter) && !name.Equals(filter, StringComparison.OrdinalIgnoreCase)) return false;
        return true;
    }

    private static Dictionary<string, object> WindowRow(long id, string title, int pid, string process, string className)
    {
        return new Dictionary<string, object>
        {
            { "id", id }, { "title", title ?? "" }, { "process_id", pid }, { "process_name", process ?? "" }, { "class_name", className ?? "" }
        };
    }

    private static Dictionary<string, object> ResolveWindow(Dictionary<string, object> request, bool browserOnly)
    {
        var windows = ListWindows(GetString(request, "process_name", ""), browserOnly);
        if (request.ContainsKey("window_id") && request["window_id"] != null)
        {
            long id = Convert.ToInt64(request["window_id"]);
            windows = windows.Where(w => Convert.ToInt64(w["id"]) == id).ToList();
        }
        var title = GetString(request, "title", "");
        if (!String.IsNullOrWhiteSpace(title))
            windows = windows.Where(w => Convert.ToString(w["title"]).IndexOf(title, StringComparison.OrdinalIgnoreCase) >= 0).ToList();
        if (windows.Count == 1) return windows[0];
        if (windows.Count == 0) throw new InvalidOperationException("No target window matched the request.");
        throw new InvalidOperationException("Target window is ambiguous; choose one returned window id first.");
    }

    private static void AssertAllowedWindow(Dictionary<string, object> window)
    {
        var blocked = new HashSet<string>(StringComparer.OrdinalIgnoreCase)
        {
            "powershell", "pwsh", "cmd", "conhost", "windowsterminal", "wt", "securityhealthsystray", "sechealthui", "chatgpt", "codex"
        };
        string name = Convert.ToString(window["process_name"]);
        if (blocked.Contains(name)) throw new InvalidOperationException("Computer Use refuses to automate this protected application: " + name);
    }

    private static AutomationElement GetRoot(long windowId)
    {
        var root = AutomationElement.FromHandle(new IntPtr(windowId));
        if (root == null) throw new InvalidOperationException("Could not bind UI Automation to the target window.");
        return root;
    }

    private static List<ElementEntry> GetEntries(long windowId)
    {
        var root = GetRoot(windowId);
        Rect rootRect = root.Current.BoundingRectangle;
        var all = root.FindAll(TreeScope.Descendants, Condition.TrueCondition);
        var entries = new List<ElementEntry>();
        int index = 0;
        foreach (AutomationElement element in all)
        {
            if (entries.Count >= 350) break;
            try
            {
                string name = element.Current.Name ?? "";
                string automationId = element.Current.AutomationId ?? "";
                if (String.IsNullOrWhiteSpace(name) && String.IsNullOrWhiteSpace(automationId)) continue;
                Rect r = element.Current.BoundingRectangle;
                var row = new Dictionary<string, object>
                {
                    { "index", index },
                    { "type", element.Current.ControlType.ProgrammaticName.Replace("ControlType.", "") },
                    { "name", name }, { "automation_id", automationId },
                    { "enabled", element.Current.IsEnabled }, { "offscreen", element.Current.IsOffscreen }
                };
                if (!r.IsEmpty && r.Width > 1 && r.Height > 1 && !rootRect.IsEmpty)
                {
                    row["x"] = (int)Math.Round(r.X - rootRect.X);
                    row["y"] = (int)Math.Round(r.Y - rootRect.Y);
                    row["width"] = (int)Math.Round(r.Width);
                    row["height"] = (int)Math.Round(r.Height);
                }
                entries.Add(new ElementEntry { Index = index, Element = element, Row = row });
                index++;
            }
            catch { }
        }
        return entries;
    }

    private static List<Dictionary<string, object>> GetElementRows(long windowId)
    {
        return GetEntries(windowId).Select(e => e.Row).ToList();
    }

    private static AutomationElement ResolveElement(Dictionary<string, object> request, long windowId)
    {
        if (request.ContainsKey("element_index") && request["element_index"] != null)
        {
            int index = Convert.ToInt32(request["element_index"]);
            var match = GetEntries(windowId).Where(e => e.Index == index).ToList();
            if (match.Count != 1) throw new InvalidOperationException("The requested element index is stale; inspect again.");
            return match[0].Element;
        }
        if (!request.ContainsKey("x") || request["x"] == null || !request.ContainsKey("y") || request["y"] == null)
            throw new InvalidOperationException("element_index or x/y is required for this action.");
        var root = GetRoot(windowId);
        Rect r = root.Current.BoundingRectangle;
        if (r.IsEmpty) throw new InvalidOperationException("Target window has no usable screen bounds.");
        var point = new System.Windows.Point(r.X + Convert.ToDouble(request["x"]), r.Y + Convert.ToDouble(request["y"]));
        var element = AutomationElement.FromPoint(point);
        if (element == null) throw new InvalidOperationException("No accessible element exists at the requested point.");
        return element;
    }

    private static void ActivateWindow(long windowId)
    {
        var root = GetRoot(windowId);
        object raw;
        if (root.TryGetCurrentPattern(WindowPattern.Pattern, out raw))
        {
            try
            {
                var pattern = (WindowPattern)raw;
                if (pattern.Current.WindowVisualState == WindowVisualState.Minimized)
                {
                    pattern.SetWindowVisualState(WindowVisualState.Normal);
                    Thread.Sleep(120);
                }
            }
            catch { }
        }
        try { root.SetFocus(); } catch { }
        Thread.Sleep(80);
    }

    private static bool TryInvokeElement(AutomationElement element)
    {
        object raw;
        if (element.TryGetCurrentPattern(InvokePattern.Pattern, out raw)) { ((InvokePattern)raw).Invoke(); return true; }
        if (element.TryGetCurrentPattern(SelectionItemPattern.Pattern, out raw)) { ((SelectionItemPattern)raw).Select(); return true; }
        if (element.TryGetCurrentPattern(TogglePattern.Pattern, out raw)) { ((TogglePattern)raw).Toggle(); return true; }
        if (element.TryGetCurrentPattern(ExpandCollapsePattern.Pattern, out raw))
        {
            var pattern = (ExpandCollapsePattern)raw;
            if (pattern.Current.ExpandCollapseState == ExpandCollapseState.Expanded) pattern.Collapse(); else pattern.Expand();
            return true;
        }
        return false;
    }

    private static void ClickTarget(Dictionary<string, object> request, long windowId, bool rightClick)
    {
        if (request.ContainsKey("element_index") && request["element_index"] != null)
        {
            var element = ResolveElement(request, windowId);
            if (!rightClick && TryInvokeElement(element)) return;

            Rect elementRect = element.Current.BoundingRectangle;
            if (elementRect.IsEmpty || elementRect.Width <= 1 || elementRect.Height <= 1)
                throw new InvalidOperationException("The target element has no usable screen bounds for a physical click.");
            MouseClickScreenPoint(
                (int)Math.Round(elementRect.X + elementRect.Width / 2.0),
                (int)Math.Round(elementRect.Y + elementRect.Height / 2.0),
                rightClick
            );
            return;
        }

        if (!request.ContainsKey("x") || request["x"] == null || !request.ContainsKey("y") || request["y"] == null)
            throw new InvalidOperationException("element_index or x/y is required for this action.");

        var root = GetRoot(windowId);
        Rect rootRect = root.Current.BoundingRectangle;
        if (rootRect.IsEmpty || rootRect.Width <= 1 || rootRect.Height <= 1)
            throw new InvalidOperationException("Target window has no usable screen bounds.");

        double x = Convert.ToDouble(request["x"]);
        double y = Convert.ToDouble(request["y"]);
        if (x < 0 || y < 0 || x >= rootRect.Width || y >= rootRect.Height)
            throw new InvalidOperationException("The requested click coordinates fall outside the target window.");

        MouseClickScreenPoint(
            (int)Math.Round(rootRect.X + x),
            (int)Math.Round(rootRect.Y + y),
            rightClick
        );
    }

    private static void MouseClickScreenPoint(int x, int y, bool rightClick)
    {
        if (!SetCursorPos(x, y))
            throw new InvalidOperationException("Windows refused to move the pointer to the requested click target.");
        Thread.Sleep(30);
        if (rightClick)
        {
            mouse_event(MouseEventRightDown, 0, 0, 0, UIntPtr.Zero);
            mouse_event(MouseEventRightUp, 0, 0, 0, UIntPtr.Zero);
        }
        else
        {
            mouse_event(MouseEventLeftDown, 0, 0, 0, UIntPtr.Zero);
            mouse_event(MouseEventLeftUp, 0, 0, 0, UIntPtr.Zero);
        }
    }

    private static void SetElementValue(AutomationElement element, string value)
    {
        object raw;
        if (element.TryGetCurrentPattern(ValuePattern.Pattern, out raw))
        {
            var pattern = (ValuePattern)raw;
            if (pattern.Current.IsReadOnly) throw new InvalidOperationException("The target value control is read-only.");
            pattern.SetValue(value);
            return;
        }

        // Some native and WinForms edit controls are writable but expose only
        // keyboard focus through UI Automation.  Keep type_text useful on
        // those controls without interpreting braces, plus signs, or other
        // SendKeys metacharacters as commands.
        try { element.SetFocus(); }
        catch { throw new InvalidOperationException("The target element is not writable and could not receive keyboard focus."); }
        SendKeys.SendWait("^a");
        SendKeys.SendWait(EscapeSendKeysText(value));
    }

    private static string EscapeSendKeysText(string value)
    {
        var escaped = new StringBuilder();
        foreach (char character in value ?? "")
        {
            if (character == '\r') continue;
            if (character == '\n') escaped.Append("{ENTER}");
            else if (character == '\t') escaped.Append("{TAB}");
            else if (character == '{') escaped.Append("{{}");
            else if (character == '}') escaped.Append("{}}");
            else if (character == '+') escaped.Append("{+}");
            else if (character == '^') escaped.Append("{^}");
            else if (character == '%') escaped.Append("{%}");
            else if (character == '~') escaped.Append("{~}");
            else if (character == '(') escaped.Append("{(}");
            else if (character == ')') escaped.Append("{)}");
            else if (character == '[') escaped.Append("{[}");
            else if (character == ']') escaped.Append("{]}");
            else escaped.Append(character);
        }
        return escaped.ToString();
    }

    private static void ScrollElement(AutomationElement element, int delta)
    {
        var current = element;
        var walker = TreeWalker.ControlViewWalker;
        for (int i = 0; i < 12 && current != null; i++)
        {
            object raw;
            if (current.TryGetCurrentPattern(ScrollPattern.Pattern, out raw))
            {
                var pattern = (ScrollPattern)raw;
                ScrollAmount amount;
                if (Math.Abs(delta) >= 500) amount = delta > 0 ? ScrollAmount.LargeIncrement : ScrollAmount.LargeDecrement;
                else amount = delta > 0 ? ScrollAmount.SmallIncrement : ScrollAmount.SmallDecrement;
                pattern.ScrollVertical(amount);
                return;
            }
            try { current = walker.GetParent(current); } catch { current = null; }
        }
        throw new InvalidOperationException("No scrollable UI Automation ancestor was found at the requested target.");
    }

    private static AutomationElement FindBrowserAddressElement(long windowId)
    {
        var candidates = GetEntries(windowId).Where(e =>
            Convert.ToString(e.Row["type"]) == "Edit" &&
            !(bool)e.Row["offscreen"] &&
            e.Row.ContainsKey("width") && Convert.ToInt32(e.Row["width"]) >= 300 &&
            e.Row.ContainsKey("y") && Convert.ToInt32(e.Row["y"]) <= 180)
            .OrderBy(e => Convert.ToInt32(e.Row["y"]))
            .ThenByDescending(e => Convert.ToInt32(e.Row["width"]))
            .ToList();
        if (candidates.Count < 1) throw new InvalidOperationException("Could not identify the browser address bar through UI Automation.");
        return candidates[0].Element;
    }

    private static Tuple<byte[], int, int> Capture(long windowId)
    {
        var root = GetRoot(windowId);
        Rect rect = root.Current.BoundingRectangle;
        if (rect.IsEmpty || rect.Width <= 1 || rect.Height <= 1) throw new InvalidOperationException("Could not read target window bounds.");
        int width = (int)Math.Round(rect.Width);
        int height = (int)Math.Round(rect.Height);
        using (var bitmap = new Bitmap(width, height, PixelFormat.Format32bppArgb))
        using (var graphics = Graphics.FromImage(bitmap))
        {
            bool captured = false;
            IntPtr hdc = IntPtr.Zero;
            try
            {
                hdc = graphics.GetHdc();
                captured = PrintWindow(new IntPtr(windowId), hdc, PrintWindowRenderFullContent);
            }
            catch { captured = false; }
            finally
            {
                if (hdc != IntPtr.Zero) graphics.ReleaseHdc(hdc);
            }
            if (!captured)
                graphics.CopyFromScreen((int)rect.X, (int)rect.Y, 0, 0, new System.Drawing.Size(width, height));
            Bitmap output = bitmap;
            Bitmap scaled = null;
            double scale = Math.Min(1.0, Math.Min(1600.0 / width, 1000.0 / height));
            if (scale < 1.0)
            {
                int outWidth = Math.Max(1, (int)Math.Round(width * scale));
                int outHeight = Math.Max(1, (int)Math.Round(height * scale));
                scaled = new Bitmap(outWidth, outHeight, PixelFormat.Format24bppRgb);
                using (var sg = Graphics.FromImage(scaled))
                {
                    sg.InterpolationMode = InterpolationMode.HighQualityBicubic;
                    sg.DrawImage(bitmap, 0, 0, outWidth, outHeight);
                }
                output = scaled;
            }
            try
            {
                using (var stream = new MemoryStream())
                {
                    var jpeg = ImageCodecInfo.GetImageEncoders().First(c => c.MimeType == "image/jpeg");
                    using (var parameters = new EncoderParameters(1))
                    {
                        parameters.Param[0] = new EncoderParameter(System.Drawing.Imaging.Encoder.Quality, 82L);
                        output.Save(stream, jpeg, parameters);
                    }
                    return Tuple.Create(stream.ToArray(), output.Width, output.Height);
                }
            }
            finally { if (scaled != null) scaled.Dispose(); }
        }
    }

    private static void SendKey(string key)
    {
        SendKeys.SendWait(ToSendKeys(key));
    }

    private static string ToSendKeys(string key)
    {
        if (String.IsNullOrWhiteSpace(key)) throw new InvalidOperationException("key is required.");
        string mods = "";
        string basis = null;
        foreach (var raw in key.Split('+'))
        {
            var part = raw.Trim().ToUpperInvariant();
            if (part == "CTRL" || part == "CONTROL" || part == "CONTROL_L") mods += "^";
            else if (part == "ALT" || part == "ALT_L") mods += "%";
            else if (part == "SHIFT" || part == "SHIFT_L") mods += "+";
            else if (part.Length > 0) basis = part;
        }
        if (basis == null) throw new InvalidOperationException("Unsupported key chord: " + key);
        var map = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase)
        {
            { "ENTER", "{ENTER}" }, { "RETURN", "{ENTER}" }, { "TAB", "{TAB}" }, { "ESC", "{ESC}" }, { "ESCAPE", "{ESC}" },
            { "SPACE", " " }, { "BACKSPACE", "{BACKSPACE}" }, { "DELETE", "{DELETE}" },
            { "LEFT", "{LEFT}" }, { "RIGHT", "{RIGHT}" }, { "UP", "{UP}" }, { "DOWN", "{DOWN}" },
            { "HOME", "{HOME}" }, { "END", "{END}" }, { "PAGEUP", "{PGUP}" }, { "PAGEDOWN", "{PGDN}" },
            { "F1", "{F1}" }, { "F2", "{F2}" }, { "F3", "{F3}" }, { "F4", "{F4}" }, { "F5", "{F5}" }, { "F6", "{F6}" },
            { "F7", "{F7}" }, { "F8", "{F8}" }, { "F9", "{F9}" }, { "F10", "{F10}" }, { "F11", "{F11}" }, { "F12", "{F12}" }
        };
        string mapped;
        if (!map.TryGetValue(basis, out mapped))
        {
            if (basis.Length != 1) throw new InvalidOperationException("Unsupported key: " + basis);
            mapped = basis.ToLowerInvariant();
        }
        return mods + mapped;
    }

    private static string GetString(Dictionary<string, object> request, string key, string fallback)
    {
        object value;
        if (!request.TryGetValue(key, out value) || value == null) return fallback;
        return Convert.ToString(value);
    }

    private static bool GetBool(Dictionary<string, object> request, string key, bool fallback)
    {
        object value;
        if (!request.TryGetValue(key, out value) || value == null) return fallback;
        return Convert.ToBoolean(value);
    }

    private static int GetInt(Dictionary<string, object> request, string key, int fallback)
    {
        object value;
        if (!request.TryGetValue(key, out value) || value == null) return fallback;
        return Convert.ToInt32(value);
    }
}
