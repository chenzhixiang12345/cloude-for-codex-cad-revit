"""Match scheduled openings to local DXF evidence and a single existing wall."""

from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from itertools import product
from math import dist
import re
from unicodedata import normalize

from shapely.geometry import Point

from tumo_engine.index import SpatialIndex
from tumo_engine.preprocess import Primitive, layer_key
from tumo_engine.recognize.geometry import Segment, dot, parallel, rounded, segments, source_handles, world
from tumo_engine.tables import OpeningScheduleRow, WallOpeningScheduleRow

_DOOR_LAYER = re.compile(r"(?:^|[-_])DOOR(?:$|[-_])|^(?:门洞|门扇|门框)$")
_WINDOW_LAYER = re.compile(r"(?:^|[-_])(?:WIND|WINDOW)(?:$|[-_])|^(?:窗洞|窗扇|窗框)$")
_WALL_OPENING_LAYER = re.compile(r"^(?:(?:A|AE)[-_])?(?:WALL[-_](?:OPENING|HOLE)|墙(?:上|体)?洞口|墙洞)$")


@dataclass(frozen=True)
class _Host:
    id: str
    centerline: Segment
    thickness: float
    level_span: tuple
    sheet: str | None


def _mark(text):
    return normalize("NFKC", text).upper().strip()


def _opening_segments(items):
    for item in items:
        layer = layer_key(item.layer)
        if _WALL_OPENING_LAYER.fullmatch(layer):
            for segment in segments([item]):
                yield segment, "wall_opening"
            continue
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
            level_span = (wall.get("base_level"), wall.get("top_level"),
                          wall.get("base_offset") or 0, wall.get("top_offset") or 0)
            yield _Host(identifier, Segment(start, end, ()), thickness, level_span,
                        (wall.get("source") or {}).get("sheet"))


def _center_on_host(symbol, host, host_tolerance, angle_tolerance, wall_opening_width=None):
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
    middle = (low + high) / 2
    if wall_opening_width is not None and (
        middle - wall_opening_width / 2 < start - 1 or middle + wall_opening_width / 2 > end + 1
    ):
        return None
    return world(middle, offset, axis)


def _matching_wall_pairs(symbol, hosts, width, host_tolerance, rules):
    """Match a measured gap to the actual opposing endpoints of two walls."""
    axis = symbol.axis
    low, high = symbol.projected(axis)
    endpoint_tolerance = rules["endpoint_tolerance"]
    left, right = [], []
    for host in hosts:
        if not host.level_span[0]:
            continue
        line = host.centerline
        wall_axis = line.axis
        if not parallel(axis, wall_axis, rules["direction_tolerance_deg"]):
            continue
        normal = (-wall_axis[1], wall_axis[0])
        offset = line.offset(wall_axis)
        if any(abs(dot(point, normal) - offset) > host.thickness / 2 + host_tolerance
               for point in (symbol.a, symbol.b)):
            continue

        first, last = sorted((line.a, line.b), key=lambda point: dot(point, axis))
        start, end = dot(first, axis), dot(last, axis)
        if start < low - endpoint_tolerance and abs(end - low) <= endpoint_tolerance:
            left.append((host, last))
        if end > high + endpoint_tolerance and abs(start - high) <= endpoint_tolerance:
            right.append((host, first))

    for (first, start), (second, end) in product(left, right):
        if (first.id == second.id or first.level_span != second.level_span or first.sheet != second.sheet
                or abs(first.thickness - second.thickness) > rules["thickness_tolerance"]
                or abs(dist(start, end) - width) > rules["width_tolerance"]):
            continue
        line = first.centerline
        wall_axis = line.axis
        normal = (-wall_axis[1], wall_axis[0])
        offset = line.offset(wall_axis)
        if any(abs(dot(point, normal) - offset) > rules["axis_tolerance"]
               for point in (second.centerline.a, second.centerline.b)):
            continue
        if not parallel(wall_axis, second.centerline.axis, rules["direction_tolerance_deg"]):
            continue
        # Anchoring to the wall endpoints gives coincident symbol strokes one match.
        at = tuple(rounded(((start[0] + end[0]) / 2, (start[1] + end[1]) / 2)))
        yield tuple(sorted((first.id, second.id))), at


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
    wall_pair_rules: Mapping[str, float] | None = None,
) -> tuple[list[dict], list[dict]]:
    """Return opening candidates and located issues for exact scheduled tags.

    Door, window and wall opening symbols use measured line segments on dedicated layers.
    A wall opening additionally requires a wall schedule row with a relative bottom.
    Width and position tolerances are in millimeters. Each host must have an
    ID and a centerline spanning the measured opening. When wall pair rules
    are supplied, a unique pair may instead return an internal ``wall_pair``
    with no ``host``; the caller must reconcile the pair before export.
    The caller supplies each candidate's final ID, floor, and sheet source.
    """
    dimensions = defaultdict(set)
    wall_dimensions = defaultdict(set)
    for row in schedule:
        rows = wall_dimensions if isinstance(row, WallOpeningScheduleRow) else dimensions
        rows[_mark(row.mark)].add((row.width, row.height, row.sill_height))
    if not (dimensions or wall_dimensions):
        return [], []

    items = list(items)
    symbols = SpatialIndex(_opening_segments(items), lambda item: item[0].geometry)
    hosts = list(_hosts(walls))
    wall_index = SpatialIndex(hosts, lambda host: host.centerline.geometry)
    maximum_half_thickness = max((host.thickness / 2 for host in hosts), default=0)

    issues = []
    proposals = defaultdict(list)
    for label in items:
        if label.kind != "text":
            continue
        mark = _mark(label.text)
        ordinary = dimensions.get(mark)
        wall_openings = wall_dimensions.get(mark)
        if not (ordinary or wall_openings):
            continue

        symbols_at_label = symbols.nearby(Point(label.points[0]), label_radius)
        is_wall = not ordinary
        if ordinary and wall_openings:
            matching_kinds = set()
            for segment, category in symbols_at_label:
                wall_kind = category == "wall_opening"
                group = wall_openings if wall_kind else ordinary
                if any(abs(segment.length - width) <= width_tolerance for width, _, _ in group):
                    matching_kinds.add(wall_kind)
            if len(matching_kinds) > 1:
                issues.append(_issue(label, mark, "opening_ambiguous", "同一标注附近有符合尺寸的墙洞和门窗。"))
                continue
            is_wall = True in matching_kinds

        measurements = wall_openings if is_wall else ordinary
        if len(measurements) != 1:
            source = "墙洞表中存在不同的尺寸或洞底高度。" if is_wall else "门窗表中存在不同的尺寸。"
            issues.append(_issue(label, mark, "schedule_conflict", source))
            continue
        (width, height, sill), = measurements
        nearby = [
            (segment, category) for segment, category in symbols_at_label
            if (category == "wall_opening") == is_wall and abs(segment.length - width) <= width_tolerance
        ]
        if not nearby:
            issues.append(_issue(label, mark, "opening_evidence_missing", "标注附近没有尺寸相符的洞口线。"))
            continue
        if is_wall and sill is None:
            issues.append(_issue(label, mark, "wall_opening_bottom_missing", "缺少明确的洞底相对标高。"))
            continue

        nearby_walls = [
            (symbol, category, list(wall_index.nearby(symbol.geometry, maximum_half_thickness + host_tolerance)))
            for symbol, category in nearby
        ]
        matches = defaultdict(list)
        for symbol, category, possible in nearby_walls:
            for host in possible:
                center = _center_on_host(symbol, host, host_tolerance, angle_tolerance, width if is_wall else None)
                if center is not None:
                    matches[((host.id,), category, tuple(rounded(center)))].extend(symbol.primitives)
        if not matches and not is_wall and wall_pair_rules is not None:
            for symbol, category, possible in nearby_walls:
                for pair, at in _matching_wall_pairs(symbol, possible, width, host_tolerance, wall_pair_rules):
                    matches[(pair, category, at)].extend(symbol.primitives)
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
            "confidence": 0.8,
            "evidence": ["schedule:exact_mark", "line:width_match"],
            "handles": handles,
        }
        if len(host) == 1:
            candidate["host"] = host[0]
            candidate["evidence"].append("wall:unique_host")
        else:
            candidate["wall_pair"] = list(host)
            candidate["evidence"].append("wall:paired_ends")
        if sill is not None:
            candidate["sill_height"] = sill
        if category == "wall_opening":
            candidate["evidence"].append("schedule:wall_opening_relative_bottom")
        result.append(candidate)
    return result, issues
