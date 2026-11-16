import re
from math import dist

from shapely.geometry import Point
from shapely.ops import polygonize

from tumo_engine.index import SpatialIndex
from tumo_engine.preprocess import layer_key
from tumo_engine.recognize.geometry import direction, heading, rounded, segments, source_handles


def _classification(shape, cfg):
    if not shape.is_valid or shape.area <= 0:
        return None
    rectangle = shape.minimum_rotated_rectangle
    if rectangle.area <= 0 or shape.area / rectangle.area < cfg["rectangularity"]:
        return None
    points = list(rectangle.exterior.coords)
    a, b = dist(points[0], points[1]), dist(points[1], points[2])
    if not cfg["minimum_dimension"] <= min(a, b) <= max(a, b) <= cfg["maximum_dimension"]:
        return None
    if max(a, b) / min(a, b) > cfg["maximum_aspect"]:
        return None
    angle = heading(direction(points[0], points[1]) if a >= b else direction(points[1], points[2]))
    return {"width": round(max(a, b), 3), "depth": round(min(a, b), 3)}, round(angle, 3)


def recognize_columns(items, rules):
    cfg = rules["column"]
    layers = rules["layers"]
    marks = SpatialIndex(
        (item for item in items if item.kind == "text" and re.fullmatch(cfg["mark_pattern"], item.text.strip().upper())),
        lambda item: Point(item.points[0]),
    )
    result = []
    profile_lines = []
    for item in items:
        layer = layer_key(item.layer)
        on_layer = bool(re.search(layers["columns"], layer))
        on_layer |= any(re.search(layers["columns"], name.upper()) for name in item.blocks)
        profile = bool(re.search(layers["structural_profiles"], layer))
        if not on_layer and not profile:
            continue
        if not item.closed and item.kind != "circle":
            if on_layer:
                profile_lines.append(item)
            continue
        shape = item.shape
        if item.kind == "circle":
            if not on_layer or not cfg["minimum_dimension"] <= item.radius * 2 <= cfg["maximum_dimension"]:
                continue
            dimensions, angle = {"diameter": round(item.radius * 2, 3)}, 0
        else:
            classification = _classification(shape, cfg)
            if classification is None:
                continue
            dimensions, angle = classification
        result.append((shape, dimensions, angle, [item], on_layer))

    if profile_lines:
        lines = list(segments(profile_lines))
        index = SpatialIndex(lines, lambda line: line.geometry)
        for shape in polygonize([line.geometry for line in lines]):
            classification = _classification(shape, cfg)
            if classification is None:
                continue
            dimensions, angle = classification
            boundary = shape.boundary
            sources = [item for line in index.nearby(boundary, 1e-6)
                       if boundary.covers(line.geometry) for item in line.primitives]
            if sources:
                result.append((shape, dimensions, angle, sources, True))

    columns = []
    for shape, dimensions, angle, sources, on_layer in sorted(result, key=lambda entry: not entry[4]):
        center = shape.centroid
        if any(center.distance(existing[1].centroid) < 30 for existing in columns):
            continue
        nearby = marks.nearby(shape, cfg["mark_distance"])
        mark = min(nearby, key=lambda text: center.distance(Point(text.points[0]))) if nearby else None
        evidence = ["column:profile", f"layer:{layer_key(sources[0].layer)}"]
        if mark:
            evidence.append(f"mark:{mark.text}")
            dimensions["mark"] = mark.text
        size = "×".join(str(round(value)) for key, value in dimensions.items() if key != "mark")
        dimensions["name"] = f"柱 {size}"
        data = {
            "category": "column",
            "type": dimensions,
            "geometry": {"kind": "point", "at": rounded(center.coords[0]), "rotation_deg": angle},
            "confidence": 0.95 if on_layer and mark else 0.86 if on_layer else 0.72 if mark else 0.58,
            "handles": source_handles((*sources, mark) if mark else sources),
            "evidence": evidence,
        }
        columns.append((data, shape))
    return columns
