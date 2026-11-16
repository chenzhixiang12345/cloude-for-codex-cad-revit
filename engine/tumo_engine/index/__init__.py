"""Spatial queries that keep geometry and provenance in the same order."""

from shapely import STRtree


class SpatialIndex:
    def __init__(self, values, shape):
        self.values = list(values)
        self.shapes = [shape(value) for value in self.values]
        self.tree = STRtree(self.shapes)

    def nearby(self, geometry, tolerance):
        indices = self.tree.query(geometry, predicate="dwithin", distance=tolerance)
        return [self.values[int(index)] for index in sorted(indices)]
