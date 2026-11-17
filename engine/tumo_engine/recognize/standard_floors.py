"""Reuse recognized elements only across an explicit, verified standard-floor range."""

from collections import defaultdict
from collections.abc import Iterable
from copy import deepcopy
from hashlib import blake2s
from itertools import pairwise
import json
from math import isfinite
import re
from unicodedata import normalize

from tumo_engine.recognize.level import _number
from tumo_engine.tables.levels import LevelScheduleRow


_CHINESE = r"(?:首|[一二三四五六七八九]?十[一二三四五六七八九]?|[零一二三四五六七八九])"
_FLOOR = rf"(?:B\d{{1,3}}|地下(?:\d{{1,3}}|{_CHINESE})|(?:[-−]\s*)?\d{{1,3}}|{_CHINESE})"
_FLOOR_TOKEN = rf"(?<![\dA-Z零一二三四五六七八九十])(?:第\s*)?{_FLOOR}\s*(?:F|层)(?![\dA-Z])"
_RANGE = re.compile(
    rf"(?<![\dA-Z零一二三四五六七八九十])(?:第\s*)?(?P<start>{_FLOOR})\s*(?:F|层)?"
    rf"\s*[-~–—−至到]\s*(?:第\s*)?(?P<end>{_FLOOR})\s*(?:F|层)(?![\dA-Z])",
    re.I,
)
_ADDITIONAL_FLOOR = re.compile(_FLOOR_TOKEN, re.I)
_EXCEPTION = re.compile(r"不含|不包括|除外|除去|跳过|隔层|单数层|双数层|奇数层|偶数层|非连续")
_PLAN = re.compile(r"平面图|FLOOR\s*PLAN", re.I)
_LEVEL_TOLERANCE = 0.001
_REUSABLE = {
    "wall": "W", "column": "C", "door": "D", "window": "WIN", "wall_opening": "WO",
    "room": "R", "slab": "SL", "slab_opening": "SO",
}


def _floor_number(label):
    if label.startswith("地下"):
        return -_floor_number(label.removeprefix("地下"))
    if label.startswith(("-", "−")):
        return -int(label[1:].strip())
    return 1 if label == "首" else _number(label)


def _elevation(row, field):
    value = getattr(row, field)
    return value if value is not None and isfinite(value) else None


def _checked_levels(schedule, first, last, field):
    by_number = defaultdict(list)
    for row in schedule:
        if row.number is not None:
            by_number[row.number].append(row)

    selected = []
    for number in range(first, last + 1):
        rows = by_number[number]
        if len(rows) != 1 or _elevation(rows[0], field) is None:
            return [], f"楼层表缺少 {number}F 的唯一明确标高，无法复用标准层。"
        selected.append(rows[0])

    numbered_top = by_number[last + 1]
    if numbered_top:
        if len(numbered_top) != 1 or _elevation(numbered_top[0], field) is None:
            return [], f"楼层表缺少 {last + 1}F 的唯一明确标高，无法复用标准层。"
        top = numbered_top[0]
    else:
        last_row = selected[-1]
        last_elevation = _elevation(last_row, field)
        higher = [(value, row) for row in schedule
                  if (value := _elevation(row, field)) is not None and value > last_elevation]
        if not higher or last_row.height is None or not isfinite(last_row.height) or last_row.height <= 0:
            return [], f"楼层表缺少 {last}F 的明确上限，无法复用标准层。"
        nearest = min(value for value, _ in higher)
        adjacent = [row for value, row in higher if value - nearest <= _LEVEL_TOLERANCE]
        if (len(adjacent) != 1 or adjacent[0].number is not None
                or abs(nearest - last_elevation - last_row.height) > _LEVEL_TOLERANCE):
            return [], f"{last}F 的明确层高与紧邻的命名标高无法唯一对应。"
        top = adjacent[0]

    selected.append(top)
    if len({row.id for row in selected}) != len(selected):
        return [], "楼层表选定范围含重复标高编号，无法复用标准层。"
    for low, high in pairwise(selected):
        height = _elevation(high, field) - _elevation(low, field)
        if height <= 0:
            return [], f"楼层表的 {low.name} 与 {high.name} 标高没有递增，无法复用标准层。"
        if low.height is not None and (not isfinite(low.height) or abs(height - low.height) > _LEVEL_TOLERANCE):
            return [], f"楼层表的 {low.name} 层高与相邻标高不吻合，无法复用标准层。"
    return selected, None


def _on_sheet(element, sheet_id):
    return (element.get("source") or {}).get("sheet") == sheet_id


def _template_elements(elements, sheet_id, base, top, warnings):
    template = {}
    for element in elements:
        if element.get("base_level") != base or not _on_sheet(element, sheet_id):
            continue
        identifier = element["id"]
        if element["category"] not in _REUSABLE:
            warnings.append(f"构件 {identifier} 的类别仅保留在原图指定楼层。")
        elif element.get("top_level") not in {None, base, top}:
            warnings.append(f"构件 {identifier} 跨标准层上下限，保留在原图指定楼层。")
        else:
            template[identifier] = element

    # A hosted element can only repeat with its host in the same standard-floor group.
    while True:
        orphans = [identifier for identifier, element in template.items()
                   if element.get("host") and element["host"] not in template]
        if not orphans:
            return template
        for identifier in orphans:
            warnings.append(f"构件 {identifier} 的宿主无法在同层复用，保留在原图指定楼层。")
            template.pop(identifier)


def _clone_id(element, sheet_id, level):
    payload = json.dumps([sheet_id, element["id"], level], sort_keys=True).encode("utf-8")
    prefix = _REUSABLE[element["category"]]
    return f"{prefix}-{blake2s(payload, digest_size=5).hexdigest().upper()}"


def _copies(template, relation_templates, selected, sheet_id, grids):
    base, top = selected[0].id, selected[1].id
    for low, high in pairwise(selected[1:]):
        identifiers = {identifier: _clone_id(element, sheet_id, low.id) for identifier, element in template.items()}
        level_map = {base: low.id, top: high.id}
        elements = []
        for identifier, source in template.items():
            element = deepcopy(source)
            element["id"] = identifiers[identifier]
            element["base_level"] = low.id
            if (upper := source.get("top_level")) is not None:
                element["top_level"] = level_map[upper]
            if host := source.get("host"):
                element["host"] = identifiers[host]
            element.setdefault("evidence", []).append(f"standard_floor_reuse:{base}->{low.id}")
            elements.append(element)

        refs = {**identifiers, **grids, **level_map}
        relations = [
            {**relation, "a": refs[relation["a"]], "b": refs[relation["b"]]}
            for relation in relation_templates
            if (relation["a"] in identifiers or relation["b"] in identifiers)
            and relation["a"] in refs and relation["b"] in refs
        ]
        yield low.id, elements, relations


def standard_floor_span(title: str) -> tuple[tuple[int, int] | None, str | None]:
    title = normalize("NFKC", title).strip()
    ranges = list(_RANGE.finditer(title))
    if not ranges:
        return None, None
    range_match = ranges[0]
    other_floors = (_ADDITIONAL_FLOOR.search(title[:range_match.start()])
                    or _ADDITIONAL_FLOOR.search(title[range_match.end():]))
    if len(ranges) != 1 or other_floors or _EXCEPTION.search(title) or not _PLAN.search(title):
        return None, "图名的标准层范围不明确，请核对后再复用。"
    first, last = (_floor_number(range_match[name].upper()) for name in ("start", "end"))
    if first is None or last is None or first < 1 or last <= first:
        return None, "图名需要明确的正向连续楼层范围，才能复用标准层。"
    return (first, last), None


def expand_standard_floors(
    model: dict, title: str, schedule: Iterable[LevelScheduleRow],
) -> tuple[dict, list[str]]:
    """Return a new expanded model and review messages for one confirmed plan."""
    title = normalize("NFKC", title).strip()
    span, warning = standard_floor_span(title)
    if span is None:
        return model, [warning] if warning else []
    first, last = span

    matching = [sheet for sheet in model["sheets"] if normalize("NFKC", sheet["title"]).strip() == title
                and sheet["kind"] == "floor_plan"]
    if len(matching) != 1 or matching[0]["discipline"] not in {"arch", "struct"}:
        return model, ["图名未对应唯一的建筑或结构平面，无法复用标准层。"]
    sheet = matching[0]
    field = "elevation_struct" if sheet["discipline"] == "struct" else "elevation_arch"
    selected, warning = _checked_levels(list(schedule), first, last, field)
    if warning:
        return model, [warning]

    old_levels = {level["id"]: level for level in model["levels"]}
    for row in selected:
        old = old_levels.get(row.id, {})
        if (old.get("source") == "楼层表" and old.get(field) is not None
                and abs(old[field] - _elevation(row, field)) > _LEVEL_TOLERANCE):
            return model, [f"{row.name} 的已有楼层表标高与复用表标高冲突。"]

    warnings = []
    sheet_id = sheet["id"]
    original = model["elements"]
    template = _template_elements(original, sheet_id, selected[0].id, selected[1].id, warnings)
    if not template:
        return model, [*warnings, f"未找到 {selected[0].name} 可复用的源构件。"]

    expanded = deepcopy(model)
    next_elements = expanded["elements"]
    next_relations = expanded["relations"]
    used_ids = {element["id"]: element for element in original}
    grids = {grid["id"]: grid["id"] for grid in model["grids"]
             if not grid.get("source") or _on_sheet(grid, sheet_id)}
    known_relations = {json.dumps(relation, sort_keys=True) for relation in next_relations}

    for level, elements, relations in _copies(template, model["relations"], selected, sheet_id, grids):
        expected_ids = {element["id"] for element in elements}
        if any(element.get("base_level") == level and _on_sheet(element, sheet_id)
               and element["id"] not in expected_ids for element in original):
            return model, [f"目标楼层 {level} 已有其他源构件，无法安全复用。"]
        for element in elements:
            identifier = element["id"]
            if identifier in used_ids:
                if used_ids[identifier] != element:
                    return model, [f"构件编号 {identifier} 与现有数据冲突，无法复用标准层。"]
                continue
            next_elements.append(element)
            used_ids[identifier] = element
        for relation in relations:
            key = json.dumps(relation, sort_keys=True)
            if key not in known_relations:
                next_relations.append(relation)
                known_relations.add(key)

    known_levels = []
    for row in selected:
        level = row.to_level()
        if row.handles:
            level["evidence"] = [f"level_schedule:{handle}" for handle in sorted(set(row.handles))]
        known_levels.append(level)
    selected_ids = [row.id for row in selected]
    expanded["levels"] = known_levels + [level for level in expanded["levels"] if level["id"] not in selected_ids]
    target_sheet = next(item for item in expanded["sheets"] if item["id"] == sheet_id)
    target_sheet["levels"] = selected_ids + [identifier for identifier in sheet["levels"] if identifier not in selected_ids]
    return expanded, warnings
