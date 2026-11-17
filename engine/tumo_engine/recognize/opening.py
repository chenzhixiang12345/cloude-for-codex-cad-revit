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
from tumo_engine.recognize.door_hosts import corner_door_host
from tumo_engine.recognize.door_symbols import door_frame_openings
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


@dataclass
class _Proposal:
    label: Primitive
    mark: str
    category: str
    width: float
    height: float
    sill: float | None
    sources: list[Primitive]
    evidence: list[str]
    extension: dict | None = None


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


def _has_closer_number(label, geometry, numbers):
    distance = geometry.distance(Point(label.points[0]))
    return any(_mark(other.text) != _mark(label.text) and geometry.distance(Point(other.points[0])) < distance
               for other in numbers.nearby(geometry, distance))


def _verified_window_gaps(label, width, symbols, walls, numbers, host_distance, pair_rules, number_rules):
    """Allow a farther window number only at a measured, uniquely supported gap."""
    radius = number_rules["radius"]
    width_tolerance = number_rules["width_tolerance"]
    strict_pairs = {**pair_rules, "width_tolerance": width_tolerance}
    origin = Point(label.points[0])
    strokes = defaultdict(list)
    for symbol, category in symbols.nearby(origin, radius):
        if category != "window" or abs(symbol.length - width) > width_tolerance:
            continue
        geometry = symbol.geometry
        if _has_closer_number(label, geometry, numbers):
            continue
        for pair, at in _matching_wall_pairs(symbol, walls.nearby(geometry, host_distance), width,
                                             number_rules["host_tolerance"], strict_pairs):
            strokes[(pair, category, at)].append(symbol)
    matches = {}
    for key, nearby in strokes.items():
        axis = nearby[0].axis
        offsets = [symbol.offset(axis) for symbol in nearby]
        if max(offsets) - min(offsets) >= number_rules["minimum_stroke_spacing"]:
            matches[key] = [item for symbol in nearby for item in symbol.primitives]
    return matches


def _symbol_hosts(symbol, category, possible, width, host_tolerance, angle_tolerance):
    for host in possible:
        center = _center_on_host(symbol, host, host_tolerance, angle_tolerance, width)
        if center is not None:
            yield (host.id,), category, tuple(rounded(center))


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
    window_number_rules: Mapping | None = None,
    door_frame_rules: Mapping | None = None,
    corner_rules: Mapping | None = None,
) -> tuple[list[dict], list[dict]]:
    """Return opening candidates and located issues for exact scheduled tags.

    Door, window and wall opening symbols use measured line segments on dedicated layers.
    A wall opening additionally requires a wall schedule row with a relative bottom.
    Width and position tolerances are in millimeters. Each host must have an
    ID and a centerline spanning the measured opening. When wall pair rules
    are supplied, a unique pair may instead return an internal ``wall_pair``
    with no ``host``. A measured door at a verified wall face may return a
    ``wall_extension``. The caller reconciles these host changes before export.
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
    walls = list(walls)
    symbols = SpatialIndex(_opening_segments(items), lambda item: item[0].geometry)
    frames = None
    if door_frame_rules:
        measured_frames = door_frame_openings(items, rules=door_frame_rules.get("measurement"))
        frames = SpatialIndex(measured_frames, lambda segment: segment.geometry)
    if frames is not None:
        door_number_layer = re.compile(door_frame_rules["label_layers"], re.IGNORECASE)
        door_mark_pattern = re.compile(door_frame_rules["mark_pattern"], re.IGNORECASE)
        door_numbers = SpatialIndex((item for item in items if item.kind == "text"
                                     and door_number_layer.search(layer_key(item.layer))
                                     and door_mark_pattern.fullmatch(_mark(item.text))),
                                    lambda item: Point(item.points[0]))
    hosts = list(_hosts(walls))
    wall_index = SpatialIndex(hosts, lambda host: host.centerline.geometry)
    maximum_half_thickness = max((host.thickness / 2 for host in hosts), default=0)
    if window_number_rules is not None and wall_pair_rules is not None:
        number_layer = re.compile(window_number_rules["label_layers"], re.IGNORECASE)
        number_pattern = re.compile(window_number_rules["mark_pattern"], re.IGNORECASE)
        window_mark_pattern = re.compile(window_number_rules["window_mark_pattern"], re.IGNORECASE)
        numbers = SpatialIndex((item for item in items if item.kind == "text" and number_layer.search(layer_key(item.layer))
                                and number_pattern.fullmatch(_mark(item.text))), lambda item: Point(item.points[0]))
    else:
        numbers = None

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
        measured = []
        if (not is_wall and frames is not None and door_number_layer.search(layer_key(label.layer))
                and door_mark_pattern.fullmatch(mark)):
            measured = [symbol for symbol in frames.nearby(Point(label.points[0]), door_frame_rules["radius"])
                        if abs(symbol.length - width) <= door_frame_rules["width_tolerance"]]
            if measured:
                nearby = [(symbol, "door") for symbol in measured]
        if nearby and is_wall and sill is None:
            issues.append(_issue(label, mark, "wall_opening_bottom_missing", "缺少明确的洞底相对标高。"))
            continue

        nearby_walls = [
            (symbol, category, list(wall_index.nearby(symbol.geometry, maximum_half_thickness + host_tolerance)))
            for symbol, category in nearby
        ]
        matches = defaultdict(list)
        extensions_by_match = {}
        uncontested_doors = set()
        for symbol, category, possible in nearby_walls:
            keys = list(_symbol_hosts(symbol, category, possible, width if is_wall or measured else None,
                                      host_tolerance, angle_tolerance))
            if not keys and measured and wall_pair_rules is not None:
                keys = [(pair, category, at) for pair, at in _matching_wall_pairs(
                    symbol, possible, width, host_tolerance, wall_pair_rules)]
            if not keys and measured and corner_rules is not None:
                extension = corner_door_host(symbol, width, walls, items, corner_rules)
                if extension:
                    key = ((extension["host"],), category, tuple(extension["at"]))
                    keys.append(key)
                    extensions_by_match[key] = extension
            for key in keys:
                matches[key].extend(symbol.primitives)
            if keys and len(measured) > 1 and not _has_closer_number(label, symbol.geometry, door_numbers):
                uncontested_doors.update(keys)
        if len(matches) > 1 and uncontested_doors:
            matches = {key: sources for key, sources in matches.items() if key in uncontested_doors}
        if not matches and not is_wall and not measured and wall_pair_rules is not None:
            for symbol, category, possible in nearby_walls:
                for pair, at in _matching_wall_pairs(symbol, possible, width, host_tolerance, wall_pair_rules):
                    matches[(pair, category, at)].extend(symbol.primitives)
        evidence = ["door:swing_and_jambs"] if measured else []
        if (not nearby and not is_wall and not measured and numbers is not None and number_layer.search(layer_key(label.layer))
                and window_mark_pattern.fullmatch(mark)):
            matches = _verified_window_gaps(label, width, symbols, wall_index, numbers,
                                             maximum_half_thickness + host_tolerance, wall_pair_rules, window_number_rules)
            if matches:
                evidence.append("window:two_strokes_verified_gap")
        if not matches:
            code = "host_missing" if nearby else "opening_evidence_missing"
            message = "没有找到覆盖该洞口的宿主墙。" if nearby else "标注附近没有尺寸相符的洞口线。"
            issues.append(_issue(label, mark, code, message))
            continue
        if len(matches) > 1:
            several_walls = len({host for host, _, _ in matches}) > 1
            code = "host_ambiguous" if several_walls else "opening_ambiguous"
            message = "有多面墙可以作为宿主。" if several_walls else "标注附近有多个可匹配的洞口。"
            issues.append(_issue(label, mark, code, message))
            continue
        (host, category, at), sources = next(iter(matches.items()))
        extension = extensions_by_match.get((host, category, at))
        if extension:
            evidence.append("wall:verified_corner_face")
        proposals[(host, at)].append(_Proposal(label, mark, category, width, height, sill, sources, evidence, extension))

    result = []
    for (host, at), options in proposals.items():
        selected = options[0]
        if len({(option.mark, option.category) for option in options}) > 1:
            for option in options:
                issues.append(_issue(option.label, option.mark, "mark_ambiguous", "同一个洞口对应多个标注或类型。"))
            continue
        extensions = [option.extension for option in options if option.extension is not None]
        if extensions and any(change != extensions[0] for change in extensions):
            issues.append(_issue(selected.label, selected.mark, "host_ambiguous", "标注对应不同的转角墙面。"))
            continue
        handles = source_handles(item for option in options for item in (option.label, *option.sources))
        candidate = {
            "category": selected.category,
            "type": {"mark": selected.mark, "width": selected.width, "height": selected.height},
            "geometry": {"kind": "point", "at": list(at)},
            "confidence": 0.8,
            "evidence": ["schedule:exact_mark", "line:width_match"],
            "handles": handles,
        }
        if len(host) == 1:
            candidate["host"] = host[0]
            candidate["evidence"].append("wall:unique_host")
            if extensions:
                candidate["wall_extension"] = extensions[0]
        else:
            candidate["wall_pair"] = list(host)
            candidate["evidence"].append("wall:paired_ends")
        candidate["evidence"].extend(sorted({entry for option in options for entry in option.evidence}))
        if selected.sill is not None:
            candidate["sill_height"] = selected.sill
        if selected.category == "wall_opening":
            candidate["evidence"].append("schedule:wall_opening_relative_bottom")
        result.append(candidate)
    return result, issues
