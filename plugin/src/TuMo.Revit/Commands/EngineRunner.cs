using System;
using System.Diagnostics;
using System.IO;
using System.Reflection;
using System.Runtime.Serialization;
using System.Runtime.Serialization.Json;
using System.Text;
using TuMo.Revit.Logging;

namespace TuMo.Revit.Commands;

internal static class EngineRunner
{
    private const int TimeoutMilliseconds = 5 * 60 * 1000;

    public static string Run(string input, RunLog log, bool repeatStandardFloors = false)
    {
        var assemblyDirectory = Path.GetDirectoryName(Assembly.GetExecutingAssembly().Location)!;
        var executable = Path.Combine(assemblyDirectory, "tumo-engine.exe");
        if (!File.Exists(executable))
            throw new FileNotFoundException("缺少 tumo-engine.exe。请重新解压对应版本的完整安装包。");

        var output = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "TuMo",
            "runs", $"{DateTime.Now:yyyyMMdd-HHmmss}-{Guid.NewGuid():N}");
        Directory.CreateDirectory(output);
        var jobPath = Path.Combine(output, "job.json");
        using (var stream = File.Create(jobPath))
        {
            new DataContractJsonSerializer(typeof(Job)).WriteObject(stream, new Job
            {
                Input = Path.GetFullPath(input),
                OutputDirectory = output
            });
        }

        var startInfo = new ProcessStartInfo
        {
            FileName = executable,
            Arguments = repeatStandardFloors ? "run job.json --repeat-standard-floors" : "run job.json",
            WorkingDirectory = output,
            UseShellExecute = false,
            CreateNoWindow = true,
            RedirectStandardOutput = true,
            RedirectStandardError = true,
            StandardOutputEncoding = Encoding.UTF8,
            StandardErrorEncoding = Encoding.UTF8
        };
        startInfo.EnvironmentVariables["PYTHONUTF8"] = "1";
        using var process = Process.Start(startInfo) ?? throw new InvalidOperationException("无法启动识别引擎。");
        var stdout = process.StandardOutput.ReadToEndAsync();
        var stderr = process.StandardError.ReadToEndAsync();
        if (!process.WaitForExit(TimeoutMilliseconds))
        {
            process.Kill();
            throw new TimeoutException("识别引擎运行超时。");
        }

        log.Write($"识别引擎退出码 {process.ExitCode}");
        log.Write(stdout.GetAwaiter().GetResult());
        var errors = stderr.GetAwaiter().GetResult();
        log.Write(errors);
        if (process.ExitCode != 0)
        {
            var marker = errors.LastIndexOf("图模一致：", StringComparison.Ordinal);
            var summary = marker >= 0 ? errors.Substring(marker).Trim() : "识别引擎失败。请查看日志。";
            throw new InvalidOperationException(summary);
        }

        if (!File.Exists(Path.Combine(output, "model.json")))
            throw new FileNotFoundException("识别引擎没有输出 model.json。");

        return output;
    }

    [DataContract]
    private sealed class Job
    {
        [DataMember(Name = "input")] public string Input { get; set; } = "";
        [DataMember(Name = "output_dir")] public string OutputDirectory { get; set; } = "";
    }
}
