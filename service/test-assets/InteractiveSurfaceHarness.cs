using System;
using System.Drawing;
using System.Windows.Forms;

internal sealed class InteractiveSurfaceHarness : Form
{
    private readonly Label status;
    private readonly TextBox input;

    private InteractiveSurfaceHarness()
    {
        Text = "Coding Tools UI Action Harness";
        Name = "CodingToolsUiActionHarness";
        StartPosition = FormStartPosition.CenterScreen;
        ClientSize = new Size(720, 520);
        TopMost = true;

        status = new Label {
            Name = "StatusLabel", Text = "STATUS: READY", AutoSize = true,
            Location = new Point(24, 22), Font = new Font("Segoe UI", 13, FontStyle.Bold)
        };
        Controls.Add(status);

        input = new TextBox {
            Name = "TestInput", AccessibleName = "Test input", Location = new Point(24, 70), Width = 430
        };
        input.TextChanged += delegate { status.Text = "STATUS: TEXT=" + input.Text; };
        input.KeyDown += delegate(object sender, KeyEventArgs e) {
            if (e.KeyCode == Keys.F2) status.Text = "STATUS: KEY=F2";
        };
        Controls.Add(input);

        var button = new Button {
            Name = "ClickButton", AccessibleName = "Click test button", Text = "Click test button",
            Location = new Point(480, 67), Size = new Size(180, 32)
        };
        button.Click += delegate { status.Text = "STATUS: CLICKED"; };
        Controls.Add(button);

        var contextPanel = new Panel {
            Name = "RightClickPanel", AccessibleName = "Right click test area",
            Location = new Point(24, 125), Size = new Size(636, 70), BackColor = Color.AliceBlue
        };
        contextPanel.Controls.Add(new Label {
            Text = "Right click test area", AutoSize = true, Location = new Point(16, 23)
        });
        var contextMenu = new ContextMenuStrip();
        contextMenu.Items.Add("Close test menu");
        contextMenu.Opening += delegate { status.Text = "STATUS: RIGHT_CLICKED"; };
        contextPanel.ContextMenuStrip = contextMenu;
        Controls.Add(contextPanel);

        var list = new ListBox {
            Name = "ScrollList", AccessibleName = "Scrollable test list",
            Location = new Point(24, 220), Size = new Size(636, 245)
        };
        for (var i = 1; i <= 80; i++) list.Items.Add("Scrollable row " + i.ToString("00"));
        Controls.Add(list);
    }

    [STAThread]
    private static void Main()
    {
        Application.EnableVisualStyles();
        Application.SetCompatibleTextRenderingDefault(false);
        Application.Run(new InteractiveSurfaceHarness());
    }
}
