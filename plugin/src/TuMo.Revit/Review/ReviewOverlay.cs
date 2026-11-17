using System;
using System.Collections.Generic;
using System.Linq;
using Autodesk.Revit.DB;
using TuMo.Core;
using TuMo.Revit.Compat;
using TuMo.Revit.Logging;

namespace TuMo.Revit.Review;

internal readonly struct ReviewResult
{
    private readonly IReadOnlyDictionary<string, int> counts;
    public int Grids { get; }
    public int LocatedIssues { get; }

    public ReviewResult(int grids, IReadOnlyDictionary<string, int> counts, int locatedIssues)
    {
        Grids = grids;
        this.counts = counts;
        LocatedIssues = locatedIssues;
    }

    public int Count(string category) => counts.TryGetValue(category, out var count) ? count : 0;
}

internal static class ReviewOverlay
{
    private static readonly IReadOnlyDictionary<string, (string Label, byte Red, byte Green, byte Blue, int Weight)> Styles =
        new Dictionary<string, (string, byte, byte, byte, int)>(StringComparer.Ordinal)
        {
            ["wall"] = ("墙", 8, 120, 105, 3),
            ["shear_wall"] = ("剪力墙", 21, 94, 117, 4),
            ["column"] = ("柱", 154, 52, 18, 3),
            ["door"] = ("门", 124, 58, 237, 4),
            ["window"] = ("窗", 2, 132, 199, 4),
            ["wall_opening"] = ("墙洞", 194, 65, 12, 4),
            ["room"] = ("房间", 145, 96, 6, 2),
            ["slab"] = ("楼板", 67, 56, 202, 2),
            ["slab_opening"] = ("楼板洞口", 194, 65, 12, 3),
            ["ramp"] = ("坡道", 162, 28, 175, 3),
        };

    public static ReviewResult Draw(Document document, ViewDrafting view, TuMoModel model, Sheet sheet,
        Func<double[], XYZ> point, RunLog log)
    {
        var painter = new Painter(document, view);
        var gridStyle = LineStyle(document, "TuMo 审图 - 轴网", new Color(37, 99, 166), 2);
        var issueStyle = LineStyle(document, "TuMo 审图 - 疑点", new Color(185, 28, 28), 4);
        var anchors = new Dictionary<string, XYZ>(StringComparer.Ordinal);

        var defaultSheetId = model.Sheets.FirstOrDefault()?.Id;
        bool OnSelectedSheet(DrawingSource? source) => (source?.Sheet ?? defaultSheetId) == sheet.Id;
        var copies = new HashSet<string>(model.Elements.Where(IsStandardFloorCopy).Select(element => element.Id), StringComparer.Ordinal);
        var elements = model.Elements.Where(element => OnSelectedSheet(element.Source) && !copies.Contains(element.Id)).ToArray();
        var hosts = elements.Where(element => element.Category is "wall" or "shear_wall").GroupBy(element => element.Id)
            .ToDictionary(group => group.Key, group => group.First(), StringComparer.Ordinal);

        var grids = 0;
        foreach (var grid in model.Grids)
        {
            if (!OnSelectedSheet(grid.Source))
                continue;
            if (!IsPoint(grid.Start) || !IsPoint(grid.End)
                || !painter.Segment(point(grid.Start), point(grid.End), gridStyle))
            {
                log.Write($"审图跳过无效或过短的轴线 {grid.Id}。");
                continue;
            }

            var anchor = point(grid.End);
            anchors[grid.Id] = anchor;
            var label = string.IsNullOrWhiteSpace(grid.Label) ? grid.Id : $"{grid.Label} / {grid.Id}";
            painter.Note(anchor, label, 0.8, 0.7);
            grids++;
        }

        var counts = new Dictionary<string, int>(StringComparer.Ordinal);
        var graphics = new Dictionary<string, GraphicsStyle>(StringComparer.Ordinal);
        foreach (var element in elements)
        {
            if (!Styles.TryGetValue(element.Category, out var specification))
                continue;
            if (!graphics.TryGetValue(element.Category, out var style))
            {
                style = LineStyle(document, $"TuMo 审图 - {specification.Label}",
                    new Color(specification.Red, specification.Green, specification.Blue), specification.Weight);
                graphics.Add(element.Category, style);
            }
            var host = element.Host != null && hosts.TryGetValue(element.Host, out var wall) ? wall : null;
            var anchor = DrawElement(element, painter, point, style, host);
            if (anchor == null)
            {
                log.Write($"审图跳过无效或过小的构件 {element.Id}。");
                continue;
            }

            anchors[element.Id] = anchor;
            var label = ElementLabel(element);
            if (element.Category == "room")
                painter.RoomNote(anchor, label);
            else
                painter.Note(anchor, label, 0.6, 0.7);
            counts[element.Category] = counts.TryGetValue(element.Category, out var previous) ? previous + 1 : 1;
        }

        var locatedIssues = 0;
        foreach (var issue in model.Issues)
        {
            var source = issue.Where?.Sheet;
            if (!string.IsNullOrEmpty(source) && source != sheet.Id)
                continue;
            if (issue.Element != null && copies.Contains(issue.Element))
                continue;

            var at = issue.Where?.At;
            XYZ? elementAnchor = null;
            if (issue.Element is { } elementId)
                anchors.TryGetValue(elementId, out elementAnchor);
            var anchor = IsPoint(at) && (source == sheet.Id || elementAnchor != null || model.Sheets.Length == 1)
                ? point(at!)
                : elementAnchor;
            if (anchor == null)
                continue;

            painter.Cross(anchor, 1.0, issueStyle);
            painter.Note(anchor, issue.Id, 0.9, -1.0);
            locatedIssues++;
        }

        return new ReviewResult(grids, counts, locatedIssues);
    }

    private static bool IsStandardFloorCopy(ModelElement element) =>
        element.Evidence?.Any(evidence => evidence.StartsWith("standard_floor_reuse:", StringComparison.Ordinal)) == true;

    private static string ElementLabel(ModelElement element)
    {
        var mark = element.Category switch
        {
            "room" => element.Type?.Name,
            "door" or "window" or "wall_opening" or "shear_wall" => element.Type?.Mark,
            _ => null,
        };
        if (string.IsNullOrWhiteSpace(mark) || mark == element.Id)
            return element.Id;
        return element.Category == "room" ? $"{mark}\n{element.Id}" : $"{mark} / {element.Id}";
    }

    private static XYZ? DrawElement(ModelElement element, Painter painter, Func<double[], XYZ> point,
        GraphicsStyle style, ModelElement? host)
    {
        var geometry = element.Geometry;
        if (geometry == null)
            return null;

        var type = element.Type;
        if (geometry.Kind == "line" && geometry.Start is { } start && IsPoint(start)
            && geometry.End is { } end && IsPoint(end))
        {
            var first = point(start);
            var last = point(end);
            if (!painter.IsLongEnough(first, last))
                return null;
            if (type?.Thickness is double thickness && IsPositive(thickness))
            {
                var outline = OverlayGeometry.WallOutline(start, end, thickness);
                if (!painter.Outline(outline.Select(point).ToArray(), style))
                    return null;
            }
            else
            {
                painter.Segment(first, last, style);
            }

            return (first + last) * 0.5;
        }

        if (geometry.Kind == "polygon" && geometry.Points is { Length: >= 3 } vertices && vertices.All(IsPoint))
        {
            var points = vertices.Select(point).ToArray();
            if (!painter.Outline(points, style))
                return null;
            var positionInRoom = element.Category == "room" ? element.Parameters?.Seed ?? element.Parameters?.LabelPoint : null;
            return IsPoint(positionInRoom) ? point(positionInRoom!)
                : new XYZ(points.Average(p => p.X), points.Average(p => p.Y), points[0].Z);
        }

        if (geometry.Kind != "point" || geometry.At is not { } center || !IsPoint(center))
            return null;

        var position = point(center);
        if (element.Category == "column" && type?.Diameter is double diameter && IsPositive(diameter))
        {
            var onCircle = point(new[] { center[0] + diameter / 2, center[1] });
            return painter.Circle(position, onCircle, style) ? position : null;
        }

        if (element.Category == "column" && type?.Width is double width && type.Depth is double depth
            && IsPositive(width) && IsPositive(depth))
        {
            var angle = geometry.Rotation.GetValueOrDefault();
            if (double.IsNaN(angle) || double.IsInfinity(angle))
                return null;
            var outline = OverlayGeometry.ColumnOutline(center, width, depth, angle);
            return painter.Outline(outline.Select(point).ToArray(), style) ? position : null;
        }

        if (element.Category is "door" or "window" or "wall_opening"
            && type?.Width is double openingWidth && IsPositive(openingWidth)
            && host?.Geometry is { Kind: "line" } axis && IsPoint(axis.Start) && IsPoint(axis.End))
        {
            var first = point(axis.Start!);
            var last = point(axis.End!);
            if (painter.IsLongEnough(first, last))
            {
                var along = (last - first).Normalize();
                var midpoint = first + along * (position - first).DotProduct(along);
                var half = along * RevitApi.Feet(openingWidth / 2);
                if (painter.Segment(midpoint - half, midpoint + half, style))
                    return midpoint;
            }
        }

        painter.Cross(position, 0.6, style);
        return position;
    }

    private static bool IsPoint(double[]? point) =>
        point is { Length: >= 2 } && !double.IsNaN(point[0]) && !double.IsInfinity(point[0])
        && !double.IsNaN(point[1]) && !double.IsInfinity(point[1]);

    private static bool IsPositive(double value) => value > 0 && !double.IsInfinity(value);

    private static GraphicsStyle LineStyle(Document document, string name, Color color, int weight)
    {
        var categories = document.Settings.Categories;
        var lines = categories.get_Item(BuiltInCategory.OST_Lines);
        foreach (Category existing in lines.SubCategories)
        {
            if (existing.Name == name)
                return existing.GetGraphicsStyle(GraphicsStyleType.Projection);
        }

        var category = categories.NewSubcategory(lines, name);
        category.LineColor = color;
        category.SetLineWeight(weight, GraphicsStyleType.Projection);
        return category.GetGraphicsStyle(GraphicsStyleType.Projection);
    }

    private static ElementId ReviewTextType(Document document, string name, double millimeters)
    {
        var existing = new FilteredElementCollector(document).OfClass(typeof(TextNoteType))
            .Cast<TextNoteType>().FirstOrDefault(type => type.Name == name);
        if (existing != null)
            return existing.Id;

        var defaultId = document.GetDefaultElementTypeId(ElementTypeGroup.TextNoteType);
        if (document.GetElement(defaultId) is not TextNoteType defaultType)
            return defaultId;

        var reviewType = defaultType.Duplicate(name);
        var size = reviewType.get_Parameter(BuiltInParameter.TEXT_SIZE);
        if (size is { IsReadOnly: false, StorageType: StorageType.Double })
            size.Set(RevitApi.Feet(millimeters));
        var background = reviewType.get_Parameter(BuiltInParameter.TEXT_BACKGROUND);
        if (background is { IsReadOnly: false, StorageType: StorageType.Integer })
            background.Set((int)TextElementBackground.TBGR_TRANSPARENT);
        return reviewType.Id;
    }

    private sealed class Painter
    {
        private readonly Document document;
        private readonly ViewDrafting view;
        private readonly ElementId textType;
        private readonly double shortCurveTolerance;
        private ElementId? roomTextType;

        public Painter(Document document, ViewDrafting view)
        {
            this.document = document;
            this.view = view;
            textType = ReviewTextType(document, "TuMo 审图编号 1.5 mm", 1.5);
            shortCurveTolerance = document.Application.ShortCurveTolerance;
        }

        public bool IsLongEnough(XYZ first, XYZ last) => first.DistanceTo(last) > shortCurveTolerance;

        public bool Segment(XYZ first, XYZ last, GraphicsStyle style)
        {
            if (!IsLongEnough(first, last))
                return false;

            document.Create.NewDetailCurve(view, Line.CreateBound(first, last)).LineStyle = style;
            return true;
        }

        public bool Outline(XYZ[] points, GraphicsStyle style)
        {
            var drawn = false;
            for (var i = 0; i < points.Length; i++)
                drawn |= Segment(points[i], points[(i + 1) % points.Length], style);
            return drawn;
        }

        public bool Circle(XYZ center, XYZ onCircle, GraphicsStyle style)
        {
            var radius = center.DistanceTo(onCircle);
            if (Math.PI * radius <= shortCurveTolerance)
                return false;

            var right = view.RightDirection;
            var up = view.UpDirection;
            var first = Arc.Create(center, radius, 0, Math.PI, right, up);
            var second = Arc.Create(center, radius, Math.PI, 2 * Math.PI, right, up);
            document.Create.NewDetailCurve(view, first).LineStyle = style;
            document.Create.NewDetailCurve(view, second).LineStyle = style;
            return true;
        }

        public void Cross(XYZ anchor, double paperRadius, GraphicsStyle style)
        {
            var radius = RevitApi.Feet(paperRadius * view.Scale);
            var dx = view.RightDirection * radius;
            var dy = view.UpDirection * radius;
            Segment(anchor - dx - dy, anchor + dx + dy, style);
            Segment(anchor - dx + dy, anchor + dx - dy, style);
        }

        public void Note(XYZ anchor, string text, double paperDx, double paperDy)
        {
            if (textType == ElementId.InvalidElementId || string.IsNullOrWhiteSpace(text))
                return;

            var origin = anchor + view.RightDirection * RevitApi.Feet(paperDx * view.Scale)
                + view.UpDirection * RevitApi.Feet(paperDy * view.Scale);
            TextNote.Create(document, view.Id, origin, text, textType);
        }

        public void RoomNote(XYZ anchor, string text)
        {
            roomTextType ??= ReviewTextType(document, "TuMo 审图房名 1.8 mm", 1.8);
            if (roomTextType == ElementId.InvalidElementId || string.IsNullOrWhiteSpace(text))
                return;

            using var options = new TextNoteOptions(roomTextType) { HorizontalAlignment = HorizontalTextAlignment.Center };
            var origin = anchor - view.UpDirection * RevitApi.Feet(2.4 * view.Scale);
            TextNote.Create(document, view.Id, origin, text, options);
        }
    }
}
