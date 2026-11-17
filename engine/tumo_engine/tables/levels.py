"""Read explicit architectural and structural CAD level schedules."""

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from math import isfinite
import re
from unicodedata import normalize

from shapely.geometry import Point, box

from tumo_engine.index import SpatialIndex
from tumo_engine.preprocess import Primitive, layer_key
from tumo_engine.recognize.level import _id, _name, _number


UNIT_FACTOR = {"M": 1000, "CM": 10, "MM": 1, "米": 1000, "厘米": 10, "毫米": 1}
HEADING_UNIT = re.compile(r"\((MM|CM|M|毫米|厘米|米)\)$")
MEASUREMENT = re.compile(r"([±+-]?(?:\d+(?:\.\d+)?|\.\d+))\s*(MM|CM|M|毫米|厘米|米)?")
FLOOR_HEADINGS = {"楼层", "楼层编号", "楼层名称", "楼层号", "层数", "层号", "层别", "FLOOR", "STOREY", "STORY"}
ELEVATION_HEADINGS = {"标高", "标高H", "楼层标高", "本层标高", "ELEVATION"}
HEIGHT_HEADINGS = {"层高", "楼层高度", "STOREYHEIGHT", "STORYHEIGHT", "FLOORHEIGHT"}
NAMED_FLOOR = re.compile(r"(?:R\d{0,2}F|(?:屋面|屋顶|机房|架空|避难|夹)层?(?:\([^()]+\))?)")
CHINESE_NUMBER = r"(?:[一二三四五六七八九]?十[一二三四五六七八九]?|[一二三四五六七八九])"
FLOOR_NUMBER = re.compile(rf"(?:B?\d{{1,3}}F?|-?\d{{1,3}}|(?:地下)?(?:{CHINESE_NUMBER}|\d{{1,3}})层)")
FIELD_NAME = {"elevation_arch": "建筑标高", "elevation_struct": "结构标高", "height": "层高"}


@dataclass(frozen=True)
class LevelScheduleRow:
    id: str
    number: int | None
    name: str
    elevation_arch: float | None
    elevation_struct: float | None
    height: float | None
    handles: tuple[str, ...]

    def to_level(self):
        """Return the fields used by the existing 0.1 level contract."""
        level = {"id": self.id, "name": self.name, "source": "楼层表", "confidence": 0.9}
        for key in ("elevation_arch", "elevation_struct"):
            if (value := getattr(self, key)) is not None:
                level[key] = value
        return level


@dataclass(frozen=True)
class _Cell:
    x: float
    y: float
    text: str
    layer: str
    handles: tuple[str, ...]


@dataclass
class _Values:
    measurements: dict[str, set[float]] = field(default_factory=lambda: defaultdict(set))
    handles: set[str] = field(default_factory=set)


def _heading(text, discipline):
    caption = re.sub(r"\s+", "", text)
    match = HEADING_UNIT.search(caption)
    unit = match[1] if match else None
    if match:
        caption = caption[:match.start()]
    if caption in FLOOR_HEADINGS:
        return "floor", None
    if caption in HEIGHT_HEADINGS:
        return "height", unit
    if caption in {"建筑标高", "建筑标高H", "建筑楼层标高", "ARCHITECTURALELEVATION"}:
        return "elevation_arch", unit
    if caption in {"结构标高", "结构标高H", "结构楼层标高", "标高HS", "STRUCTURALELEVATION"}:
        return "elevation_struct", unit
    if caption in ELEVATION_HEADINGS:
        return ("elevation_struct" if discipline == "struct" else "elevation_arch"), unit
    return None, None


def _floor(text):
    if text in {"首层", "地面层"}:
        return "L1", 1, "1F"
    if NAMED_FLOOR.fullmatch(text):
        return ("L" + text if text.startswith("R") else "L-" + text), None, text
    if not FLOOR_NUMBER.fullmatch(text):
        return None
    number = int(text) if text.startswith("-") else _number(text.removeprefix("地下").removesuffix("层"))
    if number is None:
        return None
    if text.startswith("地下"):
        number = -number
    return _id(number), number, _name(number)


def _number_in_cell(text, unit):
    match = MEASUREMENT.fullmatch(text.replace("−", "-"))
    if not match:
        return None
    value = float(match[1].lstrip("±"))
    if not isfinite(value) or (match[1].startswith("±") and value != 0):
        return None
    explicit_unit = match[2] or unit
    multiplier = UNIT_FACTOR[explicit_unit] if explicit_unit else (1000 if "." in match[1] else 1)
    converted = value * multiplier
    return round(converted, 3) if isfinite(converted) else None


def _position(cell):
    return Point(cell.x, cell.y)


def _table_columns(header, headings, row_tolerance):
    peers = sorted(
        ((cell, key, unit) for cell, key, unit in headings
         if 0 < cell.x - header.x <= 20000 and abs(cell.y - header.y) <= row_tolerance),
        key=lambda entry: entry[0].x,
    )
    columns = []
    for cell, key, unit in peers:
        if key == "floor":
            break
        columns.append((cell, key, unit))
    return columns


def _table_rows(header, columns, index, row_tolerance, max_row_gap):
    # Follow row anchors by position. Midpoints keep adjacent rows from sharing numbers.
    centers = [header.x, *(column.x for column, _, _ in columns)]
    tolerance = min(b - a for a, b in zip(centers, centers[1:]) if b > a) * .4
    for direction in (-1, 1):
        anchors = [(header, None)]
        while True:
            previous = anchors[-1][0].y
            low, high = sorted((previous, previous + direction * max_row_gap))
            minimum_gap = row_tolerance if len(anchors) == 1 else 0
            nearby = [
                (cell, floor) for cell in index.nearby(box(header.x - tolerance, low, header.x + tolerance, high), 0)
                if (cell.y - previous) * direction > minimum_gap and (floor := _floor(cell.text))
            ]
            if not nearby:
                break
            marker = min(nearby, key=lambda pair: (abs(pair[0].y - previous), abs(pair[0].x - header.x), pair[0].text))
            anchors.append(marker)
        for position, (marker, identity) in enumerate(anchors[1:], start=1):
            y = marker.y
            inward = (y + anchors[position - 1][0].y) / 2
            adjacent = position + 1 < len(anchors)
            outward = (y + anchors[position + 1][0].y) / 2 if adjacent else y + direction * row_tolerance
            row_edges = {inward, outward} if adjacent else {inward}
            low, high = sorted((inward, outward))
            row = _Values(handles=set((*header.handles, *marker.handles)))
            for column, key, unit in columns:
                window = box(column.x - tolerance, max(low, y - row_tolerance),
                             column.x + tolerance, min(high, y + row_tolerance))
                for cell in index.nearby(window, 0):
                    if cell.y in row_edges:
                        continue
                    value = _number_in_cell(cell.text, unit)
                    if value is not None and (key != "height" or value > 0):
                        row.measurements[key].add(value)
                        row.handles.update((*cell.handles, *column.handles))
            if row.measurements:
                yield identity, row


def _combine(rows):
    combined = defaultdict(_Values)
    for identity, data in rows:
        entry = combined[identity]
        entry.handles.update(data.handles)
        for key, measurements in data.measurements.items():
            entry.measurements[key].update(measurements)
    levels = []
    warnings = []
    for (identifier, number, name), entry in sorted(
        combined.items(), key=lambda item: (item[0][1] is None, item[0][1] or 0, item[0][2]),
    ):
        values = dict.fromkeys(FIELD_NAME)
        for key in FIELD_NAME:
            measurements = entry.measurements.get(key)
            if not measurements:
                continue
            if len(measurements) == 1:
                values[key] = next(iter(measurements))
            else:
                display = " / ".join(f"{value:g}" for value in sorted(measurements))
                warnings.append(f"楼层表 {name} 的{FIELD_NAME[key]}有冲突（{display} mm）。请核对原表。")
        levels.append(LevelScheduleRow(identifier, number, name, handles=tuple(sorted(entry.handles)), **values))
    return levels, warnings


def read_level_schedule(
    items: Iterable[Primitive], *, discipline: str, layer_pattern: str | None = None,
    row_tolerance: float = 180, max_row_gap: float = 1600,
) -> tuple[list[LevelScheduleRow], list[str]]:
    """Read explicit aligned DXF text cells. Conflicting fields remain unset."""
    if not 0 < row_tolerance < max_row_gap or not isfinite(max_row_gap):
        raise ValueError("楼层表行容差必须为有限正数，并小于最大行距。")
    cells = [
        _Cell(*item.points[0], normalize("NFKC", item.text).upper().strip(), layer_key(item.layer), item.handles)
        for item in items if item.kind == "text" and item.points and item.text.strip()
    ]
    pattern = re.compile(layer_pattern) if layer_pattern else None
    preferred = [cell for cell in cells if pattern and pattern.search(cell.layer)]
    raw_rows = []
    for members in (preferred, cells):
        headings = []
        for cell in members:
            key, unit = _heading(cell.text, discipline)
            if key:
                headings.append((cell, key, unit))
        anchors = [cell for cell, key, _ in headings if key == "floor" and
                   (members is preferred or not pattern or not pattern.search(cell.layer))]
        if not anchors:
            continue
        index = SpatialIndex(members, _position)
        for header in sorted(anchors, key=lambda cell: (-cell.y, cell.x)):
            columns = _table_columns(header, headings, row_tolerance)
            if any(key.startswith("elevation_") for _, key, _ in columns):
                raw_rows.extend(_table_rows(header, columns, index, row_tolerance, max_row_gap))
    return _combine(raw_rows)
