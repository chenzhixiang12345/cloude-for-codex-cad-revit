using System;
using Autodesk.Revit.DB;
using Autodesk.Revit.DB.Architecture;
using TuMo.Core;
using TuMo.Revit.Compat;

namespace TuMo.Revit.Generation;

internal sealed class RoomPlacementException : InvalidOperationException
{
    public RoomPlacementException(string message, Exception? inner = null) : base(message, inner)
    {
    }
}

internal static class RoomPlacement
{
    /// <summary>Places a room in an existing native enclosure. The caller owns the subtransaction.</summary>
    public static Room Create(Document document, ModelElement source, Level baseLevel, Level? topLevel, XYZ seed)
    {
        var name = source.Type?.Name?.Trim();
        if (string.IsNullOrEmpty(name))
            throw new RoomPlacementException("房间缺少名称。");
        if (double.IsNaN(seed.X) || double.IsInfinity(seed.X) ||
            double.IsNaN(seed.Y) || double.IsInfinity(seed.Y))
            throw new RoomPlacementException("房间定位点无效。");
        if (!string.IsNullOrEmpty(source.TopLevel) && topLevel == null)
            throw new RoomPlacementException("房间上限标高不存在。");

        var baseOffset = RevitApi.Feet(source.BaseOffset ?? 0);
        var limitOffset = RevitApi.Feet(source.TopOffset ?? 0);
        if (topLevel != null &&
            topLevel.ProjectElevation + limitOffset <= baseLevel.ProjectElevation + baseOffset)
            throw new RoomPlacementException("房间上限必须高于底部。");

        try
        {
            var room = document.Create.NewRoom(baseLevel, new UV(seed.X, seed.Y));
            if (room == null)
                throw new RoomPlacementException("Revit 无法在定位点放置房间。");

            var parameter = room.get_Parameter(BuiltInParameter.ROOM_NAME);
            if (parameter == null ||
                (parameter.AsString() != name && (parameter.IsReadOnly || !parameter.Set(name))))
                throw new RoomPlacementException("无法写入房间名称。");

            if (topLevel != null)
                room.UpperLimit = topLevel;
            if (topLevel != null || source.TopOffset.HasValue)
                room.LimitOffset = limitOffset;
            if (source.BaseOffset.HasValue)
                room.BaseOffset = baseOffset;

            var upper = room.UpperLimit;
            if (upper != null &&
                upper.ProjectElevation + room.LimitOffset <= baseLevel.ProjectElevation + room.BaseOffset)
                throw new RoomPlacementException("房间上限必须高于底部。");

            document.Regenerate();
            using var boundary = new SpatialElementBoundaryOptions();
            if (!(room.Area > 0) || room.GetBoundarySegments(boundary) is not { Count: > 0 })
                throw new RoomPlacementException("定位点未形成有效的房间围合，或该区域已有房间。");

            return room;
        }
        catch (Autodesk.Revit.Exceptions.ArgumentException error)
        {
            throw new RoomPlacementException($"Revit 拒绝放置房间：{error.Message}", error);
        }
    }
}
