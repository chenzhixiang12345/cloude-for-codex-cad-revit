"""Read numeric opening dimensions from DXF text and attributed blocks."""

from collections.abc import Iterable
from dataclasses import dataclass
from math import hypot, isfinite
import re
from unicodedata import normalize

from shapely.geometry import Point, box

from tumo_engine.index import SpatialIndex
from tumo_engine.preprocess import Primitive

_UNIT_TO_MM = {"MM": 1, "CM": 10, "M": 1000, "毫米": 1, "厘米": 10, "米": 1000}
_LENGTH = r"(\d+(?:\.\d+)?|\.\d+)\s*(MM|CM|M|毫米|厘米|米)?"
_PAIR = re.compile(rf"{_LENGTH}\s*[×X*]\s*{_LENGTH}")
_SINGLE = re.compile(_LENGTH)
_MARK = re.compile(r"([A-Z]{1,6}[-_]?\d[A-Z0-9]*(?:[-_][A-Z0-9]+)*)(?:\s*\([^)]+\)|\s*[\u4e00-\u9fff]+)?")
_HEADING_UNIT = re.compile(r"\((MM|CM|M|毫米|厘米|米)\)$")
_HEADINGS = {
    "mark": {"编号", "代号", "设计编号", "门窗编号", "门编号", "窗编号", "门窗代号"},
    "size": {"洞口尺寸", "门窗尺寸", "宽×高", "宽X高", "洞宽×洞高", "规格尺寸"},
    "width": {"宽", "宽度", "洞口宽", "洞口宽度"},
    "height": {"高", "高度", "洞口高", "洞口高度"},
    "sill": {"窗台", "窗台高", "窗台高度"},
}


@dataclass(frozen=True)
class OpeningScheduleRow:
    mark: str
    width: float
    height: float
    sill_height: float | None
    handles: tuple[str, ...]


@dataclass(frozen=True)
class _Cell:
    x: float
    y: float
    text: str
    handles: tuple[str, ...]


def _normalize(text):
    return normalize("NFKC", text).upper().strip()


def _heading(text):
    caption = re.sub(r"\s+", "", text)
    unit = _HEADING_UNIT.search(caption)
    if unit:
        caption = caption[:unit.start()]
    for key, captions in _HEADINGS.items():
        if caption in captions:
            return key, unit[1] if unit else "MM"
    return None, None


def _length(number, unit, default):
    value = float(number) * _UNIT_TO_MM[unit or default]
    return round(value, 3) if isfinite(value) else None


def _single(cell, unit):
    match = _SINGLE.fullmatch(cell.text)
    return _length(match[1], match[2], unit) if match else None


def _dimensions(cell, unit):
    match = _PAIR.fullmatch(cell.text)
    if not match:
        return None
    shared_unit = match[2] or match[4] or unit
    width = _length(match[1], match[2], shared_unit)
    height = _length(match[3], match[4], shared_unit)
    return (width, height) if width and height else None


def _row(mark, width, height, sill, cells):
    handles = tuple(dict.fromkeys(handle for cell in cells for handle in cell.handles))
    return OpeningScheduleRow(mark, width, height, sill, handles)


def _row_table(header, headers, index, row_tolerance, max_row_gap):
    # Headings from a neighboring table or a row below do not belong to this table.
    peers = sorted(
        (cell for cell in headers
         if 0 < cell.x - header.x <= 20000 and abs(cell.y - header.y) <= row_tolerance),
        key=lambda cell: cell.x,
    )
    columns = {}
    for cell in peers:
        key, unit = _heading(cell.text)
        if key == "mark":
            break
        columns.setdefault(key, (cell, unit))

    if "size" in columns:
        required = ("size",)
    elif "width" in columns and "height" in columns:
        required = ("width", "height")
    else:
        return
    spacing = min(cell.x - header.x for cell, _ in columns.values())
    column_tolerance = spacing * .38
    y = header.y
    # Grow downward through contiguous rows, including schedules longer than one row gap.
    while True:
        search_area = box(header.x - column_tolerance, y - max_row_gap,
                          header.x + column_tolerance, y - row_tolerance)
        row_marks = [
            (cell, match[1]) for cell in index.nearby(search_area, 0)
            if cell.y < y - row_tolerance and (match := _MARK.fullmatch(cell.text))
        ]
        if not row_marks:
            break
        mark_cell, mark = min(row_marks, key=lambda entry: (
            -entry[0].y, abs(entry[0].x - header.x), entry[1]))
        y = mark_cell.y
        chosen = {}
        for key in (*required, "sill"):
            if key not in columns:
                continue
            column, unit = columns[key]
            parse = _dimensions if key == "size" else _single
            cells = index.nearby(box(column.x - column_tolerance, y - row_tolerance,
                                     column.x + column_tolerance, y + row_tolerance), 0)
            candidates = [
                (cell, value) for cell in cells
                if (value := parse(cell, unit)) is not None
            ]
            if candidates:
                chosen[key] = min(candidates, key=lambda entry: (
                    abs(entry[0].y - y), abs(entry[0].x - column.x), entry[0].text))
        if all(key in chosen for key in required):
            if "size" in chosen:
                width, height = chosen["size"][1]
            else:
                width, height = chosen["width"][1], chosen["height"][1]
            if width > 0 and height > 0:
                sill = chosen["sill"][1] if "sill" in chosen else None
                yield _row(mark, width, height, sill, [mark_cell, *(cell for cell, _ in chosen.values())])


def _opening_card(size, mark_headers, index):
    # A card keeps its label column when the whole drawing or block is rotated.
    above = []
    for mark_header in mark_headers.nearby(Point(size.x, size.y), 2000):
        dx, dy = mark_header.x - size.x, mark_header.y - size.y
        if dy > 0 and abs(dx) <= dy * 1.5:
            above.append((hypot(dx, dy), dx, dy))
    if not above:
        return None
    pitch, dx, dy = min(above)
    right_x, right_y = dy / pitch, -dx / pitch
    up_x, up_y = dx / pitch, dy / pitch
    unit = _heading(size.text)[1]
    marks, dimensions = [], []
    for cell in index.nearby(Point(size.x, size.y), pitch * 6):
        vx, vy = cell.x - size.x, cell.y - size.y
        column = vx * right_x + vy * right_y
        if abs(column) < pitch * .5:
            continue
        row = vx * up_x + vy * up_y
        if abs(row - pitch) <= pitch * .31 and (mark := _MARK.fullmatch(cell.text)):
            marks.append((column, cell, mark[1]))
        if abs(row) <= pitch * .31 and (value := _dimensions(cell, unit)):
            dimensions.append((column, cell, value))

    pairs = [
        (abs(m - d), abs(m) + abs(d), marker.text, label, marker, value, cell)
        for m, marker, label in marks for d, cell, value in dimensions
        if m * d > 0 and abs(m - d) <= pitch * .85
    ]
    if not pairs:
        return None
    _, _, _, mark, marker, (width, height), cell = min(pairs, key=lambda pair: pair[:3])
    return _row(mark, width, height, None, [marker, cell])


def read_opening_schedule(
    items: Iterable[Primitive], *, row_tolerance: float = 100, max_row_gap: float = 1600,
) -> list[OpeningScheduleRow]:
    """Read exploded horizontal schedules and side-by-side opening cards.

    Positions and the two layout tolerances use millimeters. Unlabeled dimensions
    default to millimeters. Conflicting sizes for the same mark remain separate
    for the caller to resolve. Native table and OLE contents must be exploded
    into text first.
    """
    if not 0 < row_tolerance < max_row_gap or not isfinite(max_row_gap):
        raise ValueError("行高容差必须为有限正数，并小于最大行距。")
    cells = {}
    for item in items:
        if item.kind != "text" or not item.points:
            continue
        x, y = item.points[0]
        text = _normalize(item.text)
        if text:
            cells.setdefault((x, y, text), _Cell(x, y, text, item.handles))
    headings = [cell for cell in cells.values() if _heading(cell.text)[0]]
    marks = [cell for cell in headings if _heading(cell.text)[0] == "mark"]
    if not marks:
        return []

    def position(cell):
        return Point(cell.x, cell.y)

    index = SpatialIndex(cells.values(), position)
    mark_index = SpatialIndex(marks, position)
    rows = []
    for header in sorted(marks, key=lambda cell: (-cell.y, cell.x)):
        rows.extend(_row_table(header, headings, index, row_tolerance, max_row_gap))
    for header in headings:
        if _heading(header.text)[0] == "size" and (row := _opening_card(header, mark_index, index)):
            rows.append(row)
    return list(dict.fromkeys(rows))
