using System;
using Autodesk.Revit.DB;

namespace TuMo.Revit.Compat;

internal static class FloorApi
{
    public static Floor Create(Document document, CurveLoop outline, FloorType type, Level level, bool structural)
    {
#if REVIT2022_OR_GREATER
        return Floor.Create(document, new[] { outline }, type.Id, level.Id, structural, null, 0);
#else
        return document.Create.NewFloor(Curves(outline), type, level, structural)
            ?? throw new InvalidOperationException("Revit 无法创建楼板。");
#endif
    }

    public static Opening CreateVerticalOpening(Document document, Floor host, CurveLoop outline)
    {
        // The boolean selects a cut perpendicular to the host face when true.
        return document.Create.NewOpening(host, Curves(outline), false)
            ?? throw new InvalidOperationException("Revit 无法创建楼板洞口。");
    }

    private static CurveArray Curves(CurveLoop outline)
    {
        var curves = new CurveArray();
        foreach (var curve in outline)
            curves.Append(curve);
        return curves;
    }
}
