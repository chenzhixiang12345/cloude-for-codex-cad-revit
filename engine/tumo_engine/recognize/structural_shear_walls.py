"""Resolve a numbered wall body between independently drawn edge components."""

from collections import Counter, defaultdict
from math import hypot, isfinite
import re
from unicodedata import normalize

from shapely.geometry import Point, Polygon

from tumo_engine.index import SpatialIndex
from tumo_engine.preprocess import layer_key
from tumo_engine.recognize.geometry import dot, parallel, rounded, segments, source_handles, world
from tumo_engine.rules import defaults


def _lerp(segment, coordinate, axis):
    lo, hi = dot(segment.a, axis), dot(segment.b, axis)
    fraction = (coordinate - lo) / (hi - lo)
    return tuple(a + fraction * (b - a) for a, b in zip(segment.a, segment.b))


def _closed_polygon(item):
    return item.closed and len(item.points) >= 3 and item.shape.is_valid and item.shape.area > 0


def _edge_components(outlines, fills, tolerance):
    fill_index = SpatialIndex((fill for fill in fills if _closed_polygon(fill)), lambda item: item.shape)
    paths = Counter(fill.handles for fill in fills)
    for outline in outlines:
        shape = outline.shape
        matching = [fill for fill in fill_index.nearby(shape, tolerance)
                    if shape.hausdorff_distance(fill.shape) <= tolerance
                    and shape.symmetric_difference(fill.shape).area <= shape.length * tolerance]
        if len(matching) == 1 and paths[matching[0].handles] == 1 and matching[0].handles != outline.handles:
            yield outline, matching[0]


def _wall_faces(wall, anchor, thickness, cfg):
    faces = list(segments((wall,)))
    tolerance = cfg["outline_tolerance"]
    for face in faces:
        if face.geometry.distance(anchor) > cfg["label_distance"]:
            continue
        axis = face.axis
        label_along = dot(anchor.coords[0], axis)
        near_low, near_high = face.projected(axis)
        for other in faces:
            if other is face or not parallel(other.axis, axis, cfg["angle_deg"]):
                continue
            if abs(abs(face.offset(axis) - other.offset(axis)) - thickness) > tolerance:
                continue
            far_low, far_high = other.projected(axis)
            low, high = max(near_low, far_low), min(near_high, far_high)
            if low + tolerance < label_along < high - tolerance:
                yield face, other, axis, low, high


def _edge_boundaries(edge_index, wall_shape, pair, label, cfg):
    face, other, axis, low, high = pair
    normal = (-axis[1], axis[0])
    tolerance = cfg["outline_tolerance"]
    probe = cfg["edge_probe"]
    label_along = dot(label.coords[0], axis)
    lower, upper = [], []
    for outline, fill in edge_index.nearby(wall_shape, tolerance):
        shape = outline.shape
        for end in segments((outline,)):
            if not parallel(end.axis, normal, cfg["angle_deg"]):
                continue
            edge_low, edge_high = end.projected(normal)
            end_along = sum(end.projected(axis)) / 2
            if not low - tolerance <= end_along <= high + tolerance or abs(end_along - label_along) <= probe:
                continue
            left, right = sorted(dot(_lerp(side, end_along, axis), normal) for side in (face, other))
            if edge_low > left + tolerance or edge_high < right - tolerance:
                continue
            # Use the real cross edge for the end location. Probe only its filled side.
            center = (left + right) / 2
            position = dot(_lerp(end, center, normal), axis)
            outward = -probe if position < label_along else probe
            if (shape.contains(Point(world(position + outward, center, axis)))
                    and not shape.covers(Point(world(position - outward, center, axis)))):
                (lower if outward < 0 else upper).append((position, outline, fill))
    if len(lower) == len(upper) == 1 and lower[0][1].handles != upper[0][1].handles:
        return lower[0], upper[0]
    return None


def _body(wall, pair, label, thickness, edge_index, label_index, blockers, openings, cfg):
    face, other, axis, _, _ = pair
    shape = wall.shape
    boundaries = _edge_boundaries(edge_index, shape, pair, label, cfg)
    if boundaries is None:
        return None
    (low, first_outline, first_fill), (high, last_outline, last_fill) = boundaries
    tolerance = cfg["outline_tolerance"]
    if high - low < cfg["min_length"]:
        return None
    start_face, start_other = (_lerp(side, low, axis) for side in (face, other))
    end_face, end_other = (_lerp(side, high, axis) for side in (face, other))
    for a, b in ((start_face, start_other), (end_face, end_other)):
        if abs(hypot(a[0] - b[0], a[1] - b[1]) - thickness) > tolerance:
            return None
    footprint = Polygon((start_face, end_face, end_other, start_other))
    if not footprint.is_valid or not shape.buffer(tolerance).covers(footprint):
        return None
    if sum(low <= dot(anchor.coords[0], axis) <= high
           for _, anchor, _ in label_index.nearby(footprint, cfg["label_distance"])) != 1:
        return None
    if openings.nearby(footprint, 0):
        return None
    interior = footprint.buffer(-tolerance)
    for blocker, blocker_shape in blockers.nearby(footprint, 0):
        if blocker is wall:
            continue
        if interior.intersects(blocker_shape):
            return None
    start = tuple((a + b) / 2 for a, b in zip(start_face, start_other))
    end = tuple((a + b) / 2 for a, b in zip(end_face, end_other))
    return start, end, footprint, (first_outline, first_fill, last_outline, last_fill)


def recognize_structural_shear_walls(items, table_rows, *, floor_number, legend_items=None, rules=None):
    """Return confirmed wall candidates with footprints and location-based review issues."""
    cfg = (rules or defaults())["structural_shear_wall"]
    layers = {name: re.compile(pattern) for name, pattern in cfg["layers"].items()}
    mark_pattern = re.compile(cfg["mark_pattern"], re.I)
    legend_pattern = re.compile(cfg["legend_pattern"])
    table = defaultdict(list)
    for row in table_rows:
        span = row.get("floor_span")
        size = row.get("thickness_mm")
        if (span and span[0] <= floor_number <= span[1] and row.get("mark")
                and isinstance(size, (int, float)) and isfinite(size) and size > 0):
            table[row["mark"].upper()].append(row)

    items = list(items)
    legend_items = items if legend_items is None else legend_items
    legends = [item for item in legend_items if item.kind == "text" and layers["legend"].fullmatch(layer_key(item.layer))
               and legend_pattern.search(normalize("NFKC", item.text).strip())]
    labels, walls, outlines, fills, constraints, openings = [], [], [], [], [], []
    for item in items:
        layer = layer_key(item.layer)
        if item.kind == "text":
            value = normalize("NFKC", item.text).strip()
            if layers["label"].fullmatch(layer) and mark_pattern.fullmatch(value):
                labels.append((item, Point(item.points[0]), value))
            continue
        if item.kind in {"polyline", "line"} and layers["wall"].fullmatch(layer):
            walls.append(item)
        if item.kind == "polyline" and layers["edge_outline"].fullmatch(layer) and _closed_polygon(item):
            outlines.append(item)
        if item.kind == "hatch" and layers["edge_fill"].fullmatch(layer):
            fills.append(item)
        if layers["constraint"].fullmatch(layer):
            constraints.append(item)
        if layers["opening"].fullmatch(layer):
            openings.append(item.shape)

    tolerance = cfg["outline_tolerance"]
    wall_index = SpatialIndex(((wall, wall.shape) for wall in walls if wall.shape.is_valid), lambda pair: pair[1])
    label_index = SpatialIndex(labels, lambda item: item[1])
    edge_index = SpatialIndex(_edge_components(outlines, fills, tolerance), lambda pair: pair[0].shape)
    blockers = SpatialIndex(((item, item.shape) for item in (*walls, *outlines, *fills, *constraints)
                             if item.shape.is_valid), lambda pair: pair[1])
    opening_index = SpatialIndex((shape for shape in openings if shape.is_valid), lambda shape: shape)
    found, issues = [], []
    for label, anchor, mark in labels:
        rows = table[mark.upper()]
        near = [item for item, shape in wall_index.nearby(anchor, cfg["label_distance"])
                if (shape.boundary if shape.geom_type == "Polygon" else shape).distance(anchor) <= cfg["label_distance"]]
        problem = None
        if len(legends) != 1:
            problem = "缺少唯一的同图现浇墙体和边缘构件图例"
        elif len(rows) != 1:
            problem = "缺少唯一且适用于本层的墙身表记录"
        elif len(near) != 1 or not _closed_polygon(near[0]):
            problem = "编号未贴近唯一的闭合墙身轮廓"
        else:
            wall, row = near[0], rows[0]
            thickness = row["thickness_mm"]
            bodies = [body for pair in _wall_faces(wall, anchor, thickness, cfg)
                      if (body := _body(wall, pair, anchor, thickness, edge_index, label_index, blockers, opening_index, cfg))
                      is not None]
            if len(bodies) != 1:
                problem = "未找到厚度相符且由两端边缘构件限定的唯一无填充墙段"
            else:
                start, end, footprint, edge_sources = bodies[0]
                handles = sorted({*source_handles((wall, label, legends[0], *edge_sources)), *row["handles"]})
                found.append(({
                    "category": "shear_wall", "type": {"mark": mark, "name": f"剪力墙 {mark}", "thickness": thickness},
                    "geometry": {"kind": "line", "start": rounded(start), "end": rounded(end)},
                    "confidence": 0.95, "handles": handles,
                    "evidence": [f"shear_wall_schedule:{row['sheet']}:{mark}", "structural_shear_wall:matched_wall_faces",
                                 "structural_shear_wall:independent_edge_boundaries", "structural_shear_wall:unfilled_wall_body"],
                }, footprint))
        if problem:
            issues.append((f"剪力墙 {mark} {problem}。", rounded(label.points[0])))
    return (sorted(found, key=lambda pair: (pair[0]["type"]["mark"], pair[0]["geometry"]["start"])),
            sorted(issues, key=lambda issue: (issue[1], issue[0])))
