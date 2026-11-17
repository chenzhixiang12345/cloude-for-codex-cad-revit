using System;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Linq;
using Autodesk.Revit.DB;
using TuMo.Revit.Compat;
using TuMo.Revit.Logging;

namespace TuMo.Revit.Generation;

internal sealed class OpeningFamilies
{
    private static readonly string[] DoorNames =
    {
        "单嵌板木门 1", "单扇平开门", "M_Door-Single-Flush", "Door-Single-Flush", "M_Single-Flush", "Single-Flush"
    };
    private static readonly string[] WindowNames =
    {
        "固定窗", "M_Window-Fixed", "Window-Fixed", "M_Fixed", "Fixed"
    };

    private readonly Document document;
    private readonly RunLog log;
    private readonly Dictionary<BuiltInCategory, string[]> libraryFiles = new();

    public OpeningFamilies(Document document, RunLog log)
    {
        this.document = document;
        this.log = log;
    }

    public FamilySymbol Resolve(BuiltInCategory category, double width, double height, string? requestedName)
    {
        var window = category == BuiltInCategory.OST_Windows;
        var names = window ? WindowNames : DoorNames;
        var symbols = new FilteredElementCollector(document).OfClass(typeof(FamilySymbol)).OfCategory(category)
            .Cast<FamilySymbol>().Where(IsWallHosted).ToArray();
        var found = MatchSize(symbols, names, requestedName, window, width, height);
        if (found != null)
            return found;

        if (!libraryFiles.TryGetValue(category, out var paths))
        {
            paths = LocalFamilyLibrary.Find(document.Application, names, log);
            libraryFiles.Add(category, paths);
        }

        foreach (var path in paths)
        {
            if (symbols.Any(symbol => string.Equals(symbol.FamilyName, Path.GetFileNameWithoutExtension(path),
                    StringComparison.OrdinalIgnoreCase)))
                continue;

            using var load = new SubTransaction(document);
            if (load.Start() != TransactionStatus.Started)
                throw new InvalidOperationException("不能开始载入门窗族。");
            try
            {
                if (!document.LoadFamily(path, out var family) || !IsWallHosted(family, category))
                    continue;
                var loaded = family.GetFamilySymbolIds().Select(document.GetElement).OfType<FamilySymbol>().ToArray();
                found = MatchSize(loaded, names, requestedName, window, width, height);
                if (found == null || load.Commit() != TransactionStatus.Committed)
                    continue;

                log.Write($"从 Revit 本地标准库载入门窗族 {family.Name}。");
                return found;
            }
            catch (Exception error) when (ModelOpenings.CanSkip(error))
            {
                log.Write($"未能使用本地族 {Path.GetFileName(path)}：{error.Message}");
            }
            finally
            {
                if (load.GetStatus() == TransactionStatus.Started)
                    load.RollBack();
            }
        }

        var kind = window ? "窗" : "门";
        throw new InvalidOperationException($"缺少宽高匹配的墙宿主{kind}族。请在项目中载入可调宽高的{kind}族，或安装 Revit 标准族库后重新生成。");
    }

    private FamilySymbol? MatchSize(FamilySymbol[] symbols, string[] defaults, string? requestedName,
        bool window, double width, double height)
    {
        var ordered = symbols.OrderByDescending(symbol =>
                string.Equals(symbol.Name, requestedName, StringComparison.OrdinalIgnoreCase) ||
                string.Equals(symbol.FamilyName, requestedName, StringComparison.OrdinalIgnoreCase))
            .ThenBy(symbol => LocalFamilyLibrary.Preference(defaults, symbol.FamilyName))
            .ThenBy(symbol => symbol.FamilyName, StringComparer.OrdinalIgnoreCase)
            .ThenBy(symbol => symbol.Name, StringComparer.OrdinalIgnoreCase)
            .ToArray();
        foreach (var symbol in ordered)
        {
            var dimensions = Dimensions(symbol, window);
            if (Matches(dimensions.Width, width) && Matches(dimensions.Height, height))
                return symbol;
        }

        foreach (var symbol in ordered)
        {
            var dimensions = Dimensions(symbol, window);
            if (dimensions.Width == null || dimensions.Height == null || dimensions.Width.Id == dimensions.Height.Id ||
                (dimensions.Width.IsReadOnly && !Matches(dimensions.Width, width)) ||
                (dimensions.Height.IsReadOnly && !Matches(dimensions.Height, height)))
                continue;

            var duplicate = Duplicate(symbol, symbols, window, width, height);
            if (duplicate != null)
                return duplicate;
        }
        return null;
    }

    private FamilySymbol? Duplicate(FamilySymbol symbol, FamilySymbol[] symbols, bool window, double width, double height)
    {
        var size = "TuMo-" + (window ? "窗" : "门") + "-" + Millimeters(width) + "x" + Millimeters(height);
        var name = ModelTypes.AvailableName(size, symbols.Where(item => item.Family.Id == symbol.Family.Id)
            .Select(item => item.Name));
        using var change = new SubTransaction(document);
        if (change.Start() != TransactionStatus.Started)
            throw new InvalidOperationException("不能开始创建门窗类型。");
        try
        {
            var copy = (FamilySymbol)symbol.Duplicate(name);
            var sizeParameters = Dimensions(copy, window);
            if (!Apply(sizeParameters.Width, width) || !Apply(sizeParameters.Height, height) ||
                !Matches(sizeParameters.Width, width) || !Matches(sizeParameters.Height, height))
                return null;
            if (change.Commit() != TransactionStatus.Committed)
                return null;

            log.Write($"创建门窗类型 {copy.FamilyName} / {copy.Name}。");
            return copy;
        }
        catch (Exception error) when (ModelOpenings.CanSkip(error))
        {
            log.Write($"门窗族 {symbol.FamilyName} / {symbol.Name} 无法调整为所需尺寸：{error.Message}");
            return null;
        }
        finally
        {
            if (change.GetStatus() == TransactionStatus.Started)
                change.RollBack();
        }
    }

    private static bool IsWallHosted(FamilySymbol symbol) =>
        symbol.Family.FamilyPlacementType == FamilyPlacementType.OneLevelBasedHosted;

    private static bool IsWallHosted(Family family, BuiltInCategory category) =>
        family.FamilyPlacementType == FamilyPlacementType.OneLevelBasedHosted &&
        family.FamilyCategory is { } actual && RevitApi.IdValue(actual.Id) == (long)category;

    private static (Parameter? Width, Parameter? Height) Dimensions(FamilySymbol type, bool window) =>
        (Dimension(type, window ? BuiltInParameter.WINDOW_WIDTH : BuiltInParameter.DOOR_WIDTH, BuiltInParameter.FAMILY_WIDTH_PARAM),
         Dimension(type, window ? BuiltInParameter.WINDOW_HEIGHT : BuiltInParameter.DOOR_HEIGHT, BuiltInParameter.FAMILY_HEIGHT_PARAM));

    private static Parameter? Dimension(FamilySymbol type, BuiltInParameter preferred, BuiltInParameter common)
    {
        var standard = type.get_Parameter(preferred);
        if (standard is { StorageType: StorageType.Double } && standard.AsDouble() > 0)
            return standard;
        var fallback = type.get_Parameter(common);
        return fallback is { StorageType: StorageType.Double } && fallback.AsDouble() > 0 ? fallback : null;
    }

    private static bool Matches(Parameter? parameter, double value) =>
        parameter != null && Math.Abs(parameter.AsDouble() - value) <= RevitApi.Feet(0.1);

    private static bool Apply(Parameter? parameter, double value) =>
        Matches(parameter, value) || (parameter is { IsReadOnly: false } && parameter.Set(value));

    private static string Millimeters(double feet) => (feet / RevitApi.Feet(1)).ToString("0.###", CultureInfo.InvariantCulture);

}
