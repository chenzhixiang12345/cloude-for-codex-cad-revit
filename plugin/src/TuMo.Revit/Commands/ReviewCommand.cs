using System;
using System.Diagnostics;
using System.IO;
using Autodesk.Revit.Attributes;
using Autodesk.Revit.DB;
using Autodesk.Revit.UI;
using Microsoft.Win32;
using TuMo.Core;
using TuMo.Revit.Logging;
using TuMo.Revit.Review;

namespace TuMo.Revit.Commands;

[Transaction(TransactionMode.Manual)]
[Regeneration(RegenerationOption.Manual)]
public sealed class ReviewCommand : IExternalCommand
{
    public Result Execute(ExternalCommandData commandData, ref string message, ElementSet elements)
    {
        var uiDocument = commandData.Application.ActiveUIDocument;
        if (uiDocument == null || uiDocument.Document.IsFamilyDocument || uiDocument.Document.IsReadOnly)
        {
            TaskDialog.Show("TuMo", "请先打开可编辑的 Revit 项目。");
            return Result.Cancelled;
        }

        var picker = new OpenFileDialog
        {
            Filter = "CAD DXF (*.dxf)|*.dxf",
            Title = "选择要核对的 DXF",
            CheckFileExists = true
        };
        if (picker.ShowDialog() != true)
            return Result.Cancelled;

        using var log = new RunLog();
        var timer = Stopwatch.StartNew();
        try
        {
            var application = commandData.Application.Application;
            log.Write($"Revit {application.VersionNumber}（{application.VersionBuild}），审图输入 {picker.FileName}");
            var output = EngineRunner.Run(picker.FileName, log);
            var model = TuMoModel.Read(Path.Combine(output, "model.json"));
            var result = ReviewView.Create(uiDocument.Document, picker.FileName, model, log);
            ReviewView.Open(uiDocument, result, log);
            TaskDialog.Show("TuMo 审图",
                $"审图视图：{result.View.Name}\n{result.Summary}\n\n" +
                $"蓝色轴网，棕色柱，绿色墙，红色疑点。\n疑点报告：{Path.Combine(output, "issues.json")}");
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
            log.Write($"审图总耗时 {timer.ElapsedMilliseconds} ms");
        }
    }
}
