"""Restore a door host where a measured door gap ends at a CAD wall return."""

import re

from shapely.geometry import LineString, Point

from tumo_engine.preprocess import layer_key
from tumo_engine.recognize.geometry import Segment, direction, dot, parallel, rounded, segments, source_handles, world
from tumo_engine.recognize.wall import _footprint
from tumo_engine.rules import defaults


def _model_walls(walls):
    for wall in walls:
        geometry = wall.get("geometry") or {}
        thickness = (wall.get("type") or {}).get("thickness")
        if wall.get("category") != "wall" or geometry.get("kind") != "line" or not wall.get("id"):
            continue
        start, end = tuple(geometry["start"]), tuple(geometry["end"])
        if thickness and thickness > 0 and start != end:
            yield wall["id"], Segment(start, end, ()), thickness


def _cad_outlines(items, layers, search_area):
    wall_layers = re.compile("|".join((layers["walls"], layers["structural_wall_outlines"])))
    obstruction_layers = re.compile("|".join((layers["structural_profiles"], layers["columns"])))
    for item in items:
        if item.kind != "polyline" or not item.closed or len(item.points) < 3:
            continue
        layer = layer_key(item.layer)
        is_wall = bool(wall_layers.search(layer))
        if not is_wall and not obstruction_layers.search(layer):
            continue
        outline = item.shape
        if outline.is_valid and search_area.intersects(outline):
            yield item, outline, is_wall


def _corner_faces(outlines, axis, normal, offset, far, thickness, angle, rules):
    low, high = offset - thickness / 2, offset + thickness / 2
    tolerance = rules["wall_face_tolerance"]
    for item, outline, is_wall in outlines:
        if not is_wall:
            continue
        for edge in segments([item]):
            if not parallel(edge.axis, normal, angle):
                continue
            edge_start, edge_end = edge.projected(axis)
            if max(abs(edge_start - far), abs(edge_end - far)) > tolerance:
                continue
            side_start, side_end = edge.projected(normal)
            if max(abs(side_start - low), abs(side_end - high)) > tolerance:
                continue
            face = (edge_start + edge_end) / 2
            probe = min(tolerance, rules["face_probe_offset"] + (edge_end - edge_start) / 2)
            if (outline.contains(Point(world(face + probe, offset, axis)))
                    and not outline.contains(Point(world(face - probe, offset, axis)))):
                yield edge_start, item


def corner_door_host(symbol, width, walls, items, rules):
    """Propose extending one existing wall only up to the measured far jamb face."""
    opening = rules["opening"]
    corner = opening.get("door_frame", {}).get("corner_host")
    if corner is None:
        corner = defaults()["opening"]["door_frame"]["corner_host"]
    face_tolerance = corner["wall_face_tolerance"]
    if width <= 2 * face_tolerance or abs(symbol.length - width) > face_tolerance:
        return None
    angle = opening["wall_pair"]["direction_tolerance_deg"]
    model_walls = list(_model_walls(walls))
    candidates = []
    for identifier, line, thickness in model_walls:
        if not parallel(line.axis, symbol.axis, angle):
            continue
        for begin, old_end in ((line.a, line.b), (line.b, line.a)):
            axis = direction(begin, old_end)
            normal = (-axis[1], axis[0])
            offset = dot(old_end, normal)
            near, far = symbol.projected(axis)
            if (abs(dot(old_end, axis) - near) <= face_tolerance
                    and all(abs(dot(p, normal) - offset) <= face_tolerance for p in (symbol.a, symbol.b))):
                candidates.append((identifier, line, thickness, old_end, axis, normal, offset, near, far))
    if len(candidates) != 1:
        return None

    identifier, line, thickness, old_end, axis, normal, offset, near, far = candidates[0]
    outlines = list(_cad_outlines(items, rules["layers"], symbol.geometry.buffer(thickness / 2 + face_tolerance)))
    faces = list(_corner_faces(outlines, axis, normal, offset, far, thickness, angle, corner))
    if not faces:
        return None
    end = min(position for position, _ in faces)
    rounding = corner["rounding_tolerance"]
    if any(position - end > rounding for position, _ in faces):
        return None
    middle = (near + far) / 2
    cut_end = middle + width / 2
    cut_tolerance = corner["revit_cut_end_tolerance"]
    if cut_end - end > cut_tolerance - rounding:
        return None

    new_end = rounded(world(end, offset, axis))
    extension = _footprint(LineString((old_end, new_end)), thickness).buffer(-rounding)
    if any(extension.intersects(_footprint(other.geometry, other_thickness))
           for other_id, other, other_thickness in model_walls if other_id != identifier):
        return None
    # The existing CAD wall may reach into its rounded model endpoint by a fraction of a millimetre.
    interior_start = world(max(near, dot(old_end, axis)) + cut_tolerance, offset, axis)
    interior = _footprint(LineString((interior_start, new_end)), thickness).buffer(-rounding)
    if any(interior.intersects(outline) for _, outline, _ in outlines):
        return None

    return {
        "host": identifier,
        "geometry": {
            "kind": "line",
            "start": new_end if old_end == line.a else list(line.a),
            "end": new_end if old_end == line.b else list(line.b),
        },
        "at": rounded(world(middle, offset, axis)),
        "handles": source_handles(item for _, item in faces),
    }
