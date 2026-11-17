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
        var openings = model.Elements.Where(e => e.Category is "door" or "window" or "wall_opening").ToArray();
        var interiors = model.Elements.Where(e => e.Category is "room" or "slab" or "slab_opening").ToArray();
        var beams = model.Elements.Where(e => e.Category == "beam").ToArray();
        if (model.Grids.Length == 0 && candidates.Length == 0 && interiors.Length == 0 && beams.Length == 0)
            throw new InvalidOperationException("这张图没有识别出可生成的构件。请查看疑点报告。");

        RejectCollisions(document, candidates.Concat(openings).Concat(interiors).Concat(beams));
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

            // Give the new grids room for plan cut planes above and below the generated levels.
            var gridPadding = RevitApi.Feet(2000);
            var gridExtents = (Bottom: defaultBase.ProjectElevation - gridPadding,
                Top: levels.Values.Max(level => level.ProjectElevation) + gridPadding);
            foreach (var grid in model.Grids)
                CreateGrid(document, grid, existingGrids, ids, model, sheets, gridExtents);

            var types = new ModelTypes(document, log);
            var dimensions = candidates.ToDictionary(e => e.Id, e => GetExtent(e, levels, defaultBase));
            types.Prepare(candidates);
            document.Regenerate();
            var originNotice = ProjectOrigin.Verify(document, model, existingGrids, log);

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
            var openingReport = CreateOpenings(document, model, openings, created, levels, dimensions, sheets, ids, log);
            var interiorReport = CreateRoomsAndSlabs(document, model, interiors, created, levels, sheets, ids, log);
            var beamReport = CreateBeams(document, model, beams, created, levels, sheets, ids, log);
            if (transaction.Commit() != TransactionStatus.Committed)
                throw new InvalidOperationException("Revit 无法提交模型。请查看日志。");
            return $"标高 {levels.Count}，轴网 {model.Grids.Length}，柱 {candidates.Count(e => e.Category == "column")}，墙 {candidates.Count(e => e.Category == "wall")}。"
                + openingReport + interiorReport + beamReport + (originNotice == null ? "" : $"\n{originNotice}");
        }
        catch
        {
            if (transaction.GetStatus() == TransactionStatus.Started)
                transaction.RollBack();
            throw;
        }
    }

    private static string CreateBeams(Document document, TuMoModel model, ModelElement[] beams,
        Dictionary<string, Element> created, Dictionary<string, Level> levels, Dictionary<string, Sheet> sheets,
        TuMoIds ids, RunLog log)
    {
        if (beams.Length == 0)
            return "";
        var builder = new BeamPlacement(document, log);
        var placed = 0;
        foreach (var source in beams)
        {
            using var change = new SubTransaction(document);
            if (change.Start() != TransactionStatus.Started)
                throw new InvalidOperationException("不能开始结构梁子事务。");
            try
            {
                if (created.ContainsKey(source.Id))
                    throw new InvalidOperationException("TuMo 编号重复。");
                if (source.BaseLevel == null || !levels.TryGetValue(source.BaseLevel, out var level))
                    throw new InvalidOperationException("梁缺少项目中的楼层标高。");
                if (source.BaseOffset is not { } offset || double.IsNaN(offset) || double.IsInfinity(offset))
                    throw new InvalidOperationException("梁缺少明确的相对梁顶标高。");
                if (source.Geometry is not { Kind: "line" } geometry)
                    throw new InvalidOperationException("梁需要直线几何。");
                var transform = TransformFor(source.Source, sheets);
                var elevation = level.ProjectElevation + RevitApi.Feet(offset);
                var a = Point(geometry.Start, elevation, model, transform);
                var b = Point(geometry.End, elevation, model, transform);
                if (a.DistanceTo(b) <= document.Application.ShortCurveTolerance)
                    throw new InvalidOperationException("梁的长度低于 Revit 最小曲线长度。");
                var result = builder.Create(source, level, Line.CreateBound(a, b));
                SetMark(result, source.Type?.Mark);
                ids.Set(result, source.Id);
                if (change.Commit() != TransactionStatus.Committed)
                    throw new InvalidOperationException("Revit 未能提交结构梁。");
                created.Add(source.Id, result);
                placed++;
                log.Write($"生成 {source.Id}，Revit ID {RevitApi.IdValue(result.Id)}。");
            }
            catch (Exception error) when (ModelOpenings.CanSkip(error))
            {
                log.Write($"跳过梁 {source.Id}：{error.Message}");
            }
            finally
            {
                if (change.GetStatus() == TransactionStatus.Started)
                    change.RollBack();
            }
        }
        return $"梁 {placed}，跳过 {beams.Length - placed}。";
    }

    private static string CreateOpenings(Document document, TuMoModel model, ModelElement[] openings,
        Dictionary<string, Element> created, Dictionary<string, Level> levels, Dictionary<string, Extent> dimensions,
        Dictionary<string, Sheet> sheets, TuMoIds ids, RunLog log)
    {
        if (openings.Length == 0)
            return "";

        var builder = new ModelOpenings(document, log);
        var counts = new Dictionary<string, int>(StringComparer.Ordinal) { ["door"] = 0, ["window"] = 0, ["wall_opening"] = 0 };
        var attempted = new HashSet<string>(created.Keys, StringComparer.Ordinal);
        var review = 0;
        foreach (var source in openings)
        {
            if (!attempted.Add(source.Id))
            {
                log.Write($"跳过门窗或墙洞 {source.Id}：TuMo 编号重复。");
                continue;
            }

            using var change = new SubTransaction(document);
            if (change.Start() != TransactionStatus.Started)
                throw new InvalidOperationException("不能开始门窗或墙洞子事务。");
            try
            {
                if (source.Host == null || !created.TryGetValue(source.Host, out var element) || element is not Wall wall)
                    throw new InvalidOperationException($"找不到宿主墙 {source.Host ?? "（无）"}。");
                var hostExtent = dimensions[source.Host];
                var level = source.BaseLevel == null ? hostExtent.Base : levels.TryGetValue(source.BaseLevel, out var found)
                    ? found : throw new InvalidOperationException($"找不到底标高 {source.BaseLevel}。");
                var geometry = source.Geometry;
                if (geometry?.Kind != "point")
                    throw new InvalidOperationException("需要有效的点几何。");

                var transform = TransformFor(source.Source, sheets);
                var center = Point(geometry.At, level.ProjectElevation, model, transform);
                var facing = geometry.Facing == null ? null : Point(geometry.Facing, 0, model, transform)
                    - Point(new[] { 0d, 0d }, 0, model, transform);
                var bottom = hostExtent.Elevation + hostExtent.BaseOffset;
                var (result, needsReview) = builder.Create(source, wall, level, center, facing, bottom, bottom + hostExtent.Height);
                SetMark(result, source.Type?.Mark);
                ids.Set(result, source.Id);
                if (change.Commit() != TransactionStatus.Committed)
                    throw new InvalidOperationException("Revit 未能提交该构件。");

                created.Add(source.Id, result);
                counts[source.Category]++;
                if (needsReview)
                    review++;
                log.Write($"生成 {source.Id}，Revit ID {RevitApi.IdValue(result.Id)}，宿主 {source.Host}。");
            }
            catch (Exception error) when (ModelOpenings.CanSkip(error))
            {
                log.Write($"跳过门窗或墙洞 {source.Id}：{error.Message}");
            }
            finally
            {
                if (change.GetStatus() == TransactionStatus.Started)
                    change.RollBack();
            }
        }

        var skipped = openings.Length - counts.Values.Sum();
        return $"门 {counts["door"]}，窗 {counts["window"]}，墙洞 {counts["wall_opening"]}，待核对 {review}，跳过 {skipped}。";
    }

    private static string CreateRoomsAndSlabs(Document document, TuMoModel model, ModelElement[] sources,
        Dictionary<string, Element> created, Dictionary<string, Level> levels, Dictionary<string, Sheet> sheets,
        TuMoIds ids, RunLog log)
    {
        if (sources.Length == 0)
            return "";

        var counts = new Dictionary<string, int>(StringComparer.Ordinal);
        foreach (var category in new[] { "slab", "slab_opening", "room" })
        {
            counts[category] = 0;
            foreach (var source in sources.Where(item => item.Category == category))
            {
                using var change = new SubTransaction(document);
                if (change.Start() != TransactionStatus.Started)
                    throw new InvalidOperationException("不能开始楼板、板洞或房间子事务。");
                try
                {
                    if (created.ContainsKey(source.Id))
                        throw new InvalidOperationException("TuMo 编号重复。");
                    if (source.BaseLevel == null || !levels.TryGetValue(source.BaseLevel, out var level))
                        throw new InvalidOperationException($"找不到底标高 {source.BaseLevel ?? "（无）"}。");

                    var transform = TransformFor(source.Source, sheets);
                    var z = level.ProjectElevation;
                    Element result;
                    if (category == "slab")
                    {
                        result = SlabPlacement.Create(document, source, level, Outline(document, source, z, model, transform));
                        SetMark(result, source.Type?.Mark);
                        document.Regenerate();
                    }
                    else if (category == "slab_opening")
                    {
                        if (source.Host == null || !created.TryGetValue(source.Host, out var element) || element is not Floor floor
                            || floor.LevelId != level.Id)
                            throw new InvalidOperationException($"找不到同层宿主楼板 {source.Host ?? "（无）"}。");
                        z += floor.get_Parameter(BuiltInParameter.FLOOR_HEIGHTABOVELEVEL_PARAM).AsDouble();
                        result = SlabPlacement.CreateOpening(document, floor, Outline(document, source, z, model, transform));
                    }
                    else
                    {
                        var seed = Point(source.Parameters?.Seed ?? source.Parameters?.LabelPoint, z, model, transform);
                        var upper = source.TopLevel != null && levels.TryGetValue(source.TopLevel, out var top) ? top : null;
                        result = RoomPlacement.Create(document, source, level, upper, seed);
                    }

                    ids.Set(result, source.Id);
                    if (change.Commit() != TransactionStatus.Committed)
                        throw new InvalidOperationException("Revit 未能提交该构件。");
                    created.Add(source.Id, result);
                    counts[category]++;
                    log.Write($"生成 {source.Id}，Revit ID {RevitApi.IdValue(result.Id)}。");
                }
                catch (Exception error) when (ModelOpenings.CanSkip(error))
                {
                    log.Write($"跳过楼板、板洞或房间 {source.Id}：{error.Message}");
                }
                finally
                {
                    if (change.GetStatus() == TransactionStatus.Started)
                        change.RollBack();
                }
            }
        }

        return $"房间 {counts["room"]}，楼板 {counts["slab"]}，板洞 {counts["slab_opening"]}，跳过 {sources.Length - counts.Values.Sum()}。";
    }

    private static CurveLoop Outline(Document document, ModelElement source, double elevation, TuMoModel model, SheetTransform? transform)
    {
        if (source.Geometry is not { Kind: "polygon", Points: { Length: >= 3 } vertices })
            throw new InvalidOperationException("需要至少三个顶点的闭合轮廓。");
        var points = vertices.Select(vertex => Point(vertex, elevation, model, transform)).ToList();
        if (points[0].IsAlmostEqualTo(points[points.Count - 1]))
            points.RemoveAt(points.Count - 1);
        if (points.Count < 3)
            throw new InvalidOperationException("轮廓缺少有效顶点。");
        var curves = new List<Curve>(points.Count);
        for (var index = 0; index < points.Count; index++)
        {
            var start = points[index];
            var end = points[(index + 1) % points.Count];
            if (start.DistanceTo(end) <= document.Application.ShortCurveTolerance)
                throw new InvalidOperationException("轮廓包含过短的边。");
            curves.Add(Line.CreateBound(start, end));
        }
        return CurveLoop.Create(curves);
    }

    private static void RejectCollisions(Document document, IEnumerable<ModelElement> candidates)
    {
        var ids = new HashSet<string>(candidates.Select(element => element.Id), StringComparer.Ordinal);
        if (ids.Count == 0)
            return;
        var categories = new[] { BuiltInCategory.OST_Walls, BuiltInCategory.OST_StructuralColumns,
            BuiltInCategory.OST_Doors, BuiltInCategory.OST_Windows, BuiltInCategory.OST_Rooms, BuiltInCategory.OST_Floors,
            BuiltInCategory.OST_StructuralFraming };
        var existing = new FilteredElementCollector(document).WhereElementIsNotElementType()
            .WherePasses(new LogicalOrFilter(new ElementMulticategoryFilter(categories), new ElementClassFilter(typeof(Opening))));
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
                {
                    var plan = ViewPlan.Create(document, planType.Id, level.Id);
                    var discipline = plan.get_Parameter(BuiltInParameter.VIEW_DISCIPLINE);
                    if (discipline is { IsReadOnly: false } && plan.Discipline == ViewDiscipline.Structural &&
                        (document.GetElement(plan.ViewTemplateId) is not View template ||
                         template.GetNonControlledTemplateParameterIds().Contains(discipline.Id)))
                    {
                        plan.Discipline = ViewDiscipline.Coordination;
                    }
                }
            }
            ids.Set(level, data.Id);
            levels.Add(data.Id, level);
        }
        return levels;
    }

    private static void CreateGrid(Document document, ModelGrid data, Dictionary<string, Grid> existing,
        TuMoIds ids, TuMoModel model, Dictionary<string, Sheet> sheets, (double Bottom, double Top) verticalExtents)
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
            grid.SetVerticalExtents(verticalExtents.Bottom, verticalExtents.Top);
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
