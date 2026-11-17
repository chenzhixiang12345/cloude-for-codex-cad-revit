"""Recognize confirmed slab boundaries and floor openings."""

from collections.abc import Iterable
from dataclasses import dataclass, field
from math import isfinite
import re
from unicodedata import normalize

from shapely.geometry import Point, Polygon

from tumo_engine.index import SpatialIndex
from tumo_engine.preprocess import Primitive, layer_key
from tumo_engine.recognize.geometry import rounded


LAYERS = {
    "slab_openings": r"^(?:(?:A|AE|S)[-_])?(?:SLAB[-_](?:OPENING|HOLE)|楼板洞口)$|^S[-_]HOLE$",
    "slab_drops": r"^(?:(?:A|AE|S)[-_])?(?:SLAB[-_]DROP|DROP[-_]SLAB|降板(?:轮廓|边界)?)$",
    "slab_boundaries": r"^(?:(?:A|AE|S)[-_])?(?:SLAB(?:[-_](?:BOUNDARY|OUTLINE))?|楼板(?:轮廓|边界)?)$",
}
NUMBER = r"(?P<value>\d+(?:\.\d+)?)\s*(?P<unit>MM|CM|M)?"
THICKNESS = re.compile(
    rf"(?:(?P<mark>(?:LB|WB|XB)\d+[A-Z]?)\s+H\s*=|(?:板厚|厚度|厚|THK)\s*[=:为]?)\s*{NUMBER}", re.I,
)
DROP = re.compile(rf"(?:降板|下沉)\s*[=:]?\s*-?\s*{NUMBER}", re.I)
RELATIVE_LEVEL = re.compile(
    rf"(?P<slab>板顶标高|板面标高)?\s*[:=]?\s*HS?\s*(?P<sign>[-+])\s*{NUMBER}", re.I,
)
UNIT_FACTOR = {"MM": 1, "CM": 10, "M": 1000}


@dataclass
class Outline:
    kind: str
    shape: Polygon
    handles: set[str] = field(default_factory=set)


@dataclass(frozen=True)
class Note:
    kind: str
    value: float
    at: Point
    handles: tuple[str, ...]
    mark: str | None = None


def _outline_key(outline):
    return (*outline.shape.bounds, outline.kind, outline.shape.wkb)


def _outlines(items, rules):
    matches = {key: re.compile(rules.get(key, fallback)) for key, fallback in LAYERS.items()}
    distinct = {}
    for item in items:
        if not item.closed or item.kind not in {"polyline", "hatch"} or len(item.points) < 3:
            continue
        key = layer_key(item.layer)
        kind = next((name for name, match in matches.items() if match.search(key)), None)
        if kind is None:
            continue
        shape = Polygon([rounded(point) for point in item.points]).normalize()
        if not shape.is_valid or shape.area <= 0:
            continue
        entry = distinct.setdefault((kind, shape.wkb), Outline(kind, shape))
        entry.handles.update(item.handles)
    return sorted(distinct.values(), key=_outline_key)


def _dimension(match, default_unit="MM"):
    return round(float(match["value"]) * UNIT_FACTOR[(match["unit"] or default_unit).upper()], 3)


def _notes(items):
    for item in items:
        if item.kind != "text" or not item.text:
            continue
        at = Point(item.points[0])
        for line in normalize("NFKC", item.text).splitlines():
            value = line.strip().rstrip("。；;").strip().replace("−", "-")
            if match := THICKNESS.fullmatch(value):
                thickness = _dimension(match)
                if 30 <= thickness <= 1500:
                    mark = match["mark"]
                    yield Note("thickness", thickness, at, item.handles, mark and mark.upper())
            elif match := DROP.fullmatch(value):
                offset = _dimension(match)
                if isfinite(offset):
                    yield Note("offset", -offset, at, item.handles)
            elif match := RELATIVE_LEVEL.fullmatch(value):
                # H/Hs annotations without units use metres in the drawing notation.
                offset = _dimension(match, "M")
                if match["sign"] == "-":
                    offset = -offset
                if isfinite(offset):
                    yield Note("offset" if match["slab"] else "drop_offset", offset, at, item.handles)


def _element(outline, category, element_type, confidence, evidence, notes=()):
    handles = outline.handles.union(handle for note in notes for handle in note.handles)
    return {
        "category": category,
        "type": element_type,
        "geometry": {"kind": "polygon", "points": [list(point) for point in list(outline.shape.exterior.coords)[:-1]]},
        "confidence": confidence,
        "evidence": [f"layer:{outline.kind}", *evidence],
        "handles": sorted(handles),
    }


def _local_slab(outline, notes, warn):
    lowered = outline.kind == "slab_drops"
    contained = [note for note in notes.nearby(outline.shape, 0) if outline.shape.contains(note.at)]
    thickness_notes = [note for note in contained if note.kind == "thickness"]
    thicknesses = {note.value for note in thickness_notes}
    if len(thicknesses) != 1:
        warn(outline, "楼板边界内缺少唯一的板厚标注。")
        return None

    allowed_offsets = {"offset", "drop_offset"} if lowered else {"offset"}
    offset_notes = [note for note in contained if note.kind in allowed_offsets]
    offsets = {note.value for note in offset_notes}
    if len(offsets) > 1 or (lowered and (not offsets or next(iter(offsets)) >= 0)):
        warn(outline, "楼板边界内缺少唯一有效的相对板顶标高。")
        return None

    thickness = next(iter(thicknesses))
    element_type = {"name": f"楼板 {thickness:g} mm", "thickness": thickness}
    marks = {note.mark for note in thickness_notes if note.mark}
    if len(marks) == 1:
        element_type["mark"] = next(iter(marks))
    evidence = ["closed_boundary", "local_thickness"]
    if offset_notes:
        evidence.append("local_elevation_offset")
    slab = _element(outline, "slab", element_type, 0.93, evidence, [*thickness_notes, *offset_notes])
    slab["base_offset"] = next(iter(offsets), 0.0)
    return slab


def _non_overlapping(outlines, warn, message, *, allow_touch):
    index = SpatialIndex(outlines, lambda item: item.shape)
    for item in outlines:
        overlaps = (other.shape for other in index.nearby(item.shape, 0) if other is not item)
        if any((item.shape.intersection(other).area > 0 if allow_touch else item.shape.intersects(other))
               for other in overlaps):
            warn(item, message)
        else:
            yield item


def recognize_slabs(items: Iterable[Primitive], rules=None):
    """Return slab candidates, (opening, host candidate) pairs and localized warnings."""
    items = list(items)
    outlines = _outlines(items, (rules or {}).get("layers", {}))
    slab_outlines = [outline for outline in outlines if outline.kind != "slab_openings"]
    hole_outlines = [outline for outline in outlines if outline.kind == "slab_openings"]
    notes = SpatialIndex(_notes(items), lambda note: note.at)
    warnings = []

    def warn(outline, message):
        at = rounded(outline.shape.representative_point().coords[0])
        warnings.append((message, at))

    slabs = []
    for outline in _non_overlapping(slab_outlines, warn, "楼板边界重叠。请核对界线或降板范围。", allow_touch=True):
        if slab := _local_slab(outline, notes, warn):
            slabs.append((slab, outline.shape))

    hosts = SpatialIndex(slabs, lambda item: item[1])
    openings = []
    for hole in _non_overlapping(hole_outlines, warn, "楼板洞口轮廓接触或重叠。请核对洞口边界。", allow_touch=False):
        containing = [slab for slab, shape in hosts.nearby(hole.shape, 0) if shape.contains_properly(hole.shape)]
        if len(containing) != 1:
            warn(hole, "楼板洞口没有唯一且完整包含它的宿主楼板。")
            continue
        opening = _element(hole, "slab_opening", {"name": "楼板洞口"}, 0.94,
                           ["closed_boundary", "contained_in_host"])
        openings.append((opening, containing[0]))
    return [slab for slab, _ in slabs], openings, warnings
