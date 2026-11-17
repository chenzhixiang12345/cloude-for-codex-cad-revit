using System;
using System.Collections.Generic;
using System.IO;
using Autodesk.Revit.DB;
using Autodesk.Revit.DB.ExtensibleStorage;
using TuMo.Revit.Compat;
using TuMo.Revit.Logging;

namespace TuMo.Revit.Generation;

internal sealed class TuMoIds
{
    public const string ParameterName = "TuMo_ID";
    private static readonly Guid ParameterId = new("60E50BCD-0989-46FC-A19A-47D8D0C44DE0");
    private static readonly Guid StorageId = new("C253427B-F6F3-48B5-ADB4-3795C60D7852");

    private readonly RunLog log;

    private TuMoIds(RunLog log) => this.log = log;

    public static TuMoIds Prepare(Document document, RunLog log)
    {
        var application = document.Application;
        var categories = application.Create.NewCategorySet();
        foreach (var key in new[] { BuiltInCategory.OST_Levels, BuiltInCategory.OST_Grids,
                     BuiltInCategory.OST_Walls, BuiltInCategory.OST_StructuralColumns,
                     BuiltInCategory.OST_Doors, BuiltInCategory.OST_Windows })
        {
            var category = document.Settings.Categories.get_Item(key);
            if (category.AllowsBoundParameters)
                categories.Insert(category);
        }

        var map = document.ParameterBindings;
        var existing = SharedParameterElement.Lookup(document, ParameterId)?.GetDefinition();
        if (existing != null && map.get_Item(existing) is InstanceBinding current)
        {
            var changed = false;
            foreach (Category category in categories)
                changed |= !current.Categories.Contains(category);
            if (changed)
            {
                var all = application.Create.NewCategorySet();
                foreach (Category category in current.Categories)
                    all.Insert(category);
                foreach (Category category in categories)
                    all.Insert(category);
                if (!RevitApi.Bind(map, existing, application.Create.NewInstanceBinding(all), true))
                    throw new InvalidOperationException("不能更新 TuMo_ID 参数的构件类别。");
            }
        }
        else
        {
            var previousFile = application.SharedParametersFilename;
            var temporaryFile = Path.Combine(Path.GetTempPath(), $"tumo-{Guid.NewGuid():N}.txt");
            File.WriteAllText(temporaryFile, "# Revit shared parameters\r\n*META\tVERSION\tMINVERSION\r\nMETA\t2\t1\r\n");
            try
            {
                application.SharedParametersFilename = temporaryFile;
                var file = application.OpenSharedParameterFile()
                    ?? throw new InvalidOperationException("无法打开 TuMo 共享参数。");
                var group = file.Groups.Create("TuMo");
                var options = RevitApi.TextDefinition(ParameterName);
                options.GUID = ParameterId;
                var definition = group.Definitions.Create(options);
                if (!RevitApi.Bind(map, definition, application.Create.NewInstanceBinding(categories), false))
                    throw new InvalidOperationException("无法绑定 TuMo_ID 共享参数。");
            }
            finally
            {
                application.SharedParametersFilename = previousFile;
                File.Delete(temporaryFile);
            }
        }

        return new TuMoIds(log);
    }

    public static string? Get(Element element)
    {
        var parameterId = element.get_Parameter(ParameterId)?.AsString();
        if (!string.IsNullOrEmpty(parameterId))
            return parameterId;
        var schema = Schema.Lookup(StorageId);
        if (schema == null)
            return null;
        var entity = element.GetEntity(schema);
        return entity.IsValid() ? entity.Get<string>(schema.GetField(ParameterName)) : null;
    }

    public void Set(Element element, string id)
    {
        if (string.IsNullOrWhiteSpace(id))
            throw new InvalidOperationException("模型中的构件缺少编号。");

        var parameter = element.get_Parameter(ParameterId);
        if (parameter is { IsReadOnly: false } && parameter.StorageType == StorageType.String)
        {
            var existing = parameter.AsString();
            if (!string.IsNullOrEmpty(existing) && existing != id)
                throw new InvalidOperationException($"Revit 构件 {element.Id} 已有另一个 TuMo_ID。");
            if (existing == id || parameter.Set(id))
                return;
        }

        // Some datum categories reject project parameters. Keep their stable ID on the element.
        var schema = Schema.Lookup(StorageId);
        if (schema == null)
        {
            var builder = new SchemaBuilder(StorageId);
            builder.SetSchemaName("TuMoIdentity");
            builder.AddSimpleField(ParameterName, typeof(string));
            schema = builder.Finish();
        }
        var field = schema.GetField(ParameterName);
        var entity = element.GetEntity(schema);
        if (entity.IsValid() && entity.Get<string>(field) != id)
            throw new InvalidOperationException($"Revit 构件 {element.Id} 已有另一个 TuMo_ID。");
        entity = new Entity(schema);
        entity.Set(field, id);
        element.SetEntity(entity);
        log.Write($"Revit 构件 {element.Id} 的 TuMo_ID={id} 已保存在扩展数据。");
    }
}
