"""Read dimensioned structural column and wall-boundary schedule details."""

from collections import Counter
from dataclasses import dataclass
from itertools import pairwise
from math import isfinite
import re
from unicodedata import normalize

from ezdxf import bbox
from shapely.geometry import LineString, Point, Polygon, box

from tumo_engine.index import SpatialIndex
from tumo_engine.preprocess import entity_visible, hidden_layer_names, layer_key
from tumo_engine.rules import defaults


@dataclass
class _CellColumn:
    handle: str
    shape: Polygon
    limits: tuple[float, ...]

    def row_at(self, y):
        return next((index for index, (lo, hi) in enumerate(pairwise(self.limits)) if lo < y < hi), None)


def _text(value):
    return normalize("NFKC", value).strip()


def _point(vector):
    return vector.x, vector.y


def _label(entity):
    position = entity.dxf.insert if entity.dxftype() == "MTEXT" else entity.get_placement()[1]
    return entity, _text(entity.plain_text()), Point(_point(position))


def _table_column(entity, tolerance):
    vertices = [_point(vertex) for vertex in entity.vertices_in_wcs()]
    if len(vertices) < 9 or entity.has_arc or vertices[4] != vertices[0]:
        return None
    outer = Polygon(vertices[:4])
    if not outer.is_valid or outer.is_empty:
        return None
    x0, y0, x1, y1 = outer.bounds
    if not outer.equals(box(x0, y0, x1, y1)):
        return None
    rows = [y0, y1]
    for (ax, ay), (bx, by) in pairwise(vertices[4:]):
        if abs(ay - by) <= tolerance and abs(min(ax, bx) - x0) <= tolerance and abs(max(ax, bx) - x1) <= tolerance:
            if y0 < ay < y1:
                rows.append(ay)
    if len(rows) != 6:
        return None
    return _CellColumn(entity.dxf.handle, outer, tuple(sorted(rows)))


def _one_by_row(column, labels):
    values = {}
    for entity, text, point in labels.nearby(column.shape, 0):
        if column.shape.contains(point) and (row := column.row_at(point.y)) is not None:
            values.setdefault(row, []).append((entity, text))
    return values


def _frames(layout, labels, hidden, rules):
    for frame in layout.query("INSERT"):
        if (not re.search(rules["frame_pattern"], frame.dxf.name, re.I)
                or not entity_visible(frame, hidden) or frame.mcount != 1):
            continue
        titles = [attr for attr in frame.attribs if attr.dxf.tag in rules["title_tags"]
                  and entity_visible(attr, hidden) and re.fullmatch(rules["title_pattern"], _text(attr.dxf.text))]
        if len(titles) != 1:
            continue
        extent = bbox.extents([frame], fast=True)
        if not extent.has_data or extent.size.x <= 0 or extent.size.y <= 0:
            continue
        shape = box(extent.extmin.x, extent.extmin.y, extent.extmax.x, extent.extmax.y)
        stamps = [(number, entity) for entity, number, at in labels.nearby(shape, 0) if shape.covers(at)
                  and re.fullmatch(rules["sheet_number"], number)
                  and re.search(rules["layers"]["sheet"], layer_key(entity.dxf.layer))]
        if len(stamps) == 1:
            number, stamp = stamps[0]
            yield {"number": number, "title": _text(titles[0].dxf.text),
                   "handles": [frame.dxf.handle, titles[0].dxf.handle, stamp.dxf.handle]}, shape


def _dimension(entity, tolerance):
    if entity.dimtype not in (0, 1):
        return None
    start, end = _point(entity.dxf.defpoint2), _point(entity.dxf.defpoint3)
    distance = entity.get_measurement()
    settings = entity.override()
    factor = settings.get("dimlfac", 1)
    if (not isinstance(distance, (int, float)) or not isfinite(distance) or distance <= 0
            or not isfinite(factor) or factor <= 0):
        return None
    value = distance * factor
    written = entity.dxf.get("actual_measurement")
    conflict = written is not None and (not isfinite(written) or abs(written - value) > tolerance)
    display = []
    geometry = entity.get_geometry_block()
    if geometry is not None:
        display.extend(_text(text.plain_text()) for text in geometry.query("TEXT MTEXT"))
    override = _text(entity.dxf.text)
    if override not in {"", "<>"}:
        display.append(override)
    for text in display:
        number = re.fullmatch(r"(\d+(?:\.\d+)?)\s*(?:MM|毫米)?", text, re.I)
        conflict |= not number or abs(float(number[1]) - value) > tolerance
    return entity, LineString((start, end)), factor, value, bool(conflict)


def _section_sizes(shape, dimensions, cell, rules):
    x0, y0, x1, y1 = shape.bounds
    tolerance = rules["coordinate_tolerance"]
    matches = {"horizontal": [], "vertical": []}
    for dimension in dimensions.nearby(cell, 0):
        _, witnesses, _, _, _ = dimension
        if not cell.covers(witnesses):
            continue
        (ax, ay), (bx, by) = witnesses.coords
        if (abs(ay - by) <= tolerance and abs(min(ax, bx) - x0) <= tolerance
                and abs(max(ax, bx) - x1) <= tolerance):
            matches["horizontal"].append(dimension)
        if (abs(ax - bx) <= tolerance and abs(min(ay, by) - y0) <= tolerance
                and abs(max(ay, by) - y1) <= tolerance):
            matches["vertical"].append(dimension)
    if any(len(group) != 1 or group[0][-1] for group in matches.values()):
        return None
    (horizontal,), (vertical,) = matches.values()
    width_scale, depth_scale = horizontal[2], vertical[2]
    if width_scale != depth_scale:
        return None
    if not all(rules["size_range"][0] <= item[3] <= rules["size_range"][1] for item in (horizontal, vertical)):
        return None

    outline = [[round((x - x0) * width_scale, 3), round((y - y0) * depth_scale, 3)]
               for x, y in shape.normalize().exterior.coords[:-1]]
    return {"width_mm": round(horizontal[3], 3), "depth_mm": round(vertical[3], 3), "outline_mm": outline}, (
        horizontal[0].dxf.handle, vertical[0].dxf.handle,
    )


def _read_sheet(sheet, frame, table, labels, profiles, dimensions, rules):
    columns = [column for column in table.nearby(frame, 0) if frame.covers(column.shape)]
    tolerance = rules["coordinate_tolerance"]
    issues, records = [], []
    occurrences = Counter()
    section_row, mark_row, floor_row = (rules["headings"].index(name) for name in ("截面", "编号", "标高"))
    for header in columns:
        heading_rows = _one_by_row(header, labels)
        heading_names = {row: entries[0][1] for row, entries in heading_rows.items() if len(entries) == 1}
        if heading_names != dict(enumerate(rules["headings"])):
            continue
        header_sources = [heading_rows[row][0][0].dxf.handle for row in sorted(heading_rows)]
        frontier = header.shape.bounds[2]
        for column in sorted(columns, key=lambda item: item.shape.bounds):
            if abs(column.shape.bounds[0] - frontier) > tolerance or any(
                abs(x - y) > tolerance for x, y in zip(header.limits, column.limits)
            ):
                continue
            frontier = column.shape.bounds[2]
            rows = _one_by_row(column, labels)
            numbered = rows.get(mark_row, ())
            candidates = [(entry, mark, scope) for entry, mark in numbered for scope, pattern in rules["marks"].items()
                          if re.fullmatch(pattern, mark, re.I)]
            floor = rows.get(floor_row, ())
            if not candidates:
                continue
            at = [round(value, 3) for value in column.shape.centroid.coords[0]]
            source = [*sheet["handles"], header.handle, *header_sources, column.handle,
                      *(entity.dxf.handle for entity, _ in (*numbered, *floor))]
            if len(floor) == 1 and re.fullmatch(rules["floor_pattern"], floor[0][1]):
                occurrences.update({(mark.upper(), floor[0][1]) for _, mark, _ in candidates})
            else:
                floor = ()
            if len(candidates) != 1 or len(numbered) != 1 or not floor:
                issues.append({"sheet": sheet, "message": "同一柱表列内没有唯一的柱编号和楼层。", "at": at,
                               "handles": sorted(set(source))})
                continue

            entry, mark, scope = candidates[0]
            x0, _, x1, _ = column.shape.bounds
            cell = box(x0, column.limits[section_row], x1, column.limits[section_row + 1])
            within = [(entity, shape) for entity, shape in profiles.nearby(cell, 0) if cell.contains_properly(shape)]
            sizes = _section_sizes(within[0][1], dimensions, cell, rules) if len(within) == 1 else None
            if not sizes:
                issues.append({"sheet": sheet, "mark": mark, "message": "柱表截面缺少唯一闭合轮廓或一致的原标尺寸。",
                               "at": at, "handles": sorted({*source, *(entity.dxf.handle for entity, _ in within)})})
                continue
            section, size_sources = sizes
            records.append({"sheet": sheet, "mark": mark, "scope": scope, "floors": floor[0][1], "section": section,
                            "at": [round(value, 3) for value in _point(entry.dxf.insert)],
                            "handles": sorted({*source, within[0][0].dxf.handle, *size_sources})})
    accepted = []
    for record in records:
        if occurrences[(record["mark"].upper(), record["floors"])] != 1:
            issues.append({"sheet": sheet, "mark": record["mark"], "at": record["at"], "handles": record["handles"],
                           "message": "同一图号和楼层重复出现相同柱编号。请核对原表。"})
        else:
            accepted.append(record)
    return accepted, issues


def read_column_schedules(drawing, *, rules=None):
    """Read source sections by their original table cells; never creates or updates model columns."""
    cfg = (rules or defaults())["column_schedule"]
    layers = {key: re.compile(pattern) for key, pattern in cfg["layers"].items()}
    layout = drawing.modelspace()
    hidden = hidden_layer_names(drawing)
    labels, columns, profiles, dimensions = [], [], [], []
    for entity in layout.query("TEXT MTEXT LWPOLYLINE DIMENSION"):
        if not entity_visible(entity, hidden):
            continue
        layer = layer_key(entity.dxf.layer)
        kind = entity.dxftype()
        if kind in {"TEXT", "MTEXT"} and any(layers[key].fullmatch(layer) for key in ("text", "sheet")):
            labels.append(_label(entity))
        elif kind == "LWPOLYLINE" and layers["table"].fullmatch(layer):
            if column := _table_column(entity, cfg["coordinate_tolerance"]):
                columns.append(column)
        elif kind == "LWPOLYLINE" and layers["profile"].fullmatch(layer) and entity.closed and not entity.has_arc:
            vertices = [_point(vertex) for vertex in entity.vertices_in_wcs()]
            shape = Polygon(vertices)
            if shape.is_valid and shape.area > 0:
                profiles.append((entity, shape))
        elif kind == "DIMENSION" and layers["dimension"].fullmatch(layer):
            if dimension := _dimension(entity, cfg["display_tolerance"]):
                dimensions.append(dimension)

    stamps = SpatialIndex(labels, lambda label: label[2])
    text = SpatialIndex((label for label in labels if layers["text"].fullmatch(layer_key(label[0].dxf.layer))),
                        lambda label: label[2])
    table = SpatialIndex(columns, lambda column: column.shape)
    sections = SpatialIndex(profiles, lambda profile: profile[1])
    sizes = SpatialIndex(dimensions, lambda entry: entry[1])
    records, issues = [], []
    for sheet, shape in _frames(layout, stamps, hidden, cfg):
        accepted, warnings = _read_sheet(sheet, shape, table, text, sections, sizes, cfg)
        records.extend(accepted)
        issues.extend(warnings)
    return sorted(records, key=lambda row: (row["sheet"]["number"], row["at"])), sorted(
        issues, key=lambda issue: (issue["sheet"]["number"], issue["at"]),
    )
