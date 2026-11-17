using System;
using System.Globalization;
using System.Linq;
using Autodesk.Revit.DB;
using TuMo.Core;
using TuMo.Revit.Compat;

namespace TuMo.Revit.Generation;

internal static class SlabPlacement
{
    public static Floor Create(Document document, ModelElement source, Level level, CurveLoop outline)
    {
        var thickness = source.Type?.Thickness ?? 0;
        if (thickness <= 0 || double.IsNaN(thickness) || double.IsInfinity(thickness))
            throw new InvalidOperationException("楼板缺少有效的厚度。");
        var elevation = source.BaseOffset ?? 0;
        if (double.IsNaN(elevation) || double.IsInfinity(elevation))
            throw new InvalidOperationException("楼板的相对标高无效。");

        var type = ResolveType(document, thickness, source.Type?.Material);
        var slab = FloorApi.Create(document, outline, type, level, source.Discipline == "struct");
        var offset = slab.get_Parameter(BuiltInParameter.FLOOR_HEIGHTABOVELEVEL_PARAM);
        var height = RevitApi.Feet(elevation);
        if (offset is not { StorageType: StorageType.Double } ||
            (Math.Abs(offset.AsDouble() - height) > 1e-9 && (offset.IsReadOnly || !offset.Set(height))))
            throw new InvalidOperationException("无法设置楼板的相对标高。");
        return slab;
    }

    public static Opening CreateOpening(Document document, Floor host, CurveLoop outline) =>
        FloorApi.CreateVerticalOpening(document, host, outline);

    private static FloorType ResolveType(Document document, double thicknessMm, string? materialName)
    {
        var thickness = RevitApi.Feet(thicknessMm);
        var material = FindMaterial(document, materialName);
        var types = new FilteredElementCollector(document).OfClass(typeof(FloorType)).Cast<FloorType>()
            .Where(type => !type.IsFoundationSlab).ToArray();
        var exact = types.FirstOrDefault(type => Matches(type, thickness, material));
        if (exact != null)
            return exact;

        var template = types.FirstOrDefault(type => type.GetCompoundStructure() != null)
            ?? throw new InvalidOperationException("项目没有可用于复制的楼板类型。");
        var width = thicknessMm.ToString("0.###", CultureInfo.InvariantCulture);
        var label = string.IsNullOrWhiteSpace(materialName) ? $"TuMo-楼板-{width}" : $"TuMo-楼板-{width}-{materialName}";
        var name = ModelTypes.AvailableName(label, types.Select(type => type.Name));
        var copy = (FloorType)template.Duplicate(name);
        var layer = new CompoundStructureLayer(thickness, MaterialFunctionAssignment.Structure, material);
        var structure = CompoundStructure.CreateSimpleCompoundStructure(new[] { layer });
        structure.EndCap = EndCapCondition.NoEndCap;
        copy.SetCompoundStructure(structure);
        return copy;
    }

    private static bool Matches(FloorType type, double thickness, ElementId material)
    {
        var structure = type.GetCompoundStructure();
        if (structure == null || Math.Abs(structure.GetWidth() - thickness) > RevitApi.Feet(0.01))
            return false;
        if (material == ElementId.InvalidElementId)
            return true;
        var layers = structure.GetLayers();
        return layers.Count == 1 && layers[0].MaterialId == material;
    }

    private static ElementId FindMaterial(Document document, string? name)
    {
        if (string.IsNullOrWhiteSpace(name))
            return ElementId.InvalidElementId;
        var material = new FilteredElementCollector(document).OfClass(typeof(Material)).Cast<Material>()
            .FirstOrDefault(candidate => candidate.Name.Equals(name, StringComparison.OrdinalIgnoreCase));
        return material?.Id ?? Material.Create(document, name!);
    }
}
