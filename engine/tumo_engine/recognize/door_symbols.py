"""Measure single-leaf door openings from their original CAD block geometry."""

from collections import defaultdict
from collections.abc import Iterable, Iterator, Mapping
from itertools import pairwise
from math import dist
import re

from shapely.geometry import Polygon

from tumo_engine.preprocess import Primitive, layer_key
from tumo_engine.recognize.geometry import Segment, direction, dot, world
from tumo_engine.rules import defaults


def _leaf_endpoint(outline, center, endpoints, radius, rules):
    if len(outline.points) != 4:
        return None
    edges = list(pairwise((*outline.points, outline.points[0])))
    lengths = sorted(dist(start, end) for start, end in edges)
    minimum, maximum = rules["leaf_thickness"]
    tolerance = rules["linear_tolerance"]
    if not (minimum <= lengths[0] <= lengths[1] <= maximum and lengths[1] - lengths[0] <= tolerance
            and all(abs(length - radius) <= tolerance for length in lengths[2:])):
        return None
    axes = [direction(start, end) for start, end in edges]
    if any(abs(dot(a, b)) > rules["leaf_orthogonal_dot_tolerance"] for a, b in pairwise((*axes, axes[0]))):
        return None
    matches = []
    for endpoint in endpoints:
        if any(dist(start, center) <= tolerance and dist(end, endpoint) <= tolerance
               or dist(end, center) <= tolerance and dist(start, endpoint) <= tolerance
               for start, end in edges):
            matches.append(endpoint)
    return matches[0] if len(matches) == 1 else None


def _jamb_bounds(outline, center, axis, anchor, rules):
    tolerance = rules["linear_tolerance"]
    if len(outline.points) != 6 or not any(dist(point, anchor) <= tolerance for point in outline.points):
        return None
    normal = (-axis[1], axis[0])
    # Project in the closed door direction with the hinge as origin.
    local = []
    for x, y in outline.points:
        offset = (x - center[0], y - center[1])
        local.append((dot(offset, axis), dot(offset, normal)))
    if any(min(abs(a[0] - b[0]), abs(a[1] - b[1])) > tolerance
           for a, b in pairwise((*local, local[0]))):
        return None
    polygon = Polygon(local)
    first, side, last, other_side = polygon.bounds
    length, depth = last - first, other_side - side
    station = dot((anchor[0] - center[0], anchor[1] - center[1]), axis)
    minimum, maximum = rules["jamb_extent"]
    if (not polygon.is_valid or not (minimum <= length <= maximum and minimum <= depth <= maximum)
            or not (first + tolerance < station < last - tolerance)
            or length * depth - polygon.area <= tolerance * max(length, depth)):
        return None
    return first, side, last, other_side


def _frame_segment(parts, rules):
    arcs = [item for item in parts if item.kind == "arc"]
    outlines = [item for item in parts if item.kind == "polyline" and item.closed]
    if len(arcs) != 1 or len(outlines) != 3:
        return None
    arc = arcs[0]
    minimum_sweep, maximum_sweep = rules["sweep_degrees"]
    if not (minimum_sweep <= arc.sweep_deg <= maximum_sweep and arc.radius >= rules["minimum_radius"]):
        return None
    center, *curve = arc.points
    endpoints = curve[0], curve[-1]
    leaves = [(item, endpoint) for item in outlines
              if (endpoint := _leaf_endpoint(item, center, endpoints, arc.radius, rules)) is not None]
    if len(leaves) != 1:
        return None
    leaf, open_endpoint = leaves[0]
    closed_endpoint = endpoints[1] if open_endpoint == endpoints[0] else endpoints[0]
    axis = direction(center, closed_endpoint)
    jambs = [item for item in outlines if item is not leaf]
    for hinge, catch in (jambs, jambs[::-1]):
        first = _jamb_bounds(hinge, center, axis, center, rules)
        last = _jamb_bounds(catch, center, axis, closed_endpoint, rules)
        if not first or not last or first[2] >= last[0]:
            continue
        if any(abs(first[index] - last[index]) > rules["linear_tolerance"] for index in (1, 3)):
            continue
        across = (first[1] + first[3] + last[1] + last[3]) / 4
        points = [world(along, across, axis) for along in (first[0], last[2])]
        start, end = (tuple(value + origin for value, origin in zip(position, center)) for position in points)
        return Segment(start, end, (arc, leaf, hinge, catch))
    return None


def door_frame_openings(items: Iterable[Primitive], *, rules: Mapping | None = None) -> Iterator[Segment]:
    """Yield the measured outside jamb span, directed from the hinge toward the latch side."""
    if rules is None:
        rules = defaults()["opening"]["door_frame"]["measurement"]
    door_layer = re.compile(rules["outline_layers"])
    instances = defaultdict(list)
    for item in items:
        if (item.kind in {"arc", "polyline"} and len(item.handles) >= 2 and item.blocks
                and door_layer.search(layer_key(item.layer))):
            instances[item.handles[:-1]].append(item)
    for parts in instances.values():
        if (opening := _frame_segment(parts, rules)) is not None:
            yield opening
