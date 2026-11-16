using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using Autodesk.Revit.DB;
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

    public static FamilySymbol Create(Document project, string name, bool round, double width, double depth,
        string? materialName, RunLog log)
    {
        var names = new FilteredElementCollector(project).OfClass(typeof(Family)).Cast<Family>()
            .Select(family => family.Name).ToArray();
        var existing = new FilteredElementCollector(project).OfClass(typeof(FamilySymbol))
            .OfCategory(BuiltInCategory.OST_StructuralColumns).Cast<FamilySymbol>()
            .FirstOrDefault(symbol => symbol.FamilyName == name && symbol.Name == name);
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
                BuildExtrusion(family, familyName, round, width, depth, materialName);
                family.SaveAs(filename);
            }
            finally
            {
                family.Close(false);
            }
            if (!project.LoadFamily(filename, out var loaded))
                throw new InvalidOperationException($"不能载入柱族 {familyName}。");

            log.Write($"自动生成并载入柱族 {familyName}。");
            return (FamilySymbol)project.GetElement(loaded.GetFamilySymbolIds().Single());
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
        if (family.FamilyManager.CurrentType == null)
            family.FamilyManager.NewType(name);
        else
            family.FamilyManager.RenameCurrentType(name);

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
