"""Recognize ramps whose plan outline and rise agree with their slope note."""

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from hashlib import blake2s
from math import isfinite
import json
import re
from unicodedata import normalize

from shapely.geometry import MultiPoint, Point, Polygon

from tumo_engine.index import SpatialIndex
from tumo_engine.preprocess import Primitive, layer_key
from tumo_engine.recognize.geometry import direction, dot, rounded, world


RAMP_LAYERS = r"^(?:(?:A|AE|AD)[-_])?RAMP(?:[-_](?:OUTLINE|BOUNDARY))?$|^(?:建筑)?坡道(?:轮廓|边界)?$"
RAMP_TAG = re.compile(r"(?:无障碍|电动自行车|自行车|汽车|机动车|建筑)?坡道(?:起止线及侧边缘)?")
_NUMBER = r"(?:\d+(?:\.\d+)?|\.\d+)"
_END_MARK = re.compile(
    rf"(?:(?:坡道)?(?P<cn>坡底|坡顶|低端|高端|下端|上端)\s*标高|"
    rf"RAMP\s+(?P<en>BOTTOM|TOP|LOWER|UPPER)\s+(?:EL\.?|ELEVATION))"
    rf"\s*[:=]?\s*(?P<value>[±+-]?{_NUMBER})\s*(?P<unit>MM|CM|M)?",
    re.I,
)
_GRADE = re.compile(
    rf"(?:(?:坡度|坡比|SLOPE)\s*[:=]?|i\s*=)\s*(?P<rise>{_NUMBER})\s*"
    rf"(?:(?P<percent>%|‰)|:\s*(?P<run>{_NUMBER}))",
    re.I,
)
_UNIT_FACTOR = {"MM": 1, "CM": 10, "M": 1000}


@dataclass
class _Outline:
    shape: Polygon
    handles: set[str] = field(default_factory=set)
    closed: bool = False


@dataclass(frozen=True)
class _Note:
    kind: str
    value: float | None
    at: Point
    handles: tuple[str, ...]


def _elevation(match):
    raw = match["value"]
    value = float(raw.removeprefix("±"))
    if not isfinite(value) or (raw.startswith("±") and value != 0):
        return None
    unit = match["unit"]
    # Drawings commonly omit the metre unit on decimal elevations.
    if not unit and "." not in raw and value != 0:
        return None
    result = value * _UNIT_FACTOR[(unit or "M").upper()]
    return round(result, 3) if isfinite(result) else None


def _grade(match):
    rise = float(match["rise"])
    run = float(match["run"]) if match["run"] else 1000 if match["percent"] == "‰" else 100
    if isfinite(rise) and isfinite(run) and rise > 0 and run > 0:
        ratio = rise / run
        if isfinite(ratio):
            return ratio
    return None


def _notes(items):
    for item in items:
        if item.kind != "text" or not item.points:
            continue
        at = Point(item.points[0])
        for row in normalize("NFKC", item.text).replace("−", "-").splitlines():
            text = row.strip().rstrip("。;").strip()
            if match := _END_MARK.fullmatch(text):
                end = (match["cn"] or match["en"]).upper()
                kind = "low" if end in {"坡底", "低端", "下端", "BOTTOM", "LOWER"} else "high"
                value = _elevation(match)
            elif match := _GRADE.fullmatch(text):
                kind, value = "slope", _grade(match)
            elif RAMP_TAG.fullmatch(text):
                yield _Note("ramp_tag", None, at, item.handles)
                continue
            else:
                continue
            if value is not None:
                yield _Note(kind, value, at, item.handles)


def _issue(code, message, at, handles, evidence):
    return {
        "code": code,
        "message": message,
        "at": rounded(at.coords[0]),
        "handles": sorted(set(handles)),
        "evidence": sorted(set(evidence)),
    }


def _outlines(items, layer_pattern, issues):
    distinct = {}
    for item in items:
        if item.kind != "polyline" or len(item.points) < 3 or not layer_pattern.search(layer_key(item.layer)):
            continue
        shape = Polygon(item.points).normalize()
        if not shape.is_valid or shape.area == 0:
            issues.append(_issue(
                "ramp_outline_invalid", "坡道轮廓无有效面积或边界自交。", MultiPoint(item.points).representative_point(),
                item.handles, ("ramp_outline",),
            ))
            continue
        outline = distinct.setdefault(shape.wkb, _Outline(shape))
        outline.closed |= item.closed
        outline.handles.update(item.handles)
    return sorted(distinct.values(), key=lambda outline: outline.shape.wkb)


def _ascent_directions(rectangle, low_notes, high_notes, note_distance):
    corners = list(rectangle.exterior.coords)[:-1]
    for first, second in ((corners[0], corners[1]), (corners[1], corners[2])):
        axis = direction(first, second)
        for ascent in (axis, (-axis[0], -axis[1])):
            stations = [dot(point, ascent) for point in corners]
            start, end = min(stations), max(stations)
            run = end - start
            normal = (-ascent[1], ascent[0])
            sides = [dot(point, normal) for point in corners]
            spacing = (min(sides) + max(sides)) / 2
            end_tolerance = min(note_distance, run * 0.15)
            if not all(abs(dot(note.at.coords[0], ascent) - start) <= end_tolerance for note in low_notes):
                continue
            if not all(abs(dot(note.at.coords[0], ascent) - end) <= end_tolerance for note in high_notes):
                continue
            yield run, max(sides) - min(sides), world(start, spacing, ascent), world(end, spacing, ascent)


def _interpret(outline, notes, note_distance, slope_tolerance, issues):
    handles = outline.handles.union(handle for note in notes for handle in note.handles)
    by_kind = defaultdict(list)
    for note in notes:
        by_kind[note.kind].append(note)
    evidence = ["ramp_outline", *by_kind]

    def warn(code, message):
        issues.append(_issue(code, message, outline.shape.representative_point(), handles, evidence))

    if not outline.closed:
        warn("ramp_outline_incomplete", "坡道轮廓未闭合，请核对起止线和侧边。")
        return None
    rectangle = outline.shape.minimum_rotated_rectangle
    if outline.shape.hausdorff_distance(rectangle) > 1:
        warn("ramp_outline_unsupported", "坡道轮廓需要核对分段和坡向。")
        return None

    lows = {note.value for note in by_kind["low"]}
    highs = {note.value for note in by_kind["high"]}
    grades = {note.value for note in by_kind["slope"]}
    if not lows or not highs:
        warn("ramp_levels_missing", "坡道缺少低端或高端的明确标高。")
        return None
    if len(lows) != 1 or len(highs) != 1 or min(highs) <= max(lows):
        warn("ramp_levels_conflict", "坡道低端和高端标高存在冲突。")
        return None
    if not grades:
        warn("ramp_slope_missing", "坡道缺少明确的坡度标注。")
        return None
    if max(grades) - min(grades) > slope_tolerance:
        warn("ramp_slope_conflict", "坡道有相互冲突的坡度标注。")
        return None

    low, high = min(lows), min(highs)
    rise = high - low
    directions = list(_ascent_directions(rectangle, by_kind["low"], by_kind["high"], note_distance))
    consistent = [axis for axis in directions if all(abs(rise / axis[0] - grade) <= slope_tolerance for grade in grades)]
    if directions and not consistent:
        warn("ramp_slope_mismatch", "坡道高差、水平长度和标注坡度不一致。")
        return None
    if len(consistent) != 1:
        warn("ramp_direction_unclear", "坡道低端和高端标注无法确定唯一的上坡方向。")
        return None

    run, width, low_point, high_point = consistent[0]
    evidence.extend(("closed_boundary", "slope_matches_rise_and_run"))
    warn("ramp_native_unverified", "坡道轮廓、标高和坡度已对照，原生坡道仍需核对。")
    return {
        "category": "ramp",
        "discipline": "arch",
        "geometry": {
            "kind": "polygon",
            "points": [rounded(point) for point in list(outline.shape.exterior.coords)[:-1]],
        },
        "type": {"name": "建筑坡道", "width": round(width, 3)},
        "params": {
            "low_point": rounded(low_point), "high_point": rounded(high_point),
            "low_elevation": low, "high_elevation": high, "run_length": round(run, 3),
            "rise": round(rise, 3), "slope": rise / run,
        },
        "confidence": 0.9,
        "evidence": sorted(set(evidence)),
        "handles": sorted(handles),
    }


def identify_ramps(
    items: Iterable[Primitive],
    *,
    sheet_id: str,
    base_level: str,
    top_level: str | None = None,
    rules=None,
) -> tuple[list[dict], list[dict]]:
    """Return schema 0.1 ramp evidence and localized review issues for one plan."""
    rules = rules or {}
    config = rules.get("ramp", {})
    note_distance = config.get("note_distance", 300)
    slope_tolerance = config.get("slope_tolerance", 0.002)
    layer_pattern = re.compile(rules.get("layers", {}).get("ramps", RAMP_LAYERS), re.I)

    items = list(items)
    issues = []
    outlines = _outlines(items, layer_pattern, issues)
    index = SpatialIndex(enumerate(outlines), lambda entry: entry[1].shape)
    associated = defaultdict(list)
    ambiguous = set()
    for note in _notes(items):
        nearby = [(outline.shape.distance(note.at), i) for i, outline in index.nearby(note.at, note_distance)]
        if not nearby:
            if note.kind == "ramp_tag":
                issues.append(_issue(
                    "ramp_outline_missing", "图上有坡道说明，需核对轮廓、低端和高端标高及坡度。",
                    note.at, note.handles, ("ramp_tag",),
                ))
            continue
        distance = min(row[0] for row in nearby)
        nearest = [i for offset, i in nearby if abs(offset - distance) <= 0.001]
        if len(nearest) == 1:
            associated[nearest[0]].append(note)
        elif note.kind != "ramp_tag":
            ambiguous.update(nearest)
            issues.append(_issue(
                "ramp_note_conflict", "坡道标注同时匹配多个轮廓。", note.at, note.handles, (note.kind,),
            ))

    ramps = []
    for i, outline in enumerate(outlines):
        shared_boundary = any(
            other != i and candidate.shape.intersection(outline.shape).area > 0
            for other, candidate in index.nearby(outline.shape, 0)
        )
        if shared_boundary:
            issues.append(_issue(
                "ramp_outline_overlap", "坡道轮廓重叠，请核对有效范围。",
                outline.shape.representative_point(), outline.handles, ("ramp_outline",),
            ))
            continue
        if i in ambiguous:
            continue
        if ramp := _interpret(outline, associated[i], note_distance, slope_tolerance, issues):
            payload = json.dumps([sheet_id, base_level, ramp["geometry"]], sort_keys=True).encode("utf-8")
            ramp["id"] = f"RP-{blake2s(payload, digest_size=5).hexdigest().upper()}"
            ramp["base_level"] = base_level
            if top_level is not None:
                ramp["top_level"] = top_level
            ramp["source"] = {"sheet": sheet_id, "handles": ramp.pop("handles")}
            ramps.append(ramp)
    return ramps, sorted(issues, key=lambda issue: (issue["at"], issue["code"], issue["handles"]))
