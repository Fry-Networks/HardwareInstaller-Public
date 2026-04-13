using System;
using System.Collections.Generic;
using System.IO;
using System.Runtime.InteropServices;

/// <summary>
/// FryNetworks Web Agent — Brave shortcut repair utility.
/// Scans known shortcut locations for Brave browser .lnk files and ensures
/// they include --load-extension pointing to the web agent extension directory.
///
/// Usage: FryRepairShortcuts.exe --ext-path "C:\...\web-agent" [--dry-run]
/// Exit 0 on success (including nothing-to-patch), 1 on error.
/// </summary>
class FryRepairShortcuts
{
    static string LogPath;
    static bool DryRun;

    static int Main(string[] args)
    {
        try
        {
            string extPath = null;
            DryRun = false;

            for (int i = 0; i < args.Length; i++)
            {
                if (args[i] == "--ext-path" && i + 1 < args.Length)
                    extPath = args[++i];
                else if (args[i] == "--dry-run")
                    DryRun = true;
            }

            if (string.IsNullOrEmpty(extPath))
            {
                // No extension path — nothing to do
                return 0;
            }

            // Log file lives next to the exe
            string exeDir = AppDomain.CurrentDomain.BaseDirectory;
            LogPath = Path.Combine(exeDir, "FryRepairShortcuts.log");
            TruncateLogIfNeeded();

            Log("=== Start" + (DryRun ? " (dry-run)" : "") + " ===");
            Log("Extension path: " + extPath);

            if (!Directory.Exists(extPath))
            {
                Log("Extension path does not exist — nothing to do");
                return 0;
            }

            string[] locations = GetSearchLocations();
            int patchCount = 0;

            // Create WScript.Shell COM object
            Type wshType = Type.GetTypeFromProgID("WScript.Shell");
            if (wshType == null)
            {
                Log("ERROR: WScript.Shell COM not available");
                return 1;
            }
            object wshShell = Activator.CreateInstance(wshType);

            try
            {
                foreach (string loc in locations)
                {
                    if (!Directory.Exists(loc))
                    {
                        Log("Skip (not found): " + loc);
                        continue;
                    }

                    Log("Scanning: " + loc);
                    string[] lnkFiles;
                    try
                    {
                        lnkFiles = Directory.GetFiles(loc, "*.lnk", SearchOption.AllDirectories);
                    }
                    catch (Exception ex)
                    {
                        Log("  Error enumerating: " + ex.Message);
                        continue;
                    }

                    foreach (string lnkPath in lnkFiles)
                    {
                        try
                        {
                            patchCount += ProcessShortcut(wshShell, lnkPath, extPath);
                        }
                        catch (Exception ex)
                        {
                            Log("  Error processing " + Path.GetFileName(lnkPath) + ": " + ex.Message);
                        }
                    }
                }
            }
            finally
            {
                Marshal.ReleaseComObject(wshShell);
            }

            Log("Complete: " + patchCount + " shortcut(s) " + (DryRun ? "would be " : "") + "patched");
            return 0;
        }
        catch (Exception ex)
        {
            try { Log("FATAL: " + ex.ToString()); } catch { }
            return 1;
        }
    }

    static int ProcessShortcut(object wshShell, string lnkPath, string extPath)
    {
        string fileName = Path.GetFileName(lnkPath);

        // Open shortcut via COM
        object shortcut = wshShell.GetType().InvokeMember(
            "CreateShortcut",
            System.Reflection.BindingFlags.InvokeMethod,
            null, wshShell, new object[] { lnkPath });

        try
        {
            string targetPath = (string)shortcut.GetType().InvokeMember(
                "TargetPath",
                System.Reflection.BindingFlags.GetProperty,
                null, shortcut, null) ?? "";

            string arguments = (string)shortcut.GetType().InvokeMember(
                "Arguments",
                System.Reflection.BindingFlags.GetProperty,
                null, shortcut, null) ?? "";

            bool nameMatch = fileName.IndexOf("brave", StringComparison.OrdinalIgnoreCase) >= 0;
            bool targetMatch = targetPath.IndexOf("brave", StringComparison.OrdinalIgnoreCase) >= 0;

            if (!nameMatch && !targetMatch)
                return 0;

            if (arguments.IndexOf("--load-extension", StringComparison.OrdinalIgnoreCase) >= 0)
                return 0; // Already patched

            Log("  Found: " + lnkPath);
            Log("    Target: " + targetPath);
            Log("    Args: " + arguments);

            // Fix empty target
            if (string.IsNullOrEmpty(targetPath) && nameMatch)
            {
                string defaultBrave = @"C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe";
                if (File.Exists(defaultBrave))
                {
                    shortcut.GetType().InvokeMember(
                        "TargetPath",
                        System.Reflection.BindingFlags.SetProperty,
                        null, shortcut, new object[] { defaultBrave });
                    Log("    Set target: " + defaultBrave);
                }
            }

            // Append --load-extension
            string newArgs = (arguments + " --load-extension=" + extPath).Trim();

            if (DryRun)
            {
                Log("    Would set args: " + newArgs);
            }
            else
            {
                shortcut.GetType().InvokeMember(
                    "Arguments",
                    System.Reflection.BindingFlags.SetProperty,
                    null, shortcut, new object[] { newArgs });

                shortcut.GetType().InvokeMember(
                    "Save",
                    System.Reflection.BindingFlags.InvokeMethod,
                    null, shortcut, null);

                Log("    Patched args: " + newArgs);
            }

            return 1;
        }
        finally
        {
            Marshal.ReleaseComObject(shortcut);
        }
    }

    static string[] GetSearchLocations()
    {
        string appData = Environment.GetFolderPath(Environment.SpecialFolder.ApplicationData);
        string userProfile = Environment.GetFolderPath(Environment.SpecialFolder.UserProfile);
        string desktop = Environment.GetFolderPath(Environment.SpecialFolder.DesktopDirectory);
        string publicDesktop = Environment.GetEnvironmentVariable("PUBLIC") ?? @"C:\Users\Public";

        return new string[]
        {
            Path.Combine(appData, @"Microsoft\Windows\Start Menu\Programs"),
            Path.Combine(publicDesktop, "Desktop"),
            desktop,
            Path.Combine(userProfile, @"OneDrive\Desktop"),
            @"C:\ProgramData\Microsoft\Windows\Start Menu\Programs",
            Path.Combine(appData, @"Microsoft\Internet Explorer\Quick Launch\User Pinned\TaskBar"),
            Path.Combine(appData, @"Microsoft\Internet Explorer\Quick Launch"),
        };
    }

    static void Log(string message)
    {
        try
        {
            string line = DateTime.Now.ToString("yyyy-MM-dd HH:mm:ss") + "  " + message + Environment.NewLine;
            File.AppendAllText(LogPath, line);
        }
        catch { }
    }

    static void TruncateLogIfNeeded()
    {
        try
        {
            if (!File.Exists(LogPath)) return;
            var fi = new FileInfo(LogPath);
            if (fi.Length <= 50 * 1024) return;

            string content = File.ReadAllText(LogPath);
            int keep = 25 * 1024;
            if (content.Length > keep)
            {
                int cutAt = content.Length - keep;
                int nl = content.IndexOf('\n', cutAt);
                if (nl >= 0)
                    content = "[...truncated...]\n" + content.Substring(nl + 1);
                else
                    content = "[...truncated...]\n" + content.Substring(cutAt);
            }
            File.WriteAllText(LogPath, content);
        }
        catch { }
    }
}
