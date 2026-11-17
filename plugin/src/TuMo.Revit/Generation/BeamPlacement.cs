using System;
using System.Globalization;
using System.IO;
using System.Linq;
using Autodesk.Revit.DB;
using Autodesk.Revit.DB.Structure;
using Autodesk.Revit.DB.Structure.StructuralSections;
using TuMo.Core;
using TuMo.Revit.Compat;
using TuMo.Revit.Logging;
using ElementType = TuMo.Core.ElementType;

namespace TuMo.Revit.Generation;

internal sealed class BeamPlacement
{
    private static readonly string[] StandardFamilies =
    {
        "混凝土 - 矩形梁", "混凝土-矩形梁", "Concrete-Rectangular Beam", "M_Concrete-Rectangular Beam",
        "Concrete - Rectangular Beam", "M_Concrete - Rectangular Beam"
    };

    private readonly Document document;
    private readonly RunLog log;
    private string[]? libraryFiles;

    public BeamPlacement(Document document, RunLog log)
    {
        this.document = document;
        this.log = log;
    }

    public FamilyInstance Create(ModelElement source, Level level, Line location)
    {
        var data = source.Type ?? throw new InvalidOperationException("梁缺少截面。");
        var width = Dimension(data.Width, "宽度");
        var height = Dimension(data.Height, "高度");
        var type = ResolveType(data, width, height);
        if (!type.IsActive)
            type.Activate();
        var beam = document.Create.NewFamilyInstance(location, type, level, StructuralType.Beam)
            ?? throw new InvalidOperationException("Revit 未能放置梁族。");

        StructuralFramingUtils.DisallowJoinAtEnd(beam, 0);
        StructuralFramingUtils.DisallowJoinAtEnd(beam, 1);
        Set(beam, BuiltInParameter.YZ_JUSTIFICATION, (int)YZJustificationOption.Uniform);
        Set(beam, BuiltInParameter.Y_JUSTIFICATION, (int)YJustification.Center);
        Set(beam, BuiltInParameter.Z_JUSTIFICATION, (int)ZJustification.Top);
        Clear(beam, BuiltInParameter.Y_OFFSET_VALUE);
        Clear(beam, BuiltInParameter.Z_OFFSET_VALUE);
        Clear(beam, BuiltInParameter.START_EXTENSION);
        Clear(beam, BuiltInParameter.END_EXTENSION);

        document.Regenerate();
        var bounds = beam.get_BoundingBox(null);
        var tolerance = RevitApi.Feet(1);
        var sectionNormal = XYZ.BasisZ.CrossProduct(location.Direction) * (width / 2);
        var start = location.GetEndPoint(0);
        var end = location.GetEndPoint(1);
        var lateralX = Math.Abs(sectionNormal.X);
        var lateralY = Math.Abs(sectionNormal.Y);
        if (beam.Location is not LocationCurve { Curve: Line actual } ||
            !(EndpointError(location, actual) <= tolerance) || bounds == null ||
            !(Math.Abs(bounds.Max.Z - start.Z) <= tolerance) ||
            !(Math.Abs(bounds.Min.Z - (start.Z - height)) <= tolerance) ||
            !(Math.Abs(bounds.Min.X - (Math.Min(start.X, end.X) - lateralX)) <= tolerance) ||
            !(Math.Abs(bounds.Max.X - (Math.Max(start.X, end.X) + lateralX)) <= tolerance) ||
            !(Math.Abs(bounds.Min.Y - (Math.Min(start.Y, end.Y) - lateralY)) <= tolerance) ||
            !(Math.Abs(bounds.Max.Y - (Math.Max(start.Y, end.Y) + lateralY)) <= tolerance))
            throw new InvalidOperationException("梁族的实体或定位线与图纸给定的端点、梁顶标高或截面不符。");
        return beam;
    }

    private FamilySymbol ResolveType(ElementType data, double width, double height)
    {
        var symbols = new FilteredElementCollector(document).OfClass(typeof(FamilySymbol))
            .OfCategory(BuiltInCategory.OST_StructuralFraming).Cast<FamilySymbol>().Where(IsRectangularBeam)
            .OrderByDescending(symbol => string.Equals(symbol.Name, data.Name, StringComparison.OrdinalIgnoreCase))
            .ThenBy(symbol => LocalFamilyLibrary.Preference(StandardFamilies, symbol.FamilyName))
            .ThenBy(symbol => symbol.FamilyName, StringComparer.OrdinalIgnoreCase)
            .ThenBy(symbol => symbol.Name, StringComparer.OrdinalIgnoreCase).ToArray();
        if (MatchType(symbols, data, width, height) is { } existing)
            return existing;

        libraryFiles ??= LocalFamilyLibrary.Find(document.Application, StandardFamilies, log);
        foreach (var path in libraryFiles)
        {
            if (symbols.Any(symbol => string.Equals(symbol.FamilyName, Path.GetFileNameWithoutExtension(path),
                    StringComparison.OrdinalIgnoreCase)))
                continue;
            using var load = new SubTransaction(document);
            if (load.Start() != TransactionStatus.Started)
                throw new InvalidOperationException("不能开始载入矩形梁族。");
            try
            {
                if (!document.LoadFamily(path, out var family))
                    continue;
                var loaded = family.GetFamilySymbolIds().Select(document.GetElement).OfType<FamilySymbol>()
                    .Where(IsRectangularBeam).ToArray();
                if (MatchType(loaded, data, width, height) is not { } found ||
                    load.Commit() != TransactionStatus.Committed)
                    continue;
                log.Write($"从 Revit 本地标准库载入矩形混凝土梁族 {family.Name}。");
                return found;
            }
            catch (Exception error) when (ModelOpenings.CanSkip(error))
            {
                log.Write($"未能使用本地梁族 {Path.GetFileName(path)}：{error.Message}");
            }
            finally
            {
                if (load.GetStatus() == TransactionStatus.Started)
                    load.RollBack();
            }
        }
        throw new InvalidOperationException("缺少尺寸匹配的矩形混凝土梁族。请从 Revit 标准库载入矩形混凝土梁族或使用结构项目样板。");
    }

    private FamilySymbol? MatchType(FamilySymbol[] symbols, ElementType data, double width, double height)
    {
        var materialName = data.Material;
        var material = string.IsNullOrWhiteSpace(materialName) ? null :
            new FilteredElementCollector(document).OfClass(typeof(Material)).Cast<Material>()
                .FirstOrDefault(candidate => candidate.Name.Equals(materialName, StringComparison.OrdinalIgnoreCase));
        var section = symbols.FirstOrDefault(symbol => Matches(Width(symbol), width) && Matches(Height(symbol), height)
            && (string.IsNullOrWhiteSpace(materialName) ||
                (material != null && symbol.get_Parameter(BuiltInParameter.STRUCTURAL_MATERIAL_PARAM)?.AsElementId() == material.Id)));
        if (section != null)
            return section;

        foreach (var symbol in symbols)
        {
            var b = Width(symbol);
            var h = Height(symbol);
            if (b == null || h == null || b.Id == h.Id ||
                (b.IsReadOnly && !Matches(b, width)) || (h.IsReadOnly && !Matches(h, height)))
                continue;
            using var change = new SubTransaction(document);
            if (change.Start() != TransactionStatus.Started)
                throw new InvalidOperationException("不能开始创建梁类型。");
            try
            {
                var name = ModelTypes.AvailableName($"TuMo-矩形梁-{Number(data.Width)}x{Number(data.Height)}",
                    symbol.Family.GetFamilySymbolIds().Select(document.GetElement).OfType<FamilySymbol>()
                        .Select(other => other.Name));
                var copy = (FamilySymbol)symbol.Duplicate(name);
                if (!Apply(Width(copy), width) || !Apply(Height(copy), height) ||
                    !Matches(Width(copy), width) || !Matches(Height(copy), height))
                    continue;
                if (!string.IsNullOrWhiteSpace(materialName))
                {
                    var materialParameter = copy.get_Parameter(BuiltInParameter.STRUCTURAL_MATERIAL_PARAM);
                    if (materialParameter is not { StorageType: StorageType.ElementId })
                        continue;
                    if (material == null || materialParameter.AsElementId() != material.Id)
                    {
                        if (materialParameter.IsReadOnly || !materialParameter.Set(material?.Id ?? Material.Create(document, materialName!)))
                            continue;
                    }
                }
                if (change.Commit() != TransactionStatus.Committed)
                    continue;
                log.Write($"创建梁类型 {copy.FamilyName} / {copy.Name}。");
                return copy;
            }
            catch (Exception error) when (ModelOpenings.CanSkip(error))
            {
                log.Write($"梁族 {symbol.FamilyName} / {symbol.Name} 无法调整为所需截面：{error.Message}");
            }
            finally
            {
                if (change.GetStatus() == TransactionStatus.Started)
                    change.RollBack();
            }
        }
        return null;
    }

    private static bool IsRectangularBeam(FamilySymbol symbol) =>
        symbol.Family.FamilyPlacementType == FamilyPlacementType.CurveDrivenStructural &&
        (symbol.Family.StructuralSectionShape == StructuralSectionShape.ConcreteRectangle ||
         (symbol.Family.StructuralSectionShape == StructuralSectionShape.NotDefined &&
          LocalFamilyLibrary.Preference(StandardFamilies, symbol.FamilyName) < StandardFamilies.Length));

    private static Parameter? Width(FamilySymbol type) =>
        Find(type, BuiltInParameter.STRUCTURAL_SECTION_COMMON_WIDTH, "b", "Width", "宽度");

    private static Parameter? Height(FamilySymbol type) =>
        Find(type, BuiltInParameter.STRUCTURAL_SECTION_COMMON_HEIGHT, "h", "Height", "高度");

    private static Parameter? Find(FamilySymbol type, BuiltInParameter key, params string[] names)
    {
        return new[] { type.get_Parameter(key) }.Concat(names.Select(type.LookupParameter))
            .FirstOrDefault(parameter => parameter is { StorageType: StorageType.Double } && parameter.AsDouble() > 0);
    }

    private static bool Matches(Parameter? parameter, double value) =>
        parameter is { StorageType: StorageType.Double } && Math.Abs(parameter.AsDouble() - value) <= RevitApi.Feet(0.05);

    private static bool Apply(Parameter? parameter, double value) =>
        Matches(parameter, value) || (parameter is { IsReadOnly: false } && parameter.Set(value));

    private static void Set(FamilyInstance beam, BuiltInParameter key, int value)
    {
        if (beam.get_Parameter(key) is not { StorageType: StorageType.Integer } parameter ||
            (parameter.AsInteger() != value && (parameter.IsReadOnly || !parameter.Set(value))))
            throw new InvalidOperationException($"梁族不支持设置 {key}。");
    }

    private static void Clear(FamilyInstance beam, BuiltInParameter key)
    {
        var parameter = beam.get_Parameter(key);
        if (parameter != null && !Apply(parameter, 0))
            throw new InvalidOperationException($"梁族不能清除默认的偏移或端点延长 {key}。");
    }

    private static double Dimension(double? value, string label)
    {
        if (!value.HasValue || !(value.Value > 0) || double.IsInfinity(value.Value))
            throw new InvalidOperationException($"梁缺少有效的{label}。");
        return RevitApi.Feet(value.Value);
    }

    private static string Number(double? value) => value?.ToString("0.###", CultureInfo.InvariantCulture) ?? "";

    private static double EndpointError(Line expected, Line actual)
    {
        var a = expected.GetEndPoint(0);
        var b = expected.GetEndPoint(1);
        var c = actual.GetEndPoint(0);
        var d = actual.GetEndPoint(1);
        return Math.Min(Math.Max(a.DistanceTo(c), b.DistanceTo(d)), Math.Max(a.DistanceTo(d), b.DistanceTo(c)));
    }
}
