"""Read dimensions tied to named door and window diagrams in elevation drawings."""

from collections import defaultdict
from math import isclose, isfinite
import re
from unicodedata import normalize

from ezdxf import bbox
from shapely.geometry import LineString, Point

from tumo_engine.index import SpatialIndex
from tumo_engine.preprocess import entity_visible, hidden_layer_names, layer_key
from tumo_engine.rules import defaults


def _mark(text):
    return normalize("NFKC", text).strip().upper()


def _number_text_matches(entity, measurement, tolerance):
    text = _mark(entity.dxf.text)
    if text in ("", "<>"):
        return True
    if not re.fullmatch(r"\d+(?:\.\d+)?", text):
        return False
    return abs(float(text) - measurement) <= tolerance


def _dimension_index(entities, dimension_layers):
    records = []
    for entity in entities:
        if (entity.dxftype() != "DIMENSION" or entity.dimtype not in (0, 1)
                or not re.search(dimension_layers, layer_key(entity.dxf.layer))):
            continue
        first, second = entity.dxf.defpoint2, entity.dxf.defpoint3
        measurement = entity.get_measurement()
        if (isinstance(measurement, (int, float)) and isfinite(measurement) and measurement > 0
                and isclose(entity.override().get("dimlfac", 1), 1)):
            records.append((entity, LineString(((first.x, first.y), (second.x, second.y))), measurement))
    return SpatialIndex(records, lambda record: record[1])


def _at_witnesses(index, points, tolerance):
    first, second = map(Point, points)
    expected = first.distance(second)
    for entity, geometry, length in index.nearby(first, tolerance):
        ends = tuple(map(Point, geometry.coords))
        forward = max(first.distance(ends[0]), second.distance(ends[1]))
        reverse = max(first.distance(ends[1]), second.distance(ends[0]))
        if (min(forward, reverse) <= tolerance and abs(length - expected) <= tolerance
                and _number_text_matches(entity, length, tolerance)):
            yield entity, length


def _unique_dimension(matches, scale):
    by_handle = {entity.dxf.handle: (entity, round(value * scale, 3)) for entity, value in matches}
    return next(iter(by_handle.values())) if len(by_handle) == 1 else None


def _window_sill(floors, dimensions, x0, y0, x1, scale, tolerance, floor_search):
    results = []
    for floor in floors:
        x, y = floor.dxf.insert.x, floor.dxf.insert.y
        if not tolerance < y0 - y <= floor_search:
            continue
        if x0 - floor_search <= x <= x0 + tolerance:
            side = x0
        elif x1 - tolerance <= x <= x1 + floor_search:
            side = x1
        else:
            continue
        attributes = {attribute.dxf.tag: attribute for attribute in floor.attribs}
        scope, height = attributes.get("标高类型"), attributes.get("标高")
        if not (scope and height and _mark(scope.dxf.text) == "相对于本层" and _mark(height.dxf.text) == "H"):
            continue
        matches = _at_witnesses(dimensions, ((side, y), (side, y0)), tolerance)
        if dimension := _unique_dimension(matches, scale):
            entity, value = dimension
            results.append((value, (floor, scope, height, entity), entity.dxf.text))
    return results[0] if len(results) == 1 else (None, (), None)


def _diagram_records(entities, path, ancestors, scale, rules, hidden):
    diagrams, captions, floors = [], defaultdict(list), []
    for entity in entities:
        kind = entity.dxftype()
        layer = layer_key(entity.dxf.layer)
        if kind in {"TEXT", "MTEXT"} and re.search(rules["caption_layers"], layer):
            text = entity.plain_text() if kind == "MTEXT" else entity.dxf.text
            captions[_mark(text)].append((entity, text))
        elif kind == "INSERT" and re.search(rules["floor_layers"], layer):
            floors.append(entity)
        elif kind == "INSERT" and re.search(rules["diagram_layers"], layer):
            mark = _mark(re.split(r"\$\d+\$", entity.dxf.name)[-1])
            if category := next((category for category, pattern in rules["marks"].items()
                                 if re.fullmatch(pattern, mark)), None):
                diagrams.append((entity, mark, category))
    if not diagrams or not captions:
        return
    dimensions = _dimension_index(entities, rules["dimension_layers"])
    tolerance = rules["witness_tolerance"] / scale
    caption_search = rules["caption_search"] / scale
    for diagram, mark, category in diagrams:
        if mark not in captions or diagram.mcount != 1 or diagram.dxf.extrusion.z <= 0:
            continue
        visible_outline = (
            entity for entity in diagram.virtual_entities()
            if entity.dxftype() in {"LINE", "LWPOLYLINE", "POLYLINE", "ARC", "CIRCLE", "ELLIPSE", "SPLINE"}
            and entity_visible(entity, hidden, effective_layer=diagram.dxf.layer if entity.dxf.layer == "0"
                               else entity.dxf.layer)
        )
        bounds = bbox.extents(visible_outline, fast=True)
        if not bounds.has_data or min(bounds.size.x, bounds.size.y) <= tolerance:
            continue
        x0, y0 = bounds.extmin.x, bounds.extmin.y
        x1, y1 = bounds.extmax.x, bounds.extmax.y
        labels = [(label, text) for label, text in captions[mark] if y0 - caption_search <= label.dxf.insert.y < y0
                  and x0 - caption_search <= label.dxf.insert.x <= x1 + caption_search]
        if len(labels) != 1:
            continue
        width = _unique_dimension((match for y in (y0, y1) for match in
                                  _at_witnesses(dimensions, ((x0, y), (x1, y)), tolerance)), scale)
        height = _unique_dimension((match for x in (x0, x1) for match in
                                   _at_witnesses(dimensions, ((x, y0), (x, y1)), tolerance)), scale)
        if not width or not height:
            continue
        label, text = labels[0]
        width_dim, width_mm = width
        height_dim, height_mm = height
        sill, sill_sources, sill_text = (None, (), None)
        if category == "window":
            sill, sill_sources, sill_text = _window_sill(
                floors, dimensions, x0, y0, x1, scale, tolerance, rules["floor_search"] / scale,
            )
        sources = (*ancestors, diagram, label, width_dim, height_dim, *sill_sources)
        source = {"file": path, "handles": list(dict.fromkeys(entity.dxf.handle for entity in sources))}
        for field, witnesses in (("hidden_size_handles", (width_dim, height_dim)),
                                 ("hidden_sill_handles", sill_sources)):
            handles = [entity.dxf.handle for entity in witnesses if entity.dxf.layer.casefold() in hidden]
            if handles:
                source[field] = handles
        yield {
            "kind": "opening_detail", "mark": mark, "category": category, "text": text,
            "width_mm": width_mm, "height_mm": height_mm, "sill_height_mm": sill,
            "dimension_texts": {"width": width_dim.dxf.text, "height": height_dim.dxf.text, "sill": sill_text},
            "source": source,
        }, label.dxf.insert


def read_opening_details(drawing, path, unit_factor, *, rules=None):
    """Read only explicit measurements on captioned CAD opening-type details."""
    hidden = hidden_layer_names(drawing)
    if rules is None:
        rules = defaults()["opening_detail"]

    def details(entities, ancestors, names, scale):
        visible, support = [], []
        hidden_witnesses = {"DIMENSION": rules["dimension_layers"], "INSERT": rules["floor_layers"]}
        for entity in entities:
            kind = entity.dxftype()
            if kind not in {"INSERT", "TEXT", "MTEXT", "DIMENSION"}:
                continue
            if entity_visible(entity, hidden):
                visible.append(entity)
            elif (rules["include_hidden_witnesses"] and kind in hidden_witnesses and entity_visible(entity, ())
                  and re.search(hidden_witnesses[kind], layer_key(entity.dxf.layer))):
                # The unnamed evidence can support a visible, captioned diagram only when its witness points agree.
                support.append(entity)
        for record, at in _diagram_records(visible + support, path, ancestors, scale, rules, hidden):
            for parent in reversed(ancestors):
                at = parent.matrix44().transform(at)
            record["at"] = [round(value * unit_factor, 3) for value in (at.x, at.y)]
            yield record
        for ref in visible:
            if ref.dxftype() != "INSERT" or ref.mcount != 1:
                continue
            name = ref.dxf.name.casefold()
            xscale, yscale = ref.dxf.xscale, ref.dxf.yscale
            if name in names or xscale <= 0 or not isclose(xscale, yscale) or ref.dxf.extrusion.z <= 0:
                continue
            if block := ref.block():
                yield from details(block, (*ancestors, ref), {*names, name}, scale * xscale)

    return list(details(drawing.modelspace(), (), set(), unit_factor))
