using System;
using System.Collections.Generic;
using System.Globalization;
using Autodesk.Revit.DB;
using TuMo.Core;
using TuMo.Revit.Compat;
using TuMo.Revit.Logging;

namespace TuMo.Revit.Generation;

internal static class ProjectOrigin
{
    public static string? Verify(Document document, TuMoModel model, IReadOnlyDictionary<string, Grid> grids, RunLog log)
    {
        var tolerance = RevitApi.Feet(1);
        var basePoint = BasePoint.GetProjectBasePoint(document)?.Position;
        if (basePoint != null)
            log.Write($"项目基点的 Revit 内部坐标为 {Coordinates(basePoint)}。");
        var originAxes = model.Project.Origin?.Grid;
        var anchor = XYZ.Zero;
        if (originAxes is { Length: > 0 })
        {
            if (originAxes.Length != 2 || !grids.TryGetValue(originAxes[0], out var first)
                || !grids.TryGetValue(originAxes[1], out var second))
                throw new InvalidOperationException("无法核对识别结果指定的原点轴网。请检查轴号和轴线。");
            if (first.Curve is not Line firstLine || second.Curve is not Line secondLine)
                throw new InvalidOperationException($"原点轴网 {originAxes[0]} 与 {originAxes[1]} 缺少直线几何。");

            var intersection = Intersect(firstLine, secondLine);
            if (intersection == null)
                throw new InvalidOperationException($"原点轴网 {originAxes[0]} 与 {originAxes[1]} 没有唯一交点。");

            anchor = intersection;
            log.Write($"轴网 {originAxes[0]} 与 {originAxes[1]} 的 Revit 内部交点为 {Coordinates(intersection)}。");
            if (intersection.GetLength() > tolerance)
                throw new InvalidOperationException($"轴网 {originAxes[0]} 与 {originAxes[1]} 的交点未对齐 Revit 内部原点。请检查坐标变换。");
        }
        else
        {
            log.Write("识别结果未指定可核对的原点轴网。沿用图纸到 Revit 内部坐标的变换。");
        }

        if (basePoint == null)
            return ReportWarning(log, "无法读取项目基点。模型采用 Revit 内部坐标。");
        if (HorizontalDistance(anchor, basePoint) > tolerance)
            return ReportWarning(log, "项目基点的 XY 位置与模型原点不一致。生成结果沿用 Revit 内部坐标，项目基点保留在现有位置。");
        if (originAxes is { Length: 2 })
            log.Write("轴网交点与项目基点的 XY 位置一致。");
        return null;
    }

    private static string ReportWarning(RunLog log, string warning)
    {
        log.Write(warning);
        return warning;
    }

    private static XYZ? Intersect(Line first, Line second)
    {
        var a = first.GetEndPoint(0);
        var b = second.GetEndPoint(0);
        var u = first.Direction;
        var v = second.Direction;
        var denominator = u.X * v.Y - u.Y * v.X;
        if (Math.Abs(denominator) < 1e-9)
            return null;

        var delta = b - a;
        var distance = (delta.X * v.Y - delta.Y * v.X) / denominator;
        return new XYZ(a.X + distance * u.X, a.Y + distance * u.Y, 0);
    }

    private static double HorizontalDistance(XYZ first, XYZ second)
    {
        var x = first.X - second.X;
        var y = first.Y - second.Y;
        return Math.Sqrt(x * x + y * y);
    }

    private static string Coordinates(XYZ position)
    {
        var millimeter = RevitApi.Feet(1);
        return string.Format(CultureInfo.InvariantCulture, "({0:F3}, {1:F3}, {2:F3}) mm",
            position.X / millimeter, position.Y / millimeter, position.Z / millimeter);
    }
}
