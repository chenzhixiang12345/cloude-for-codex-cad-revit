using System;
using System.Collections.Generic;
using System.Linq;
using Autodesk.Revit.DB;
using TuMo.Core;
using TuMo.Revit.Compat;
using TuMo.Revit.Logging;

namespace TuMo.Revit.Generation;

internal static class JoinGeometry
{
    public static void Connect(Document document, Dictionary<string, Element> created, Relation[] relations, RunLog log)
    {
        var attempted = new HashSet<(ElementId Wall, ElementId Column)>();
        foreach (var relation in relations.Where(item => item.Kind == "join"))
        {
            if (!created.TryGetValue(relation.A, out var first) || !created.TryGetValue(relation.B, out var second))
                continue;

            if (first is Wall wall && second is FamilyInstance column)
                Join(document, wall, column, attempted, log, explicitRelation: true);
            else if (second is Wall otherWall && first is FamilyInstance otherColumn)
                Join(document, otherWall, otherColumn, attempted, log, explicitRelation: true);
        }

        var columns = new HashSet<ElementId>(created.Values.OfType<FamilyInstance>().Select(column => column.Id));
        if (columns.Count == 0)
            return;

        foreach (var wall in created.Values.OfType<Wall>())
        {
            var bbox = wall.get_BoundingBox(null);
            if (bbox == null)
                continue;
            using var outline = new Outline(bbox.Min, bbox.Max);
            var nearby = new FilteredElementCollector(document).OfClass(typeof(FamilyInstance))
                .WherePasses(new BoundingBoxIntersectsFilter(outline));
            foreach (var column in nearby.Where(element => columns.Contains(element.Id)))
                Join(document, wall, column, attempted, log);
        }
    }

    private static void Join(Document document, Wall wall, Element column,
        HashSet<(ElementId Wall, ElementId Column)> attempted, RunLog log, bool explicitRelation = false)
    {
        if (!attempted.Add((wall.Id, column.Id)))
            return;

        try
        {
            JoinWithColumnCutting(document, wall, column);
            if (explicitRelation)
                log.Write($"墙 {wall.Id} 和柱 {column.Id} 已连接，柱切墙。");
        }
        catch (Autodesk.Revit.Exceptions.ArgumentException error)
        {
            if (explicitRelation && !JoinGeometryUtils.AreElementsJoined(document, wall, column) &&
                TryJoinAtContact(document, wall, column, log))
                return;
            log.Write($"墙 {wall.Id} 和柱 {column.Id} 无法连接，{error.Message}");
        }
    }

    private static void JoinWithColumnCutting(Document document, Wall wall, Element column)
    {
        if (!JoinGeometryUtils.AreElementsJoined(document, wall, column))
            JoinGeometryUtils.JoinGeometry(document, wall, column);
        if (!JoinGeometryUtils.IsCuttingElementInJoin(document, column, wall))
            JoinGeometryUtils.SwitchJoinOrder(document, wall, column);
    }

    private static bool TryJoinAtContact(Document document, Wall wall, Element column, RunLog log)
    {
        if (wall.Location is not LocationCurve location || location.Curve is not Line line ||
            wall.get_BoundingBox(null) is not { } wallBounds || column.get_BoundingBox(null) is not { } columnBounds)
            return false;

        var baseZ = Math.Max(wallBounds.Min.Z, columnBounds.Min.Z);
        var topZ = Math.Min(wallBounds.Max.Z, columnBounds.Max.Z);
        var height = Math.Min(topZ - baseZ, RevitApi.Feet(20));
        if (height <= document.Application.ShortCurveTolerance)
            return false;

        var side = XYZ.BasisZ.CrossProduct(line.Direction).Normalize() * (wall.WallType.Width / 2);
        var z = (baseZ + topZ - height) / 2;
        var start = line.GetEndPoint(0);
        var end = line.GetEndPoint(1);
        var atStart = TouchesColumn(document, column, start, -line.Direction, side, z, height);
        var atEnd = TouchesColumn(document, column, end, line.Direction, side, z, height);
        if (atStart == atEnd)
            return false;

        var endIndex = atStart ? 0 : 1;
        if (location.get_ElementsAtJoin(endIndex).Cast<Element>().Any(other => other is Wall && other.Id != wall.Id))
        {
            log.Write($"墙 {wall.Id} 的接触端已与另一道墙连接，保留原位置。");
            return false;
        }

        const int extensionMillimeters = 10;
        var extension = line.Direction * RevitApi.Feet(extensionMillimeters);
        using var change = new SubTransaction(document);
        if (change.Start() != TransactionStatus.Started)
            return false;
        try
        {
            location.Curve = Line.CreateBound(atStart ? start - extension : start, atEnd ? end + extension : end);
            document.Regenerate();
            JoinWithColumnCutting(document, wall, column);
            if (change.Commit() != TransactionStatus.Committed)
                return false;
            log.Write($"墙 {wall.Id} 的接触端延长 {extensionMillimeters} 毫米后与柱 {column.Id} 连接，柱切墙。");
            return true;
        }
        catch (Autodesk.Revit.Exceptions.ArgumentException error)
        {
            log.Write($"墙 {wall.Id} 尝试连接柱 {column.Id} 仍失败，{error.Message}");
            return false;
        }
        finally
        {
            if (change.GetStatus() == TransactionStatus.Started)
                change.RollBack();
        }
    }

    private static bool TouchesColumn(Document document, Element column, XYZ endpoint, XYZ outward,
        XYZ side, double z, double height)
    {
        // Probe only a short strip immediately outside the wall end, using the actual column solid.
        var near = new XYZ(endpoint.X, endpoint.Y, z);
        var far = near + outward * RevitApi.Feet(2);
        var footprint = new[] { near - side, near + side, far + side, far - side };
        var loop = CurveLoop.Create(Enumerable.Range(0, footprint.Length)
            .Select(i => (Curve)Line.CreateBound(footprint[i], footprint[(i + 1) % footprint.Length])).ToList());
        using var probe = GeometryCreationUtilities.CreateExtrusionGeometry(new[] { loop }, XYZ.BasisZ, height);
        using var filter = new ElementIntersectsSolidFilter(probe);
        return filter.PassesFilter(document, column.Id);
    }
}
