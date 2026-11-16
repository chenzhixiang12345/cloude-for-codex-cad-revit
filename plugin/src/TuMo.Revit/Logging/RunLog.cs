using System;
using System.IO;
using System.Text;

namespace TuMo.Revit.Logging;

internal sealed class RunLog : IDisposable
{
    private readonly StreamWriter writer;

    public string Path { get; }

    public RunLog()
    {
        var root = System.IO.Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
            "TuMo", "logs");
        Directory.CreateDirectory(root);
        var stamp = DateTime.Now.ToString("yyyyMMdd-HHmmss");
        Path = System.IO.Path.Combine(root, stamp + ".log");
        if (File.Exists(Path))
            Path = System.IO.Path.Combine(root, $"{stamp}-{Guid.NewGuid():N}.log");

        writer = new StreamWriter(new FileStream(Path, FileMode.CreateNew), new UTF8Encoding(false))
        {
            AutoFlush = true
        };
    }

    public void Write(string message) => writer.WriteLine($"{DateTimeOffset.Now:O} {message}");

    public void Dispose() => writer.Dispose();
}
