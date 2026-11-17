"""Read CAD spot elevations from the arrow tip in machine-room and roof plans."""

from collections import Counter, defaultdict
from dataclasses import dataclass
from math import dist
import re
from unicodedata import normalize

from shapely.geometry import Point

from tumo_engine.index import SpatialIndex
from tumo_engine.preprocess import Primitive, XY, layer_key
from tumo_engine.recognize.geometry import direction, dot


@dataclass(frozen=True)
class RoofSpot:
    at: XY
    elevation_mm: float
    is_parapet: bool
    label_handles: tuple[str, ...]
    evidence_handles: tuple[tuple[str, ...], ...]


def is_roof_zone_plan(title):
    return bool(re.search(r"机房|屋面|屋顶", normalize("NFKC", title)))


def _relative(point, origin):
    return point[0] - origin[0], point[1] - origin[1]


def _other_endpoint(item, at, tolerance):
    a, b = item.points
    if dist(a, at) <= tolerance:
        return b
    if dist(b, at) <= tolerance:
        return a
    return None


def _arrow_arms(leader, edge, axis, normal, inward, length, index, config):
    tolerance = config["endpoint_tolerance"]
    minimum, maximum = config["half_width"]
    for first_arm in index.nearby(Point(edge), tolerance):
        if first_arm is leader or (tip := _other_endpoint(first_arm, edge, tolerance)) is None:
            continue
        offset = _relative(tip, edge)
        half_width = inward * dot(offset, axis)
        height = -dot(offset, normal)
        if not (minimum <= half_width <= maximum and 2 * half_width <= length + tolerance):
            continue
        if abs(height - half_width) > half_width * config["arm_proportion_tolerance"]:
            continue
        step = inward * 2 * half_width
        expected = edge[0] + step * axis[0], edge[1] + step * axis[1]
        other_arms = [
            arm for arm in index.nearby(Point(tip), tolerance)
            if arm is not leader and arm is not first_arm
            and (foot := _other_endpoint(arm, tip, tolerance)) is not None
            and dist(foot, expected) <= tolerance
        ]
        if len(other_arms) == 1:
            yield tip, half_width, first_arm, other_arms[0]


def _arrow_shapes(lines, config):
    index = SpatialIndex(lines, lambda item: item.shape)
    for leader in lines:
        a, b = leader.points
        leader_length = dist(a, b)
        if leader_length < 2 * config["half_width"][0] - config["endpoint_tolerance"]:
            continue
        for left, right in ((a, b), (b, a)):
            axis = direction(left, right)
            normal = -axis[1], axis[0]
            used = set()
            for edge, inward in ((left, 1), (right, -1)):
                for tip, half_width, first, second in _arrow_arms(
                    leader, edge, axis, normal, inward, leader_length, index, config,
                ):
                    key = frozenset((first.handles, second.handles))
                    if key not in used:
                        used.add(key)
                        yield tip, left, axis, normal, leader_length, half_width, (leader, first, second)


def _roof_spots(items, config):
    line_layer = re.compile(config["arrow_layers"])
    note_layer = re.compile(config["label_layers"])
    reading = re.compile(config["metre_pattern"])
    endpoint_tolerance = config["endpoint_tolerance"]
    glyphs = defaultdict(list)
    labels = defaultdict(list)
    for item in items:
        if not item.handles:
            continue
        layer = layer_key(item.layer)
        ancestry = item.handles[:-1]
        if item.kind == "line" and line_layer.fullmatch(layer):
            glyphs[ancestry].append(item)
        elif item.kind == "text" and note_layer.fullmatch(layer):
            labels[ancestry].append((item, normalize("NFKC", item.text).strip()))

    for ancestry, lines in glyphs.items():
        if ancestry not in labels:
            continue
        numbers = [(item, match[1]) for item, text in labels[ancestry] if (match := reading.fullmatch(text))]
        parapets = [item for item, text in labels[ancestry] if re.fullmatch(r"\(?\s*女[儿兒][墙牆]\s*\)?", text)]
        matches = []
        for tip, left, axis, normal, length, half_width, shapes in _arrow_shapes(lines, config):
            candidates = []
            for label, value in numbers:
                offset = _relative(label.points[0], left)
                x, y = dot(offset, axis), dot(offset, normal)
                along_leader = -endpoint_tolerance <= x <= length + endpoint_tolerance
                if along_leader and 0 < y <= half_width * config["label_height_ratio"]:
                    candidates.append((label, value))
            if len(candidates) == 1:
                label, value = candidates[0]
                qualifiers = []
                for item in parapets:
                    offset = _relative(item.points[0], label.points[0])
                    separation = dot(offset, axis)
                    if 0 < separation <= half_width * config["qualifier_distance_ratio"] and abs(
                        dot(offset, normal)
                    ) <= config["qualifier_row_tolerance"]:
                        qualifiers.append(item)
                if len(qualifiers) <= 1:
                    matches.append((tip, label, value, (*shapes, *qualifiers), bool(qualifiers)))
        usage = Counter(label.handles for _, label, _, _, _ in matches)
        for tip, label, value, evidence, is_parapet in matches:
            if usage[label.handles] == 1:
                yield RoofSpot(
                    tip, round(float(value.replace("−", "-").lstrip("±")) * 1000, 3),
                    is_parapet, label.handles, tuple(item.handles for item in evidence),
                )


def roof_zone_spots(items: list[Primitive], rules: dict) -> list[RoofSpot]:
    """Return source-local points only when arrow geometry and text correspond uniquely."""
    return sorted(_roof_spots(items, rules["roof_zone"]), key=lambda spot: (spot.at, spot.label_handles))
