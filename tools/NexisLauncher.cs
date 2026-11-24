using System;
using System.Diagnostics;
using System.IO;
using System.Reflection;
using System.Windows.Forms;

[assembly: AssemblyTitle("Nexis Order")]
[assembly: AssemblyProduct("Nexis Order")]
[assembly: AssemblyDescription("Launcher for the Nexis desktop app")]
[assembly: AssemblyVersion("0.1.0.0")]
[assembly: AssemblyFileVersion("0.1.0.0")]

internal static class Program
{
    [STAThread]
    private static void Main()
    {
        string dir = AppDomain.CurrentDomain.BaseDirectory;
        string python = LocatePython(dir);
        if (python == null)
        {
            MessageBox.Show(
                "Nexis cannot find its Python environment.\n\n" +
                "Open Command Prompt in this folder:\n\n" + dir + "\n\n" +
                "and run:\n\n" +
                "py -m venv .venv\n" +
                ".venv\\Scripts\\python -m pip install -e \".[app,voice,whisper]\"",
                "Nexis setup required",
                MessageBoxButtons.OK,
                MessageBoxIcon.Information);
            return;
        }

        try
        {
            ProcessStartInfo start = new ProcessStartInfo(python);
            start.Arguments = "-m nexis.app";
            start.WorkingDirectory = dir;
            start.UseShellExecute = false;
            start.CreateNoWindow = true;
            Process.Start(start);
        }
        catch (Exception error)
        {
            MessageBox.Show(
                "Nexis could not start:\n\n" + error.Message,
                "Nexis",
                MessageBoxButtons.OK,
                MessageBoxIcon.Error);
        }
    }

    private static string LocatePython(string dir)
    {
        string venv = Path.Combine(dir, ".venv", "Scripts", "python.exe");
        if (File.Exists(venv))
        {
            return venv;
        }

        string bundled = Path.Combine(dir, "python", "python.exe");
        if (File.Exists(bundled))
        {
            return bundled;
        }

        string path = Environment.GetEnvironmentVariable("PATH") ?? string.Empty;
        string[] parts = path.Split(Path.PathSeparator);
        for (int i = 0; i < parts.Length; i++)
        {
            string folder = parts[i].Trim().Trim('"');
            if (folder.Length == 0)
            {
                continue;
            }
            string candidate = Path.Combine(folder, "python.exe");
            if (File.Exists(candidate))
            {
                return candidate;
            }
        }
        return null;
    }
}
