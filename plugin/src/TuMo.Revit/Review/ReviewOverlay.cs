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
    public int Grids { get; }
    public int Columns { get; }
    public int Walls { get; }
    public int LocatedIssues { get; }

    public ReviewResult(int grids, int columns, int walls, int locatedIssues)
    {
        Grids = grids;
        Columns = columns;
        Walls = walls;
        LocatedIssues = locatedIssues;
    }
}

internal static class ReviewOverlay
{
    public static ReviewResult Draw(Document document, ViewDrafting view, TuMoModel model, Sheet sheet,
        Func<double[], XYZ> point, RunLog log)
    {
        var painter = new Painter(document, view);
        var gridStyle = LineStyle(document, "TuMo 审图 - 轴网", new Color(37, 99, 166), 2);
        var wallStyle = LineStyle(document, "TuMo 审图 - 墙", new Color(8, 120, 105), 3);
        var columnStyle = LineStyle(document, "TuMo 审图 - 柱", new Color(154, 52, 18), 3);
        var issueStyle = LineStyle(document, "TuMo 审图 - 疑点", new Color(185, 28, 28), 4);
        var anchors = new Dictionary<string, XYZ>(StringComparer.Ordinal);

        var defaultSheetId = model.Sheets.FirstOrDefault()?.Id;
        bool OnSelectedSheet(DrawingSource? source) => (source?.Sheet ?? defaultSheetId) == sheet.Id;

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

        var columns = 0;
        var walls = 0;
        foreach (var element in model.Elements)
        {
            if (!OnSelectedSheet(element.Source))
                continue;

            var isColumn = element.Category == "column";
            if (!isColumn && element.Category != "wall")
                continue;

            var style = isColumn ? columnStyle : wallStyle;
            var anchor = DrawElement(element, painter, point, style);
            if (anchor == null)
            {
                log.Write($"审图跳过无效或过小的构件 {element.Id}。");
                continue;
            }

            anchors[element.Id] = anchor;
            painter.Note(anchor, element.Id, 0.6, 0.7);
            if (isColumn)
                columns++;
            else
                walls++;
        }

        var locatedIssues = 0;
        foreach (var issue in model.Issues)
        {
            var source = issue.Where?.Sheet;
            if (!string.IsNullOrEmpty(source) && source != sheet.Id)
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

        return new ReviewResult(grids, columns, walls, locatedIssues);
    }

    private static XYZ? DrawElement(ModelElement element, Painter painter, Func<double[], XYZ> point,
        GraphicsStyle style)
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
            return painter.Outline(points, style)
                ? new XYZ(points.Average(p => p.X), points.Average(p => p.Y), points[0].Z)
                : null;
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

    private static ElementId ReviewTextType(Document document)
    {
        const string name = "TuMo 审图编号 1.5 mm";
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
            size.Set(RevitApi.Feet(1.5));
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

        public Painter(Document document, ViewDrafting view)
        {
            this.document = document;
            this.view = view;
            textType = ReviewTextType(document);
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
    }
}
