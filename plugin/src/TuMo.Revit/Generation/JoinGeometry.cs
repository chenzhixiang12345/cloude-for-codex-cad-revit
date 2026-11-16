using System;
using System.Collections.Generic;
using System.Linq;
using Autodesk.Revit.DB;
using TuMo.Core;
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
                Join(document, wall, column, attempted, log);
            else if (second is Wall otherWall && first is FamilyInstance otherColumn)
                Join(document, otherWall, otherColumn, attempted, log);
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
        HashSet<(ElementId Wall, ElementId Column)> attempted, RunLog log)
    {
        if (!attempted.Add((wall.Id, column.Id)))
            return;

        try
        {
            if (!JoinGeometryUtils.AreElementsJoined(document, wall, column))
                JoinGeometryUtils.JoinGeometry(document, wall, column);
            if (!JoinGeometryUtils.IsCuttingElementInJoin(document, column, wall))
                JoinGeometryUtils.SwitchJoinOrder(document, wall, column);
        }
        catch (Autodesk.Revit.Exceptions.ArgumentException error)
        {
            log.Write($"墙 {wall.Id} 和柱 {column.Id} 无法连接，{error.Message}");
        }
    }
}
