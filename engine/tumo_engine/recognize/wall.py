import re
from itertools import combinations
from math import dist

from shapely import get_coordinates
from shapely.geometry import LineString, Point
from shapely.ops import nearest_points, unary_union

from tumo_engine.index import SpatialIndex
from tumo_engine.preprocess import layer_key
from tumo_engine.recognize.geometry import direction, merge_collinear, parallel, rounded, segments, source_handles, world


def _pair(first, second, cfg):
    axis = first.axis
    if not parallel(axis, second.axis, cfg["direction_tolerance_deg"]):
        return None
    a0, a1 = first.projected(axis)
    b0, b1 = second.projected(axis)
    begin, finish = max(a0, b0), min(a1, b1)
    overlap = finish - begin
    offset1, offset2 = first.offset(axis), second.offset(axis)
    width = abs(offset1 - offset2)
    tolerance = min(abs(width - value) for value in cfg["thicknesses"])
    if tolerance > cfg["thickness_tolerance"]:
        return None
    if overlap < max(cfg["minimum_length"], width * cfg["minimum_length_ratio"]):
        return None
    if overlap / max(a1 - a0, b1 - b0) < cfg["minimum_overlap_ratio"]:
        return None
    center = (offset1 + offset2) / 2
    return overlap, tolerance, round(width, 2), world(begin, center, axis), world(finish, center, axis)


def _footprint(line, width):
    return line.buffer(width / 2, cap_style=2)


def _extend_within_outline(line, width, first, outline, padded_outline):
    axis = first.axis
    center = line.interpolate(0.5, normalized=True)
    reach = dist(outline.bounds[:2], outline.bounds[2:])
    x, y = center.coords[0]
    dx, dy = axis[0] * reach, axis[1] * reach
    through = outline.intersection(LineString(((x - dx, y - dy), (x + dx, y + dy))))
    for part in getattr(through, "geoms", (through,)):
        if part.geom_type != "LineString" or part.distance(center) > 1e-6:
            continue
        endpoints = sorted((part.coords[0], part.coords[-1]), key=lambda p: p[0] * axis[0] + p[1] * axis[1])
        extended = LineString(endpoints)
        if extended.length > line.length and padded_outline.covers(_footprint(extended, width)):
            return extended
    return line


def _trim_tiny_end_overlaps(line, wall_overlaps, cfg):
    axis = direction(line.coords[0], line.coords[-1])
    length = line.length
    begin, finish = 0, length
    tolerance = cfg["outline_end_overlap"]
    for other_line, overlap in wall_overlaps:
        other_axis = direction(other_line.coords[0], other_line.coords[-1])
        if not parallel(axis, other_axis, cfg["direction_tolerance_deg"]):
            continue
        offsets = [line.project(Point(x, y)) for x, y in get_coordinates(overlap)]
        low, high = min(offsets), max(offsets)
        # Leave room for the final 0.001 mm coordinate rounding.
        if low <= 1e-6 and high <= tolerance:
            begin = max(begin, high + 0.002)
        elif high >= length - 1e-6 and length - low <= tolerance:
            finish = min(finish, low - 0.002)
    return LineString((line.interpolate(begin), line.interpolate(finish)))


def _supplement_closed_outlines(items, columns, ordinary_walls, rules):
    # Work within one real CAD outline at a time. Never pair edges across separate outlines.
    cfg = dict(rules["wall"], minimum_overlap_ratio=0)
    layers = rules["layers"]
    existing = []
    for wall, _ in ordinary_walls:
        line = LineString((wall["geometry"]["start"], wall["geometry"]["end"]))
        existing.append((line, _footprint(line, wall["type"]["thickness"])))
    wall_index = SpatialIndex(existing, lambda pair: pair[1])
    column_index = SpatialIndex((shape for _, shape in columns), lambda shape: shape)
    accepted = []
    for item in items:
        layer = layer_key(item.layer)
        if item.kind != "polyline" or not item.closed:
            continue
        if not (re.search(layers["structural_wall_outlines"], layer) or re.search(layers["walls"], layer)):
            continue
        outline = item.shape
        if not outline.is_valid or outline.area <= 0:
            continue
        # Allow small CAD rounding errors when checking the width of the whole strip.
        padded_outline = outline.buffer(1)
        candidates = []
        for first, second in combinations(segments([item]), 2):
            pair = _pair(first, second, cfg)
            if pair is None:
                continue
            _, tolerance, width, start, end = pair
            line = LineString((start, end))
            if not padded_outline.covers(_footprint(line, width)):
                continue
            line = _extend_within_outline(line, width, first, outline, padded_outline)
            candidates.append((line, width, tolerance))

        for line, width, _ in sorted(candidates, key=lambda row: (-row[0].length, row[2])):
            strip = _footprint(line, width)
            wall_overlaps = []
            for other_line, other in wall_index.nearby(strip, 0):
                overlap = other.intersection(strip)
                if overlap.area > 0:
                    wall_overlaps.append((other_line, overlap))
            covered = [overlap for _, overlap in wall_overlaps]
            covered.extend(shape.intersection(strip) for shape in column_index.nearby(strip, 0))
            covered.extend(shape.intersection(strip) for shape in accepted if shape.intersects(strip))
            if covered and unary_union(covered).area >= 0.5 * strip.area:
                continue
            if wall_overlaps:
                line = _trim_tiny_end_overlaps(line, wall_overlaps, cfg)
            if line.length < cfg["minimum_length"]:
                continue
            accepted.append(_footprint(line, width))
            yield ({
                "category": "wall",
                "type": {"name": f"墙 {width:g} mm", "thickness": width},
                "geometry": {"kind": "line", "start": rounded(line.coords[0]), "end": rounded(line.coords[-1])},
                "confidence": 0.82,
                "handles": source_handles([item]),
                "evidence": [f"layer:{layer}", "closed_wall_outline", f"parallel_pair:{width:g}"],
            }, [])


def recognize_walls(items, columns, rules):
    cfg = rules["wall"]
    walls = [item for item in items if re.search(rules["layers"]["walls"], layer_key(item.layer))]
    lines = merge_collinear(list(segments(walls)), cfg["gap"], cfg["collinear_tolerance"], cfg["direction_tolerance_deg"])
    lines = [line for line in lines if line.length >= cfg["minimum_length"]]
    index = SpatialIndex(enumerate(lines), lambda pair: pair[1].geometry)
    candidates = []
    for i, first in enumerate(lines):
        for j, second in index.nearby(first.geometry, max(cfg["thicknesses"]) + cfg["thickness_tolerance"]):
            if i >= j:
                continue
            pair = _pair(first, second, cfg)
            if pair is None:
                continue
            overlap, tolerance, width, start, end = pair
            common_outline = any(
                a is b and a.closed for a in first.primitives for b in second.primitives
            )
            candidates.append((not common_outline, -overlap, tolerance, i, j, width, start, end))

    used_lines = set()
    column_index = SpatialIndex(columns, lambda item: item[1])
    output = []
    for not_common, _, _, i, j, width, start, end in sorted(candidates):
        if i in used_lines or j in used_lines:
            continue
        used_lines.update((i, j))
        snapped = []
        relations = []
        for endpoint in (start, end):
            point = Point(endpoint)
            nearby = column_index.nearby(point, rules["column"]["snap_distance"])
            if nearby:
                column, profile = min(nearby, key=lambda item: point.distance(item[1]))
                if not profile.covers(point):
                    endpoint = nearest_points(point, profile.boundary)[1].coords[0]
                relations.append(column)
            snapped.append(endpoint)
        if dist(*snapped) < cfg["minimum_length"]:
            continue
        evidence = [f"parallel_pair:{width:g}"]
        if not not_common:
            evidence.append("closed_outline")
        if relations:
            evidence.append("ends_on:column")
        output.append(({
            "category": "wall",
            "type": {"name": f"墙 {width:g} mm", "thickness": width},
            "geometry": {"kind": "line", "start": rounded(snapped[0]), "end": rounded(snapped[1])},
            "confidence": 0.9 if not not_common else 0.87 if relations else 0.78,
            "handles": source_handles((*lines[i].primitives, *lines[j].primitives)),
            "evidence": evidence,
        }, relations))
    output.extend(list(_supplement_closed_outlines(items, columns, output, rules)))
    return output
