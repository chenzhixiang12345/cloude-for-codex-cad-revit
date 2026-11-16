using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using Autodesk.Revit.DB;
using Autodesk.Revit.UI;
using Autodesk.Revit.UI.Events;
using TuMo.Core;
using TuMo.Revit.Compat;
using TuMo.Revit.Logging;

namespace TuMo.Revit.Review;

internal static class ReviewView
{
    public static (ViewDrafting View, string Summary, XYZ? Lower, XYZ? Upper) Create(
        Document document, string drawing, TuMoModel model, RunLog log)
    {
        // The engine selects a model-space plan and records its model-space bounds.
        var sheet = model.Sheets.FirstOrDefault()
            ?? throw new InvalidDataException("识别结果里没有图纸。");
        if (sheet.Layout is { Length: > 0 } layout && !layout.Equals("Model", StringComparison.OrdinalIgnoreCase))
            throw new InvalidDataException("审图需要模型空间平面。请把所需布局导出为模型空间 DXF。");

        var viewType = new FilteredElementCollector(document).OfClass(typeof(ViewFamilyType))
            .Cast<ViewFamilyType>().FirstOrDefault(type => type.ViewFamily == ViewFamily.Drafting)
            ?? throw new InvalidOperationException("项目缺少绘图视图类型。请用 Revit 自带的项目样板创建审图项目。");

        using var transaction = new Transaction(document, "TuMo 创建审图视图");
        transaction.Start();
        var options = transaction.GetFailureHandlingOptions();
        options.SetFailuresPreprocessor(new LoggedFailures(log));
        options.SetClearAfterRollback(true);
        transaction.SetFailureHandlingOptions(options);

        var name = UniqueName(document, sheet);
        var view = ViewDrafting.Create(document, viewType.Id);
        view.Name = name;
        view.ViewTemplateId = ElementId.InvalidElementId;
        view.Scale = 100;
        document.Regenerate();

        var bounds = sheet.Bounds;
        var hasBounds = bounds is { Length: 4 } && bounds[0] < bounds[2] && bounds[1] < bounds[3];
        var instance = Import(document, view, drawing, sheet.UnitsToMillimeters ?? 1);
        document.Regenerate();
        var transform = instance.GetTransform();
        var origin = transform.Origin;
        var axisX = transform.BasisX.Normalize();
        var axisY = transform.BasisY.Normalize();
        XYZ Point(double[] mm)
        {
            // The import options already set the scale. Use the instance for placement and orientation.
            var position = origin + axisX * RevitApi.Feet(mm[0]) + axisY * RevitApi.Feet(mm[1]);
            return new XYZ(position.X, position.Y, view.Origin.Z);
        }

        log.Write($"审图导入：{sheet.Id}，毫米换算 {sheet.UnitsToMillimeters ?? 1}，" +
            $"CAD 原点 {origin}，X 方向 {axisX}，Y 方向 {axisY}。");
        var overlay = ReviewOverlay.Draw(document, view, model, sheet, Point, log);
        var summary = $"轴网 {overlay.Grids}，柱 {overlay.Columns}，墙 {overlay.Walls}，带位置的疑点 {overlay.LocatedIssues}。";

        XYZ? lower = null;
        XYZ? upper = null;
        if (hasBounds)
        {
            var corners = new[]
            {
                Point(new[] { bounds![0], bounds[1] }), Point(new[] { bounds[2], bounds[1] }),
                Point(new[] { bounds[0], bounds[3] }), Point(new[] { bounds[2], bounds[3] })
            };
            var x0 = corners.Min(point => point.X);
            var y0 = corners.Min(point => point.Y);
            var x1 = corners.Max(point => point.X);
            var y1 = corners.Max(point => point.Y);
            var margin = Math.Max(x1 - x0, y1 - y0) * .06;
            lower = new XYZ(x0 - margin, y0 - margin, view.Origin.Z);
            upper = new XYZ(x1 + margin, y1 + margin, view.Origin.Z);
        }

        if (transaction.Commit() != TransactionStatus.Committed)
            throw new InvalidOperationException("Revit 已回滚审图视图。请查看日志。");

        return (view, summary, lower, upper);
    }

    public static void Open(UIDocument uiDocument, (ViewDrafting View, string Summary, XYZ? Lower, XYZ? Upper) result,
        RunLog log)
    {
        try
        {
            uiDocument.ActiveView = result.View;
            var application = uiDocument.Application;
            var document = uiDocument.Document;
            var viewId = result.View.Id;
            var logPath = log.Path;
            var attempts = 3;

            // Revit may still be opening the UIView or fitting its initial extents during this command.
            application.Idling += ZoomWhenReady;

            void ZoomWhenReady(object? sender, IdlingEventArgs args)
            {
                application.Idling -= ZoomWhenReady;
                try
                {
                    var active = application.ActiveUIDocument;
                    if (active == null || !document.Equals(active.Document) || !viewId.Equals(active.ActiveView.Id))
                        return;

                    using var window = active.GetOpenUIViews().FirstOrDefault(view => view.ViewId.Equals(viewId));
                    if (window == null)
                    {
                        if (--attempts > 0)
                        {
                            args.SetRaiseWithoutDelay();
                            application.Idling += ZoomWhenReady;
                        }
                        else
                        {
                            RecordZoom(logPath, "无法缩放审图视图：新视图窗口没有就绪。");
                        }
                        return;
                    }

                    if (result.Lower != null && result.Upper != null)
                        window.ZoomAndCenterRectangle(result.Lower, result.Upper);
                    else
                        window.ZoomToFit();
                    RecordZoom(logPath, "已缩放审图视图。");
                }
                catch (Autodesk.Revit.Exceptions.ApplicationException error)
                {
                    RecordZoom(logPath, $"无法缩放审图视图：{error.Message}");
                }
            }
        }
        catch (Autodesk.Revit.Exceptions.ApplicationException error)
        {
            // The view is already committed. Users can open it in the project browser.
            log.Write($"无法自动打开审图视图：{error.Message}");
        }
    }

    private static void RecordZoom(string path, string message)
    {
        // Idling runs after the command's RunLog is disposed.
        try
        {
            File.AppendAllText(path, $"{DateTimeOffset.Now:O} {message}{Environment.NewLine}");
        }
        catch (Exception error) when (error is IOException or UnauthorizedAccessException)
        {
            System.Diagnostics.Trace.TraceWarning(error.Message);
        }
    }

    private static ImportInstance Import(Document document, View view, string drawing, double factor)
    {
        using var options = new DWGImportOptions
        {
            ThisViewOnly = true,
            Placement = ImportPlacement.Origin,
            ColorMode = ImportColorMode.BlackAndWhite,
            VisibleLayersOnly = true,
            AutoCorrectAlmostVHLines = false
        };
        // Match the DXF-to-millimeters factor used by the recognition engine.
        if (Math.Abs(factor - 1) < 1e-8)
            options.Unit = ImportUnit.Millimeter;
        else if (Math.Abs(factor - 10) < 1e-8)
            options.Unit = ImportUnit.Centimeter;
        else if (Math.Abs(factor - 100) < 1e-8)
            options.Unit = ImportUnit.Decimeter;
        else if (Math.Abs(factor - 1000) < 1e-8)
            options.Unit = ImportUnit.Meter;
        else if (Math.Abs(factor - 25.4) < 1e-8)
            options.Unit = ImportUnit.Inch;
        else if (Math.Abs(factor - 304.8) < 1e-8)
            options.Unit = ImportUnit.Foot;
        else
            options.CustomScale = RevitApi.Feet(factor);

        if (!document.Import(drawing, options, view, out var id) ||
            document.GetElement(id) is not ImportInstance instance)
            throw new InvalidOperationException("Revit 无法导入 DXF。请检查原图是否可在 Revit 里手动导入。");

        using var graphics = new OverrideGraphicSettings();
        view.SetElementOverrides(id, graphics.SetHalftone(true));
        instance.Pinned = true;
        return instance;
    }

    private static string UniqueName(Document document, Sheet sheet)
    {
        var code = string.IsNullOrWhiteSpace(sheet.Number) ? sheet.Id : sheet.Number;
        var clean = new string(code!.Where(c => char.IsLetterOrDigit(c) || c is '-' or '_' or '.').Take(48).ToArray());
        var prefix = clean.Length > 0 ? $"TuMo 审图 - {clean}" : "TuMo 审图";
        var names = new HashSet<string>(new FilteredElementCollector(document).OfClass(typeof(View))
            .Cast<View>().Select(view => view.Name), StringComparer.OrdinalIgnoreCase);
        var name = prefix;
        for (var index = 2; names.Contains(name); index++)
            name = $"{prefix} - {index}";
        return name;
    }
}
