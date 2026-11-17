"""Place ordinary structural columns only when the plan and table agree."""

from collections import Counter, defaultdict
from math import dist, isfinite
import re
from unicodedata import normalize

from shapely.geometry import Point

from tumo_engine.index import SpatialIndex
from tumo_engine.preprocess import layer_key
from tumo_engine.recognize.geometry import direction, heading, rounded, source_handles
from tumo_engine.recognize.level import floor_from_title
from tumo_engine.recognize.standard_floors import standard_floor_span
from tumo_engine.rules import defaults


def _floor_span(label):
    text = normalize("NFKC", label).strip()
    if re.fullmatch(r"B?\d+F", text, re.I):
        floor = floor_from_title(f"{text}层平面图")
        return (floor, floor) if floor is not None else None
    if not text.endswith("平面图"):
        text += "平面图"
    span, issue = standard_floor_span(text)
    if issue or span:
        return span
    if not re.fullmatch(r"(?:首层|地下一层|(?:B?\d+F?|[一二三四五六七八九十]+)层)(?:墙柱|结构)?平面图", text, re.I):
        return None
    floor = -1 if text.startswith("地下") else floor_from_title(text.replace("墙柱", ""))
    return (floor, floor) if floor is not None else None


def _mark(text):
    return normalize("NFKC", text).strip().upper()


def _column_shapes(items, layer):
    for item in items:
        if (item.closed and len(item.points) >= 3 and layer.fullmatch(layer_key(item.layer))
                and (shape := item.shape).is_valid and shape.area > 0):
            yield item, shape


def _leader_tips(label, leaders, rules):
    at = Point(label.points[0])
    caption = rules["caption_tolerance"]
    for leader in leaders.nearby(at, caption):
        first, last = map(Point, (leader.points[0], leader.points[-1]))
        for tip in (first, last):
            if rules["tip_to_caption"][0] <= tip.distance(at) <= rules["tip_to_caption"][1]:
                yield leader, tip


def _position(label, outlines, fills, leaders, rules):
    candidates = {}
    tolerance = rules["tip_tolerance"]
    fill_tolerance = rules["fill_tolerance"]
    for leader, point in _leader_tips(label, leaders, rules):
        for outline, shape in outlines.nearby(point, tolerance):
            if shape.boundary.distance(point) > tolerance:
                continue
            matching = [fill for fill, fill_shape in fills.nearby(shape, fill_tolerance)
                        if shape.hausdorff_distance(fill_shape) <= fill_tolerance]
            if matching:
                key = outline.handles, shape.normalize().wkb
                found, _, sources = candidates.setdefault(key, (outline, shape, []))
                sources.extend((found, leader, *matching))
    if not candidates:
        return None
    candidates = list(candidates.values())
    # The editable column outline and an attached structural outline can describe the same column.
    outline, shape, _ = min(candidates, key=lambda row: (
        layer_key(row[0].layer) != rules["preferred_outline_layer"], row[0].handles,
    ))
    if any(shape.hausdorff_distance(other) > fill_tolerance for _, other, _ in candidates):
        return None
    return outline, shape, [item for _, _, sources in candidates for item in sources]


def _profile_rotation(shape, width, depth, tolerance):
    rectangle = shape.minimum_rotated_rectangle
    if rectangle.geom_type != "Polygon" or shape.hausdorff_distance(rectangle) > tolerance:
        return None
    first, second, third, *_ = rectangle.exterior.coords
    if abs(width - depth) <= tolerance:
        angle = heading(direction(first, second)) % 90
        if (abs(dist(first, second) - width) <= tolerance and abs(dist(second, third) - depth) <= tolerance):
            return round(angle, 3) % 90
        return None
    for a, b, c in ((first, second, third), (third, second, first)):
        if abs(dist(a, b) - width) <= tolerance and abs(dist(b, c) - depth) <= tolerance:
            return round(heading(direction(a, b)), 3) % 180
    return None


def recognize_structural_columns(items, table_rows, *, plan_title, floor_number, rules=None):
    """Return (column candidate, footprint) pairs and source-located warnings from millimetre plan primitives."""
    cfg = (rules or defaults())["structural_column"]
    layers = {key: re.compile(pattern) for key, pattern in cfg["layers"].items()}
    items = list(items)
    labels = [item for item in items if item.kind == "text" and item.points
              and layers["label"].fullmatch(layer_key(item.layer)) and re.fullmatch(cfg["mark_pattern"], _mark(item.text))]
    if not labels:
        return [], []

    rows = defaultdict(list)
    plan = _floor_span(plan_title)
    if plan and plan[0] <= floor_number <= plan[1]:
        for row in table_rows:
            span = _floor_span(row["floors"])
            if row["scope"] == "column" and span and span[0] <= floor_number <= span[1]:
                rows[_mark(row["mark"])].append(row)
    outlines = SpatialIndex(_column_shapes((item for item in items if item.kind == "polyline"), layers["outline"]),
                            lambda row: row[1])
    fills = SpatialIndex(_column_shapes((item for item in items if item.kind == "hatch"), layers["fill"]),
                         lambda row: row[1])
    leaders = SpatialIndex((item for item in items if item.kind in {"line", "polyline"} and not item.closed
                            and layers["leader"].fullmatch(layer_key(item.layer))), lambda item: item.shape)
    warnings, located, used = [], [], Counter()
    for label in labels:
        mark = _mark(label.text)
        at = rounded(label.points[0])
        position = _position(label, outlines, fills, leaders, cfg)
        if position:
            key = position[0].handles, position[1].normalize().wkb
            used[key] += 1
            located.append((key, label, position))
        else:
            warnings.append((f"结构柱 {mark} 的引线未指向唯一吻合的闭合柱轮廓和填充。", at))

    accepted = []
    for key, label, (_, shape, sources) in located:
        mark = _mark(label.text)
        at = rounded(label.points[0])
        records = rows[mark]
        if used[key] != 1 or len(records) != 1:
            warnings.append((f"结构柱 {mark} 的平面柱位或本层柱表截面不唯一。请核对原图。", at))
            continue
        row = records[0]
        width, depth = row["section"]["width_mm"], row["section"]["depth_mm"]
        if not (all(isfinite(value) and value > 0 for value in (width, depth)) and
                (angle := _profile_rotation(shape, width, depth, cfg["section_tolerance"])) is not None):
            warnings.append((f"结构柱 {mark} 的闭合截面与本层柱表尺寸不符。请核对原图。", at))
            continue
        sheet = row["sheet"]["number"]
        section_trace = f"column_schedule:{sheet}:" + ",".join(sorted(set(row["handles"])))
        candidate = {
            "category": "column", "type": {"mark": mark, "name": f"结构柱 {width:g}×{depth:g} mm", "width": width, "depth": depth},
            "geometry": {"kind": "point", "at": rounded(shape.centroid.coords[0]), "rotation_deg": angle},
            "confidence": 0.95, "handles": source_handles((label, *sources)),
            "evidence": ["structural_column:leader_to_closed_section", "structural_column:matching_fill", section_trace],
        }
        accepted.append((candidate, shape))
    return sorted(accepted, key=lambda row: row[0]["geometry"]["at"]), sorted(warnings, key=lambda row: (row[1], row[0]))
