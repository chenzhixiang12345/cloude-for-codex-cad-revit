using Autodesk.Revit.DB;

namespace TuMo.Revit.Compat;

internal static class RevitApi
{
    public static double Feet(double mm)
    {
        // Revit 2021 introduced ForgeTypeId for units. Revit 2020 needs DisplayUnitType.
#if REVIT2021_OR_GREATER
        return UnitUtils.ConvertToInternalUnits(mm, UnitTypeId.Millimeters);
#else
        return UnitUtils.ConvertToInternalUnits(mm, DisplayUnitType.DUT_MILLIMETERS);
#endif
    }

    public static FamilyParameter FamilyLength(FamilyManager manager, string name)
    {
#if REVIT2022_OR_GREATER
        return manager.AddParameter(name, GroupTypeId.Geometry, SpecTypeId.Length, false);
#else
        return manager.AddParameter(name, BuiltInParameterGroup.PG_GEOMETRY, ParameterType.Length, false);
#endif
    }

    public static long IdValue(ElementId id)
    {
        // The ID moved from Int32 to Int64 in Revit 2024.
#if REVIT2024_OR_GREATER
        return id.Value;
#else
        return id.IntegerValue;
#endif
    }

    public static ExternalDefinitionCreationOptions TextDefinition(string name)
    {
        // Revit 2022 introduced Forge type IDs for parameter data types.
#if REVIT2022_OR_GREATER
        return new ExternalDefinitionCreationOptions(name, SpecTypeId.String.Text);
#else
        return new ExternalDefinitionCreationOptions(name, ParameterType.Text);
#endif
    }

    public static bool Bind(BindingMap map, Definition definition, Binding binding, bool replace)
    {
        // The Forge group ID overload replaces BuiltInParameterGroup in Revit 2024.
#if REVIT2024_OR_GREATER
        return replace ? map.ReInsert(definition, binding, GroupTypeId.IdentityData)
            : map.Insert(definition, binding, GroupTypeId.IdentityData);
#else
        return replace ? map.ReInsert(definition, binding, BuiltInParameterGroup.PG_IDENTITY_DATA)
            : map.Insert(definition, binding, BuiltInParameterGroup.PG_IDENTITY_DATA);
#endif
    }
}
