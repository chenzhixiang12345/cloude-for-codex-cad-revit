import re
from unicodedata import normalize

from shapely.geometry import Point

from tumo_engine.index import SpatialIndex
from tumo_engine.preprocess import layer_key
from tumo_engine.recognize.geometry import dot, merge_collinear, segments, source_handles, rounded


def _label(item, pattern):
    label = normalize("NFKC", item.text).strip().upper()
    return label if re.fullmatch(pattern, label) else None


def recognize_grids(items, rules):
    cfg = rules["grid"]
    layers = rules["layers"]
    grid_items = [
        item for item in items
        if re.search(layers["grids"], layer_key(item.layer))
        and not re.search(layers["grid_exclude"], layer_key(item.layer))
    ]
    texts = [
        (item, _label(item, cfg["label_pattern"])) for item in items
        if item.kind == "text" and _label(item, cfg["label_pattern"])
    ]
    text_index = SpatialIndex(texts, lambda pair: Point(pair[0].points[0]))
    circles = SpatialIndex(
        (item for item in items if item.kind == "circle" and 80 <= item.radius <= 850),
        lambda item: Point(item.points[0]),
    )
    lines = merge_collinear(list(segments(grid_items)), cfg["gap"], cfg["collinear_tolerance"], cfg["direction_tolerance_deg"])
    results = []
    for line in lines:
        if line.length < cfg["minimum_length"]:
            continue
        # Extension lines can also be collinear with the axis. Prefer candidates
        # whose labels have independent bubble/axis annotation evidence.
        options = []
        axis = line.axis
        perpendicular = -axis[1], axis[0]
        for end in (line.a, line.b):
            for item, label in text_index.nearby(Point(end), cfg["end_search"]):
                p = item.points[0]
                delta = p[0] - end[0], p[1] - end[1]
                lateral = abs(dot(delta, perpendicular))
                if lateral > cfg["lateral_tolerance"]:
                    continue
                bubble = any(
                    Point(circle.points[0]).distance(Point(p)) <= circle.radius
                    for circle in circles.nearby(Point(p), 850)
                )
                on_layer = bool(re.search(layers["grid_text"], layer_key(item.layer)))
                on_layer &= not bool(re.search(layers["grid_exclude"], layer_key(item.layer)))
                source_match = any(set(item.handles) & set(geom.handles) for geom in line.primitives)
                if not bubble and not on_layer and not source_match:
                    continue
                options.append(((not bubble, not on_layer, lateral, Point(end).distance(Point(p))), label, item, bubble))
        if not options:
            continue
        _, label, text, bubble = min(options, key=lambda item: item[0])
        results.append({
            "label": label,
            "start": rounded(line.a),
            "end": rounded(line.b),
            "confidence": 0.96 if bubble else 0.86,
            "handles": source_handles((*line.primitives, text)),
            "evidence": ["axis:line", "axis:label"] + (["axis:bubble"] if bubble else []),
        })
    return results
