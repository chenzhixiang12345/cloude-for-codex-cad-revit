using System;
using System.Collections.Generic;
using System.Globalization;
using System.Linq;
using Autodesk.Revit.DB;
using Autodesk.Revit.DB.Structure.StructuralSections;
using TuMo.Core;
using ElementType = TuMo.Core.ElementType;
using TuMo.Revit.Compat;
using TuMo.Revit.Logging;

namespace TuMo.Revit.Generation;

internal sealed class ModelTypes
{
    private readonly Document document;
    private readonly RunLog log;
    private readonly Dictionary<string, WallType> walls = new(StringComparer.Ordinal);
    private readonly Dictionary<string, FamilySymbol> columns = new(StringComparer.Ordinal);

    public ModelTypes(Document document, RunLog log)
    {
        this.document = document;
        this.log = log;
    }

    public void Prepare(ModelElement[] elements)
    {
        var wallTypes = new FilteredElementCollector(document).OfClass(typeof(WallType)).Cast<WallType>()
            .Where(type => type.Kind == WallKind.Basic).ToList();
        var columnTypes = new FilteredElementCollector(document).OfClass(typeof(FamilySymbol))
            .OfCategory(BuiltInCategory.OST_StructuralColumns).Cast<FamilySymbol>().ToList();
        foreach (var source in elements)
        {
            var data = source.Type ?? throw new InvalidOperationException($"构件 {source.Id} 缺少类型。");
            if (source.Category == "wall" && !walls.ContainsKey(WallKey(data)))
                walls.Add(WallKey(data), ResolveWall(data, wallTypes));
            if (source.Category == "column" && !columns.ContainsKey(ColumnKey(data)))
                columns.Add(ColumnKey(data), ResolveColumn(data, columnTypes));
        }

        foreach (var symbol in columns.Values)
        {
            if (!symbol.IsActive)
                symbol.Activate();
        }
    }

    public WallType Wall(ModelElement source) => walls[WallKey(source.Type!)];
    public FamilySymbol Column(ModelElement source) => columns[ColumnKey(source.Type!)];

    private WallType ResolveWall(ElementType data, List<WallType> types)
    {
        var thickness = RevitApi.Feet(Positive(data.Thickness, "墙厚"));
        var material = FindMaterial(data.Material);
        var exact = types.FirstOrDefault(type => Matches(type.Width, thickness)
            && (material == null ? string.IsNullOrWhiteSpace(data.Material) : UsesMaterial(type, material.Id)));
        if (exact != null)
            return exact;

        var basis = types.FirstOrDefault()
            ?? throw new InvalidOperationException("项目没有基本墙类型。请从建筑或结构项目样板新建项目。");
        var name = AvailableName(WallKey(data), types.Select(type => type.Name));
        var copy = (WallType)basis.Duplicate(name);
        var compound = CompoundStructure.CreateSimpleCompoundStructure(new List<CompoundStructureLayer>
        {
            new(thickness, MaterialFunctionAssignment.Structure, material?.Id ?? ElementId.InvalidElementId)
        });
        copy.SetCompoundStructure(compound);
        types.Add(copy);
        log.Write($"创建墙类型 {copy.Name}。");
        return copy;
    }

    private FamilySymbol ResolveColumn(ElementType data, List<FamilySymbol> types)
    {
        var round = data.Diameter.HasValue;
        var width = RevitApi.Feet(Positive(round ? data.Diameter : data.Width, "柱宽或直径"));
        var depth = round ? width : RevitApi.Feet(Positive(data.Depth, "柱深"));
        var desiredMaterial = FindMaterial(data.Material);

        // Width and depth are also available on steel I-sections and hollow profiles.
        var compatible = types.Where(type => !ColumnFamily.IsGenerated(type) && HasMaterial(type, desiredMaterial) &&
            HasColumnShape(type.Family.StructuralSectionShape, round)).ToArray();
        foreach (var type in compatible)
        {
            var size = ColumnSize(type, round);
            if (size.Width != null && Matches(size.Width.AsDouble(), width) &&
                (round || (size.Depth != null && Matches(size.Depth.AsDouble(), depth))))
                return type;
        }

        // Duplicate native configurable types from the current project's library when available.
        foreach (var type in compatible)
        {
            var size = ColumnSize(type, round);
            if (size.Width is not { IsReadOnly: false } || (!round && size.Depth is not { IsReadOnly: false }))
                continue;

            var name = AvailableName(ColumnKey(data), types.Where(other => other.Family.Id == type.Family.Id)
                .Select(other => other.Name));
            var copy = (FamilySymbol)type.Duplicate(name);
            var newSize = ColumnSize(copy, round);
            if (newSize.Width!.Set(width) && (round || newSize.Depth!.Set(depth)))
            {
                types.Add(copy);
                log.Write($"创建柱类型 {copy.FamilyName} / {copy.Name}。");
                return copy;
            }
            document.Delete(copy.Id);
        }
        return CreateColumn(data, round, width, depth, types);
    }

    private FamilySymbol CreateColumn(ElementType data, bool round, double width, double depth, List<FamilySymbol> types)
    {
        var type = ColumnFamily.Create(document, ColumnKey(data), round, width, depth, data.Material, log);
        types.Add(type);
        return type;
    }

    private Material? FindMaterial(string? name)
    {
        if (string.IsNullOrWhiteSpace(name))
            return null;

        var material = new FilteredElementCollector(document).OfClass(typeof(Material)).Cast<Material>()
            .FirstOrDefault(candidate => candidate.Name.Equals(name, StringComparison.OrdinalIgnoreCase));
        if (material != null)
            return material;
        return (Material)document.GetElement(Material.Create(document, name!));
    }

    private static bool UsesMaterial(WallType type, ElementId material) =>
        type.GetCompoundStructure()?.GetLayers().Any(layer => layer.MaterialId == material) == true;

    private static bool HasMaterial(FamilySymbol type, Material? desired) =>
        desired == null || type.get_Parameter(BuiltInParameter.STRUCTURAL_MATERIAL_PARAM)?.AsElementId() == desired.Id;

    private static bool HasColumnShape(StructuralSectionShape shape, bool round) => round
        ? shape is StructuralSectionShape.ConcreteRound or StructuralSectionShape.RoundBar
        : shape is StructuralSectionShape.ConcreteRectangle or StructuralSectionShape.RectangularBar;

    private static (Parameter? Width, Parameter? Depth) ColumnSize(FamilySymbol type, bool round)
    {
        var diameter = Dimension(type, BuiltInParameter.STRUCTURAL_SECTION_COMMON_DIAMETER,
            "TuMo_Diameter", "d", "Diameter", "直径");
        if (round)
            return (diameter, null);
        if (diameter != null)
            return (null, null);
        return (Dimension(type, BuiltInParameter.STRUCTURAL_SECTION_COMMON_WIDTH, "TuMo_Width", "b", "Width", "宽度"),
            Dimension(type, BuiltInParameter.STRUCTURAL_SECTION_COMMON_HEIGHT, "TuMo_Depth", "h", "Depth", "深度",
                "Height", "高度"));
    }

    private static Parameter? Dimension(FamilySymbol symbol, BuiltInParameter key, params string[] names)
    {
        var builtIn = symbol.get_Parameter(key);
        if (builtIn?.StorageType == StorageType.Double && builtIn.AsDouble() > 0)
            return builtIn;

        foreach (var name in names)
        {
            var parameter = symbol.LookupParameter(name);
            if (parameter?.StorageType == StorageType.Double && parameter.AsDouble() > 0)
                return parameter;
        }
        return null;
    }

    private static double Positive(double? value, string label)
    {
        if (!value.HasValue || value <= 0 || double.IsNaN(value.Value) || double.IsInfinity(value.Value))
            throw new InvalidOperationException($"缺少有效的{label}。");
        return value.Value;
    }

    private static bool Matches(double first, double second) => Math.Abs(first - second) < RevitApi.Feet(0.05);
    private static string Number(double? value) => value?.ToString("0.###", CultureInfo.InvariantCulture) ?? "0";
    private static string Suffix(ElementType data) => string.IsNullOrWhiteSpace(data.Material) ? "" : "-" + data.Material;
    private static string WallKey(ElementType data) => $"TuMo-墙-{Number(data.Thickness)}{Suffix(data)}";
    private static string ColumnKey(ElementType data) =>
        (data.Diameter.HasValue ? $"TuMo-圆柱-D{Number(data.Diameter)}" :
            $"TuMo-矩形柱-{Number(data.Width)}x{Number(data.Depth)}") + Suffix(data);

    internal static string AvailableName(string candidate, IEnumerable<string> existing)
    {
        var names = new HashSet<string>(existing, StringComparer.OrdinalIgnoreCase);
        if (!names.Contains(candidate))
            return candidate;
        var number = 2;
        while (names.Contains($"{candidate}-{number}"))
            number++;
        return $"{candidate}-{number}";
    }
}
