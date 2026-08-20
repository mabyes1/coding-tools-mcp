using System;
using System.IO;
using System.Management.Automation;

internal static class InteractiveBrokerLauncher
{
    [STAThread]
    private static int Main(string[] args)
    {
        var serviceRoot = @"C:\ProgramData\WebGPTCodingToolsMCPService";
        var broker = Path.Combine(serviceRoot, "interactive-broker.ps1");

        try
        {
            using (var powershell = PowerShell.Create())
            {
                if (args != null && Array.Exists(args, item => String.Equals(item, "--self-test", StringComparison.OrdinalIgnoreCase)))
                {
                    powershell.AddScript("$PSVersionTable.PSVersion.ToString() | Out-Null");
                    powershell.Invoke();
                    return powershell.HadErrors ? 5 : 0;
                }

                if (!File.Exists(broker)) return 2;
                Directory.SetCurrentDirectory(serviceRoot);
                powershell.AddCommand(broker);
                powershell.Invoke();
                return powershell.HadErrors ? 3 : 0;
            }
        }
        catch
        {
            return 4;
        }
    }
}
