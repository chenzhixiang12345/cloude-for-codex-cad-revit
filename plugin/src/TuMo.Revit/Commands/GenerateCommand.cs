using System;
using System.Diagnostics;
using System.IO;
using System.Reflection;
using Autodesk.Revit.Attributes;
using Autodesk.Revit.DB;
using Autodesk.Revit.UI;
using Microsoft.Win32;
using TuMo.Core;
using TuMo.Revit.Logging;

namespace TuMo.Revit.Commands;

[Transaction(TransactionMode.Manual)]
[Regeneration(RegenerationOption.Manual)]
public sealed class GenerateCommand : IExternalCommand
{
    public Result Execute(ExternalCommandData commandData, ref string message, ElementSet elements)
        => Run(commandData, ref message, FloorMode.Single);

    internal static Result Run(ExternalCommandData commandData, ref string message, FloorMode mode, bool includeStructuralSlab = false)
    {
        var document = commandData.Application.ActiveUIDocument?.Document;
        if (document == null || document.IsFamilyDocument || document.IsReadOnly)
        {
            TaskDialog.Show("TuMo", "请先打开可编辑的 Revit 项目。");
            return Result.Cancelled;
        }

        var picker = new OpenFileDialog
        {
            Filter = "CAD DXF (*.dxf)|*.dxf",
            Title = mode switch
            {
                FloorMode.Standard => "选择带楼层范围和楼层表的标准层平面图",
                FloorMode.Building => "选择包含多张楼层平面的建筑 DXF",
                _ => "选择一张平面图"
            },
            Multiselect = false,
            CheckFileExists = true
        };
        if (picker.ShowDialog() != true)
            return Result.Cancelled;

        string? structuralSlab = null;
        if (includeStructuralSlab)
        {
            var slabPicker = new OpenFileDialog
            {
                Filter = picker.Filter,
                Title = "选择带楼层表和降板图例的结构顶板 DXF",
                Multiselect = false,
                CheckFileExists = true
            };
            if (slabPicker.ShowDialog() != true)
                return Result.Cancelled;
            structuralSlab = slabPicker.FileName;
        }

        using var log = new RunLog();
        var timer = Stopwatch.StartNew();
        try
        {
            var application = commandData.Application.Application;
            log.Write($"Revit {application.VersionNumber}, build {application.VersionBuild}; " +
                $"TuMo {Assembly.GetExecutingAssembly().GetName().Version}");
            log.Write($"输入 {picker.FileName}");
            if (structuralSlab != null)
                log.Write($"结构顶板图 {structuralSlab}");
            var output = EngineRunner.Run(picker.FileName, log, mode, structuralSlab);
            var model = TuMoModel.Read(Path.Combine(output, "model.json"));
            log.Write($"识别结果，标高 {model.Levels.Length}，轴网 {model.Grids.Length}，构件 {model.Elements.Length}。");
            var reportPath = Path.Combine(output, "issues.json");
            var issues = IssueReport.Read(reportPath);
            log.Write($"疑点报告 {reportPath}");
            var report = Generation.ModelBuilder.Build(document, model, log);
            TaskDialog.Show("TuMo", $"{report}\n识别疑点 {issues.Issues.Length} 项。\n\n输出目录\n{output}\n\n日志\n{log.Path}");
            return Result.Succeeded;
        }
        catch (Exception error)
        {
            log.Write(error.ToString());
            message = $"{error.Message}\n\n日志\n{log.Path}";
            return Result.Failed;
        }
        finally
        {
            log.Write($"总耗时 {timer.ElapsedMilliseconds} ms");
        }
    }
}

[Transaction(TransactionMode.Manual)]
[Regeneration(RegenerationOption.Manual)]
public sealed class GenerateStandardFloorsCommand : IExternalCommand
{
    public Result Execute(ExternalCommandData commandData, ref string message, ElementSet elements)
        => GenerateCommand.Run(commandData, ref message, FloorMode.Standard);
}

[Transaction(TransactionMode.Manual)]
[Regeneration(RegenerationOption.Manual)]
public sealed class MergeFloorsCommand : IExternalCommand
{
    public Result Execute(ExternalCommandData commandData, ref string message, ElementSet elements)
        => GenerateCommand.Run(commandData, ref message, FloorMode.Building);
}

[Transaction(TransactionMode.Manual)]
[Regeneration(RegenerationOption.Manual)]
public sealed class MergeStructuralSlabsCommand : IExternalCommand
{
    public Result Execute(ExternalCommandData commandData, ref string message, ElementSet elements)
        => GenerateCommand.Run(commandData, ref message, FloorMode.Building, includeStructuralSlab: true);
}
