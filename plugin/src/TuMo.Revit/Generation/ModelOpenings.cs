using System;
using Autodesk.Revit.DB;
using Autodesk.Revit.DB.Structure;
using TuMo.Core;
using TuMo.Revit.Compat;
using TuMo.Revit.Logging;

namespace TuMo.Revit.Generation;

internal sealed class ModelOpenings
{
    private const double DefaultWindowSillMillimeters = 900;

    private readonly Document document;
    private readonly RunLog log;
    private readonly OpeningFamilies families;

    public ModelOpenings(Document document, RunLog log)
    {
        this.document = document;
        this.log = log;
        families = new OpeningFamilies(document, log);
    }

    public (Element Result, bool NeedsReview) Create(ModelElement source, Wall wall, Level level,
        XYZ center, XYZ? facing, double wallBottom, double wallTop)
    {
        var window = source.Category == "window";
        var assumedSill = window && source.SillHeight == null;
        var width = Size(source.Type?.Width, "宽度");
        var height = Size(source.Type?.Height, "高度");
        var sill = RevitApi.Feet(source.SillHeight ?? (window ? DefaultWindowSillMillimeters : 0));
        if (double.IsNaN(sill) || double.IsInfinity(sill))
            throw new InvalidOperationException("洞口底部相对标高无效。");

        var bottom = level.ProjectElevation + sill;
        var top = bottom + height;
        var tolerance = RevitApi.Feet(1);
        if (bottom < wallBottom - tolerance || top > wallTop + tolerance)
            throw new InvalidOperationException(assumedSill
                ? $"图纸未给窗台高度，试用 {DefaultWindowSillMillimeters} 毫米时超出宿主墙高度，请核对。"
                : "洞口高程超出宿主墙范围。");

        if (wall.Location is not LocationCurve { Curve: Line line })
            throw new InvalidOperationException("宿主需要直线墙。");

        var start = line.GetEndPoint(0);
        var along = line.Direction;
        var horizontalCenter = new XYZ(center.X, center.Y, start.Z);
        var distance = (horizontalCenter - start).DotProduct(along);
        var projected = start + along * distance;
        if (horizontalCenter.DistanceTo(projected) > wall.Width / 2 + RevitApi.Feet(25) ||
            distance - width / 2 < -tolerance || distance + width / 2 > line.Length + tolerance)
            throw new InvalidOperationException("洞口位置或宽度超出宿主墙范围。");

        var position = new XYZ(projected.X, projected.Y, bottom);
        if (source.Category == "wall_opening")
        {
            var firstCorner = position - along * (width / 2);
            var oppositeCorner = position + along * (width / 2) + XYZ.BasisZ * height;
            var opening = document.Create.NewOpening(wall, firstCorner, oppositeCorner)
                ?? throw new InvalidOperationException("Revit 未能创建矩形墙洞。");
            return (opening, false);
        }

        var category = window ? BuiltInCategory.OST_Windows : BuiltInCategory.OST_Doors;
        var symbol = families.Resolve(category, width, height, source.Type!.Name);
        if (!symbol.IsActive)
            symbol.Activate();
        var instance = document.Create.NewFamilyInstance(position, symbol, wall, level, StructuralType.NonStructural)
            ?? throw new InvalidOperationException("Revit 未能在墙上放置门窗族。");
        if (instance.Host?.Id != wall.Id)
            throw new InvalidOperationException("门窗族与指定宿主墙不一致。");

        SetSill(instance, sill, bottom, tolerance);
        var reviewOrientation = Align(instance, facing, source.Geometry!.Hand);
        if (assumedSill)
            log.Write($"待核对 {source.Id}：图纸未给窗台高度，暂按 {DefaultWindowSillMillimeters} 毫米生成。");
        if (reviewOrientation)
            log.Write($"待核对 {source.Id}：族的朝向或开启侧无法匹配图纸记录。");
        return (instance, assumedSill || reviewOrientation);
    }

    private static void SetSill(FamilyInstance instance, double sill, double bottom, double tolerance)
    {
        var parameter = instance.get_Parameter(BuiltInParameter.INSTANCE_SILL_HEIGHT_PARAM)
            ?? instance.get_Parameter(BuiltInParameter.INSTANCE_ELEVATION_PARAM);
        if (parameter is { StorageType: StorageType.Double })
        {
            if (Math.Abs(parameter.AsDouble() - sill) <= tolerance || (!parameter.IsReadOnly && parameter.Set(sill)))
                return;
        }
        else if (instance.Location is LocationPoint location && Math.Abs(location.Point.Z - bottom) <= tolerance)
        {
            return;
        }

        throw new InvalidOperationException("族无法设置图纸给定的洞底高度。");
    }

    private static bool Align(FamilyInstance instance, XYZ? facing, string? hand)
    {
        var pending = false;
        if (facing != null)
        {
            var dot = facing.Normalize().DotProduct(instance.FacingOrientation);
            if (dot < -0.5)
                pending = !instance.CanFlipFacing || !instance.flipFacing();
            else if (dot < 0.5)
                pending = true;
        }

        if (hand != null)
        {
            // Read left/right as viewed from the instance's facing side.
            var right = XYZ.BasisZ.CrossProduct(instance.FacingOrientation);
            var dot = right.DotProduct(instance.HandOrientation);
            if (Math.Abs(dot) < 0.5 || hand is not ("left" or "right"))
                return true;
            if ((dot > 0) != (hand == "right"))
                pending |= !instance.CanFlipHand || !instance.flipHand();
        }
        return pending;
    }

    private static double Size(double? mm, string label)
    {
        if (!mm.HasValue || mm.Value <= 0 || double.IsNaN(mm.Value) || double.IsInfinity(mm.Value))
            throw new InvalidOperationException($"缺少有效的{label}。");
        return RevitApi.Feet(mm.Value);
    }

    // Document regeneration failures must abort the outer transaction.
    internal static bool CanSkip(Exception error) => error is not Autodesk.Revit.Exceptions.RegenerationFailedException &&
        error is (ArgumentException or InvalidOperationException or Autodesk.Revit.Exceptions.ArgumentException or
            Autodesk.Revit.Exceptions.InvalidOperationException);
}
