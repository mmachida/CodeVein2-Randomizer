using System.Diagnostics;
using System.IO.Compression;
using System.Reflection;
using System.Runtime.InteropServices;

internal static class Program
{
    private const string PayloadResourceName = "CV2Launcher.Payload.zip";
    private const string TempFolderName = "CV2R-DotNetHost";

    // Start the packaged randomizer
    [STAThread]
    private static int Main(string[] args)
    {
        string? extractionRoot = null;

        try
        {
            extractionRoot = CreateExtractionRoot();
            ExtractPayload(extractionRoot);

            string randomizerPath = FindRandomizerExecutable(extractionRoot);
            var startInfo = new ProcessStartInfo
            {
                FileName = randomizerPath,
                WorkingDirectory = extractionRoot,
                UseShellExecute = false,
                WindowStyle = ProcessWindowStyle.Normal
            };
            foreach (string argument in args)
            {
                startInfo.ArgumentList.Add(argument);
            }

            using Process process = Process.Start(startInfo)
                ?? throw new InvalidOperationException("Windows did not start the randomizer process.");
            process.WaitForExit();
            return process.ExitCode;
        }
        catch (Exception error)
        {
            ShowError("The randomizer launcher could not start the app:\n\n" + error.Message);
            return 1;
        }
        finally
        {
            if (extractionRoot is not null)
            {
                TryRemoveExtraction(extractionRoot);
            }
        }
    }

    // Create a private temp folder
    private static string CreateExtractionRoot()
    {
        string root = Path.Combine(
            Path.GetTempPath(),
            $"{TempFolderName}-{Environment.ProcessId}-{Guid.NewGuid():N}"
        );
        DirectoryInfo directory = Directory.CreateDirectory(root);
        if ((directory.Attributes & FileAttributes.ReparsePoint) != 0)
        {
            throw new InvalidOperationException("The temporary extraction folder cannot be a reparse point.");
        }
        return root;
    }

    // Extract the embedded payload
    private static void ExtractPayload(string extractionRoot)
    {
        using Stream payload = Assembly.GetExecutingAssembly()
            .GetManifestResourceStream(PayloadResourceName)
            ?? throw new InvalidOperationException("The embedded app payload was not found.");
        using var archive = new ZipArchive(payload, ZipArchiveMode.Read, leaveOpen: false);

        string root = Path.GetFullPath(extractionRoot);
        foreach (ZipArchiveEntry entry in archive.Entries)
        {
            string destination = Path.GetFullPath(Path.Combine(root, entry.FullName));
            string relative = Path.GetRelativePath(root, destination);
            if (Path.IsPathRooted(relative) || relative == ".." ||
                relative.StartsWith(".." + Path.DirectorySeparatorChar, StringComparison.Ordinal))
            {
                throw new InvalidDataException("The embedded payload contains an invalid path.");
            }

            if (entry.FullName.EndsWith('/') || entry.FullName.EndsWith('\\'))
            {
                Directory.CreateDirectory(destination);
                continue;
            }

            string? directory = Path.GetDirectoryName(destination);
            if (directory is not null)
            {
                Directory.CreateDirectory(directory);
            }

            using Stream input = entry.Open();
            using FileStream output = new(destination, FileMode.CreateNew, FileAccess.Write, FileShare.None);
            input.CopyTo(output);
        }
    }

    // Find the bundled app executable
    private static string FindRandomizerExecutable(string extractionRoot)
    {
        string[] matches = Directory.GetFiles(extractionRoot, "CodeVein2Randomizer-v*.exe", SearchOption.TopDirectoryOnly);
        if (matches.Length != 1)
        {
            throw new InvalidDataException("The embedded payload must contain exactly one versioned randomizer executable.");
        }

        return matches[0];
    }

    // Remove extracted files after exit
    private static void TryRemoveExtraction(string extractionRoot)
    {
        try
        {
            Directory.Delete(extractionRoot, recursive: true);
        }
        catch
        {
            // Keep leftovers if another process still owns them.
        }
    }

    // Show a startup error
    private static void ShowError(string message)
    {
        _ = MessageBoxW(IntPtr.Zero, message, "Code Vein 2 Randomizer", 0x10);
    }

    // Display a native Windows dialog
    [DllImport("user32.dll", CharSet = CharSet.Unicode, ExactSpelling = true, SetLastError = false)]
    private static extern int MessageBoxW(IntPtr window, string text, string caption, uint type);
}
