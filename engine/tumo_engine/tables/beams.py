"""Read support- and geometry-checked single-span beam candidates from one plan."""

from collections import Counter, defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from itertools import combinations
from math import cos, radians, sin
import re
from unicodedata import normalize

from shapely.geometry import LineString, Point

from tumo_engine.index import SpatialIndex
from tumo_engine.preprocess import Primitive, XY, decompose, hidden_layer_names, layer_key, point, unit_factor
from tumo_engine.recognize.geometry import direction, dot, parallel, rounded, segments, world
from tumo_engine.rules import defaults


@dataclass(frozen=True)
class BeamCandidate:
    mark: str
    start: XY
    end: XY
    width: float
    height: float
    mark_handles: tuple[str, ...]
    leader_handles: tuple[str, ...]
    edge_handles: tuple[tuple[str, ...], tuple[str, ...]]
    section_handles: tuple[str, ...]
    support_handles: tuple[tuple[str, ...], tuple[str, ...]]


@dataclass(frozen=True)
class BeamIssue:
    code: str
    mark: str
    at: XY
    handles: tuple[str, ...]


def _text_axes(doc, notes):
    by_instance = defaultdict(set)
    for item in notes:
        by_instance[item.handles[0]].add(item.handles)
    scale = unit_factor(doc)
    hidden = hidden_layer_names(doc)
    directions = defaultdict(set)
    for handle, traces in by_instance.items():
        root = doc.entitydb.get(handle)
        if root is None:
            continue
        for entity, _, handles, _ in decompose([root], hidden_layers=hidden):
            if handles not in traces or entity.dxftype() != "TEXT":
                continue
            ocs = entity.ocs()
            if abs(ocs.uz.z) < 1 - 1e-8:
                continue
            rotation = radians(entity.dxf.get("rotation", 0))
            axis = ocs.to_wcs((cos(rotation), sin(rotation), 0))
            position = point(ocs.to_wcs(entity.get_placement()[1]), scale)
            directions[handles, position, entity.plain_text().strip()].add((axis.x, axis.y))
    return {key: next(iter(axes)) for key, axes in directions.items() if len(axes) == 1}


def _beam_edges(index, leader, mark, rules):
    a, b = sorted(leader.points, key=lambda p: -Point(p).distance(mark))
    leader_axis = direction(a, b)
    tip = Point(a)
    width_min, width_max = rules["width_range"]
    angle = rules["angle_deg"]
    normal = (-leader_axis[1], leader_axis[0])
    edges = [edge for edge in index.nearby(tip, width_max) if parallel(normal, edge.axis, angle)]
    for first, second in combinations(edges, 2):
        item_a, = first.primitives
        item_b, = second.primitives
        axis = first.axis
        if item_a.handles[0] != item_b.handles[0] or not parallel(axis, second.axis, angle):
            continue
        start, end = first.projected(axis)
        lo, hi = second.projected(axis)
        limit = rules["endpoint_tolerance"]
        if max(abs(start - lo), abs(end - hi)) > limit:
            continue
        low, high = max(start, lo), min(end, hi)
        if high - low < rules["minimum_beam_length"] or not low < dot(a, axis) < high:
            continue
        left, right = sorted((first.offset(axis), second.offset(axis)))
        width = right - left
        offset = dot(a, (-axis[1], axis[0]))
        if (abs(offset - (left + right) / 2) > rules["leader_tip_tolerance"]
                or not width_min - rules["width_tolerance"] <= width <= width_max + rules["width_tolerance"]):
            continue
        yield (world(low, (left + right) / 2, axis), world(high, (left + right) / 2, axis),
               width, (item_a.handles, item_b.handles))


def _norm(text):
    return normalize("NFKC", text).upper().strip()


def read_beam_candidates(doc, items: Iterable[Primitive], rules=None) -> tuple[list[BeamCandidate], list[BeamIssue]]:
    """Join one-span KL mark, leader, double dashed edges, section, and distinct supports."""
    cfg = (rules or defaults())["beam_candidate"]
    layers = {key: re.compile(value) for key, value in cfg["layers"].items()}
    linetype = re.compile(cfg["dashed_linetype"])
    mark_pattern = re.compile(cfg["mark_pattern"])
    size_pattern = re.compile(cfg["section_pattern"])
    marks, leaders, edges, sections, supports = [], [], [], [], []
    width_min, width_max = cfg["width_range"]
    height_min, height_max = cfg["height_range"]
    for item in items:
        if not item.handles:
            continue
        layer = layer_key(item.layer)
        if layers["annotation"].fullmatch(layer) and not item.blocks:
            if item.kind == "text" and (match := mark_pattern.fullmatch(_norm(item.text))):
                marks.append((item, match))
            elif item.kind == "line" and item.shape.length > cfg["minimum_leader_length"]:
                leaders.append(item)
        elif (item.kind == "line" and layers["edge"].fullmatch(layer)
              and linetype.search(item.linetype.upper()) and item.shape.length >= cfg["minimum_beam_length"]):
            edges.extend(segments([item]))
        elif item.kind == "text" and layers["section"].fullmatch(layer) and (match := size_pattern.fullmatch(_norm(item.text))):
            width, height = int(match["width"]), int(match["height"])
            if width_min <= width <= width_max and height_min <= height <= height_max:
                sections.append((item, width, height))
        elif item.kind == "polyline" and item.closed and layers["support"].fullmatch(layer) and item.shape.is_valid:
            supports.append(item)

    leader_index = SpatialIndex(leaders, lambda item: item.shape)
    edge_index = SpatialIndex(edges, lambda edge: edge.geometry)
    support_index = SpatialIndex(supports, lambda item: item.shape)
    section_index = SpatialIndex(sections, lambda row: row[0].shape)
    text_items = [item for item, _ in marks] + [item for item, _, _ in sections]
    axes = _text_axes(doc, text_items)

    def text_axis(item):
        return axes.get((item.handles, item.points[0], item.text))

    reviewed = []
    issues = []
    for label, match in marks:
        location = tuple(rounded(label.points[0]))
        mark = match["mark"]

        def warn(code, *extra):
            issues.append(BeamIssue(code, mark, location, tuple(dict.fromkeys((*label.handles, *extra)))))

        if match["span"] != "1" or match["overhang"] or match["qualifier"]:
            warn("beam_span_unsupported")
            continue
        heading = text_axis(label)
        normal = (-heading[1], heading[0]) if heading else None
        near = [line for line in leader_index.nearby(label.shape, cfg["leader_label_tolerance"])
                if normal and parallel(normal, direction(*line.points), cfg["angle_deg"])]
        if len(near) != 1:
            warn("beam_leader_unconfirmed", *(handle for line in near for handle in line.handles))
            continue
        leader, = near
        pairs = list(_beam_edges(edge_index, leader, label.shape, cfg))
        if len(pairs) != 1:
            warn("beam_edges_unconfirmed", *leader.handles, *(h for *_, pair in pairs for edge in pair for h in edge))
            continue
        start, end, measured_width, edge_handles = pairs[0]
        reference = edge_handles[0][0]
        trace = (*leader.handles, *edge_handles[0], *edge_handles[1])
        span = LineString((start, end))
        axis = direction(start, end)
        end_margin = cfg["section_end_margin"]
        dimensions = []
        for section, width, height in section_index.nearby(span, cfg["section_distance"]):
            if section.handles[0] != reference or abs(width - measured_width) > cfg["width_tolerance"]:
                continue
            heading = text_axis(section)
            if (heading and parallel(axis, heading, cfg["angle_deg"])
                    and end_margin < span.project(section.shape) < span.length - end_margin):
                dimensions.append((section, width, height))
        if len(dimensions) != 1:
            warn("beam_section_unconfirmed", *trace, *(h for section, _, _ in dimensions for h in section.handles))
            continue
        section, width, height = dimensions[0]
        connected = [
            [support for support in support_index.nearby(Point(p), cfg["support_tolerance"])
             if support.handles[0] == reference]
            for p in (start, end)
        ]
        if not all(len(side) == 1 for side in connected) or connected[0][0] is connected[1][0]:
            warn("beam_supports_unconfirmed", *trace, *section.handles,
                 *(h for supports_at in connected for support in supports_at for h in support.handles))
            continue
        candidate = BeamCandidate(
            mark, tuple(rounded(start)), tuple(rounded(end)), width, height, label.handles,
            leader.handles, edge_handles, section.handles, (connected[0][0].handles, connected[1][0].handles),
        )
        reviewed.append((candidate, location))

    claims = Counter((candidate.start, candidate.end) for candidate, _ in reviewed)
    result = []
    for candidate, at in reviewed:
        if claims[candidate.start, candidate.end] != 1:
            issues.append(BeamIssue("beam_mark_conflict", candidate.mark, at, candidate.mark_handles))
            continue
        result.append(candidate)
    return result, issues
