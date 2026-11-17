"""Recognize labeled rooms bounded by wall centerlines."""

from collections.abc import Iterable, Mapping
from hashlib import blake2s
import json
from math import isfinite
import re
from unicodedata import normalize

from shapely import get_parts, union_all
from shapely.geometry import Point, Polygon
from shapely.ops import polygonize

from tumo_engine.index import SpatialIndex
from tumo_engine.preprocess import Primitive, layer_key
from tumo_engine.recognize.geometry import rounded, segments
from tumo_engine.recognize.room_boundaries import local_issue, opening_bridges, wall_boundaries, wall_junctions
from tumo_engine.rules import defaults


ROOM_LAYERS = r"(?:^|[-_])(?:ROOM|房间名称|房间|房名)(?:$|[-_])"
MEASUREMENT = re.compile(r"[+-]?\d+(?:[.,]\d+)?\s*(?:mm|cm|m[23]?)?", re.IGNORECASE)
ROOM_AREA = re.compile(r"(\d+(?:\.\d+)?)\s*m(?:2|\\U\+00B2)", re.IGNORECASE)


def _room_label(item: Primitive):
    lines = item.text.strip().splitlines()
    if not lines:
        return ""
    name = " ".join(normalize("NFKC", lines[0]).split())
    if MEASUREMENT.fullmatch(name) or not any(character.isalpha() for character in name):
        return ""
    return name


def _labels(items, room_layers):
    label_layer = re.compile(room_layers, re.IGNORECASE)
    for item in items:
        if item.kind == "text" and item.points and label_layer.search(layer_key(item.layer)):
            if name := _room_label(item):
                yield Point(item.points[0]), name, item.handles


def _area_labels(items, area_layer):
    for item in items:
        if layer_key(item.layer) != area_layer:
            continue
        match = ROOM_AREA.fullmatch(normalize("NFKC", item.text).strip())
        if match:
            value = float(match[1])
            if isfinite(value) and value > 0:
                yield Point(item.points[0]), match[1]


def _unconfirmed_room_areas(items, labels, rooms, rules):
    areas = list(_area_labels(items, layer_key(rules["area_layer"])))
    if not areas:
        return []
    distance = rules["area_pair_distance"]
    area_index = SpatialIndex(areas, lambda area: area[0])
    name_index = SpatialIndex(labels, lambda label: label[0])
    confirmed = SpatialIndex((Polygon(room["geometry"]["points"]) for room in rooms), lambda polygon: polygon)
    issues = []
    for point, name, _ in labels:
        matching = area_index.nearby(point, distance)
        if len(matching) != 1:
            continue
        area_point, value = matching[0]
        if len(name_index.nearby(area_point, distance)) != 1:
            continue
        if confirmed.nearby(point, 0):
            continue
        issues.append(local_issue(
            "room_area_unconfirmed",
            f"房间“{name}”标注面积 {value} 平方米，尚未确认围合边界。请核对墙体和门洞。",
            point.coords[0],
        ))
    return issues


def _room_seed(polygon, label, footprint_index):
    nearby = footprint_index.nearby(polygon, 0)
    available = polygon.difference(union_all(nearby)) if nearby else polygon
    point = Point(rounded(label.coords[0]))
    if available.contains(point):
        return point.coords[0]
    parts = [part for part in get_parts(available) if part.area > 0]
    if len(parts) == 1:
        # A label can land inside half the thickness of a wall.
        seed = parts[0].representative_point()
        rounded_seed = Point(rounded(seed.coords[0]))
        if parts[0].contains(rounded_seed):
            return rounded_seed.coords[0]
    return None


def _detect_rooms(labels, walls, closures):
    if not labels or not walls:
        return [], []

    edges = [(wall.line, wall.handles, "wall") for wall in walls] + closures
    edge_index = SpatialIndex(edges, lambda edge: edge[0])
    label_index = SpatialIndex(labels, lambda label: label[0])
    footprints = SpatialIndex((wall.footprint for wall in walls if wall.half_width), lambda shape: shape)

    rooms = []
    issues = []
    # One micrometre matches the precision of the published wall coordinates.
    # It also nodes calculated junctions on the axis they meet.
    network = union_all([line for line, _, _ in edges], grid_size=0.001)
    for polygon in polygonize(network):
        inside = [label for label in label_index.nearby(polygon, 0) if polygon.contains(label[0])]
        if not inside:
            continue

        boundary = polygon.boundary.buffer(0.002)
        supporting = [
            (handles, kind)
            for line, handles, kind in edge_index.nearby(boundary, 0)
            if line.intersection(boundary).length > 0.02
        ]
        kinds = {kind for _, kind in supporting}
        if "wall" not in kinds:
            continue

        names = sorted({name for _, name, _ in inside})
        point, name, _ = min(inside, key=lambda label: (label[0].x, label[0].y))
        if len(names) != 1:
            issues.append(local_issue(
                "room_label_conflict", f"同一房间边界内有多个名称：{'、'.join(names)}。", point.coords[0],
            ))
            continue
        if polygon.interiors:
            issues.append(local_issue(
                "room_inner_boundary", f"房间“{name}”包含内边界，需要复核。", point.coords[0],
            ))
            continue
        seed = _room_seed(polygon, point, footprints)
        if seed is None:
            issues.append(local_issue(
                "room_seed_ambiguous", f"房间“{name}”没有唯一可用的室内定位点。", point.coords[0],
            ))
            continue

        handles = {handle for sources, _ in supporting for handle in sources}
        handles.update(handle for _, _, sources in inside for handle in sources)
        evidence = ["closed_wall_boundary", "room_label"]
        if "opening" in kinds:
            evidence.append("opening_boundary")
        if "wall_junction" in kinds:
            evidence.append("wall_junction")
        rooms.append({
            "category": "room",
            "type": {"name": name},
            "geometry": {
                "kind": "polygon",
                "points": [rounded(vertex) for vertex in polygon.simplify(0).normalize().exterior.coords[:-1]],
            },
            "params": {"label_point": rounded(point.coords[0]), "seed": rounded(seed)},
            "confidence": 0.83 if len(evidence) > 2 else 0.9,
            "evidence": evidence,
            "handles": sorted(handles),
        })
    return sorted(rooms, key=lambda room: room["geometry"]["points"]), issues


def recognize_rooms(
    items: Iterable[Primitive],
    walls: Iterable[dict],
    *,
    opening_boundaries: Iterable[Primitive] = (),
    room_layers: str = ROOM_LAYERS,
) -> list[dict]:
    """Return labeled wall faces, optionally closed by verified boundary lines."""
    closures = [
        (segment.geometry, segment.primitives[0].handles, "opening")
        for segment in segments(opening_boundaries)
    ]
    rooms, _ = _detect_rooms(list(_labels(items, room_layers)), list(wall_boundaries(walls)), closures)
    return rooms


def identify_rooms(
    items: Iterable[Primitive],
    walls: Iterable[dict],
    openings: Iterable[dict] = (),
    *,
    sheet_id: str,
    base_level: str,
    top_level: str | None = None,
    room_layers: str = ROOM_LAYERS,
    room_rules: Mapping | None = None,
) -> tuple[list[dict], list[dict]]:
    """Return schema 0.1 room elements and local issues for one selected plan."""
    texts = [item for item in items if item.kind == "text" and item.points]
    labels = list(_labels(texts, room_layers))
    if not labels:
        return [], []

    def on_plan(element):
        source = (element.get("source") or {}).get("sheet")
        floor = element.get("base_level")
        return (source is None or source == sheet_id) and (floor is None or floor == base_level)

    edges = list(wall_boundaries(wall for wall in walls if on_plan(wall)))
    rooms, boundary_issues = [], []
    if edges:
        joints, joint_issues = wall_junctions(edges)
        bridges, opening_issues = opening_bridges(edges, (element for element in openings if on_plan(element)))
        rooms, room_issues = _detect_rooms(labels, edges, joints + bridges)
        boundary_issues = [*joint_issues, *opening_issues, *room_issues]
    if room_rules is None:
        room_rules = defaults()["room"]
    area_issues = _unconfirmed_room_areas(texts, labels, rooms, room_rules)
    for room in rooms:
        payload = json.dumps([sheet_id, base_level, room["geometry"]], sort_keys=True).encode("utf-8")
        room["id"] = f"R-{blake2s(payload, digest_size=5).hexdigest().upper()}"
        room["discipline"] = "arch"
        room["base_level"] = base_level
        if top_level is not None:
            room["top_level"] = top_level
        room["source"] = {"sheet": sheet_id, "handles": room.pop("handles")}
    issues = sorted(
        [*boundary_issues, *area_issues],
        key=lambda issue: (issue["code"], issue["at"] or [], issue["message"]),
    )
    return rooms, issues
