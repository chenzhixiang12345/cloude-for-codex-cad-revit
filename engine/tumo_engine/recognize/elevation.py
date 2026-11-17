"""Read explicit elevations and dimensions from CAD elevation and section views."""

from collections import Counter, defaultdict
from math import isfinite
from pathlib import Path
import re
from unicodedata import normalize

from tumo_engine.io.dxf_reader import read_dxf
from tumo_engine.preprocess import bind_xrefs, layer_key, primitives, unit_factor
from tumo_engine.recognize.level import _id, _number
from tumo_engine.recognize.opening_details import read_opening_details


_LAYER = re.compile(r"(?:^|[-_])(?:LEVL[-_]HIGH|ELEVATION)(?:$|[-_])|^(?:标高|楼层标高)$")
_DIMENSION_LAYER = re.compile(r"(?:^|[-_])(?:DIM|DIMS|DIMENSION)(?:$|[-_])|尺寸|标注尺寸")
_NOTE = re.compile(
    r"(?P<sill_elevation>窗台(?:顶)?标高)|(?P<sill_height>窗台(?:高度?|距地(?:面)?))"
    r"|(?P<parapet_elevation>女儿墙(?:顶|顶部)?标高)|(?P<parapet_height>女儿墙(?:墙身)?高度?)"
    r"|(?P<story_height>层高)|(?P<elevation>(?:建筑|结构)?(?:楼层)?标高)"
)
_UNIT = r"毫米|厘米|MM|CM|米|M"
_UNIT_NAME = {"毫米": "mm", "厘米": "cm", "米": "m", "MM": "mm", "CM": "cm", "M": "m"}
_UNIT_FACTOR = {"mm": 1, "cm": 10, "m": 1000}
_UNIT_DECLARATION = re.compile(
    rf"(?:\(\s*)?(?:本图\s*)?(?:建筑|结构)?标高\s*(?:均\s*)?"
    rf"(?:单位\s*(?:为|是|[:=])?|以)\s*({_UNIT})(?:\s*为单位)?[)。.]?"
)
_VALUE = re.compile(
    rf"(?P<number>[±+-]?(?:\d+(?:\.\d+)?|\.\d+))\s*(?P<unit>{_UNIT})?"
)
_ELEVATION_FORMAT = re.compile(r"[±+-]?\d{1,3}\.\d{3}")
_CN_FLOOR = r"(?:[一二三四五六七八九]?十[一二三四五六七八九]?|[一二三四五六七八九])"
_FLOOR = re.compile(
    rf"首层|(?:第)?(?P<below>地下|负|-)?(?P<chinese>(?<![A-Z0-9.])\d{{1,2}}|{_CN_FLOOR})层"
    r"|(?<![A-Z0-9])(?P<latin>RF|B?\d{1,2}F)(?![A-Z0-9])"
)
_FLOOR_RANGE = re.compile(
    rf"(?:B?\d{{1,2}}F?|{_CN_FLOOR})(?:层)?\s*[-~—–至到、/]\s*(?:第)?"
    rf"(?:B?\d{{1,2}}F|(?:\d{{1,2}}|{_CN_FLOOR})层)(?![A-Z0-9])"
)
_WINDOW = re.compile(r"(?<![A-Z0-9])(?:TC|BYC|LC|GC|C)[-_]?\d[A-Z0-9]*(?:[-_][A-Z0-9]+)*(?![A-Z0-9])")
_TRANSLATION = str.maketrans({"−": "-", "臺": "台", "兒": "儿", "牆": "墙", "標": "标", "層": "层", "頂": "顶",
                             "負": "负"})
_ABSOLUTE = {"elevation", "sill_elevation", "parapet_elevation"}


def _normalized(text):
    return normalize("NFKC", text).translate(_TRANSLATION).strip().upper()


def _floor_id(match):
    if match["latin"] == "RF":
        return "LRF"
    if match[0] == "首层":
        return "L1"
    number = _number(match["latin"] or match["chinese"])
    return _id(-number if match["below"] else number)


def _scope(text):
    levels = {_floor_id(match) for match in _FLOOR.finditer(text)}
    marks = set(_WINDOW.findall(text))
    level = levels.pop() if len(levels) == 1 and not _FLOOR_RANGE.search(text) else None
    return level, marks.pop() if len(marks) == 1 else None


def _source(file, items):
    return {"file": str(file), "handles": list(dict.fromkeys(h for item in items for h in item.handles))}


def _issue(code, message, file, item, *support):
    return {
        "code": code, "message": message, "source": _source(file, (item, *support)),
        "at": list(item.points[0]), "text": item.text,
    }


def _measurement(text, allow_units, absolute, drawing_units, default_unit):
    match = _VALUE.fullmatch(text)
    if not match:
        return None
    number = match["number"]
    value = float(number.lstrip("±"))
    if not isfinite(value) or (number.startswith("±") and value != 0) or (not absolute and value < 0):
        return None
    if match["unit"]:
        unit, basis, support = _UNIT_NAME[match["unit"]], "explicit", ()
    elif not absolute:
        return value, None, None, ()
    elif len(drawing_units) == 1:
        unit, support = next(iter(drawing_units.items()))
        basis = "drawing_note"
    elif drawing_units:
        return value, None, None, ()
    elif default_unit:
        unit, basis, support = default_unit, "default", ()
    elif allow_units and _ELEVATION_FORMAT.fullmatch(number):
        # Conventional three-decimal elevation notation in level symbols is in metres.
        unit, basis, support = "m", "elevation_format", ()
    else:
        return value, None, None, ()
    converted = value * _UNIT_FACTOR[unit]
    return (converted, unit, basis, support) if isfinite(converted) else None


def _record(kind, measure, level, mark, source_file, value_item, *related, elevation_type=None):
    value, unit, basis, support = measure
    record = {
        "kind": kind, "value_mm": round(value, 3), "level": level, "mark": mark,
        "source": _source(source_file, (*related, value_item, *support)),
        "at": list(value_item.points[0]), "text": value_item.text, "source_unit": unit, "unit_basis": basis,
    }
    if elevation_type is not None:
        record["elevation_type"] = elevation_type
    return record


def _level_rows(texts, measurements, source_file):
    groups = defaultdict(list)
    for index, (item, _, _) in enumerate(texts):
        if len(item.handles) > 1:
            groups[item.handles[:-1]].append(index)
    records, issues, paired = {}, [], set()
    for group in groups.values():
        row_values = [
            index for index in group if index in measurements
            and not _DIMENSION_LAYER.search(layer_key(texts[index][0].layer))
        ]
        rows = {}
        for index in group:
            item, text, is_level_layer = texts[index]
            label = text.strip("()").strip()
            floor = _FLOOR.fullmatch(label)
            if not is_level_layer or (not floor and label != "女儿墙"):
                continue
            x, y = item.points[0]
            candidates = [
                candidate for candidate in row_values if abs(texts[candidate][0].points[0][1] - y) <= 120
                and 0 < abs(texts[candidate][0].points[0][0] - x) <= 1800
            ]
            if candidates:
                rows[index] = floor, candidates
        usage = Counter(candidate for _, candidates in rows.values() for candidate in candidates)
        for index, (floor, candidates) in rows.items():
            item = texts[index][0]
            paired.update(candidates)
            if len(candidates) != 1 or usage[candidates[0]] != 1:
                issues.append(_issue("ambiguous_elevation_row", "同一标高块中有多个可能的楼层或标高数值。",
                                     source_file, item, *(texts[candidate][0] for candidate in candidates)))
                continue
            candidate, = candidates
            value_item = texts[candidate][0]
            measure = measurements[candidate]
            if measure[1] is None:
                issues.append(_issue("annotation_unit_unknown", "标高标注的单位不明确。", source_file, item, value_item))
                continue
            kind = "elevation" if floor else "parapet_elevation"
            records[index] = _record(kind, measure, _floor_id(floor) if floor else None, None,
                                     source_file, value_item, item)
    return records, issues, paired


def recognize_elevation_annotations(items, source_file, *, default_elevation_unit=None):
    """Return source-located annotations and issues. Only unique relative sills can populate a window."""
    if default_elevation_unit not in (None, *_UNIT_FACTOR):
        raise ValueError("default_elevation_unit must be mm, cm, m, or None")
    texts = [
        (item, _normalized(item.text), bool(_LAYER.search(layer_key(item.layer))))
        for item in items if item.kind == "text" and item.points
    ]
    drawing_units = defaultdict(list)
    declarations = set()
    for index, (item, text, _) in enumerate(texts):
        match = _UNIT_DECLARATION.fullmatch(text)
        if match:
            drawing_units[_UNIT_NAME[match[1]]].append(item)
            declarations.add(index)
    measurements = {
        index: measure for index, (_, text, _) in enumerate(texts)
        if (measure := _measurement(text, True, True, drawing_units, default_elevation_unit))
    }
    rows, issues, paired = _level_rows(texts, measurements, source_file)

    annotations = []
    sill_notes = defaultdict(list)
    for index, (item, text, is_level_layer) in enumerate(texts):
        if index in rows:
            annotations.append(rows[index])
            continue
        if index in declarations or index in paired:
            continue
        note = _NOTE.search(text)
        remainder = text
        if note:
            kind = note.lastgroup
            remainder = text[note.end():].lstrip(" :=")
            measure = _measurement(remainder, is_level_layer, kind in _ABSOLUTE, drawing_units,
                                   default_elevation_unit)
        elif is_level_layer and index in measurements and not text.isdecimal():
            kind, measure = "elevation", measurements[index]
        else:
            if text.strip("()").strip() == "女儿墙" and is_level_layer:
                issues.append(_issue("annotation_no_value", "女儿墙标签旁未找到可唯一配对的标高。",
                                     source_file, item))
            continue
        level, mark = _scope(text)
        if kind.startswith("sill") and (level is None or mark is None):
            issues.append(_issue("unlocated_sill", "窗台标注缺少唯一楼层或窗号。", source_file, item))
            continue
        if measure is None:
            # Ignore generic note titles such as 标高符号、数字 and 层高说明.
            if kind not in {"elevation", "story_height"} or re.match(r"[0-9±+.-]", remainder):
                issues.append(_issue("annotation_no_value", "标注缺少明确的单一数值。", source_file, item))
            continue
        if measure[1] is None:
            issues.append(_issue("annotation_unit_unknown", "标注单位不明确。", source_file, item))
            continue
        label = note[0] if note else ""
        elevation_type = {"建筑": "arch", "结构": "struct"}.get(label[:2])
        record = _record(kind, measure, level, mark, source_file, item, elevation_type=elevation_type)
        annotations.append(record)
        if kind == "sill_height":
            sill_notes[(level, mark)].append((item, record))
    omitted = set()
    for (level, mark), matches in sill_notes.items():
        item, *support = (item for item, _ in matches)
        if len({record["value_mm"] for _, record in matches}) > 1:
            omitted.update(id(record) for _, record in matches)
            issue = _issue("conflicting_sill", f"{level} 的 {mark} 有互相冲突的窗台高。",
                           source_file, item, *support)
            issues.append({**issue, "level": level, "mark": mark})
        elif support:
            first = matches[0][1]
            first["observations"] = [
                {key: record[key] for key in ("source", "at", "text", "source_unit", "unit_basis")}
                for _, record in matches
            ]
            first["source"] = _source(source_file, (item, *support))
            omitted.update(id(record) for _, record in matches[1:])
    return [record for record in annotations if id(record) not in omitted], issues


def read_elevation_drawing(path: Path, root: Path | None = None, *, default_elevation_unit=None):
    """Read a DXF and return its annotations and issues with source paths relative to root."""
    source = path.resolve()
    root = root.resolve() if root is not None else source.parent
    if not source.is_relative_to(root):
        raise ValueError("立面或剖面图不在指定的源目录内。")
    relative = source.relative_to(root).as_posix()
    drawing = read_dxf(source)
    warnings = bind_xrefs(drawing, source, root)
    factor = unit_factor(drawing)
    items, parsing_warnings = primitives(drawing, factor)
    annotations, issues = recognize_elevation_annotations(
        items, relative, default_elevation_unit=default_elevation_unit,
    )
    annotations.extend(read_opening_details(drawing, relative, factor))
    file_issues = [
        {"code": "elevation_drawing_warning", "message": message,
         "source": _source(relative, ()), "at": None}
        for message in dict.fromkeys([*warnings, *parsing_warnings])
    ]
    return annotations, [*file_issues, *issues]
