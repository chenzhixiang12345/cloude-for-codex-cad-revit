using System;

namespace TuMo.Revit.Review;

internal static class OverlayGeometry
{
    public static double[][] WallOutline(double[] start, double[] end, double thickness)
    {
        var dx = end[0] - start[0];
        var dy = end[1] - start[1];
        var length = Math.Sqrt(dx * dx + dy * dy);
        if (length == 0)
            return Array.Empty<double[]>();

        var nx = -dy / length * thickness / 2;
        var ny = dx / length * thickness / 2;
        return new[]
        {
            new[] { start[0] + nx, start[1] + ny },
            new[] { end[0] + nx, end[1] + ny },
            new[] { end[0] - nx, end[1] - ny },
            new[] { start[0] - nx, start[1] - ny }
        };
    }

    public static double[][] ColumnOutline(double[] center, double width, double depth, double rotationDegrees)
    {
        var angle = rotationDegrees * Math.PI / 180;
        var cosine = Math.Cos(angle);
        var sine = Math.Sin(angle);
        double[] Corner(double x, double y) => new[]
        {
            center[0] + x * cosine - y * sine,
            center[1] + x * sine + y * cosine
        };

        var halfWidth = width / 2;
        var halfDepth = depth / 2;
        return new[]
        {
            Corner(-halfWidth, -halfDepth),
            Corner(halfWidth, -halfDepth),
            Corner(halfWidth, halfDepth),
            Corner(-halfWidth, halfDepth)
        };
    }
}
