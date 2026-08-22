using System;
using System.Diagnostics;
using System.Drawing;
using System.Drawing.Drawing2D;
using System.IO;
using System.Windows.Forms;

internal sealed class ComputerUseOverlayForm : Form
{
    private const int WsExNoActivate = 0x08000000;
    private const int WsExToolWindow = 0x00000080;
    private const int WsExTransparent = 0x00000020;
    // Browser/Computer Use is implemented as a sequence of short helper calls.
    // Keep the indicator alive briefly between calls so one logical AI-control
    // flow does not look like a series of flashing toasts.
    private const int IdleGraceMilliseconds = 10000;
    private readonly string _leasesRoot;
    private readonly string _pidPath;
    private readonly Timer _timer;
    private readonly Label _title;
    private readonly Label _subtitle;
    private DateTime _lastLeaseUtc;
    private bool _fadingOut;
    private int _pulse;
    private string _mode = "computer";
    private string _action = "inspect";

    public ComputerUseOverlayForm(string leasesRoot, string pidPath, string mascotPath)
    {
        _leasesRoot = leasesRoot;
        _pidPath = pidPath;
        // The helper that launches us can finish very quickly. Starting the grace
        // window here also covers the race where the first lease disappears just
        // before the overlay process gets its first timer tick.
        _lastLeaseUtc = DateTime.UtcNow;
        FormBorderStyle = FormBorderStyle.None;
        Text = "Coding Tools Computer Use";
        ShowInTaskbar = false;
        TopMost = true;
        StartPosition = FormStartPosition.Manual;
        ClientSize = new Size(360, 126);
        BackColor = Color.FromArgb(14, 20, 29);
        ForeColor = Color.FromArgb(238, 243, 249);
        Opacity = 0.0;

        var area = Screen.PrimaryScreen.WorkingArea;
        Location = new Point(area.Right - Width - 22, area.Top + 22);

        _title = new Label
        {
            AutoSize = true,
            Text = "AI 正在操作電腦",
            Font = new Font("Microsoft JhengHei UI", 14.5f, FontStyle.Bold),
            ForeColor = Color.FromArgb(242, 246, 252),
            Location = new Point(118, 30),
            BackColor = Color.Transparent
        };
        Controls.Add(_title);

        _subtitle = new Label
        {
            AutoSize = true,
            Text = "Computer Use  •",
            Font = new Font("Microsoft JhengHei UI", 9.5f, FontStyle.Regular),
            ForeColor = Color.FromArgb(159, 176, 198),
            Location = new Point(120, 67),
            BackColor = Color.Transparent
        };
        Controls.Add(_subtitle);

        if (File.Exists(mascotPath))
        {
            try
            {
                var picture = new PictureBox
                {
                    Image = Image.FromFile(mascotPath),
                    SizeMode = PictureBoxSizeMode.Zoom,
                    Location = new Point(18, 13),
                    Size = new Size(90, 96),
                    BackColor = Color.Transparent
                };
                Controls.Add(picture);
            }
            catch { }
        }

        _timer = new Timer { Interval = 50 };
        _timer.Tick += OnTimer;
        Shown += delegate
        {
            RefreshLeases();
            _timer.Start();
        };
        FormClosed += delegate
        {
            try
            {
                if (File.Exists(_pidPath)) File.Delete(_pidPath);
            }
            catch { }
        };
    }

    protected override bool ShowWithoutActivation { get { return true; } }

    protected override CreateParams CreateParams
    {
        get
        {
            var cp = base.CreateParams;
            cp.ExStyle |= WsExNoActivate | WsExToolWindow | WsExTransparent;
            return cp;
        }
    }

    protected override void OnPaint(PaintEventArgs e)
    {
        e.Graphics.SmoothingMode = SmoothingMode.AntiAlias;
        using (var path = RoundedRect(new Rectangle(0, 0, ClientSize.Width - 1, ClientSize.Height - 1), 18))
        using (var fill = new SolidBrush(Color.FromArgb(236, 14, 20, 29)))
        using (var border = new Pen(Color.FromArgb(65, 121, 151, 188), 1f))
        {
            e.Graphics.FillPath(fill, path);
            e.Graphics.DrawPath(border, path);
        }
        base.OnPaint(e);
    }

    protected override void OnResize(EventArgs e)
    {
        base.OnResize(e);
        using (var path = RoundedRect(new Rectangle(0, 0, Width, Height), 18))
        {
            Region = new Region(path);
        }
    }

    private static GraphicsPath RoundedRect(Rectangle rect, int radius)
    {
        var path = new GraphicsPath();
        int d = radius * 2;
        path.AddArc(rect.Left, rect.Top, d, d, 180, 90);
        path.AddArc(rect.Right - d, rect.Top, d, d, 270, 90);
        path.AddArc(rect.Right - d, rect.Bottom - d, d, d, 0, 90);
        path.AddArc(rect.Left, rect.Bottom - d, d, d, 90, 90);
        path.CloseFigure();
        return path;
    }

    private bool RefreshLeases()
    {
        try
        {
            if (!Directory.Exists(_leasesRoot)) return false;
            var now = DateTime.UtcNow;
            FileInfo newest = null;
            foreach (var path in Directory.GetFiles(_leasesRoot, "*.lease"))
            {
                try
                {
                    var info = new FileInfo(path);
                    if (now - info.LastWriteTimeUtc > TimeSpan.FromMinutes(2))
                    {
                        try { info.Delete(); } catch { }
                        continue;
                    }
                    if (newest == null || info.LastWriteTimeUtc > newest.LastWriteTimeUtc) newest = info;
                }
                catch { }
            }
            if (newest != null)
            {
                // This is an activity lease, not a file-age timer. While a lease
                // is still present, refresh the last-active time on every poll so
                // long-running UI actions remain visibly "active" until they end.
                _lastLeaseUtc = now;
                var raw = File.ReadAllText(newest.FullName).Trim();
                var parts = raw.Split('|');
                if (parts.Length > 0 && !String.IsNullOrWhiteSpace(parts[0])) _mode = parts[0].Trim().ToLowerInvariant();
                if (parts.Length > 1 && !String.IsNullOrWhiteSpace(parts[1])) _action = parts[1].Trim().ToLowerInvariant();
                return true;
            }
        }
        catch { }
        return false;
    }

    private static string ActionLabel(string action)
    {
        switch (action)
        {
            case "inspect": return "查看畫面";
            case "screenshot": return "截圖";
            case "activate": return "切換視窗";
            case "click": return "點擊";
            case "right_click": return "右鍵";
            case "type_text": return "輸入文字";
            case "press_key": return "按鍵";
            case "scroll": return "捲動畫面";
            case "navigate": return "前往網址";
            case "list_windows": return "查看視窗";
            default: return "操作中";
        }
    }

    private void OnTimer(object sender, EventArgs e)
    {
        var hasActiveLease = RefreshLeases();
        _pulse = (_pulse + 1) % 24;
        var browser = String.Equals(_mode, "browser", StringComparison.OrdinalIgnoreCase);
        _title.Text = browser ? "AI 正在操作瀏覽器" : "AI 正在操作電腦";
        _subtitle.Text = (browser ? "Browser Use" : "Computer Use") + " · " + ActionLabel(_action) + "  " + (_pulse < 12 ? "●" : "•");

        if (!_fadingOut && Opacity < 0.96)
            Opacity = Math.Min(0.96, Opacity + 0.12);

        _fadingOut = !hasActiveLease
            && (DateTime.UtcNow - _lastLeaseUtc).TotalMilliseconds > IdleGraceMilliseconds;

        if (_fadingOut)
        {
            Opacity = Math.Max(0.0, Opacity - 0.10);
            if (Opacity <= 0.01) Close();
        }
    }
}

internal static class Program
{
    [STAThread]
    private static int Main(string[] args)
    {
        string leasesRoot = null;
        string pid = null;
        string mascot = null;
        for (int i = 0; i + 1 < args.Length; i += 2)
        {
            if (args[i] == "--leases-dir") leasesRoot = args[i + 1];
            else if (args[i] == "--pid") pid = args[i + 1];
            else if (args[i] == "--mascot") mascot = args[i + 1];
        }
        if (string.IsNullOrWhiteSpace(leasesRoot) || string.IsNullOrWhiteSpace(pid)) return 2;

        try
        {
            File.WriteAllText(pid, Process.GetCurrentProcess().Id.ToString());
            Application.EnableVisualStyles();
            Application.SetCompatibleTextRenderingDefault(false);
            Application.Run(new ComputerUseOverlayForm(leasesRoot, pid, mascot ?? ""));
            return 0;
        }
        catch
        {
            return 1;
        }
    }
}
