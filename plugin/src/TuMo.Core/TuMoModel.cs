using System;
using System.IO;
using System.Runtime.Serialization;
using System.Runtime.Serialization.Json;

namespace TuMo.Core;

[DataContract]
public sealed class TuMoModel
{
    [DataMember(Name = "schema_version", IsRequired = true)] public string SchemaVersion { get; set; } = "";
    [DataMember(Name = "project", IsRequired = true)] public Project Project { get; set; } = new();
    [DataMember(Name = "sheets", IsRequired = true)] public Sheet[] Sheets { get; set; } = Array.Empty<Sheet>();
    [DataMember(Name = "levels", IsRequired = true)] public ModelLevel[] Levels { get; set; } = Array.Empty<ModelLevel>();
    [DataMember(Name = "grids", IsRequired = true)] public ModelGrid[] Grids { get; set; } = Array.Empty<ModelGrid>();
    [DataMember(Name = "elements", IsRequired = true)] public ModelElement[] Elements { get; set; } = Array.Empty<ModelElement>();
    [DataMember(Name = "relations", IsRequired = true)] public Relation[] Relations { get; set; } = Array.Empty<Relation>();
    [DataMember(Name = "issues", IsRequired = true)] public ModelIssue[] Issues { get; set; } = Array.Empty<ModelIssue>();

    public static TuMoModel Read(string path)
    {
        using var stream = File.OpenRead(path);
        var reader = new DataContractJsonSerializer(typeof(TuMoModel));
        if (reader.ReadObject(stream) is not TuMoModel model)
            throw new InvalidDataException("模型文件为空。");

        if (model.SchemaVersion != "0.1" || model.Project?.Units != "mm")
            throw new InvalidDataException("需要 0.1 版毫米单位模型。");

        if (model.Sheets == null || model.Levels == null || model.Grids == null ||
            model.Elements == null || model.Relations == null || model.Issues == null)
            throw new InvalidDataException("模型中的列表不能为空值。");

        return model;
    }
}

[DataContract]
public sealed class Project
{
    [DataMember(Name = "name")] public string Name { get; set; } = "";
    [DataMember(Name = "units")] public string Units { get; set; } = "";
    [DataMember(Name = "origin")] public ProjectOrigin? Origin { get; set; }
}

[DataContract]
public sealed class ProjectOrigin
{
    [DataMember(Name = "grid")] public string[]? Grid { get; set; }
    [DataMember(Name = "at")] public double[]? At { get; set; }
}

[DataContract]
public sealed class Sheet
{
    [DataMember(Name = "id")] public string Id { get; set; } = "";
    [DataMember(Name = "file")] public string File { get; set; } = "";
    [DataMember(Name = "discipline")] public string? Discipline { get; set; }
    [DataMember(Name = "kind")] public string? Kind { get; set; }
    [DataMember(Name = "title")] public string? Title { get; set; }
    [DataMember(Name = "levels")] public string[]? Levels { get; set; }
    [DataMember(Name = "number")] public string? Number { get; set; }
    [DataMember(Name = "layout")] public string? Layout { get; set; }
    [DataMember(Name = "bounds")] public double[]? Bounds { get; set; }
    [DataMember(Name = "units_to_mm")] public double? UnitsToMillimeters { get; set; }
    [DataMember(Name = "scale")] public double? Scale { get; set; }
    [DataMember(Name = "transform")] public SheetTransform? Transform { get; set; }
}

[DataContract]
public sealed class SheetTransform
{
    [DataMember(Name = "dx")] public double Dx { get; set; }
    [DataMember(Name = "dy")] public double Dy { get; set; }
    [DataMember(Name = "rot_deg")] public double? Rotation { get; set; }
}

[DataContract]
public sealed class ModelLevel
{
    [DataMember(Name = "id")] public string Id { get; set; } = "";
    [DataMember(Name = "name")] public string Name { get; set; } = "";
    [DataMember(Name = "elevation_arch")] public double? ElevationArch { get; set; }
    [DataMember(Name = "elevation_struct")] public double? ElevationStruct { get; set; }
    [DataMember(Name = "source")] public string? Source { get; set; }
    [DataMember(Name = "confidence")] public double? Confidence { get; set; }
    [DataMember(Name = "evidence")] public string[]? Evidence { get; set; }
}

[DataContract]
public sealed class ModelGrid
{
    [DataMember(Name = "id")] public string Id { get; set; } = "";
    [DataMember(Name = "label")] public string Label { get; set; } = "";
    [DataMember(Name = "start")] public double[] Start { get; set; } = Array.Empty<double>();
    [DataMember(Name = "end")] public double[] End { get; set; } = Array.Empty<double>();
    [DataMember(Name = "confidence")] public double? Confidence { get; set; }
    [DataMember(Name = "evidence")] public string[]? Evidence { get; set; }
    [DataMember(Name = "source")] public DrawingSource? Source { get; set; }
}

[DataContract]
public sealed class ModelElement
{
    [DataMember(Name = "id")] public string Id { get; set; } = "";
    [DataMember(Name = "category")] public string Category { get; set; } = "";
    [DataMember(Name = "discipline")] public string? Discipline { get; set; }
    [DataMember(Name = "type")] public ElementType? Type { get; set; }
    [DataMember(Name = "geometry")] public Geometry? Geometry { get; set; }
    [DataMember(Name = "params")] public ModelElementParameters? Parameters { get; set; }
    [DataMember(Name = "base_level")] public string? BaseLevel { get; set; }
    [DataMember(Name = "top_level")] public string? TopLevel { get; set; }
    [DataMember(Name = "base_offset")] public double? BaseOffset { get; set; }
    [DataMember(Name = "top_offset")] public double? TopOffset { get; set; }
    [DataMember(Name = "host")] public string? Host { get; set; }
    [DataMember(Name = "sill_height")] public double? SillHeight { get; set; }
    [DataMember(Name = "confidence")] public double? Confidence { get; set; }
    [DataMember(Name = "evidence")] public string[]? Evidence { get; set; }
    [DataMember(Name = "source")] public DrawingSource? Source { get; set; }
}

[DataContract]
public sealed class ModelElementParameters
{
    [DataMember(Name = "seed")] public double[]? Seed { get; set; }
    [DataMember(Name = "label_point")] public double[]? LabelPoint { get; set; }
}

[DataContract]
public sealed class ElementType
{
    [DataMember(Name = "name")] public string? Name { get; set; }
    [DataMember(Name = "material")] public string? Material { get; set; }
    [DataMember(Name = "mark")] public string? Mark { get; set; }
    [DataMember(Name = "width")] public double? Width { get; set; }
    [DataMember(Name = "depth")] public double? Depth { get; set; }
    [DataMember(Name = "height")] public double? Height { get; set; }
    [DataMember(Name = "diameter")] public double? Diameter { get; set; }
    [DataMember(Name = "thickness")] public double? Thickness { get; set; }
}

[DataContract]
public sealed class Geometry
{
    [DataMember(Name = "kind")] public string Kind { get; set; } = "";
    [DataMember(Name = "at")] public double[]? At { get; set; }
    [DataMember(Name = "start")] public double[]? Start { get; set; }
    [DataMember(Name = "end")] public double[]? End { get; set; }
    [DataMember(Name = "points")] public double[][]? Points { get; set; }
    [DataMember(Name = "facing")] public double[]? Facing { get; set; }
    [DataMember(Name = "hand")] public string? Hand { get; set; }
    [DataMember(Name = "rotation_deg")] public double? Rotation { get; set; }
}

[DataContract]
public sealed class DrawingSource
{
    [DataMember(Name = "sheet")] public string? Sheet { get; set; }
    [DataMember(Name = "handles")] public string[]? Handles { get; set; }
}

[DataContract]
public sealed class Relation
{
    [DataMember(Name = "kind")] public string Kind { get; set; } = "";
    [DataMember(Name = "a")] public string A { get; set; } = "";
    [DataMember(Name = "b")] public string B { get; set; } = "";
    [DataMember(Name = "priority")] public string? Priority { get; set; }
}

[DataContract]
public sealed class IssueReport
{
    [DataMember(Name = "issues", IsRequired = true)] public ModelIssue[] Issues { get; set; } = Array.Empty<ModelIssue>();

    public static IssueReport Read(string path)
    {
        using var stream = File.OpenRead(path);
        var reader = new DataContractJsonSerializer(typeof(IssueReport));
        if (reader.ReadObject(stream) is not IssueReport report || report.Issues == null)
            throw new InvalidDataException("疑点报告为空。");
        return report;
    }
}

[DataContract]
public sealed class ModelIssue
{
    [DataMember(Name = "id")] public string Id { get; set; } = "";
    [DataMember(Name = "severity")] public string? Severity { get; set; }
    [DataMember(Name = "element")] public string? Element { get; set; }
    [DataMember(Name = "message")] public string? Message { get; set; }
    [DataMember(Name = "where")] public IssueLocation? Where { get; set; }
}

[DataContract]
public sealed class IssueLocation
{
    [DataMember(Name = "sheet")] public string? Sheet { get; set; }
    [DataMember(Name = "at")] public double[]? At { get; set; }
}
