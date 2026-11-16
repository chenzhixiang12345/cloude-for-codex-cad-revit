"""Recognize labeled rooms bounded by wall centerlines."""

from collections.abc import Iterable
import re
from unicodedata import normalize

from shapely.geometry import LineString, Point
from shapely.ops import polygonize, unary_union

from tumo_engine.index import SpatialIndex
from tumo_engine.preprocess import Primitive, layer_key
from tumo_engine.recognize.geometry import rounded, segments


ROOM_LAYERS = r"(?:^|[-_])(?:ROOM|房间名称|房间|房名)(?:$|[-_])"
MEASUREMENT = re.compile(r"[+-]?\d+(?:[.,]\d+)?\s*(?:mm|cm|m[23]?)?", re.IGNORECASE)


def _room_label(item: Primitive):
    lines = item.text.strip().splitlines()
    if not lines:
        return ""
    name = " ".join(normalize("NFKC", lines[0]).split())
    if MEASUREMENT.fullmatch(name) or not any(character.isalpha() for character in name):
        return ""
    return name


def recognize_rooms(
    items: Iterable[Primitive],
    walls: Iterable[dict],
    *,
    opening_boundaries: Iterable[Primitive] = (),
    room_layers: str = ROOM_LAYERS,
) -> list[dict]:
    """Return room candidates for a single plan in local millimeter coordinates.

    Opening boundaries are caller-verified segments across gaps in the walls.
    The contract exposes a single polygon ring, so faces with holes are excluded.
    """
    label_layer = re.compile(room_layers, re.IGNORECASE)
    labels = []
    for item in items:
        if item.kind == "text" and item.points and label_layer.search(layer_key(item.layer)):
            if name := _room_label(item):
                labels.append((Point(item.points[0]), name, item.handles))
    if not labels:
        return []

    wall_edges = []
    for wall in walls:
        if wall.get("category") not in {"wall", "shear_wall"}:
            continue
        geometry = wall["geometry"]
        if geometry["kind"] == "line" and geometry["start"] != geometry["end"]:
            line = LineString((geometry["start"], geometry["end"]))
            handles = wall.get("handles") or (wall.get("source") or {}).get("handles") or ()
            wall_edges.append((line, handles, "wall"))
    if not wall_edges:
        return []

    opening_edges = [
        (segment.geometry, segment.primitives[0].handles, "opening")
        for segment in segments(opening_boundaries)
    ]
    edges = wall_edges + opening_edges
    edge_index = SpatialIndex(edges, lambda edge: edge[0])
    label_index = SpatialIndex(labels, lambda label: label[0])

    rooms = []
    network = unary_union([line for line, _, _ in edges])
    for polygon in polygonize(network):
        if polygon.interiors:
            continue
        inside = [label for label in label_index.nearby(polygon, 0) if polygon.contains(label[0])]
        if len({name for _, name, _ in inside}) != 1:
            continue
        point, name, _ = min(inside, key=lambda label: (label[0].x, label[0].y))

        boundary = polygon.boundary
        supporting = [
            (handles, kind)
            for line, handles, kind in edge_index.nearby(boundary, 0)
            if line.intersection(boundary).length > 0
        ]
        kinds = {kind for _, kind in supporting}
        if "wall" not in kinds:
            continue
        handles = {handle for sources, _ in supporting for handle in sources}
        handles.update(handle for _, _, sources in inside for handle in sources)

        evidence = ["closed_wall_boundary", "room_label"]
        if "opening" in kinds:
            evidence.append("opening_boundary")
        rooms.append({
            "category": "room",
            "type": {"name": name},
            "geometry": {
                "kind": "polygon",
                "points": [rounded(vertex) for vertex in polygon.normalize().exterior.coords[:-1]],
            },
            "params": {"label_point": rounded((point.x, point.y))},
            "confidence": 0.83 if "opening" in kinds else 0.9,
            "evidence": evidence,
            "handles": sorted(handles),
        })
    return sorted(rooms, key=lambda room: room["geometry"]["points"])
