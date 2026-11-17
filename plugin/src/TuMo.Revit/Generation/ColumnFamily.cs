using System;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Linq;
using Autodesk.Revit.DB;
using TuMo.Revit.Compat;
using TuMo.Revit.Logging;

namespace TuMo.Revit.Generation;

internal static class ColumnFamily
{
    private static readonly string[] TemplateNames =
    {
        "Metric Structural Column.rft", "Structural Column.rft", "公制结构柱.rft", "公制結構柱.rft",
        "Pilar estrutural métrico.rft", "Pilar estrutural.rft", "Pilar estructural métrico.rft",
        "柱 構造(メートル単位).rft"
    };

    private static readonly (string Name, BuiltInParameter[] Keys, string[] Aliases) Width = (
        "TuMo_Width", new[] { BuiltInParameter.STRUCTURAL_SECTION_COMMON_WIDTH, BuiltInParameter.FAMILY_WIDTH_PARAM },
        new[] { "Width", "宽度", "寬度", "幅", "Largura", "Anchura", "Ancho", "b" });
    private static readonly (string Name, BuiltInParameter[] Keys, string[] Aliases) Depth = (
        "TuMo_Depth", new[] { BuiltInParameter.STRUCTURAL_SECTION_COMMON_HEIGHT, BuiltInParameter.GENERIC_DEPTH },
        new[] { "Depth", "深度", "奥行き", "Profundidade", "Profundidad", "h" });
    private static readonly (string Name, BuiltInParameter[] Keys, string[] Aliases) Diameter = (
        "TuMo_Diameter", new[] { BuiltInParameter.STRUCTURAL_SECTION_COMMON_DIAMETER },
        new[] { "Diameter", "直径", "Diâmetro", "Diámetro", "d" });

    public static FamilySymbol Create(Document project, string name, bool round, double width, double depth,
        string? materialName, RunLog log)
    {
        var names = new FilteredElementCollector(project).OfClass(typeof(Family)).Cast<Family>()
            .Select(family => family.Name).ToArray();
        var existing = new FilteredElementCollector(project).OfClass(typeof(FamilySymbol))
            .OfCategory(BuiltInCategory.OST_StructuralColumns).Cast<FamilySymbol>()
            .FirstOrDefault(symbol => IsNamedVariant(symbol, name) && HasFixedSection(symbol, round, width, depth));
        if (existing != null)
            return existing;

        var familyName = ModelTypes.AvailableName(name, names);
        var application = project.Application;
        var template = LocateTemplate(application.FamilyTemplatePath, application.VersionNumber);
        var temporary = Path.Combine(Path.GetTempPath(), $"tumo-column-{Guid.NewGuid():N}");
        Directory.CreateDirectory(temporary);
        try
        {
            var filename = Path.Combine(temporary, familyName + ".rfa");
            var family = application.NewFamilyDocument(template)
                ?? throw new InvalidOperationException("无法新建 TuMo 柱族。");
            try
            {
                // The type name keeps the full section key even if the family name needs a suffix.
                BuildExtrusion(family, name, round, width, depth, materialName);
                family.SaveAs(filename);
            }
            finally
            {
                family.Close(false);
            }
            if (!project.LoadFamily(filename, out var loaded))
                throw new InvalidOperationException($"不能载入柱族 {familyName}。");

            var symbol = (FamilySymbol)project.GetElement(loaded.GetFamilySymbolIds().Single());
            if (!HasFixedSection(symbol, round, width, depth))
                throw new InvalidOperationException($"柱族 {familyName} 的类型尺寸与实体截面不一致。");

            log.Write($"自动生成并载入柱族 {familyName}，类型尺寸已核对。");
            return symbol;
        }
        finally
        {
            Directory.Delete(temporary, true);
        }
    }

    private static void BuildExtrusion(Document family, string name, bool round, double width, double depth,
        string? materialName)
    {
        var levels = new FilteredElementCollector(family).OfClass(typeof(Level)).Cast<Level>()
            .OrderBy(level => level.ProjectElevation).ToArray();
        var elevations = new FilteredElementCollector(family).OfClass(typeof(View)).Cast<View>()
            .Where(view => !view.IsTemplate && view.ViewType is ViewType.Elevation or ViewType.Section).ToArray();
        if (levels.Length < 2 || elevations.Length == 0)
            throw new InvalidOperationException("结构柱族样板需要底部、顶部标高和立面视图。");

        var bottom = levels.First();
        var top = levels.Last();
        var elevationView = elevations.First();
        using var transaction = new Transaction(family, "TuMo 创建标准柱");
        transaction.Start();
        var manager = family.FamilyManager;
        if (manager.CurrentType == null)
            manager.NewType(name);
        else
            manager.RenameCurrentType(name);

        // The extrusion has a fixed profile. Its exposed type dimensions must have fixed values too.
        SetDimension(manager, Width, width, required: !round);
        SetDimension(manager, Depth, depth, required: !round);
        if (round)
            SetDimension(manager, Diameter, width, required: true);

        var sketchPlane = SketchPlane.Create(family, bottom.Id);
        var profile = Profile(round, width, depth, bottom.ProjectElevation);
        var height = top.ProjectElevation - bottom.ProjectElevation;
        var extrusion = family.FamilyCreate.NewExtrusion(true, profile, sketchPlane, height);
        if (!string.IsNullOrWhiteSpace(materialName) &&
            extrusion.get_Parameter(BuiltInParameter.MATERIAL_ID_PARAM) is { IsReadOnly: false } materialParameter)
        {
            var materialId = new FilteredElementCollector(family).OfClass(typeof(Material)).Cast<Material>()
                .FirstOrDefault(material => material.Name == materialName)?.Id ?? Material.Create(family, materialName!);
            materialParameter.Set(materialId);
        }
        family.Regenerate();
        var options = new Options { ComputeReferences = true };
        var topFace = extrusion.get_Geometry(options).OfType<Solid>().SelectMany(solid => solid.Faces.Cast<Face>())
            .OfType<PlanarFace>().FirstOrDefault(face => face.FaceNormal.DotProduct(XYZ.BasisZ) > 0.999);
        if (topFace?.Reference == null)
            throw new InvalidOperationException("无法取得新柱族的顶部面。");

        // The bottom sketch uses the template's lower level. Lock the top face to its upper level.
        family.FamilyCreate.NewAlignment(elevationView, top.GetPlaneReference(), topFace.Reference);
        if (transaction.Commit() != TransactionStatus.Committed)
            throw new InvalidOperationException("Revit 无法保存柱族几何。");
    }

    internal static bool IsGenerated(FamilySymbol symbol) =>
        (symbol.Name.StartsWith("TuMo-矩形柱-", StringComparison.Ordinal) ||
         symbol.Name.StartsWith("TuMo-圆柱-D", StringComparison.Ordinal)) &&
        IsNamedVariant(symbol, symbol.Name);

    private static bool IsNamedVariant(FamilySymbol symbol, string name)
    {
        if (symbol.Name != name)
            return false;
        if (symbol.FamilyName == name)
            return true;
        return symbol.FamilyName.StartsWith(name + "-", StringComparison.Ordinal) &&
               int.TryParse(symbol.FamilyName.Substring(name.Length + 1), out var suffix) && suffix >= 2;
    }

    private static bool HasFixedSection(FamilySymbol symbol, bool round, double width, double depth) =>
        round ? HasFixedDimension(symbol, Diameter, width) :
            HasFixedDimension(symbol, Width, width) && HasFixedDimension(symbol, Depth, depth);

    private static bool HasFixedDimension(FamilySymbol symbol,
        (string Name, BuiltInParameter[] Keys, string[] Aliases) dimension, double size)
    {
        return symbol.Parameters.Cast<Parameter>().Any(parameter =>
            parameter.StorageType == StorageType.Double && parameter.IsReadOnly &&
            IsDimension(parameter.Definition, dimension) &&
            Math.Abs(parameter.AsDouble() - size) < RevitApi.Feet(0.05));
    }

    private static void SetDimension(FamilyManager manager,
        (string Name, BuiltInParameter[] Keys, string[] Aliases) dimension, double size, bool required)
    {
        var parameters = manager.GetParameters().Where(parameter =>
            parameter.StorageType == StorageType.Double && !parameter.IsInstance &&
            parameter.CanAssignFormula && IsDimension(parameter.Definition, dimension)).ToList();
        if (parameters.Count == 0 && required)
            parameters.Add(RevitApi.FamilyLength(manager, dimension.Name));

        // Integer micrometres keep the formula independent of the host's decimal separator.
        var micrometres = Math.Round(size / RevitApi.Feet(0.001)).ToString("0", CultureInfo.InvariantCulture);
        foreach (var parameter in parameters)
            manager.SetFormula(parameter, $"{micrometres} mm / 1000");
    }

    private static bool IsDimension(Definition definition,
        (string Name, BuiltInParameter[] Keys, string[] Aliases) dimension) =>
        definition.Name == dimension.Name ||
        definition is InternalDefinition builtIn && dimension.Keys.Contains(builtIn.BuiltInParameter) ||
        dimension.Aliases.Contains(definition.Name, StringComparer.OrdinalIgnoreCase);

    private static CurveArrArray Profile(bool round, double width, double depth, double elevation)
    {
        var loop = new CurveArray();
        if (round)
        {
            var center = new XYZ(0, 0, elevation);
            loop.Append(Arc.Create(center, width / 2, 0, Math.PI, XYZ.BasisX, XYZ.BasisY));
            loop.Append(Arc.Create(center, width / 2, Math.PI, 2 * Math.PI, XYZ.BasisX, XYZ.BasisY));
        }
        else
        {
            var corners = new[]
            {
                new XYZ(-width / 2, -depth / 2, elevation), new XYZ(width / 2, -depth / 2, elevation),
                new XYZ(width / 2, depth / 2, elevation), new XYZ(-width / 2, depth / 2, elevation)
            };
            for (var i = 0; i < corners.Length; i++)
                loop.Append(Line.CreateBound(corners[i], corners[(i + 1) % corners.Length]));
        }
        var profile = new CurveArrArray();
        profile.Append(loop);
        return profile;
    }

    private static string LocateTemplate(string configured, string version)
    {
        var fallback = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.CommonApplicationData),
            "Autodesk", $"RVT {version}", "Family Templates");
        foreach (var root in new[] { configured, fallback }.Distinct(StringComparer.OrdinalIgnoreCase))
        {
            if (!Directory.Exists(root))
                continue;
            var available = new HashSet<string>(Directory.EnumerateFiles(root, "*.rft", SearchOption.AllDirectories),
                StringComparer.OrdinalIgnoreCase);
            foreach (var name in TemplateNames)
            {
                var match = available.FirstOrDefault(path => Path.GetFileName(path).Equals(name,
                    StringComparison.OrdinalIgnoreCase));
                if (match != null)
                    return match;
            }
        }
        throw new InvalidOperationException("找不到结构柱族样板。请安装 Revit 族样板，或先给项目载入同形状的结构柱族。");
    }
}
