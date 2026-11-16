from dataclasses import dataclass
from itertools import pairwise
from math import atan2, cos, degrees, hypot, pi, sin

from shapely.geometry import LineString

from tumo_engine.index import SpatialIndex
from tumo_engine.preprocess import Primitive, XY


def direction(a, b):
    dx, dy = b[0] - a[0], b[1] - a[1]
    length = hypot(dx, dy)
    return dx / length, dy / length


def dot(p, q):
    return p[0] * q[0] + p[1] * q[1]


def world(along, across, axis):
    return (along * axis[0] - across * axis[1], along * axis[1] + across * axis[0])


def parallel(a, b, degrees_tolerance):
    return abs(dot(a, b)) >= cos(degrees_tolerance * pi / 180)


@dataclass
class Segment:
    a: XY
    b: XY
    primitives: tuple[Primitive, ...]

    @property
    def axis(self):
        dx, dy = direction(self.a, self.b)
        if abs(dx) >= abs(dy):
            return (dx, dy) if dx >= 0 else (-dx, -dy)
        return (dx, dy) if dy >= 0 else (-dx, -dy)

    @property
    def length(self):
        return hypot(self.b[0] - self.a[0], self.b[1] - self.a[1])

    @property
    def geometry(self):
        return LineString((self.a, self.b))

    def projected(self, axis):
        return tuple(sorted((dot(self.a, axis), dot(self.b, axis))))

    def offset(self, axis):
        normal = (-axis[1], axis[0])
        return (dot(self.a, normal) + dot(self.b, normal)) / 2


def segments(items):
    for item in items:
        if item.kind not in {"line", "polyline"}:
            continue
        points = (*item.points, item.points[0]) if item.closed else item.points
        for start, end in pairwise(points):
            if start != end:
                yield Segment(start, end, (item,))


def merge_collinear(lines, gap, deviation, angle):
    """Merge endpoint fragments with a bounded gap and parallel centerlines."""
    remaining = set(range(len(lines)))
    index = SpatialIndex(enumerate(lines), lambda item: item[1].geometry)
    result = []
    order = sorted(remaining, key=lambda n: (-lines[n].length, n))
    for i in order:
        if i not in remaining:
            continue
        current = lines[i]
        remaining.remove(i)
        axis = current.axis
        offset = current.offset(axis)
        start, end = current.projected(axis)
        sources = list(current.primitives)
        while True:
            geom = LineString((world(start, offset, axis), world(end, offset, axis)))
            changed = False
            for j, other in index.nearby(geom, hypot(gap, deviation)):
                if j not in remaining or not parallel(axis, other.axis, angle):
                    continue
                normal = (-axis[1], axis[0])
                max_deviation = max(abs(dot(p, normal) - offset) for p in (other.a, other.b))
                lo, hi = other.projected(axis)
                if max_deviation > deviation or lo > end + gap or hi < start - gap:
                    continue
                remaining.remove(j)
                start, end = min(start, lo), max(end, hi)
                sources.extend(other.primitives)
                changed = True
            if not changed:
                break
        result.append(Segment(world(start, offset, axis), world(end, offset, axis), tuple(sources)))
    return result


def source_handles(items):
    return sorted({handle for item in items for handle in item.handles})


def rounded(p):
    return [round(float(value), 3) for value in p]


def heading(axis):
    return degrees(atan2(axis[1], axis[0])) % 180
