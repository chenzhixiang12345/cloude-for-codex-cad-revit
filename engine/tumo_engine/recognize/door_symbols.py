"""Measure single-leaf door openings from their original CAD block geometry."""

from collections import defaultdict
from collections.abc import Iterable, Iterator
from itertools import pairwise
from math import dist
import re

from shapely.geometry import Polygon

from tumo_engine.preprocess import Primitive, layer_key
from tumo_engine.recognize.geometry import Segment, direction, dot, world


_DOOR_LAYER = re.compile(r"(?:^|[-_])DOOR(?:$|[-_])|^(?:门洞|门扇|门框)$")
_TOLERANCE_MM = 2


def _leaf_endpoint(outline, center, endpoints, radius):
    if len(outline.points) != 4:
        return None
    edges = list(pairwise((*outline.points, outline.points[0])))
    lengths = sorted(dist(start, end) for start, end in edges)
    if not (8 <= lengths[0] <= lengths[1] <= 120 and lengths[1] - lengths[0] <= _TOLERANCE_MM
            and all(abs(length - radius) <= _TOLERANCE_MM for length in lengths[2:])):
        return None
    axes = [direction(start, end) for start, end in edges]
    if any(abs(dot(a, b)) > 0.02 for a, b in pairwise((*axes, axes[0]))):
        return None
    matches = []
    for endpoint in endpoints:
        if any(dist(start, center) <= _TOLERANCE_MM and dist(end, endpoint) <= _TOLERANCE_MM
               or dist(end, center) <= _TOLERANCE_MM and dist(start, endpoint) <= _TOLERANCE_MM
               for start, end in edges):
            matches.append(endpoint)
    return matches[0] if len(matches) == 1 else None


def _jamb_bounds(outline, center, axis, anchor):
    if len(outline.points) != 6 or not any(dist(point, anchor) <= _TOLERANCE_MM for point in outline.points):
        return None
    normal = (-axis[1], axis[0])
    # Project in the closed door direction with the hinge as origin.
    local = []
    for x, y in outline.points:
        offset = (x - center[0], y - center[1])
        local.append((dot(offset, axis), dot(offset, normal)))
    if any(min(abs(a[0] - b[0]), abs(a[1] - b[1])) > _TOLERANCE_MM
           for a, b in pairwise((*local, local[0]))):
        return None
    polygon = Polygon(local)
    first, side, last, other_side = polygon.bounds
    length, depth = last - first, other_side - side
    station = dot((anchor[0] - center[0], anchor[1] - center[1]), axis)
    if (not polygon.is_valid or not (10 <= length <= 200 and 10 <= depth <= 200)
            or not (first + _TOLERANCE_MM < station < last - _TOLERANCE_MM)
            or length * depth - polygon.area <= _TOLERANCE_MM * max(length, depth)):
        return None
    return first, side, last, other_side


def _frame_segment(parts):
    arcs = [item for item in parts if item.kind == "arc"]
    outlines = [item for item in parts if item.kind == "polyline" and item.closed]
    if len(arcs) != 1 or len(outlines) != 3:
        return None
    arc = arcs[0]
    if not (85 <= arc.sweep_deg <= 95 and arc.radius >= 250):
        return None
    center, *curve = arc.points
    endpoints = curve[0], curve[-1]
    leaves = [(item, endpoint) for item in outlines
              if (endpoint := _leaf_endpoint(item, center, endpoints, arc.radius)) is not None]
    if len(leaves) != 1:
        return None
    leaf, open_endpoint = leaves[0]
    closed_endpoint = endpoints[1] if open_endpoint == endpoints[0] else endpoints[0]
    axis = direction(center, closed_endpoint)
    jambs = [item for item in outlines if item is not leaf]
    for hinge, catch in (jambs, jambs[::-1]):
        first = _jamb_bounds(hinge, center, axis, center)
        last = _jamb_bounds(catch, center, axis, closed_endpoint)
        if not first or not last or first[2] >= last[0]:
            continue
        if any(abs(first[index] - last[index]) > _TOLERANCE_MM for index in (1, 3)):
            continue
        across = (first[1] + first[3] + last[1] + last[3]) / 4
        points = [world(along, across, axis) for along in (first[0], last[2])]
        start, end = (tuple(value + origin for value, origin in zip(position, center)) for position in points)
        return Segment(start, end, (arc, leaf, hinge, catch))
    return None


def door_frame_openings(items: Iterable[Primitive]) -> Iterator[Segment]:
    """Yield the measured outside jamb span, directed from the hinge toward the latch side."""
    instances = defaultdict(list)
    for item in items:
        if (item.kind in {"arc", "polyline"} and len(item.handles) >= 2 and item.blocks
                and _DOOR_LAYER.search(layer_key(item.layer))):
            instances[item.handles[:-1]].append(item)
    for parts in instances.values():
        if (opening := _frame_segment(parts)) is not None:
            yield opening
