"""Read normalized CAD primitives while preserving source handles."""

from collections import Counter
from dataclasses import dataclass
from math import ceil, isfinite
from pathlib import Path
import re
from statistics import median

import ezdxf
from ezdxf import units, xref
from shapely.geometry import LineString, Point, Polygon, box

Bounds = tuple[float, float, float, float]
XY = tuple[float, float]


@dataclass
class Primitive:
    kind: str
    layer: str
    points: tuple[XY, ...]
    handles: tuple[str, ...]
    blocks: tuple[str, ...]
    text: str = ""
    radius: float = 0
    linetype: str = ""
    closed: bool = False
    sweep_deg: float = 0

    @property
    def shape(self):
        if self.kind == "circle":
            return Point(self.points[0]).buffer(self.radius)
        if self.kind == "arc":
            return LineString(self.points[1:])
        if self.closed and len(self.points) >= 3:
            return Polygon(self.points)
        if len(self.points) > 1:
            return LineString(self.points)
        return Point(self.points[0])


def hidden_layer_names(doc):
    """Return case-insensitive keys of globally hidden layers."""
    return {layer.dxf.name.casefold() for layer in doc.layers if layer.is_off() or layer.is_frozen()}


def entity_visible(entity, hidden_layers, *, effective_layer=None):
    """Test graphic flags and the effective layer after block inheritance."""
    if effective_layer is None:
        effective_layer = entity.dxf.get("layer", "0")
    if effective_layer.casefold() in hidden_layers:
        return False
    if entity.dxf.is_supported("invisible") and entity.dxf.get("invisible", 0):
        return False
    return entity.dxftype() not in {"ATTRIB", "ATTDEF"} or not entity.is_invisible


def unit_factor(doc):
    """Prefer declared units. Infer a unanimous scale only for unitless DXF."""
    if doc.units:
        try:
            return units.conversion_factor(doc.units, units.MM)
        except (IndexError, ValueError, TypeError):
            return 1.0
    hidden_layers = hidden_layer_names(doc)
    ratios = []
    for dim in doc.modelspace().query("DIMENSION"):
        if dim.dimtype not in (0, 1) or not entity_visible(dim, hidden_layers):
            continue
        text = dim.dxf.text.strip()
        if not re.fullmatch(r"\d+(?:\.\d+)?", text):
            continue
        measurement = dim.get_measurement()
        if isinstance(measurement, (int, float)) and measurement > 0:
            ratio = float(text) / measurement
            if ratio > 0 and isfinite(ratio):
                ratios.append(ratio)
    if len(ratios) >= 3:
        center = median(ratios)
        if sum(abs(ratio / center - 1) <= 0.02 for ratio in ratios) >= 0.8 * len(ratios):
            return center
    return 1.0


def point(vector, factor):
    return tuple(round(float(value) * factor, 5) for value in (vector[0], vector[1]))


def layer_key(layer):
    return re.sub(r"^(?:0[-_])+", "", re.split(r"\$\d+\$", layer.upper())[-1])


def bind_xrefs(doc, source, root):
    warnings = []
    for block in list(doc.blocks):
        header = block.block
        if not (header.is_xref or header.is_xref_overlay):
            continue
        # Bound DXFs already contain local copies of their reference blocks.
        ref = Path(header.dxf.xref_path.replace("\\", "/"))
        ref = (source.parent / ref).resolve()
        if ref == source.resolve() or not ref.is_relative_to(root.resolve()) or ref.suffix.lower() != ".dxf" or not ref.is_file():
            warnings.append(f"外部参照未读取：{header.dxf.name}。请在 CAD 中绑定参照后导出 DXF。")
            continue
        try:
            xref.embed(block, search_paths=[source.parent], load_fn=lambda _: ezdxf.readfile(ref))
        except (xref.XrefError, ezdxf.DXFError, OSError):
            warnings.append(f"外部参照读取失败：{header.dxf.name}。")
    return warnings


def viewport_bounds(doc, factor):
    """Find the model-space area of the largest active top-view viewport."""
    candidates = []
    for layout in doc.layouts:
        if layout.is_modelspace:
            continue
        for viewport in layout.query("VIEWPORT"):
            data = viewport.dxf
            if data.id <= 1 or data.status < 1 or data.view_height <= 0 or data.height <= 0:
                continue
            axis = data.view_direction_vector
            if abs(axis.x) > 1e-6 or abs(axis.y) > 1e-6 or axis.z <= 0:
                continue
            cx = data.view_center_point.x + data.view_target_point.x
            cy = data.view_center_point.y + data.view_target_point.y
            width = data.view_height * data.width / data.height
            points = [
                point((cx - width / 2, cy - data.view_height / 2), factor),
                point((cx + width / 2, cy + data.view_height / 2), factor),
            ]
            if abs(data.view_twist_angle) <= 1e-6:
                candidates.append((width * data.view_height, (*points[0], *points[1])))
    return max(candidates)[1] if candidates else None


def decompose(entities, layer="0", handles=(), names=(), skipped=None, *, hidden_layers=frozenset()):
    """Expand nested INSERTs and preserve the top-level and definition handles."""
    for entity in entities:
        kind = entity.dxftype()
        logical_layer = entity.dxf.get("layer", "0")
        if logical_layer == "0":
            logical_layer = layer
        if not entity_visible(entity, hidden_layers, effective_layer=logical_layer):
            continue
        original = entity.source_of_copy or entity
        handle = original.dxf.handle
        trace = (*handles, handle) if handle and handle not in handles else handles
        if kind != "INSERT":
            yield entity, logical_layer, trace, names
            continue
        name = entity.dxf.name
        if name in names:
            continue
        if entity.mcount > 1:
            for expanded in entity.multi_insert():
                # multi_insert uses free-standing virtual copies.
                expanded.set_source_of_copy(original)
                yield from decompose(
                    [expanded], logical_layer, trace, names, skipped, hidden_layers=hidden_layers,
                )
            continue
        for attrib in entity.attribs:
            attr_layer = attrib.dxf.layer if attrib.dxf.layer != "0" else logical_layer
            if not entity_visible(attrib, hidden_layers, effective_layer=attr_layer):
                continue
            ancestor = attrib.source_of_copy or attrib
            attr_trace = (*trace, ancestor.dxf.handle) if ancestor.dxf.handle else trace
            yield attrib, attr_layer, attr_trace, (*names, name)
        try:
            children = entity.virtual_entities(skipped_entity_callback=skipped)
            yield from decompose(
                children, logical_layer, trace, (*names, name), skipped, hidden_layers=hidden_layers,
            )
        except (ezdxf.DXFError, RecursionError):
            if skipped:
                skipped(entity, "无法展开嵌套块")


def _make_primitive(entity, layer, handles, names, factor):
    kind = entity.dxftype()
    linetype = entity.dxf.get("linetype", "")
    if linetype in {"", "BYLAYER"} and entity.doc is not None:
        try:
            linetype = entity.doc.layers.get(layer).dxf.linetype
        except ezdxf.DXFTableEntryError:
            pass
    if kind == "LINE":
        points = tuple(point(p, factor) for p in (entity.dxf.start, entity.dxf.end))
        return Primitive("line", layer, points, handles, names, linetype=linetype)
    if kind in {"LWPOLYLINE", "POLYLINE"}:
        if kind == "LWPOLYLINE":
            if entity.has_arc:
                return None
            vertices = entity.vertices_in_wcs()
            closed = entity.closed
        else:
            if not entity.is_2d_polyline or entity.has_arc:
                return None
            vertices = entity.points_in_wcs()
            closed = entity.is_closed
        points = tuple(dict.fromkeys(point(vertex, factor) for vertex in vertices))
        return Primitive("polyline", layer, points, handles, names, linetype=linetype, closed=closed) if len(points) > 1 else None
    if kind == "CIRCLE":
        return Primitive("circle", layer, (point(entity.ocs().to_wcs(entity.dxf.center), factor),), handles, names, radius=entity.dxf.radius * factor)
    if kind == "ARC":
        radius = entity.dxf.radius * factor
        start = entity.dxf.start_angle
        sweep = (entity.dxf.end_angle - start) % 360
        ocs = entity.ocs()
        if not (isfinite(radius) and radius > 0 and isfinite(sweep) and sweep > 0 and abs(ocs.uz.z) >= 1 - 1e-8):
            return None
        steps = ceil(sweep / 10)
        offsets = {sweep * index / steps for index in range(steps + 1)}
        for angle in (0, 90, 180, 270):
            offset = (angle - start) % 360
            if offset < sweep:
                offsets.add(offset)
        center = point(ocs.to_wcs(entity.dxf.center), factor)
        curve = tuple(point(vertex, factor) for vertex in entity.vertices(start + offset for offset in sorted(offsets)))
        return Primitive("arc", layer, (center, *curve), handles, names, radius=radius,
                         linetype=linetype, sweep_deg=sweep)
    if kind in {"TEXT", "MTEXT", "ATTRIB"}:
        p = entity.dxf.insert
        if kind != "MTEXT":
            p = entity.get_placement()[1]
            p = entity.ocs().to_wcs(p)
        return Primitive("text", layer, (point(p, factor),), handles, names, text=entity.plain_text().strip())
    if kind == "HATCH":
        return None  # Hatch paths are handled separately.
    return None


def _hatch_boundaries(entity, layer, handles, names, factor):
    for boundary in entity.paths:
        if getattr(boundary, "has_bulge", lambda: False)():
            continue
        vertices = getattr(boundary, "vertices", None)
        if not vertices:
            edges = getattr(boundary, "edges", [])
            if not edges or any(type(edge).__name__ != "LineEdge" for edge in edges):
                continue
            vertices = [edge.start for edge in edges]
        points = tuple(dict.fromkeys(point(vertex, factor) for vertex in vertices))
        if len(points) >= 3:
            yield Primitive("hatch", layer, points, handles, names, closed=True)


def primitives(doc, factor, bounds=None):
    area = box(*bounds) if bounds else None
    result = []
    seen = set()
    warnings = Counter()

    def skipped(entity, reason):
        warnings[(entity.dxftype(), reason)] += 1

    entities = decompose(doc.modelspace(), skipped=skipped, hidden_layers=hidden_layer_names(doc))
    for entity, layer, handles, names in entities:
        kind = entity.dxftype()
        if kind == "HATCH":
            converted = _hatch_boundaries(entity, layer, handles, names, factor)
        else:
            primitive = _make_primitive(entity, layer, handles, names, factor)
            converted = [primitive] if primitive else []
        for item in converted:
            if item.kind == "line" and item.points[0] == item.points[1]:
                continue
            if not all(isfinite(value) for p in item.points for value in p):
                continue
            if area is not None and not area.intersects(item.shape):
                continue
            key = (item.kind, layer_key(item.layer), tuple(sorted(item.points)), item.text,
                   item.radius, item.closed, item.sweep_deg)
            if key in seen:
                continue
            seen.add(key)
            result.append(item)
    messages = [f"跳过 {count} 个无法展开的 {kind} 图元。" for (kind, _), count in warnings.items()]
    return result, messages
