"""Export a native CAD overlay that Revit can import without the TuMo add-in."""

from __future__ import annotations

from math import cos, hypot, isclose, isfinite, radians, sin
from pathlib import Path

from ezdxf import DXFError, bbox, units
from ezdxf.entities import DXFGraphic
from ezdxf.lldxf.const import DXF2000, DXF2004

from tumo_engine.io.dxf_reader import read_dxf

_LAYER_COLORS = {
    "GRID": (5, (37, 99, 166)),
    "COLUMN": (34, (154, 52, 18)),
    "WALL": (3, (8, 120, 105)),
    "ISSUE": (1, (185, 28, 28)),
    "LABELS": (7, None),
}
_BOUNDED_GEOMETRY = {"LINE", "ARC", "CIRCLE", "ELLIPSE", "LWPOLYLINE", "POINT", "SOLID", "TRACE", "3DFACE"}
_COMMON_UNITS = (units.MM, units.CM, units.M, units.IN, units.FT)


def _unique(name, table):
    base = name
    suffix = 2
    while name in table:
        name = f"{base}_{suffix}"
        suffix += 1
    return name


def _style_background(drawing):
    """Mute the copy of the original linework while keeping its CAD layers."""
    modern = drawing.dxfversion >= DXF2004
    for layer in drawing.layers:
        layer.color = 8
        if modern:
            layer.rgb = (169, 174, 182)
    for entity in drawing.entitydb.values():
        if isinstance(entity, DXFGraphic) and entity.is_alive:
            entity.dxf.color = 256
            entity.dxf.discard("true_color")


def _center(points):
    return tuple(sum(value) / len(points) for value in zip(*points))


def _length_factor(drawing, sheet):
    factor = sheet.get("units_to_mm")
    if factor is None:
        try:
            factor = units.conversion_factor(drawing.units, units.MM) if drawing.units else 1
        except (ValueError, IndexError, TypeError):
            factor = 1
    if factor <= 0 or not isfinite(factor):
        raise ValueError("图纸的毫米换算系数必须是有限正数。")
    return factor


def _can_crop(entity, blocks):
    kind = entity.dxftype()
    if kind != "INSERT":
        return (kind in _BOUNDED_GEOMETRY and not entity.dxf.get("thickness", 0)
                and (kind != "LWPOLYLINE" or not entity.has_width))
    if entity.attribs or entity.mcount > 1 or not entity.has_uniform_scaling:
        return False
    block = entity.block()
    if block is None or block.block.is_xref or block.block.is_xref_overlay:
        return False
    key = block.name.casefold()
    if key not in blocks:
        blocks[key] = False  # Incomplete or cyclic definitions must stay in the copy.
        blocks[key] = all(_can_crop(child, blocks) for child in block)
    return blocks[key]


def _crop_background(layout, bounds, factor):
    x0, y0, x1, y1 = (value / factor for value in bounds)
    pad = max(x1 - x0, y1 - y0) * 0.04
    known_blocks = {}
    for entity in tuple(layout):
        if not _can_crop(entity, known_blocks):
            continue
        try:
            extent = bbox.extents([entity], fast=True)
        except (DXFError, ValueError, ArithmeticError):
            continue
        # Text, unsupported entities, and geometry that crosses the view edge are retained.
        if extent.has_data and (extent.extmax.x < x0 - pad or extent.extmin.x > x1 + pad
                                or extent.extmax.y < y0 - pad or extent.extmin.y > y1 + pad):
            layout.delete_entity(entity)


def write_review_dxf(model: dict, source: Path, output: Path, *, sheet_id: str | None = None) -> Path:
    """Write the selected sheet's annotations to a copy of its original model-space DXF."""
    source, output = Path(source), Path(output)
    if output.suffix.lower() != ".dxf":
        raise ValueError("输出文件需要 .dxf 扩展名。")
    if source.resolve() == output.resolve() or (output.exists() and source.samefile(output)):
        raise ValueError("请选择不同于原始 DXF 的输出路径。")
    sheets = model.get("sheets", [])
    sheet = next((item for item in sheets if sheet_id is None or item["id"] == sheet_id), None)
    if sheet is None:
        raise ValueError("识别结果里没有所选图纸。")
    if (sheet.get("layout") or "Model").casefold() != "model":
        raise ValueError("矢量审图需要模型空间 DXF。")

    drawing = read_dxf(source)
    layout = drawing.modelspace()
    factor = _length_factor(drawing, sheet)
    if not drawing.units and drawing.dxfversion >= DXF2000:
        for unit in _COMMON_UNITS:
            if isclose(factor, units.conversion_factor(unit, units.MM), rel_tol=1e-9):
                drawing.units = unit
                break
    first_sheet = sheets[0]["id"]
    selected = sheet["id"]
    if sheet.get("bounds"):
        _crop_background(layout, sheet["bounds"], factor)
    _style_background(drawing)

    layers = {}
    for key, (aci, rgb) in _LAYER_COLORS.items():
        name = _unique(f"TUMO_REVIEW_{key}", drawing.layers)
        layer = drawing.layers.new(name, dxfattribs={"color": aci})
        if rgb is not None and drawing.dxfversion >= DXF2004:
            layer.rgb = rgb
        layers[key] = {"layer": name}
    text_style = _unique("TUMO_REVIEW_TEXT", drawing.styles)
    drawing.styles.new(text_style, dxfattribs={"font": "arial.ttf"})
    height = 1.5 * (sheet.get("scale") or 100) / factor

    def point(mm):
        return mm[0] / factor, mm[1] / factor

    def belongs(item):
        return ((item.get("source") or {}).get("sheet") or first_sheet) == selected

    def label(text, at, *, down=False):
        x, y = point(at)
        layout.add_text(text, dxfattribs={
            **layers["LABELS"], "style": text_style, "height": height,
            "insert": (x + height * 0.45, y - height * 1.35 if down else y + height * 0.4),
        })

    def line(start, end, style):
        if tuple(start) != tuple(end):
            layout.add_line(point(start), point(end), dxfattribs=style)

    def outline(points, style):
        vertices = [point(vertex) for vertex in points]
        if drawing.dxfversion >= DXF2000:
            layout.add_lwpolyline(vertices, close=True, dxfattribs=style)
        else:
            layout.add_polyline2d(vertices, close=True, dxfattribs=style)

    def cross(at, style, size):
        x, y = at
        line((x - size, y - size), (x + size, y + size), style)
        line((x - size, y + size), (x + size, y - size), style)

    anchors = {}
    for grid in model.get("grids", []):
        if belongs(grid):
            line(grid["start"], grid["end"], layers["GRID"])
            identifier = grid["id"]
            label(f"{grid['label']} / {identifier}" if grid.get("label") else identifier, grid["end"])
            anchors[identifier] = grid["end"]

    for element in model.get("elements", []):
        category = element.get("category", "").upper()
        geometry = element.get("geometry")
        if category not in {"COLUMN", "WALL"} or not geometry or not belongs(element):
            continue
        kind = geometry["kind"]
        dimensions = element.get("type") or {}
        style = layers[category]
        if kind == "line":
            start, end = geometry["start"], geometry["end"]
            dx, dy = end[0] - start[0], end[1] - start[1]
            length = hypot(dx, dy)
            if not length:
                continue
            thickness = dimensions.get("thickness")
            if thickness:
                nx, ny = -dy * thickness / (2 * length), dx * thickness / (2 * length)
                outline([(start[0] + nx, start[1] + ny), (end[0] + nx, end[1] + ny),
                         (end[0] - nx, end[1] - ny), (start[0] - nx, start[1] - ny)], style)
            else:
                line(start, end, style)
            anchor = _center([start, end])
        elif kind == "polygon":
            points = geometry["points"]
            outline(points, style)
            anchor = _center(points)
        elif kind == "point":
            anchor = geometry["at"]
            width, depth, diameter = (dimensions.get(key) for key in ("width", "depth", "diameter"))
            if category == "COLUMN" and diameter:
                layout.add_circle(point(anchor), diameter / (2 * factor), dxfattribs=style)
            elif category == "COLUMN" and width and depth:
                angle = radians(geometry.get("rotation_deg") or 0)
                c, s = cos(angle), sin(angle)
                corners = [(-width / 2, -depth / 2), (width / 2, -depth / 2),
                           (width / 2, depth / 2), (-width / 2, depth / 2)]
                outline([(anchor[0] + x * c - y * s, anchor[1] + x * s + y * c)
                         for x, y in corners], style)
            else:
                cross(anchor, style, height * factor / 2)
        else:
            continue
        anchors[element["id"]] = anchor
        label(element["id"], anchor)

    for issue in model.get("issues", []):
        where = issue.get("where") or {}
        fallback = anchors.get(issue.get("element"))
        issue_sheet = where.get("sheet") or (selected if fallback is not None or len(sheets) == 1 else None)
        anchor = where.get("at") or fallback
        if issue_sheet == selected and anchor is not None:
            cross(anchor, layers["ISSUE"], height * factor * 0.65)
            label(issue["id"], anchor, down=True)

    if sheet.get("bounds"):
        x0, y0, x1, y1 = sheet["bounds"]
        drawing.set_modelspace_vport(max(y1 - y0, (x1 - x0) / 1.5) / factor * 1.1,
                                     center=point(((x0 + x1) / 2, (y0 + y1) / 2)))
    output.parent.mkdir(parents=True, exist_ok=True)
    drawing.saveas(output)
    return output
