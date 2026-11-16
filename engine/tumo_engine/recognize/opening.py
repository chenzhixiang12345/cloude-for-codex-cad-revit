"""Match scheduled openings to local DXF evidence and a single existing wall."""

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
import re
from unicodedata import normalize

from shapely.geometry import Point

from tumo_engine.index import SpatialIndex
from tumo_engine.preprocess import Primitive, layer_key
from tumo_engine.recognize.geometry import Segment, dot, parallel, rounded, segments, source_handles, world
from tumo_engine.tables import OpeningScheduleRow

_DOOR_LAYER = re.compile(r"(?:^|[-_])DOOR(?:$|[-_])|^(?:门洞|门扇|门框)$")
_WINDOW_LAYER = re.compile(r"(?:^|[-_])(?:WIND|WINDOW)(?:$|[-_])|^(?:窗洞|窗扇|窗框)$")


@dataclass(frozen=True)
class _Host:
    id: str
    centerline: Segment
    thickness: float


def _mark(text):
    return normalize("NFKC", text).upper().strip()


def _opening_segments(items):
    for item in items:
        layer = layer_key(item.layer)
        door = bool(_DOOR_LAYER.search(layer))
        window = bool(_WINDOW_LAYER.search(layer))
        if door != window:
            for segment in segments([item]):
                yield segment, "door" if door else "window"


def _hosts(walls):
    for wall in walls:
        geometry = wall.get("geometry") or {}
        thickness = (wall.get("type") or {}).get("thickness")
        identifier = wall.get("id")
        if wall.get("category") != "wall" or geometry.get("kind") != "line" or not identifier:
            continue
        start, end = tuple(geometry["start"]), tuple(geometry["end"])
        if thickness and thickness > 0 and start != end:
            yield _Host(identifier, Segment(start, end, ()), thickness)


def _center_on_host(symbol, host, host_tolerance, angle_tolerance):
    line = host.centerline
    axis = line.axis
    if not parallel(axis, symbol.axis, angle_tolerance):
        return None
    start, end = line.projected(axis)
    low, high = symbol.projected(axis)
    if low < start - host_tolerance or high > end + host_tolerance:
        return None
    normal = (-axis[1], axis[0])
    offset = line.offset(axis)
    allowed = host.thickness / 2 + host_tolerance
    if any(abs(dot(endpoint, normal) - offset) > allowed for endpoint in (symbol.a, symbol.b)):
        return None
    return world((low + high) / 2, offset, axis)


def _issue(label, mark, code, description):
    return {"code": code, "mark": mark, "message": f"{mark}：{description}", "at": rounded(label.points[0])}


def recognize_openings(
    items: Iterable[Primitive],
    walls: Iterable[dict],
    schedule: Iterable[OpeningScheduleRow],
    *,
    label_radius: float = 450,
    width_tolerance: float = 50,
    host_tolerance: float = 25,
    angle_tolerance: float = 5,
) -> tuple[list[dict], list[dict]]:
    """Return opening candidates and located issues for exact scheduled tags.

    Only measured line segments on explicit door/window layers are used.
    Width and position tolerances are in millimeters. Each host must have an
    ID and a centerline spanning the measured opening. The caller supplies
    each candidate's final ID, floor, and sheet source.
    """
    dimensions = defaultdict(set)
    for row in schedule:
        dimensions[_mark(row.mark)].add((row.width, row.height, row.sill_height))
    if not dimensions:
        return [], []

    items = list(items)
    symbols = SpatialIndex(_opening_segments(items), lambda item: item[0].geometry)
    hosts = list(_hosts(walls))
    wall_index = SpatialIndex(hosts, lambda host: host.centerline.geometry)
    maximum_half_thickness = max((host.thickness / 2 for host in hosts), default=0)

    issues = []
    proposals = defaultdict(list)
    for label in items:
        if label.kind != "text" or (mark := _mark(label.text)) not in dimensions:
            continue
        if len(dimensions[mark]) != 1:
            issues.append(_issue(label, mark, "schedule_conflict", "门窗表中存在不同的尺寸。"))
            continue
        (width, height, sill), = dimensions[mark]
        nearby = [
            (segment, category)
            for segment, category in symbols.nearby(Point(label.points[0]), label_radius)
            if abs(segment.length - width) <= width_tolerance
        ]
        if not nearby:
            issues.append(_issue(label, mark, "opening_evidence_missing", "标注附近没有尺寸相符的洞口线。"))
            continue

        matches = defaultdict(list)
        for symbol, category in nearby:
            for host in wall_index.nearby(symbol.geometry, maximum_half_thickness + host_tolerance):
                center = _center_on_host(symbol, host, host_tolerance, angle_tolerance)
                if center is not None:
                    matches[(host.id, category, tuple(rounded(center)))].extend(symbol.primitives)
        if not matches:
            issues.append(_issue(label, mark, "host_missing", "没有找到覆盖该洞口的宿主墙。"))
            continue
        if len(matches) > 1:
            several_walls = len({host for host, _, _ in matches}) > 1
            code = "host_ambiguous" if several_walls else "opening_ambiguous"
            message = "有多面墙可以作为宿主。" if several_walls else "标注附近有多个可匹配的洞口。"
            issues.append(_issue(label, mark, code, message))
            continue
        (host, category, at), sources = next(iter(matches.items()))
        proposals[(host, at)].append((label, mark, category, width, height, sill, sources))

    result = []
    for (host, at), options in proposals.items():
        _, mark, category, width, height, sill, _ = options[0]
        if len({(tag, kind) for _, tag, kind, *_ in options}) > 1:
            for label, tag, *_ in options:
                issues.append(_issue(label, tag, "mark_ambiguous", "同一个洞口对应多个标注或类型。"))
            continue
        handles = source_handles(
            item for label, *_, sources in options for item in [label, *sources]
        )
        candidate = {
            "category": category,
            "type": {"mark": mark, "width": width, "height": height},
            "geometry": {"kind": "point", "at": list(at)},
            "host": host,
            "confidence": 0.8,
            "evidence": ["schedule:exact_mark", "line:width_match", "wall:unique_host"],
            "handles": handles,
        }
        if sill is not None:
            candidate["sill_height"] = sill
        result.append(candidate)
    return result, issues
