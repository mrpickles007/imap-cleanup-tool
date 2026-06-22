// Tiny native launcher for the Windows build (compiled with csc to launcher.exe,
// with the app icon embedded via /win32icon:app.ico).
//
// Why: the desktop shortcut and the MSIX entry point need a single executable
// that starts the web UI with NO arguments (MSIX cannot pass arguments to the
// main executable) AND that carries our branding (so the MSIX tiles are
// generated from our logo, not Python's). This launcher simply runs the bundled
// Python:  python\python.exe -m imap_cleanup_tool.webapp
//
// It is a console app on purpose: the window shows the local server log and
// closing it stops the server.
using System;
using System.Diagnostics;
using System.IO;

class Launcher
{
    static int Main()
    {
        string baseDir = AppDomain.CurrentDomain.BaseDirectory.TrimEnd('\\');
        string py = Path.Combine(baseDir, "python", "python.exe");
        if (!File.Exists(py))
        {
            Console.Error.WriteLine("Bundled Python not found at: " + py);
            Console.Error.WriteLine("Press Enter to close...");
            Console.ReadLine();
            return 1;
        }
        var psi = new ProcessStartInfo(py, "-m imap_cleanup_tool.webapp");
        psi.UseShellExecute = false;
        psi.WorkingDirectory = baseDir;
        Process p = Process.Start(psi);
        p.WaitForExit();
        return p.ExitCode;
    }
}
