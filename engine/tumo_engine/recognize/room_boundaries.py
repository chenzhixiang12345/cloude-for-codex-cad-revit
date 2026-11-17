"""Derive room boundaries from wall footprints and accepted openings."""

from collections import defaultdict
from dataclasses import dataclass
from math import isfinite, radians, sin

from shapely.geometry import LineString, Point, Polygon

from tumo_engine.index import SpatialIndex
from tumo_engine.preprocess import XY
from tumo_engine.recognize.geometry import direction, dot, parallel, rounded

JUNCTION_TOLERANCE = 2
OPENING_TOLERANCE = 50
OPENING_ANGLE = 5
_MIN_JUNCTION_SINE = sin(radians(15))


@dataclass(frozen=True)
class WallBoundary:
    id: str | None
    line: LineString
    footprint: LineString | Polygon
    half_width: float
    handles: tuple[str, ...]


def source_handles(element):
    return tuple(element.get("handles") or (element.get("source") or {}).get("handles") or ())


def local_issue(code, message, point):
    return {"code": code, "message": message, "at": rounded(point) if point is not None else None}


def _xy(value) -> XY | None:
    if isinstance(value, (list, tuple)) and len(value) == 2 and all(
        isinstance(coordinate, (float, int)) and isfinite(coordinate) for coordinate in value
    ):
        return value[0], value[1]
    return None


def wall_boundaries(walls):
    for wall in walls:
        if wall.get("category") not in {"wall", "shear_wall"}:
            continue
        geometry = wall.get("geometry") or {}
        if geometry.get("kind") != "line":
            continue
        start, end = _xy(geometry.get("start")), _xy(geometry.get("end"))
        if start is None or end is None or start == end:
            continue
        line = LineString((start, end))
        width = (wall.get("type") or {}).get("thickness")
        half = width / 2 if isinstance(width, (int, float)) and isfinite(width) and width > 0 else 0
        footprint = line.buffer(half, cap_style="flat") if half else line
        yield WallBoundary(wall.get("id"), line, footprint, half, source_handles(wall))


def _cross(a, b):
    return a[0] * b[1] - a[1] * b[0]


def _difference(a, b):
    return a[0] - b[0], a[1] - b[1]


def _along(origin, axis, distance):
    return origin[0] + axis[0] * distance, origin[1] + axis[1] * distance


def _junction(first, second):
    a, b = first.line.coords[0], second.line.coords[0]
    axis_a = direction(a, first.line.coords[-1])
    axis_b = direction(b, second.line.coords[-1])
    sine = _cross(axis_a, axis_b)
    if abs(sine) < _MIN_JUNCTION_SINE:
        return None

    delta = _difference(b, a)
    along_a = _cross(delta, axis_b) / sine
    along_b = _cross(delta, axis_a) / sine
    extensions = []
    for along, wall, other in ((along_a, first, second), (along_b, second, first)):
        beyond = max(-along, along - wall.line.length)
        if beyond * abs(sine) > other.half_width + JUNCTION_TOLERANCE:
            return None
        extensions.append(0 if along < 0 else -1 if along > wall.line.length else None)
    return _along(a, axis_a, along_a), extensions


def wall_junctions(walls):
    """Extend axes only when their actual wall footprints meet at a unique joint."""
    index = SpatialIndex(enumerate(walls), lambda item: item[1].footprint)
    alternatives = defaultdict(set)
    proposals = []
    for i, first in enumerate(walls):
        if not first.half_width:
            continue
        for j, second in index.nearby(first.footprint, JUNCTION_TOLERANCE):
            if i >= j or not second.half_width:
                continue
            joint = _junction(first, second)
            if joint is None:
                continue
            at, extensions = joint
            ends = tuple((index, side) for index, side in zip((i, j), extensions) if side is not None)
            for endpoint in ends:
                alternatives[endpoint].add(tuple(rounded(at)))
            if ends:
                handles = (*first.handles, *second.handles)
                proposals.append((at, ends, handles))

    ambiguous = {end for end, choices in alternatives.items() if len(choices) > 1}
    bridges = []
    for at, ends, handles in proposals:
        if ambiguous.intersection(ends):
            continue
        for index, side in ends:
            end = walls[index].line.coords[side]
            if end != at:
                bridges.append((LineString((end, at)), handles, "wall_junction"))
    issues = [
        local_issue("room_junction_conflict", "房间边界的墙端存在多个可能的连接位置。", walls[index].line.coords[side])
        for index, side in sorted(ambiguous)
    ]
    return bridges, issues


def _opening_span(opening, host):
    geometry = opening.get("geometry") or {}
    width = (opening.get("type") or {}).get("width")
    if not isinstance(width, (int, float)) or not isfinite(width) or width <= 0:
        width = None

    if geometry.get("kind") == "line":
        a, b = _xy(geometry.get("start")), _xy(geometry.get("end"))
        if a is not None and b is not None and a != b:
            span = LineString((a, b))
            at = span.interpolate(0.5, normalized=True).coords[0]
            if width is None or abs(span.length - width) <= OPENING_TOLERANCE:
                return span, at
            return None, at

    at = _xy(geometry.get("at"))
    if at is None or geometry.get("kind") != "point" or host is None or width is None:
        return None, at
    start = host.line.coords[0]
    axis = direction(start, host.line.coords[-1])
    delta = _difference(at, start)
    if abs(_cross(delta, axis)) > host.half_width + OPENING_TOLERANCE:
        return None, at
    along = dot(delta, axis)
    return LineString((_along(start, axis, along - width / 2), _along(start, axis, along + width / 2))), at


def _opening_ends(point, inward, index):
    candidates = []
    for wall_index, wall in index.nearby(point, OPENING_TOLERANCE):
        ends = wall.line.coords
        if not parallel(inward, direction(ends[0], ends[-1]), OPENING_ANGLE):
            continue
        for endpoint, other in ((ends[0], ends[-1]), (ends[-1], ends[0])):
            if point.distance(Point(endpoint)) <= OPENING_TOLERANCE and dot(_difference(other, endpoint), inward) < 0:
                candidates.append((wall_index, endpoint))
    return candidates


def _bridge(span, host, index):
    a, b = span.coords
    axis = direction(a, b)
    if host is not None and not parallel(axis, direction(*host.line.coords), OPENING_ANGLE):
        return None

    # A continuous wall already bounds the room, including at hosted door/window locations.
    near = index.nearby(span, OPENING_TOLERANCE)
    for _, wall in near:
        if (host is None or wall == host) and parallel(axis, direction(*wall.line.coords), OPENING_ANGLE):
            if wall.line.distance(Point(a)) <= OPENING_TOLERANCE and wall.line.distance(Point(b)) <= OPENING_TOLERANCE:
                return span

    first = _opening_ends(Point(a), axis, index)
    last = _opening_ends(Point(b), (-axis[0], -axis[1]), index)
    if len(first) != 1 or len(last) != 1:
        return None
    (i, a), (j, b) = first[0], last[0]
    if i == j or (host is not None and host not in (index.values[i][1], index.values[j][1])):
        return None

    line = LineString((a, b))
    if line.length <= 0.02 or abs(line.length - span.length) > OPENING_TOLERANCE:
        return None
    inside = LineString((line.interpolate(0.01), line.interpolate(line.length - 0.01)))
    if any(wall.line.intersects(inside) for _, wall in index.nearby(inside, 0)):
        return None
    return line


def opening_bridges(walls, openings):
    index = SpatialIndex(enumerate(walls), lambda item: item[1].line)
    by_id = {wall.id: wall for wall in walls if wall.id}
    proposals = defaultdict(list)
    issues = []
    for opening in openings:
        category = opening.get("category")
        if category not in {"door", "window", "wall_opening"}:
            continue
        host_id = opening.get("host")
        host = by_id.get(host_id)
        span, at = _opening_span(opening, host)
        bridge = _bridge(span, host, index) if span is not None and (not host_id or host is not None) else None
        if bridge is None:
            issues.append(local_issue(
                "room_opening_conflict", "已识别洞口的尺寸或位置与房间边界不一致。", at,
            ))
        elif bridge is not span:
            proposals[tuple(sorted(bridge.coords))].append((category, source_handles(opening)))
    bridges = []
    for points, sources in sorted(proposals.items()):
        line = LineString(points)
        if len({category for category, _ in sources}) != 1:
            issues.append(local_issue(
                "room_opening_conflict", "同一处房间边界缺口对应不同的门窗或洞口。", line.centroid.coords[0],
            ))
            continue
        handles = tuple(handle for _, entries in sources for handle in entries)
        bridges.append((line, handles, "opening"))
    return bridges, issues
