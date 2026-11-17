"""Read numbered wall bodies from one structural plan's visible DXF table."""

from collections import defaultdict
from dataclasses import dataclass
from itertools import pairwise
import re
from unicodedata import normalize

from tumo_engine.preprocess import layer_key
from tumo_engine.recognize.geometry import segments
from tumo_engine.recognize.level import floor_from_title
from tumo_engine.recognize.standard_floors import standard_floor_span
from tumo_engine.rules import defaults


@dataclass
class _Rule:
    at: float
    lo: float
    hi: float
    handles: tuple[str, ...]


def _table_rules(items, layer, tolerance):
    horizontal, vertical = [], []
    for line in segments(item for item in items if layer.fullmatch(layer_key(item.layer))):
        (ax, ay), (bx, by) = line.a, line.b
        handles = line.primitives[0].handles
        if abs(ay - by) <= tolerance < abs(ax - bx):
            horizontal.append(_Rule((ay + by) / 2, min(ax, bx), max(ax, bx), handles))
        elif abs(ax - bx) <= tolerance < abs(ay - by):
            vertical.append(_Rule((ax + bx) / 2, min(ay, by), max(ay, by), handles))
    return horizontal, vertical


def _merge_positions(rules, tolerance):
    positions = []
    for rule in sorted(rules, key=lambda item: item.at):
        if positions and rule.at - positions[-1].at <= tolerance:
            current = positions[-1]
            positions[-1] = _Rule(current.at, current.lo, current.hi, (*current.handles, *rule.handles))
        else:
            positions.append(rule)
    return positions


def _columns(vertical, lo, hi, left, right, tolerance):
    return _merge_positions((line for line in vertical if left - tolerance <= line.at <= right + tolerance
                             and line.lo <= lo + tolerance and line.hi >= hi - tolerance), tolerance)


def _cell(column, texts, lo, hi):
    left, right = column
    return [(item, value) for item, value in texts
            if left.at < item.points[0][0] < right.at and lo.at < item.points[0][1] < hi.at]


def _layout(title, texts, horizontal, vertical, cfg):
    tolerance = cfg["coordinate_tolerance"]
    x, y = title.points[0]
    candidates = [line for line in horizontal if line.lo < x < line.hi]
    upper = min((line for line in candidates if line.at > y), key=lambda line: line.at, default=None)
    lower = max((line for line in candidates if line.at < y), key=lambda line: line.at, default=None)
    if upper is None or lower is None:
        return None
    left, right = lower.lo, lower.hi
    if abs(upper.lo - left) > tolerance or abs(upper.hi - right) > tolerance:
        return None
    rows = _merge_positions((line for line in candidates if line.at <= lower.at + tolerance
                             and abs(line.lo - left) <= tolerance and abs(line.hi - right) <= tolerance), tolerance)
    if len(rows) < 3:
        return None
    low, high = rows[-2:]
    columns = _columns(vertical, low.at, high.at, left, right, tolerance)
    headings = defaultdict(list)
    for column in pairwise(columns):
        for item, value in _cell(column, texts, low, high):
            for key, names in cfg["headings"].items():
                if value.upper() in names:
                    headings[key].append((column, item))
    if any(len(headings[key]) != 1 for key in cfg["headings"]):
        return None
    selected = {key: matches[0][0] for key, matches in headings.items()}
    if len({column[0].at for column in selected.values()}) != len(selected):
        return None
    handles = {*title.handles, *upper.handles, *low.handles, *high.handles}
    handles.update(handle for matches in headings.values() for handle in matches[0][1].handles)
    return rows[:-1], selected, handles


def _floor_span(value):
    span, issue = standard_floor_span(f"{value}平面图")
    if span or issue:
        return span
    if re.search(r"[-~至到—–]", value):
        return None
    single = value.upper().removesuffix("F").removesuffix("层").strip()
    floor = floor_from_title(f"{single}层平面图")
    return (floor, floor) if floor is not None and floor > 0 else None


def read_shear_wall_schedule(items, *, sheet_id, rules=None):
    """Return wall-body rows and review issues from primitives cropped to one sheet."""
    cfg = (rules or defaults())["shear_wall_schedule"]
    text_layer = re.compile(cfg["layers"]["text"])
    items = list(items)
    texts = [(item, normalize("NFKC", item.text).strip()) for item in items
             if item.kind == "text" and text_layer.fullmatch(layer_key(item.layer)) and item.text.strip()]
    titles = [item for item, value in texts if re.fullmatch(cfg["title_pattern"], value)]
    if not titles:
        return [], []

    tolerance = cfg["coordinate_tolerance"]
    mark_pattern = re.compile(cfg["mark_pattern"], re.I)
    floor_pattern = re.compile(cfg["floor_pattern"], re.I)
    thickness_pattern = re.compile(cfg["thickness_pattern"], re.I)
    min_size, max_size = cfg["thickness_range"]
    horizontal, vertical = _table_rules(items, re.compile(cfg["layers"]["grid"]), tolerance)
    issues, candidates = [], []
    claims = defaultdict(list)

    def issue(at, handles, message, mark=None):
        issues.append({"sheet": sheet_id, "mark": mark, "at": list(at),
                       "handles": sorted(set(handles)), "message": message})

    for title in titles:
        layout = _layout(title, texts, horizontal, vertical, cfg)
        if layout is None:
            issue(title.points[0], title.handles, "剪力墙身表缺少唯一且有栏线的编号、楼层或墙厚表头。")
            continue
        rows, headings, table_handles = layout
        left = min(column[0].at for column in headings.values())
        right = max(column[1].at for column in headings.values())
        for lo, hi in reversed(list(pairwise(rows))):
            row_columns = list(pairwise(_columns(vertical, lo.at, hi.at, left, right, tolerance)))
            values, handles = {}, {*table_handles, *lo.handles, *hi.handles}
            for key, borders in headings.items():
                matching = next((column for column in row_columns
                                 if all(abs(line.at - border.at) <= tolerance
                                        for line, border in zip(column, borders))), None)
                if matching is not None:
                    values[key] = _cell(matching, texts, lo, hi)
                    handles.update(handle for line in matching for handle in line.handles)
            if len(values) != len(headings):
                numbered = [(item, name) for item, name in _cell(headings["mark"], texts, lo, hi)
                            if mark_pattern.fullmatch(name)]
                if numbered:
                    entry, name = numbered[0]
                    issue(entry.points[0], {*handles, *entry.handles}, "剪力墙身表数据行缺少相符的栏线。", name)
                    for _, claimed in numbered:
                        claims[claimed.upper()].append({"floor_span": None, "handles": sorted(handles)})
                if not values:
                    break
                continue
            marks = [(item, value) for item, value in values["mark"] if mark_pattern.fullmatch(value)]
            if not any(values.values()):
                continue
            at = marks[0][0].points[0] if marks else (left, (lo.at + hi.at) / 2)
            mark = marks[0][1] if len(marks) == 1 else None
            handles.update(handle for group in values.values() for item, _ in group for handle in item.handles)
            floors = values["floor"][0][1] if len(values["floor"]) == 1 else ""
            span = _floor_span(floors) if floor_pattern.fullmatch(floors) else None
            thickness = (thickness_pattern.fullmatch(values["thickness"][0][1])
                         if len(values["thickness"]) == 1 else None)
            size = float(thickness[1]) if thickness else None
            row = {"sheet": sheet_id, "mark": mark, "floors": floors, "floor_span": span, "thickness_mm": size,
                   "at": list(at), "handles": sorted(handles)}
            for name in {value.upper() for _, value in marks}:
                claims[name].append(row)
            if not mark or len(values["mark"]) != 1 or not span or size is None or not min_size <= size <= max_size:
                issue(at, handles, "剪力墙身表同一行缺少唯一的编号、明确的楼层或有效的墙厚。", mark)
                continue
            candidates.append(row)

    records = []
    for row in candidates:
        low, high = row["floor_span"]
        conflicts = [other for other in claims[row["mark"].upper()] if other is not row and (
            other["floor_span"] is None or other["floor_span"][0] <= high and other["floor_span"][1] >= low
        )]
        if conflicts:
            handles = [*row["handles"], *(handle for other in conflicts for handle in other["handles"])]
            issue(row["at"], handles, "剪力墙身表相同编号的适用楼层重叠或不明确。", row["mark"])
        else:
            records.append(row)
    return records, issues
