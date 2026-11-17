from collections import defaultdict
import re
from itertools import combinations
from math import dist

from shapely import get_coordinates, get_parts
from shapely.geometry import LineString, Point
from shapely.ops import nearest_points, unary_union

from tumo_engine.index import SpatialIndex
from tumo_engine.preprocess import layer_key
from tumo_engine.recognize.geometry import direction, dot, merge_collinear, parallel, rounded, segments, source_handles, world


_COINCIDENT_TOLERANCE_MM = 0.01


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


def _wall_footprint(wall):
    return _footprint(_wall_line(wall), wall["type"]["thickness"])


def _wall_line(wall):
    geometry = wall["geometry"]
    return LineString((geometry["start"], geometry["end"]))


def unbuilt_structural_wall_areas(items, walls, columns, rules):
    """Locate visible wall outlines that accepted walls and columns do not cover."""
    pattern = rules["layers"]["structural_wall_outlines"]
    cutoff = rules["confidence"]["publish"]
    footprints = [_wall_footprint(wall) for wall, _ in walls if wall["confidence"] >= cutoff]
    footprints.extend(shape for column, shape in columns if column["confidence"] >= cutoff)
    index = SpatialIndex(footprints, lambda shape: shape)
    cfg = rules["wall"]
    minimum_area = cfg["minimum_length"] * min(cfg["thicknesses"])
    for item in items:
        if item.kind != "polyline" or not item.closed or not re.search(pattern, layer_key(item.layer)):
            continue
        outline = item.shape
        if not outline.is_valid or outline.area < minimum_area:
            continue
        # Ignore submillimetre edges left by CAD and model coordinate rounding.
        nearby = index.nearby(outline, 1)
        remaining = outline.difference(unary_union(nearby).buffer(1)) if nearby else outline
        for part in get_parts(remaining):
            if part.area >= minimum_area:
                yield item.handles[-1] if item.handles else None, rounded(part.representative_point().coords[0])


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


def _closed_outline_spans(outline, padded, edges, cfg):
    for first, second in combinations(edges, 2):
        pair = _pair(first, second, cfg)
        if pair is None:
            continue
        _, tolerance, width, start, end = pair
        line = LineString((start, end))
        if padded.covers(_footprint(line, width)):
            yield _extend_within_outline(line, width, first, outline, padded), width, tolerance


def _has_full_end_faces(line, width, edges):
    start, end = line.coords
    dx, dy = direction(start, end)
    ox, oy = -dy * width / 2, dx * width / 2
    for x, y in (start, end):
        first, last = (x - ox, y - oy), (x + ox, y + oy)
        if not any(
            max(dist(first, edge.a), dist(last, edge.b)) <= _COINCIDENT_TOLERANCE_MM
            or max(dist(first, edge.b), dist(last, edge.a)) <= _COINCIDENT_TOLERANCE_MM
            for edge in edges
        ):
            return False
    return True


def pending_wall_end_extensions(items, elements, relations, rules):
    """Save single-end extensions backed by both end faces of one source outline."""
    related = {rel[key] for rel in relations for key in ("a", "b")}
    walls = defaultdict(list)
    for wall in elements:
        if wall["category"] == "wall" and "closed_outline" in wall["evidence"] and wall["id"] not in related:
            for handle in wall["source"]["handles"]:
                walls[handle].append(wall)

    proposals = defaultdict(set)
    cfg = dict(rules["wall"], minimum_overlap_ratio=0)
    layers = rules["layers"]
    outline_layer = re.compile("|".join((layers["walls"], layers["structural_wall_outlines"])))
    for item in items:
        if (item.kind != "polyline" or not item.closed or not item.handles
                or not outline_layer.search(layer_key(item.layer))):
            continue
        matching = [wall for wall in walls.get(item.handles[-1], ())
                    if set(item.handles) <= set(wall["source"]["handles"])]
        if not matching:
            continue
        outline = item.shape
        if not outline.is_valid or outline.area <= 0:
            continue
        edges = list(segments([item]))
        for line, width, _ in _closed_outline_spans(outline, outline.buffer(1), edges, cfg):
            if not _has_full_end_faces(line, width, edges):
                continue
            axis = line.buffer(_COINCIDENT_TOLERANCE_MM)
            for wall in matching:
                old = _wall_line(wall)
                if width != wall["type"]["thickness"] or old.length < line.length / 2 or not axis.covers(old):
                    continue
                for end, other in (("start", "end"), ("end", "start")):
                    old_tip, anchor = wall["geometry"][end], wall["geometry"][other]
                    far_tip, near_tip = sorted(line.coords, key=lambda point: -dist(point, anchor))
                    if (dist(near_tip, anchor) <= _COINCIDENT_TOLERANCE_MM
                            and dist(far_tip, old_tip) >= cfg["minimum_length"]):
                        # Different edge pairs can measure the same CAD end within model precision.
                        proposals[wall["id"]].add((end, tuple(old_tip), tuple(rounded(far_tip))))
    return {wall: next(iter(choices)) for wall, choices in proposals.items() if len(choices) == 1}


def apply_wall_end_extensions(elements, columns, pending):
    """Extend the unmodified end of a bound wall while retaining its identity and openings."""
    if not pending:
        return
    walls = [wall for wall in elements if wall["category"] == "wall"]
    footprints = SpatialIndex(((wall["id"], _wall_footprint(wall)) for wall in walls), lambda entry: entry[1])
    column_index = SpatialIndex((shape for _, shape in columns), lambda shape: shape)
    proposals = []
    for wall in walls:
        if wall["id"] not in pending:
            continue
        end, old_tip, target = pending[wall["id"]]
        geo = wall["geometry"]
        if tuple(geo[end]) != old_tip:
            continue
        other = geo["start"] if end == "end" else geo["end"]
        axis = direction(other, old_tip)
        along = dot((target[0] - old_tip[0], target[1] - old_tip[1]), axis)
        tip = rounded((old_tip[0] + along * axis[0], old_tip[1] + along * axis[1]))
        if along <= 0 or dist(tip, target) > _COINCIDENT_TOLERANCE_MM:
            continue
        added = _footprint(LineString((old_tip, tip)), wall["type"]["thickness"])
        interior = added.buffer(-_COINCIDENT_TOLERANCE_MM)
        if (any(identifier != wall["id"] and interior.intersection(shape).area > 0
                for identifier, shape in footprints.nearby(interior, 0))
                or any(interior.intersection(shape).area > 0 for shape in column_index.nearby(interior, 0))):
            continue
        proposals.append((wall, end, tip, added, interior))
    proposed = SpatialIndex(enumerate(proposals), lambda entry: entry[1][3])
    for index, (wall, end, tip, _, interior) in enumerate(proposals):
        if any(index != other and interior.intersection(other_plan[3]).area > 0
               for other, other_plan in proposed.nearby(interior, 0)):
            continue
        wall["geometry"][end] = tip
        wall["evidence"].append("full_cad_end_faces")


def _supplement_closed_outlines(items, columns, ordinary_walls, rules):
    # Work within one real CAD outline at a time. Never pair edges across separate outlines.
    cfg = dict(rules["wall"], minimum_overlap_ratio=0)
    layers = rules["layers"]
    existing = []
    for wall, _ in ordinary_walls:
        line = _wall_line(wall)
        existing.append((line, _footprint(line, wall["type"]["thickness"])))
    wall_index = SpatialIndex(existing, lambda pair: pair[1])
    column_index = SpatialIndex((shape for _, shape in columns), lambda shape: shape)
    accepted = []
    architectural_outlines = []
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
        if re.search(layers["walls"], layer):
            architectural_outlines.append((item, layer, padded_outline))
        candidates = _closed_outline_spans(outline, padded_outline, list(segments([item])), cfg)

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
            yield item, {
                "category": "wall",
                "type": {"name": f"墙 {width:g} mm", "thickness": width},
                "geometry": {"kind": "line", "start": rounded(line.coords[0]), "end": rounded(line.coords[-1])},
                "confidence": 0.82,
                "handles": source_handles([item]),
                "evidence": [f"layer:{layer}", "closed_wall_outline", f"parallel_pair:{width:g}"],
            }
    yield from _supplement_wall_fills(items, architectural_outlines, wall_index, column_index, accepted, cfg)


def _supplement_wall_fills(items, outlines, wall_index, column_index, accepted, cfg):
    outline_index = SpatialIndex(outlines, lambda entry: entry[2])
    for fill in items:
        layer = layer_key(fill.layer)
        if fill.kind != "polyline" or not layer.endswith(("-HACH", "_HACH")) or not fill.closed or len(fill.points) != 4:
            continue
        area = fill.shape
        if not area.is_valid or area.area <= 0 or not area.buffer(1).covers(area.minimum_rotated_rectangle):
            continue
        # A fill layer can also contain legend and floor graphics. Require a separate wall outline.
        enclosing = [entry for entry in outline_index.nearby(area, 0) if entry[1] == layer[:-5] and entry[2].covers(area)]
        if len(enclosing) != 1:
            continue
        outline, _, padded_outline = enclosing[0]
        pair = max(
            (pair for first, second in combinations(segments([fill]), 2)
             if (pair := _pair(first, second, cfg)) is not None),
            default=None,
        )
        if pair is None:
            continue
        _, _, width, start, end = pair
        line = LineString((start, end))
        strip = _footprint(line, width)
        if not area.buffer(1).covers(strip) or not padded_outline.covers(strip):
            continue
        inside = strip.buffer(-1)
        walls = wall_index.nearby(strip, 0)
        footprints = [other for _, other in walls]
        footprints.extend(column_index.nearby(strip, 0))
        footprints.extend(accepted)
        if any(profile.intersection(inside).area > 0 for profile in footprints):
            continue
        overlaps = [(other_line, other.intersection(strip)) for other_line, other in walls]
        line = _trim_tiny_end_overlaps(line, [(axis, area) for axis, area in overlaps if area.area > 0], cfg)
        if line.length < cfg["minimum_length"]:
            continue
        accepted.append(_footprint(line, width))
        yield fill, {
            "category": "wall",
            "type": {"name": f"墙 {width:g} mm", "thickness": width},
            "geometry": {"kind": "line", "start": rounded(line.coords[0]), "end": rounded(line.coords[-1])},
            "confidence": 0.82,
            "handles": source_handles([outline, fill]),
            "evidence": [f"layer:{layer}", "wall_fill_inside_outline", f"parallel_pair:{width:g}"],
        }


def _replace_covered_walls(ordinary_walls, by_outline, supplements):
    replaced = set()
    additions = []
    for outline, wall in supplements:
        line = _wall_line(wall)
        axis = line.buffer(_COINCIDENT_TOLERANCE_MM)
        footprint = _wall_footprint(wall).buffer(_COINCIDENT_TOLERANCE_MM)
        for candidate, joins in by_outline.get(id(outline), ()):
            # A short wall with column joins needs a confirmed endpoint to transfer the joins.
            if joins or candidate["type"] != wall["type"] or id(candidate) in replaced:
                continue
            short_line = _wall_line(candidate)
            if line.length <= short_line.length + _COINCIDENT_TOLERANCE_MM or not axis.covers(short_line):
                continue
            if not footprint.covers(_wall_footprint(candidate)):
                continue
            wall["handles"] = sorted(set(wall["handles"] + candidate["handles"]))
            wall["evidence"] = list(dict.fromkeys(wall["evidence"] + candidate["evidence"]))
            replaced.add(id(candidate))
        additions.append((wall, []))
    return [(wall, joined) for wall, joined in ordinary_walls if id(wall) not in replaced] + additions


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
            common_outlines = {id(item) for item in first.primitives if item.closed} & {
                id(item) for item in second.primitives if item.closed
            }
            candidates.append((not common_outlines, -overlap, tolerance, i, j, width, start, end, common_outlines))

    used_lines = set()
    column_index = SpatialIndex(columns, lambda item: item[1])
    output = []
    by_outline = defaultdict(list)
    for not_common, _, _, i, j, width, start, end, common_outlines in sorted(candidates, key=lambda row: row[:5]):
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
        wall = {
            "category": "wall",
            "type": {"name": f"墙 {width:g} mm", "thickness": width},
            "geometry": {"kind": "line", "start": rounded(snapped[0]), "end": rounded(snapped[1])},
            "confidence": 0.9 if not not_common else 0.87 if relations else 0.78,
            "handles": source_handles((*lines[i].primitives, *lines[j].primitives)),
            "evidence": evidence,
        }
        output.append((wall, relations))
        for outline in common_outlines:
            by_outline[outline].append((wall, relations))
    return _replace_covered_walls(output, by_outline, _supplement_closed_outlines(items, columns, output, rules))
