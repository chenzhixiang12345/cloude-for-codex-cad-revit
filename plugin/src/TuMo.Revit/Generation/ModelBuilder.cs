using System;
using System.Collections.Generic;
using System.Linq;
using Autodesk.Revit.DB;
using Autodesk.Revit.DB.Structure;
using TuMo.Core;
using TuMo.Revit.Compat;
using TuMo.Revit.Logging;

namespace TuMo.Revit.Generation;

internal static class ModelBuilder
{
    public static string Build(Document document, TuMoModel model, RunLog log)
    {
        var candidates = model.Elements.Where(e => e.Category is "column" or "wall").ToArray();
        if (model.Grids.Length == 0 && candidates.Length == 0)
            throw new InvalidOperationException("这张图没有识别出轴网、柱或墙。请查看疑点报告。");

        RejectCollisions(document, candidates);
        var sheets = model.Sheets.ToDictionary(s => s.Id, StringComparer.Ordinal);
        using var transaction = new Transaction(document, "TuMo 生成模型");
        transaction.Start();
        var options = transaction.GetFailureHandlingOptions();
        options.SetFailuresPreprocessor(new LoggedFailures(log));
        options.SetClearAfterRollback(true);
        transaction.SetFailureHandlingOptions(options);
        try
        {
            var ids = TuMoIds.Prepare(document, log);
            var levels = CreateLevels(document, model, ids);
            var defaultBase = levels.Values.OrderBy(level => level.ProjectElevation).First();
            var existingGrids = new FilteredElementCollector(document).OfClass(typeof(Grid))
                .Cast<Grid>().ToDictionary(grid => grid.Name, StringComparer.OrdinalIgnoreCase);

            foreach (var grid in model.Grids)
                CreateGrid(document, grid, existingGrids, ids, model, sheets);

            var types = new ModelTypes(document, log);
            var dimensions = candidates.ToDictionary(e => e.Id, e => GetExtent(e, levels, defaultBase));
            types.Prepare(candidates);
            document.Regenerate();

            var created = new Dictionary<string, Element>(StringComparer.Ordinal);
            foreach (var source in candidates)
            {
                var extent = dimensions[source.Id];
                var transform = TransformFor(source.Source, sheets);
                Element result = source.Category == "column"
                    ? CreateColumn(document, source, extent, types.Column(source), model, transform)
                    : CreateWall(document, source, extent, types.Wall(source), model, transform);
                ids.Set(result, source.Id);
                created.Add(source.Id, result);
                log.Write($"生成 {source.Id}，Revit ID {RevitApi.IdValue(result.Id)}");
            }

            document.Regenerate();
            JoinGeometry.Connect(document, created, model.Relations, log);
            if (transaction.Commit() != TransactionStatus.Committed)
                throw new InvalidOperationException("Revit 无法提交模型。请查看日志。");
            return $"标高 {levels.Count}，轴网 {model.Grids.Length}，柱 {candidates.Count(e => e.Category == "column")}，墙 {candidates.Count(e => e.Category == "wall")}。";
        }
        catch
        {
            if (transaction.GetStatus() == TransactionStatus.Started)
                transaction.RollBack();
            throw;
        }
    }

    private static void RejectCollisions(Document document, ModelElement[] candidates)
    {
        var ids = new HashSet<string>(candidates.Select(element => element.Id), StringComparer.Ordinal);
        if (ids.Count == 0)
            return;
        var categories = new[] { BuiltInCategory.OST_Walls, BuiltInCategory.OST_StructuralColumns };
        var existing = new FilteredElementCollector(document).WhereElementIsNotElementType()
            .WherePasses(new ElementMulticategoryFilter(categories));
        foreach (var element in existing)
        {
            var id = TuMoIds.Get(element);
            if (id != null && ids.Contains(id))
                throw new InvalidOperationException($"项目已包含构件 {id}。请在空白项目中测试，或撤销前一次生成。");
        }
    }

    private static Dictionary<string, Level> CreateLevels(Document document, TuMoModel model, TuMoIds ids)
    {
        var levels = new Dictionary<string, Level>(StringComparer.Ordinal);
        var existing = new FilteredElementCollector(document).OfClass(typeof(Level)).Cast<Level>().ToList();
        var requested = model.Levels.Length > 0 ? model.Levels : new[]
        {
            new ModelLevel { Id = "L-TUMO-BASE", Name = "TuMo 参考标高", ElevationArch = 0 }
        };
        var planType = new FilteredElementCollector(document).OfClass(typeof(ViewFamilyType)).Cast<ViewFamilyType>()
            .FirstOrDefault(type => type.ViewFamily == ViewFamily.FloorPlan);
        foreach (var data in requested.OrderBy(level => level.ElevationArch ?? level.ElevationStruct))
        {
            var elevation = RevitApi.Feet(data.ElevationArch ?? data.ElevationStruct ??
                throw new InvalidOperationException($"标高 {data.Id} 缺少高度。"));
            var level = existing.FirstOrDefault(value => value.Name.Equals(data.Name, StringComparison.OrdinalIgnoreCase));
            if (level != null && Math.Abs(level.ProjectElevation - elevation) > RevitApi.Feet(1))
                throw new InvalidOperationException($"项目已有同名标高“{data.Name}”，高度和识别结果不同。请使用空白项目测试。");

            if (level == null)
            {
                level = Level.Create(document, elevation);
                level.Name = data.Name;
                existing.Add(level);
                if (planType != null)
                    ViewPlan.Create(document, planType.Id, level.Id);
            }
            ids.Set(level, data.Id);
            levels.Add(data.Id, level);
        }
        return levels;
    }

    private static void CreateGrid(Document document, ModelGrid data, Dictionary<string, Grid> existing,
        TuMoIds ids, TuMoModel model, Dictionary<string, Sheet> sheets)
    {
        var transform = TransformFor(data.Source, sheets);
        var line = Line.CreateBound(Point(data.Start, 0, model, transform), Point(data.End, 0, model, transform));
        if (existing.TryGetValue(data.Label, out var grid))
        {
            if (grid.Curve is not Line prior || !SameAxis(prior, line))
                throw new InvalidOperationException($"项目已有不同位置的轴网“{data.Label}”。请使用空白项目测试。");
        }
        else
        {
            grid = Grid.Create(document, line);
            grid.Name = data.Label;
            existing.Add(data.Label, grid);
        }

        ids.Set(grid, data.Id);
    }

    private static bool SameAxis(Line first, Line second)
    {
        var origin = first.GetEndPoint(0);
        var direction = first.Direction;
        var tolerance = RevitApi.Feet(1);
        return direction.CrossProduct(second.GetEndPoint(0) - origin).GetLength() < tolerance
            && direction.CrossProduct(second.GetEndPoint(1) - origin).GetLength() < tolerance;
    }

    private static Extent GetExtent(ModelElement source, Dictionary<string, Level> levels, Level defaultBase)
    {
        var baseLevel = source.BaseLevel == null ? defaultBase : levels[source.BaseLevel];
        var topLevel = source.TopLevel == null ? null : levels[source.TopLevel];
        var baseOffset = RevitApi.Feet(source.BaseOffset ?? 0);
        var topOffset = RevitApi.Feet(source.TopOffset ?? 0);
        var elevation = baseLevel.ProjectElevation;
        var height = topLevel == null
            ? RevitApi.Feet(source.Category == "wall" ? source.Type?.Height ?? 3000 : 3000)
            : topLevel.ProjectElevation + topOffset - elevation - baseOffset;
        if (height <= RevitApi.Feet(1))
            throw new InvalidOperationException($"构件 {source.Id} 的高度无效。");
        return new Extent(baseLevel, topLevel, elevation, height, baseOffset, topOffset);
    }

    private static FamilyInstance CreateColumn(Document document, ModelElement source, Extent extent,
        FamilySymbol symbol, TuMoModel model, SheetTransform? transform)
    {
        var geometry = source.Geometry ?? throw new InvalidOperationException($"柱 {source.Id} 缺少几何。");
        if (geometry.Kind != "point")
            throw new InvalidOperationException($"柱 {source.Id} 需要点几何。");
        var point = Point(geometry.At, extent.Elevation, model, transform);
        var column = document.Create.NewFamilyInstance(point, symbol, extent.Base, StructuralType.Column);
        Set(column, BuiltInParameter.FAMILY_BASE_LEVEL_PARAM, extent.Base.Id);
        Set(column, BuiltInParameter.FAMILY_BASE_LEVEL_OFFSET_PARAM, extent.BaseOffset);
        Set(column, BuiltInParameter.FAMILY_TOP_LEVEL_PARAM, (extent.Top ?? extent.Base).Id);
        Set(column, BuiltInParameter.FAMILY_TOP_LEVEL_OFFSET_PARAM,
            extent.Top == null ? extent.BaseOffset + extent.Height : extent.TopOffset);

        var rotation = ((geometry.Rotation ?? 0) + (transform?.Rotation ?? 0)) * Math.PI / 180;
        if (Math.Abs(rotation) > 1e-9)
            ElementTransformUtils.RotateElement(document, column.Id, Line.CreateBound(point, point + XYZ.BasisZ), rotation);
        SetMark(column, source.Type?.Mark);
        return column;
    }

    private static Wall CreateWall(Document document, ModelElement source, Extent extent, WallType type,
        TuMoModel model, SheetTransform? transform)
    {
        var geometry = source.Geometry ?? throw new InvalidOperationException($"墙 {source.Id} 缺少几何。");
        if (geometry.Kind != "line")
            throw new InvalidOperationException($"墙 {source.Id} 需要线几何。");
        var line = Line.CreateBound(Point(geometry.Start, extent.Elevation, model, transform),
            Point(geometry.End, extent.Elevation, model, transform));
        var wall = Autodesk.Revit.DB.Wall.Create(document, line, type.Id, extent.Base.Id, extent.Height,
            extent.BaseOffset, false, false);
        if (extent.Top != null)
        {
            Set(wall, BuiltInParameter.WALL_HEIGHT_TYPE, extent.Top.Id);
            Set(wall, BuiltInParameter.WALL_TOP_OFFSET, extent.TopOffset);
        }
        SetMark(wall, source.Type?.Mark);
        return wall;
    }

    private static void SetMark(Element element, string? mark)
    {
        if (!string.IsNullOrWhiteSpace(mark) &&
            element.get_Parameter(BuiltInParameter.ALL_MODEL_MARK) is { IsReadOnly: false } parameter)
            parameter.Set(mark);
    }

    private static void Set(Element element, BuiltInParameter key, ElementId value)
    {
        var parameter = element.get_Parameter(key);
        if (parameter == null || (parameter.AsElementId() != value && (parameter.IsReadOnly || !parameter.Set(value))))
            throw new InvalidOperationException($"不能设置构件参数 {key}。");
    }

    private static void Set(Element element, BuiltInParameter key, double value)
    {
        var parameter = element.get_Parameter(key);
        if (parameter == null || (Math.Abs(parameter.AsDouble() - value) > 1e-9 && (parameter.IsReadOnly || !parameter.Set(value))))
            throw new InvalidOperationException($"不能设置构件参数 {key}。");
    }

    private static SheetTransform? TransformFor(DrawingSource? source, Dictionary<string, Sheet> sheets) =>
        source?.Sheet != null && sheets.TryGetValue(source.Sheet, out var sheet) ? sheet.Transform : null;

    private static XYZ Point(double[]? coordinates, double z, TuMoModel model, SheetTransform? transform)
    {
        if (coordinates?.Length != 2 || coordinates.Any(value => double.IsInfinity(value) || double.IsNaN(value)))
            throw new InvalidOperationException("模型坐标必须是两个有限数值。");
        var angle = (transform?.Rotation ?? 0) * Math.PI / 180;
        var origin = model.Project.Origin?.At;
        var x = Math.Cos(angle) * coordinates[0] - Math.Sin(angle) * coordinates[1] + (transform?.Dx ?? 0);
        var y = Math.Sin(angle) * coordinates[0] + Math.Cos(angle) * coordinates[1] + (transform?.Dy ?? 0);
        return new XYZ(RevitApi.Feet(x - (origin?[0] ?? 0)), RevitApi.Feet(y - (origin?[1] ?? 0)), z);
    }

    private sealed class Extent
    {
        public Level Base { get; }
        public Level? Top { get; }
        public double Elevation { get; }
        public double Height { get; }
        public double BaseOffset { get; }
        public double TopOffset { get; }

        public Extent(Level baseLevel, Level? top, double elevation, double height, double baseOffset, double topOffset)
        {
            Base = baseLevel;
            Top = top;
            Elevation = elevation;
            Height = height;
            BaseOffset = baseOffset;
            TopOffset = topOffset;
        }
    }
}
