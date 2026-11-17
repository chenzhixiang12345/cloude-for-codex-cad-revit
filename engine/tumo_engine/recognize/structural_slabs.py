"""Identify explicitly lowered structural slab regions and their CAD evidence."""

from collections import defaultdict
from math import cos, isfinite, radians, sin
import re
from unicodedata import normalize

from ezdxf import DXFError
from ezdxf.path import from_hatch_boundary_path
from shapely.geometry import Point, Polygon, box

from tumo_engine.index import SpatialIndex
from tumo_engine.preprocess import decompose, hidden_layer_names, layer_key, point, unit_factor
from tumo_engine.recognize.geometry import rounded, source_handles
from tumo_engine.rules import defaults


def _text(value):
    return normalize("NFKC", value).strip().replace("−", "-").replace("－", "-")


def _pattern(entity, digits):
    scale = entity.dxf.pattern_scale
    if entity.dxf.solid_fill or not entity.pattern or not isfinite(scale) or scale <= 0:
        return None
    angle = entity.dxf.pattern_angle
    if not isfinite(angle):
        return None
    c, s = cos(radians(angle)), sin(radians(angle))

    def local(vector):
        x, y = vector
        return ((x * c + y * s) / scale, (y * c - x * s) / scale)

    def quantize(vector):
        return tuple(round(value, digits) for value in vector)

    rows = sorted((round((line.angle - angle) % 360, digits) % 360, quantize(local(line.offset)),
                   tuple(round(value / scale, digits) for value in line.dash_length_items), local(line.base_point))
                  for line in entity.pattern.lines)
    if not rows:
        return None
    anchor = rows[0][-1]
    signature = tuple((direction, offset, dashes, quantize((base[0] - anchor[0], base[1] - anchor[1])))
                      for direction, offset, dashes, base in rows)
    return entity.dxf.pattern_name.upper(), bool(entity.dxf.pattern_double), signature


def _native_ring(path, tolerance):
    if vertices := getattr(path, "vertices", None):
        if not path.is_closed or path.has_bulge():
            return None
        return [vertex[:2] for vertex in vertices]
    edges = getattr(path, "edges", [])
    if not edges or any(type(edge).__name__ != "LineEdge" for edge in edges):
        return None
    if any((edge.end - successor.start).magnitude > tolerance
           for edge, successor in zip(edges, (*edges[1:], edges[0]))):
        return None
    return [edge.start for edge in edges]


def _polygon(vertices):
    if len(vertices) < 3:
        return None
    shape = Polygon(vertices)
    return shape if shape.is_valid and shape.area > 0 else None


def _plan_hatches(drawing, items, bounds, matcher, tolerance):
    hatch_items = [item for item in items if item.kind == "hatch" and matcher.fullmatch(layer_key(item.layer))]
    if bounds is None:
        groups = defaultdict(list)
        for item in hatch_items:
            groups[item.handles].append(item)
        for handles, group in groups.items():
            entity = drawing.entitydb.get(handles[-1]) if handles else None
            if entity is None or entity.dxftype() != "HATCH":
                continue
            rings = [_native_ring(path, tolerance) for path in entity.paths]
            valid = len(rings) == 1 and rings[0] is not None and bool(entity.paths.paths[0].path_type_flags & 1)
            for item in group:
                if shape := _polygon(item.points):
                    yield entity, shape, handles, valid
        return

    # Read native instances from the covered source containers before deduplication can erase overlapping fills.
    roots = {item.handles[0] for item in items if len(item.handles) > 1 and matcher.fullmatch(layer_key(item.layer))}
    factor, area = unit_factor(drawing), box(*bounds)
    source = (entity for entity in drawing.modelspace() if entity.dxftype() == "HATCH" or entity.dxf.handle in roots)
    for entity, layer, handles, _ in decompose(source, hidden_layers=hidden_layer_names(drawing)):
        if entity.dxftype() != "HATCH" or not matcher.fullmatch(layer_key(layer)) or entity.dxf.extrusion.z < 1:
            continue
        paths = list(entity.paths)
        for path in paths:
            vertices = _native_ring(path, tolerance / factor)
            linear = vertices is not None
            if not linear:
                # Curved or open paths can obstruct confirmed boards but cannot establish one.
                try:
                    curve = from_hatch_boundary_path(path)
                    vertices = tuple(curve.flattening(tolerance / factor))
                except (DXFError, ValueError, TypeError, ZeroDivisionError):
                    continue
            shape = _polygon([point(vertex, factor) for vertex in vertices])
            if shape and area.intersects(shape):
                valid = linear and len(paths) == 1 and bool(path.path_type_flags & 1) and area.covers(shape)
                yield entity, shape, handles, valid


def _same_boundary(shape, candidates, tolerance):
    return [item for item in candidates.nearby(shape, tolerance)
            if shape.hausdorff_distance(item.shape) <= tolerance and shape.symmetric_difference(item.shape).area
            <= shape.length * tolerance]


def _legends(drawing, fills, outlines, notes, definitions, rules):
    rows = defaultdict(list)
    labels_by_hatch = {}
    usages = defaultdict(int)
    for fill in fills:
        references = definitions.get(fill.handles[:-1], ())
        if len(references) != 1:
            continue
        entity = drawing.entitydb.get(fill.handles[-1]) if fill.handles else None
        if (entity is None or entity.dxftype() != "HATCH" or len(entity.paths) != 1
                or not entity.paths.paths[0].path_type_flags & 1
                or _native_ring(entity.paths.paths[0], rules["outline_tolerance"]) is None):
            continue
        same_outline = [item for item in _same_boundary(fill.shape, outlines, rules["outline_tolerance"])
                        if item.handles[:-1] == fill.handles[:-1]]
        if len(same_outline) != 1:
            continue
        left, bottom, _, _ = fill.shape.bounds
        candidates = []
        for label, offset in notes:
            x, y = label.points[0]
            if (label.handles[:-1] == fill.handles[:-1] and rules["legend_x"][0] <= left - x <= rules["legend_x"][1]
                    and rules["legend_y"][0] <= bottom - y <= rules["legend_y"][1]):
                candidates.append((label, offset))
                usages[label.handles] += 1
        if len(candidates) == 1 and (signature := _pattern(entity, rules["pattern_digits"])):
            labels_by_hatch[fill.handles] = signature, same_outline[0], fill, candidates[0], references[0]
    for signature, outline, fill, (label, offset), reference in labels_by_hatch.values():
        if usages[label.handles] == 1:
            rows[signature].append((offset, source_handles((outline, fill, label, reference))))
    return rows


def recognize_structural_slabs(drawing, items, rules=None, *, bounds=None):
    """Return confirmed board regions with relative structural Hs offsets and review warnings."""
    cfg = (rules or defaults())["structural_slab"]
    tolerance = cfg["outline_tolerance"]
    board_layer, legend_layer, label_layer, hole_layer = (re.compile(cfg["layers"][kind]) for kind in
                                                          ("board", "legend", "thickness", "hole"))
    items = list(items)
    plan_boundaries, legend_boundaries, legend_fills, legend_notes, notes, holes = [], [], [], [], [], []
    structural_references = defaultdict(list)
    for item in items:
        layer = layer_key(item.layer)
        if item.kind == "text" and item.points:
            text = _text(item.text)
            if legend_layer.fullmatch(layer) and re.fullmatch(cfg["reference_pattern"], text, re.I):
                structural_references[item.handles[:-1]].append(item)
            if legend_layer.fullmatch(layer) and (match := re.fullmatch(cfg["legend_pattern"], text, re.I)):
                value = float(match["value"])
                unit = (match["unit"] or "M").upper()
                if isfinite(value) and value > 0:
                    legend_notes.append((item, -round(value * {"M": 1000, "CM": 10, "MM": 1}[unit], 3)))
            if label_layer.fullmatch(layer) and (match := re.fullmatch(cfg["thickness_pattern"], text, re.I)):
                thickness = float(match["thickness"])
                if cfg["thickness_range"][0] <= thickness <= cfg["thickness_range"][1]:
                    notes.append((item, match["mark"].upper(), thickness))
        elif hole_layer.fullmatch(layer) and item.kind in {"line", "polyline", "hatch", "circle", "arc"}:
            if item.shape.is_valid:
                holes.append(item.shape)
        elif item.closed and len(item.points) >= 3 and item.shape.is_valid:
            if board_layer.fullmatch(layer) and item.kind == "polyline":
                plan_boundaries.append(item)
            elif legend_layer.fullmatch(layer) and item.kind == "polyline":
                legend_boundaries.append(item)
            elif legend_layer.fullmatch(layer) and item.kind == "hatch":
                legend_fills.append(item)

    legend = _legends(drawing, legend_fills, SpatialIndex(legend_boundaries, lambda item: item.shape), legend_notes,
                      structural_references, cfg)
    boards = list(_plan_hatches(drawing, items, bounds, board_layer, tolerance))
    if not boards:
        return [], []
    board_index = SpatialIndex(enumerate(boards), lambda row: row[1][1])
    outlines = SpatialIndex(plan_boundaries, lambda item: item.shape)
    hole_index = SpatialIndex(holes, lambda shape: shape)
    note_index = SpatialIndex(notes, lambda note: Point(note[0].points[0]))
    slabs, warnings = [], []
    for number, (entity, shape, handles, valid) in enumerate(boards):
        at = rounded(shape.representative_point().coords[0])
        labels = [note for note in note_index.nearby(shape, 0) if shape.contains(Point(note[0].points[0]))]
        if len(labels) != 1:
            warnings.append(("结构板区内缺少唯一板号和板厚。请核对原图。", at))
            continue
        same_outline = _same_boundary(shape, outlines, tolerance)
        if (not valid or len(same_outline) != 1 or same_outline[0].handles[-1] == handles[-1]):
            warnings.append(("结构板填充没有一个独立吻合的闭合外边界。", at))
            continue
        signature = _pattern(entity, cfg["pattern_digits"])
        matching_legend = legend.get(signature, ())
        if len(matching_legend) != 1:
            warnings.append(("结构板填充没有唯一匹配的同图板顶降板图例。", at))
            continue
        if (any(number != other and shape.intersection(board[1]).area > cfg["overlap_tolerance_area"]
                for other, board in board_index.nearby(shape, 0))
                or any(shape.intersects(hole) for hole in hole_index.nearby(shape, 0))):
            warnings.append(("结构板区与其他板区或洞口相交。请核对边界。", at))
            continue

        label, mark, thickness = labels[0]
        offset, legend_handles = matching_legend[0]
        source = sorted({*handles, *legend_handles, *source_handles((same_outline[0], label))})
        slabs.append({
            "category": "slab", "type": {"mark": mark, "name": f"结构楼板 {thickness:g} mm", "thickness": thickness},
            "geometry": {"kind": "polygon", "points": [rounded(vertex) for vertex in shape.normalize().exterior.coords[:-1]]},
            "base_offset": offset, "confidence": 0.93, "handles": source,
            "evidence": ["structural_slab:closed_board_outline", "structural_slab:local_thickness", "structural_slab:matched_hs_legend"],
        })
    slabs.sort(key=lambda slab: (slab["type"]["mark"], slab["geometry"]["points"]))
    return slabs, sorted(warnings, key=lambda warning: (warning[1], warning[0]))
