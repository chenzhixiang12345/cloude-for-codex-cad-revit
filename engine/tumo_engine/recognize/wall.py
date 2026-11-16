import re
from math import dist

from shapely.geometry import LineString, Point
from shapely.ops import nearest_points

from tumo_engine.index import SpatialIndex
from tumo_engine.preprocess import layer_key
from tumo_engine.recognize.geometry import merge_collinear, parallel, rounded, segments, source_handles, world


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
    return output
