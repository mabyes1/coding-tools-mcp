using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.Text;
using System.Text.RegularExpressions;
using System.Windows.Forms;

internal sealed class ActivityLogViewerForm : Form
{
    private readonly string _logPath;
    private readonly string _pidPath;
    private readonly string _dndPath;
    private readonly RichTextBox _output;
    private readonly CheckBox _detailsToggle;
    private readonly CheckBox _autoScrollToggle;
    private readonly CheckBox _dndToggle;
    private readonly Label _statusLabel;
    private readonly Timer _timer;
    private long _position;
    private string _pendingLine = "";
    private bool _showDetails;

    private readonly Font _normalFont = new Font("Microsoft JhengHei UI", 10.5f, FontStyle.Regular);
    private readonly Font _titleFont = new Font("Microsoft JhengHei UI", 10.5f, FontStyle.Bold);
    private readonly Font _monoFont = new Font("Consolas", 9.7f, FontStyle.Regular);

    private static readonly Color WindowBg = Color.FromArgb(12, 16, 22);
    private static readonly Color PanelBg = Color.FromArgb(18, 24, 33);
    private static readonly Color TextPrimary = Color.FromArgb(232, 237, 244);
    private static readonly Color TextSecondary = Color.FromArgb(151, 163, 181);
    private static readonly Color TextMuted = Color.FromArgb(112, 124, 142);
    private static readonly Color Accent = Color.FromArgb(119, 178, 255);
    private static readonly Color Success = Color.FromArgb(103, 210, 151);
    private static readonly Color Failure = Color.FromArgb(255, 123, 132);
    private static readonly Color Command = Color.FromArgb(194, 205, 221);
    private static readonly Color Divider = Color.FromArgb(40, 51, 65);

    public ActivityLogViewerForm(string logPath, string pidPath, string dndPath)
    {
        _logPath = logPath;
        _pidPath = pidPath;
        _dndPath = dndPath;

        Text = "AI 工作紀錄 · coding-tools";
        StartPosition = FormStartPosition.Manual;
        ClientSize = new Size(960, 610);
        MinimumSize = new Size(700, 420);
        BackColor = WindowBg;
        ShowInTaskbar = true;
        if (IsDndEnabled()) WindowState = FormWindowState.Minimized;

        var area = Screen.PrimaryScreen.WorkingArea;
        Location = new Point(
            Math.Max(area.Left, area.Right - Width - 28),
            Math.Max(area.Top, area.Bottom - Height - 28)
        );

        var header = new Panel
        {
            Dock = DockStyle.Top,
            Height = 104,
            BackColor = PanelBg,
            Padding = new Padding(18, 14, 18, 10),
        };
        Controls.Add(header);

        var title = new Label
        {
            AutoSize = true,
            Text = "AI 工作紀錄",
            Font = new Font("Microsoft JhengHei UI", 15f, FontStyle.Bold),
            ForeColor = TextPrimary,
            Location = new Point(18, 14),
            BackColor = Color.Transparent,
        };
        header.Controls.Add(title);

        var subtitle = new Label
        {
            AutoSize = true,
            Text = "即時顯示我正在做什麼。敏感資訊會自動遮蔽，關掉這個視窗也不會中斷工作。",
            Font = new Font("Microsoft JhengHei UI", 9.5f, FontStyle.Regular),
            ForeColor = TextSecondary,
            Location = new Point(19, 45),
            BackColor = Color.Transparent,
        };
        header.Controls.Add(subtitle);

        _statusLabel = new Label
        {
            AutoSize = true,
            Text = "● 即時",
            Font = new Font("Microsoft JhengHei UI", 9.5f, FontStyle.Bold),
            ForeColor = Success,
            BackColor = Color.Transparent,
            Anchor = AnchorStyles.Top | AnchorStyles.Right,
        };
        header.Controls.Add(_statusLabel);
        header.Resize += delegate
        {
            _statusLabel.Location = new Point(header.ClientSize.Width - _statusLabel.Width - 20, 18);
        };
        _statusLabel.Location = new Point(header.ClientSize.Width - _statusLabel.Width - 20, 18);

        _detailsToggle = new CheckBox
        {
            AutoSize = true,
            Text = "顯示詳細輸出",
            Checked = false,
            Font = new Font("Microsoft JhengHei UI", 9f, FontStyle.Regular),
            ForeColor = TextSecondary,
            BackColor = Color.Transparent,
            Location = new Point(18, 75),
        };
        header.Controls.Add(_detailsToggle);

        _autoScrollToggle = new CheckBox
        {
            AutoSize = true,
            Text = "自動捲到最新",
            Checked = true,
            Font = new Font("Microsoft JhengHei UI", 9f, FontStyle.Regular),
            ForeColor = TextSecondary,
            BackColor = Color.Transparent,
            Location = new Point(146, 75),
        };
        header.Controls.Add(_autoScrollToggle);

        _dndToggle = new CheckBox
        {
            AutoSize = true,
            Text = "免打擾（不要跳出）",
            Checked = IsDndEnabled(),
            Font = new Font("Microsoft JhengHei UI", 9f, FontStyle.Regular),
            ForeColor = TextSecondary,
            BackColor = Color.Transparent,
            Location = new Point(280, 75),
        };
        header.Controls.Add(_dndToggle);

        var clearButton = new Button
        {
            Text = "清除畫面",
            AutoSize = true,
            FlatStyle = FlatStyle.Flat,
            Font = new Font("Microsoft JhengHei UI", 8.8f, FontStyle.Regular),
            ForeColor = TextSecondary,
            BackColor = Color.FromArgb(24, 31, 42),
            Location = new Point(440, 70),
            Height = 27,
            TabStop = false,
        };
        clearButton.FlatAppearance.BorderColor = Divider;
        clearButton.Click += delegate { _output.Clear(); };
        header.Controls.Add(clearButton);

        var body = new Panel
        {
            Dock = DockStyle.Fill,
            BackColor = WindowBg,
            Padding = new Padding(14, 14, 14, 14),
        };
        Controls.Add(body);
        header.BringToFront();

        _output = new RichTextBox
        {
            Dock = DockStyle.Fill,
            ReadOnly = true,
            BorderStyle = BorderStyle.None,
            BackColor = Color.FromArgb(10, 14, 20),
            ForeColor = TextPrimary,
            Font = _normalFont,
            DetectUrls = false,
            HideSelection = false,
            WordWrap = true,
            ScrollBars = RichTextBoxScrollBars.ForcedVertical,
            ShortcutsEnabled = true,
        };
        body.Controls.Add(_output);

        _detailsToggle.CheckedChanged += delegate
        {
            _showDetails = _detailsToggle.Checked;
            ReloadFromTail();
        };
        _dndToggle.CheckedChanged += delegate
        {
            SetDnd(_dndToggle.Checked);
            UpdateDndStatus();
            if (_dndToggle.Checked) WindowState = FormWindowState.Minimized;
        };

        Shown += delegate
        {
            WriteWelcome();
            LoadTail();
            UpdateDndStatus();
        };

        FormClosed += delegate
        {
            try
            {
                if (File.Exists(_pidPath)) File.Delete(_pidPath);
            }
            catch { }
        };

        _timer = new Timer { Interval = 250 };
        _timer.Tick += delegate { ReadNewContent(); };
        _timer.Start();
    }

    private bool IsDndEnabled()
    {
        try { return !String.IsNullOrWhiteSpace(_dndPath) && File.Exists(_dndPath); }
        catch { return false; }
    }

    private void SetDnd(bool enabled)
    {
        try
        {
            if (String.IsNullOrWhiteSpace(_dndPath)) return;
            if (enabled)
            {
                var directory = Path.GetDirectoryName(_dndPath);
                if (!String.IsNullOrWhiteSpace(directory)) Directory.CreateDirectory(directory);
                File.WriteAllText(_dndPath, DateTimeOffset.Now.ToString("o"));
            }
            else if (File.Exists(_dndPath))
            {
                File.Delete(_dndPath);
            }
        }
        catch { }
    }

    private void UpdateDndStatus()
    {
        if (_dndToggle != null && _dndToggle.Checked)
        {
            _statusLabel.Text = "● 免打擾";
            _statusLabel.ForeColor = TextSecondary;
        }
        else
        {
            _statusLabel.Text = "● 即時";
            _statusLabel.ForeColor = Success;
        }
        if (_statusLabel.Parent != null)
            _statusLabel.Location = new Point(_statusLabel.Parent.ClientSize.Width - _statusLabel.Width - 20, 18);
    }

    private void WriteWelcome()
    {
        AppendText("準備好了。接下來的操作會出現在這裡。\n\n", TextMuted, _normalFont);
    }

    private void ReloadFromTail()
    {
        _output.Clear();
        _pendingLine = "";
        _position = 0;
        WriteWelcome();
        LoadTail();
    }

    private void LoadTail()
    {
        try
        {
            if (!File.Exists(_logPath)) return;
            using (var stream = OpenLog())
            {
                const int tailBytes = 128 * 1024;
                _position = Math.Max(0, stream.Length - tailBytes);
                stream.Position = _position;
                AppendBytes(stream);
                _position = stream.Position;
            }
        }
        catch { }
    }

    private void ReadNewContent()
    {
        try
        {
            if (!File.Exists(_logPath)) return;
            using (var stream = OpenLog())
            {
                if (stream.Length < _position)
                {
                    _position = 0;
                    AppendText("紀錄檔已輪替，從新的內容繼續。\n\n", TextMuted, _normalFont);
                }
                if (stream.Length == _position) return;
                stream.Position = _position;
                AppendBytes(stream);
                _position = stream.Position;
            }
        }
        catch { }
    }

    private FileStream OpenLog()
    {
        return new FileStream(
            _logPath,
            FileMode.Open,
            FileAccess.Read,
            FileShare.ReadWrite | FileShare.Delete
        );
    }

    private void AppendBytes(FileStream stream)
    {
        using (var buffer = new MemoryStream())
        {
            var chunk = new byte[8192];
            int read;
            while ((read = stream.Read(chunk, 0, chunk.Length)) > 0)
                buffer.Write(chunk, 0, read);

            var text = Encoding.UTF8.GetString(buffer.ToArray());
            if (String.IsNullOrEmpty(text)) return;

            text = _pendingLine + text;
            var lines = text.Replace("\r\n", "\n").Replace("\r", "\n").Split('\n');
            _pendingLine = lines.Length > 0 ? lines[lines.Length - 1] : "";
            for (int i = 0; i < lines.Length - 1; i++)
                RenderLine(lines[i]);

            if (_autoScrollToggle.Checked)
            {
                _output.SelectionStart = _output.TextLength;
                _output.ScrollToCaret();
            }

            if (_output.TextLength > 260000)
            {
                _output.Select(0, _output.TextLength - 180000);
                _output.SelectedText = "較舊的畫面內容已省略。完整紀錄仍保留在磁碟。\n\n";
            }
        }
    }

    private void RenderLine(string raw)
    {
        if (String.IsNullOrWhiteSpace(raw))
        {
            AppendText("\n", TextMuted, _normalFont);
            return;
        }

        var line = raw.TrimEnd();
        var start = Regex.Match(line, @"^\[(\d{2}:\d{2}:\d{2})\]\s+▶\s+(.+)$");
        if (start.Success)
        {
            var time = start.Groups[1].Value;
            var human = HumanizeEvent(start.Groups[2].Value);
            AppendText(time + "  ", TextMuted, _normalFont);
            AppendText(human + "\n", Accent, _titleFont);
            return;
        }

        var done = Regex.Match(line, @"^\[(\d{2}:\d{2}:\d{2})\]\s+([✓✗])\s+(.+)$");
        if (done.Success)
        {
            var ok = done.Groups[2].Value == "✓";
            var human = HumanizeCompletion(done.Groups[3].Value);
            AppendText("          " + (ok ? "✓ " : "✗ "), ok ? Success : Failure, _titleFont);
            AppendText(human + "\n", ok ? Success : Failure, _normalFont);
            return;
        }

        if (line.StartsWith("> "))
        {
            AppendText("          動作  ", TextMuted, _normalFont);
            AppendText(HumanizeCommand(line.Substring(2).Trim()) + "\n", Command, _monoFont);
            return;
        }

        if (line.StartsWith("  "))
        {
            if (!_showDetails) return;
            AppendText("          " + CleanOutput(line.Trim()) + "\n", TextSecondary, _monoFont);
            return;
        }

        if (line.StartsWith("[activity log rotated]", StringComparison.OrdinalIgnoreCase))
        {
            AppendText("紀錄檔已輪替。\n", TextMuted, _normalFont);
            return;
        }

        if (_showDetails)
            AppendText("          " + CleanOutput(line) + "\n", TextSecondary, _monoFont);
    }

    private static string HumanizeEvent(string raw)
    {
        var parts = raw.Split(new[] { " · " }, StringSplitOptions.None);
        var tool = parts.Length > 0 ? parts[0].Trim() : raw.Trim();
        var detail = parts.Length > 1 ? String.Join(" · ", parts, 1, parts.Length - 1) : "";
        var label = ToolLabel(tool);

        if (tool == "exec_command")
            detail = detail == "active_user" ? "桌面" : detail == "service" ? "背景" : detail;
        else if (tool == "browser_use" || tool == "computer_use")
            detail = ActionLabel(detail);
        else if (tool == "HUMAN HELP")
            detail = HumanHelpReason(detail);

        return String.IsNullOrWhiteSpace(detail) ? label : label + "  ·  " + detail;
    }

    private static string HumanizeCompletion(string raw)
    {
        var parts = raw.Split(new[] { " · " }, StringSplitOptions.None);
        if (parts.Length == 0) return "完成";

        var pieces = new List<string>();
        for (int i = 1; i < parts.Length; i++)
        {
            var item = parts[i].Trim();
            if (item == "active_user" || item == "service") continue;
            if (item == "ok") item = "完成";
            if (item == "failed") item = "失敗";
            if (item == "human_action_required") item = "等待你操作";
            if (item == "human_completed") item = "你已完成";
            item = Regex.Replace(item, @"^exit\s+0$", "完成", RegexOptions.IgnoreCase);
            item = Regex.Replace(item, @"^exit\s+(-?\d+)$", "結束碼 $1", RegexOptions.IgnoreCase);
            item = Regex.Replace(item, @"^(\d+)\s+ms$", delegate(Match match)
            {
                var ms = Int32.Parse(match.Groups[1].Value);
                return ms >= 1000 ? (ms / 1000.0).ToString("0.0") + " 秒" : ms + " ms";
            }, RegexOptions.IgnoreCase);
            item = Regex.Replace(item, @"(\d+) additions", "+$1 行", RegexOptions.IgnoreCase);
            item = Regex.Replace(item, @"(\d+) removals", "-$1 行", RegexOptions.IgnoreCase);
            item = Regex.Replace(item, @"(\d+) matches", "$1 筆結果", RegexOptions.IgnoreCase);
            item = Regex.Replace(item, @"(\d+) items", "$1 個項目", RegexOptions.IgnoreCase);
            pieces.Add(item);
        }
        return pieces.Count == 0 ? "完成" : String.Join("  ·  ", pieces.ToArray());
    }

    private static string ToolLabel(string tool)
    {
        switch (tool)
        {
            case "exec_command": return "執行指令";
            case "apply_patch": return "修改檔案";
            case "read_file": return "讀取檔案";
            case "search_text": return "搜尋程式碼";
            case "list_files": return "瀏覽檔案";
            case "list_dir": return "瀏覽資料夾";
            case "git_status": return "檢查 Git 狀態";
            case "git_diff": return "查看程式變更";
            case "git_log": return "查看 Git 紀錄";
            case "git_show": return "查看 Git 版本";
            case "browser_use": return "操作瀏覽器";
            case "computer_use": return "操作電腦";
            case "server_info": return "檢查 coding-tools 狀態";
            case "human_help_me":
            case "HUMAN HELP": return "需要你協助";
            case "check_exec_environment": return "檢查執行環境";
            case "view_image": return "查看圖片";
            default: return tool.Replace('_', ' ');
        }
    }

    private static string ActionLabel(string action)
    {
        switch ((action ?? "").Trim())
        {
            case "inspect": return "查看畫面";
            case "screenshot": return "截圖";
            case "activate": return "切換視窗";
            case "click": return "點擊";
            case "type_text": return "輸入文字";
            case "press_key": return "按鍵";
            case "scroll": return "捲動畫面";
            case "navigate": return "前往網址";
            case "list_windows": return "列出視窗";
            default: return action;
        }
    }

    private static string HumanHelpReason(string reason)
    {
        switch ((reason ?? "").Trim())
        {
            case "permission_blocked": return "需要系統權限";
            case "gui_required": return "需要你操作畫面";
            case "physical_action": return "需要實體操作";
            case "faster_by_human": return "這一步你做比較快";
            case "need_information": return "需要你提供資訊";
            case "need_decision": return "需要你決定";
            default: return reason;
        }
    }

    private static string HumanizeCommand(string command)
    {
        if (String.IsNullOrWhiteSpace(command)) return "執行命令";

        if (command.IndexOf("update-private-mcp.ps1", StringComparison.OrdinalIgnoreCase) >= 0)
        {
            if (command.IndexOf("ValidateOnly", StringComparison.OrdinalIgnoreCase) >= 0)
                return "驗證 coding-tools MCP 更新";
            return "更新 coding-tools MCP";
        }
        if (command.IndexOf("computer-use-helper.exe", StringComparison.OrdinalIgnoreCase) >= 0)
        {
            if (command.IndexOf("list_windows", StringComparison.OrdinalIgnoreCase) >= 0) return "查看目前開啟的視窗";
            if (command.IndexOf("inspect", StringComparison.OrdinalIgnoreCase) >= 0) return "讀取目前視窗內容";
            return "執行 Computer Use 操作";
        }
        if (command.IndexOf("Get-ScheduledTask", StringComparison.OrdinalIgnoreCase) >= 0)
            return "檢查 Windows 背景工作";
        if (command.IndexOf("Get-CimInstance Win32_Process", StringComparison.OrdinalIgnoreCase) >= 0)
            return "檢查 Windows 執行中的程序";

        var cleaned = Regex.Replace(command, @"^pwsh(?:\.exe)?\s+-NoLogo\s+-NoProfile\s+-NonInteractive\s+-ExecutionPolicy\s+Bypass\s+-Command\s+", "", RegexOptions.IgnoreCase);
        cleaned = Regex.Replace(cleaned, @"\s+", " ").Trim();
        return Shorten(cleaned, 220);
    }

    private static string CleanOutput(string value)
    {
        var cleaned = value.Replace("[REDACTED]", "••••••");
        cleaned = Regex.Replace(cleaned, @"\x1B\[[0-9;]*[A-Za-z]", "");
        return Shorten(cleaned, 360);
    }

    private static string Shorten(string text, int max)
    {
        if (String.IsNullOrEmpty(text) || text.Length <= max) return text;
        return text.Substring(0, Math.Max(0, max - 1)) + "…";
    }

    private void AppendText(string text, Color color, Font font)
    {
        _output.SelectionStart = _output.TextLength;
        _output.SelectionLength = 0;
        _output.SelectionColor = color;
        _output.SelectionFont = font;
        _output.AppendText(text);
        _output.SelectionColor = TextPrimary;
        _output.SelectionFont = _normalFont;
    }
}

internal static class ActivityLogViewerProgram
{
    [STAThread]
    private static int Main(string[] args)
    {
        string logPath = @"C:\ProgramData\WebGPTCodingToolsMCPService\logs\ai-activity.log";
        string pidPath = @"C:\ProgramData\WebGPTCodingToolsMCPService\interactive-requests\activity-log-viewer.pid";
        string dndPath = @"C:\ProgramData\WebGPTCodingToolsMCPService\interactive-requests\activity-log-viewer.dnd";
        for (int i = 0; i + 1 < args.Length; i += 2)
        {
            if (String.Equals(args[i], "--log", StringComparison.OrdinalIgnoreCase)) logPath = args[i + 1];
            else if (String.Equals(args[i], "--pid", StringComparison.OrdinalIgnoreCase)) pidPath = args[i + 1];
            else if (String.Equals(args[i], "--dnd", StringComparison.OrdinalIgnoreCase)) dndPath = args[i + 1];
        }

        try
        {
            var directory = Path.GetDirectoryName(logPath);
            if (!String.IsNullOrWhiteSpace(directory)) Directory.CreateDirectory(directory);
            File.WriteAllText(pidPath, Process.GetCurrentProcess().Id.ToString());

            Application.EnableVisualStyles();
            Application.SetCompatibleTextRenderingDefault(false);
            Application.Run(new ActivityLogViewerForm(logPath, pidPath, dndPath));
            return 0;
        }
        catch
        {
            return 1;
        }
    }
}
