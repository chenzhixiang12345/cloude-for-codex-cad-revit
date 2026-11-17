"""Register verified structural framing to the building levels."""

from collections import defaultdict
from copy import deepcopy
from math import isclose

from shapely.affinity import rotate, translate
from shapely.geometry import LineString, Point, box

from tumo_engine.recognize.geometry import rounded
from tumo_engine.recognize.plan_registration import POSITION_TOLERANCE_MM, register_plan
from tumo_engine.rules import defaults


def _project_shape(shape, sheet, *, inverse=False):
    transform = sheet.get("transform") or {}
    dx, dy, angle = (transform.get(key, 0) for key in ("dx", "dy", "rot_deg"))
    if inverse:
        return rotate(translate(shape, -dx, -dy), -angle, origin=(0, 0))
    return translate(rotate(shape, angle, origin=(0, 0)) if angle else shape, dx, dy)


def _footprint(element, sheet):
    geometry, size = element["geometry"], element["type"]
    if element["category"] == "column" and geometry["kind"] == "point":
        x, y = geometry["at"]
        if diameter := size.get("diameter"):
            shape = Point(x, y).buffer(diameter / 2)
        elif size.get("width") and size.get("depth"):
            w, d = size["width"] / 2, size["depth"] / 2
            shape = rotate(box(x - w, y - d, x + w, y + d), geometry.get("rotation_deg") or 0, origin=(x, y))
        else:
            return None
    elif geometry["kind"] == "line":
        width = size.get("width" if element["category"] == "beam" else "thickness")
        if not width:
            return None
        shape = LineString((geometry["start"], geometry["end"])).buffer(width / 2, cap_style="flat")
    else:
        return None
    return _project_shape(shape, sheet)


def _wall_fragments(incoming, incoming_sheet, existing, existing_sheet, minimum_length, stable_id):
    """Cut the existing wall axis at the two confirmed structural ends, in its own coordinates."""
    new, old = incoming["geometry"], existing["geometry"]
    tolerance = POSITION_TOLERANCE_MM
    if (existing["category"] != "wall" or existing.get("discipline") != "arch"
            or incoming.get("top_level") != existing.get("top_level")
            or abs(incoming["type"]["thickness"] - existing["type"]["thickness"]) > tolerance):
        return None
    axis = LineString((old["start"], old["end"]))
    incoming_axis = _project_shape(LineString((new["start"], new["end"])), incoming_sheet)
    local_points = [Point(point) for point in _project_shape(incoming_axis, existing_sheet, inverse=True).coords]
    if any(point.distance(axis) > tolerance for point in local_points):
        return None
    lower, upper = sorted(axis.project(point) for point in local_points)
    if upper - lower < minimum_length:
        return None
    parts = []
    for start, end in ((0, lower), (upper, axis.length)):
        if end - start <= tolerance:
            continue
        if end - start < minimum_length:
            return None
        geometry = {"kind": "line", "start": rounded(axis.interpolate(start).coords[0]),
                    "end": rounded(axis.interpolate(end).coords[0])}
        part = deepcopy(existing)
        part.update(id=stable_id("W", existing["id"], geometry), geometry=geometry)
        part.setdefault("evidence", []).append(f"structural_wall:remaining_part_of:{existing['id']}")
        parts.append(part)
    return parts


def _level_shift(identifier, structural, architectural, tolerance):
    source, target = structural.get(identifier, {}), architectural.get(identifier, {})
    old, new = source.get("elevation_struct"), target.get("elevation_arch")
    if old is None or new is None or source.get("name") != target.get("name"):
        raise ValueError("缺少同名楼层对应的建筑与结构标高。")
    if (known := target.get("elevation_struct")) is not None and not isclose(known, old, abs_tol=tolerance, rel_tol=0):
        raise ValueError(f"{target['name']} 的结构标高冲突。")
    return old - new, old


def _project_elevation(levels, key):
    level = levels.get(key, {})
    value = level.get("elevation_arch")
    return level.get("elevation_struct") if value is None else value


def _wall_extent(element, levels):
    bottom = _project_elevation(levels, element.get("base_level"))
    if bottom is None:
        return None
    bottom += element.get("base_offset") or 0
    top = _project_elevation(levels, element.get("top_level"))
    if top is None:
        top = bottom + (element["type"].get("height") or 3000)
    else:
        top += element.get("top_offset") or 0
    return bottom, top


def _check_adjacent_walls(element, shape, occupancies, offsets, levels, rules):
    base = element.get("base_level")
    bottom = offsets["base_offset"][1] + (element.get("base_offset") or 0)
    top = offsets["top_offset"][1] + (element.get("top_offset") or 0)
    if top - bottom <= rules["level_tolerance"]:
        return "剪力墙底顶标高无效。"
    for (category, floor), group in occupancies.items():
        if category != "shear_wall" or floor == base:
            continue
        for existing, footprint in group.values():
            extent = _wall_extent(existing, levels)
            overlap = None if extent is None else min(top, extent[1]) - max(bottom, extent[0])
            if overlap is not None and overlap <= rules["level_tolerance"]:
                continue
            if shape.intersection(footprint).area <= rules["overlap_area"]:
                continue
            if overlap is None:
                return f"与跨层墙 {existing['id']} 平面重叠，缺少可核对的标高。"
            return f"与跨层墙 {existing['id']} 竖向重叠 {round(overlap, 3):g} 毫米。请核对墙顶和结构楼层标高。"
    return None


def merge_structural_framing(building, structural):
    """Register the original source frame and retain verified absolute elevations."""
    from tumo_engine.pipeline import _stable_id

    config = defaults()
    rules = config["structural_framing_merge"]
    merged = deepcopy(building)

    def issue(message, sheet=None, at=None):
        entry = {"id": _stable_id("I", sheet, message, at), "severity": "warn", "message": message}
        if sheet is not None:
            entry["where"] = {"sheet": sheet, "at": at}
        if entry not in merged["issues"]:
            merged["issues"].append(entry)

    if len(structural["sheets"]) != 1 or structural["sheets"][0]["discipline"] != "struct":
        issue("结构构件合并需要一张独立结构平面图。")
        return merged
    source = structural["sheets"][0]
    sheet_id = source["id"]
    sheets = {sheet["id"]: sheet for sheet in merged["sheets"]}
    if sheet_id in sheets:
        issue(f"结构图号 {sheet_id} 已存在，无法再次合并。")
        return merged
    transform, message = register_plan(merged["grids"], structural["grids"])
    if transform is None:
        issue(f"{sheet_id} 无法与建筑配准。{message}")
        return merged

    project_sheet = {**deepcopy(source), "transform": transform, "levels": []}
    merged["sheets"].append(project_sheet)
    for notice in structural["issues"]:
        where = notice.get("where") or {}
        if where.get("sheet") == sheet_id:
            issue(notice["message"], sheet_id, where.get("at"))

    eligible = {"column", "beam", "shear_wall"}
    incoming = [element for element in structural["elements"]
                if element["category"] in eligible and element.get("discipline") == "struct"]
    indexed = {element["category"] for element in incoming}
    if "shear_wall" in indexed:
        indexed.update(("wall", "column"))
    occupancies = defaultdict(dict)
    for element in merged["elements"]:
        category = element["category"]
        if category not in indexed or element["source"]["sheet"] not in sheets:
            continue
        shape = _footprint(element, sheets[element["source"]["sheet"]])
        if shape is not None and not shape.is_empty:
            key = "shear_wall" if category == "wall" else category
            occupancies[(key, element.get("base_level"))][element["id"]] = element, shape

    building_levels = {level["id"]: level for level in merged["levels"]}
    structural_levels = {level["id"]: level for level in structural["levels"]}
    used_ids = {element["id"] for element in merged["elements"]}
    referenced = {element.get("host") for element in merged["elements"]}
    referenced.update(relation[key] for relation in merged["relations"] for key in ("a", "b"))
    remapped = {}
    split_ids = set()
    for element in incoming:
        category, identifier = element["category"], element["id"]
        shape = _footprint(element, project_sheet)
        mark = element["type"].get("mark") or identifier
        if shape is None or shape.is_empty or element["source"]["sheet"] != sheet_id:
            issue(f"{mark} 结构平面几何或来源无效。", sheet_id)
            continue
        at = rounded(_project_shape(shape.centroid, project_sheet, inverse=True).coords[0])
        base, top = element.get("base_level"), element.get("top_level")
        refs = [("base_offset", base)]
        if category != "beam":
            refs.append(("top_offset", top))
        try:
            offsets = {field: _level_shift(level, structural_levels, building_levels, rules["level_tolerance"])
                       for field, level in refs}
        except ValueError as error:
            issue(f"{mark} {error}", sheet_id, at)
            continue
        if category == "beam" and element.get("base_offset") is None:
            issue(f"{mark} 缺少梁顶相对标高。", sheet_id, at)
            continue
        if identifier in used_ids:
            issue(f"{mark} 的构件编号重复。", sheet_id, at)
            continue
        if category == "shear_wall" and any(shape.intersection(footprint).area > rules["overlap_area"]
                                            for _, footprint in occupancies[("column", base)].values()):
            issue(f"{mark} 与项目已有同层柱重叠，请核对墙身边界。", sheet_id, at)
            continue
        if category == "shear_wall" and (conflict := _check_adjacent_walls(
                element, shape, occupancies, offsets, building_levels, rules)):
            issue(f"{mark} {conflict}", sheet_id, at)
            continue
        group = occupancies[(category, base)]
        conflicts = [(existing, footprint) for existing, footprint in group.values()
                     if shape.intersection(footprint).area > rules["overlap_area"]]
        replaced = None
        fragment_shapes = []
        if conflicts:
            existing, footprint = conflicts[0]
            if (len(conflicts) == 1 and category == "column" and existing.get("discipline") == "arch"
                    and existing.get("top_level") == top
                    and shape.hausdorff_distance(footprint) <= rules["column_match_tolerance"]):
                replaced = existing["id"]
            if len(conflicts) == 1 and category == "shear_wall":
                if existing["id"] in referenced:
                    issue(f"{mark} 重叠的墙有宿主或连接关系，请核对墙段。", sheet_id, at)
                    continue
                existing_sheet = sheets.get(existing["source"]["sheet"])
                if existing_sheet is not None:
                    fragments = _wall_fragments(element, project_sheet, existing, existing_sheet,
                                                config["wall"]["minimum_length"], _stable_id)
                    if fragments is not None:
                        fragment_shapes = [(part, _footprint(part, existing_sheet)) for part in fragments]
                        if all(part["id"] not in used_ids and shape.intersection(footprint).area <= rules["overlap_area"]
                               for part, footprint in fragment_shapes):
                            replaced = existing["id"]
            if replaced is None:
                issue(f"{mark} 与项目已有同层构件重叠，请核对位置或截面。", sheet_id, at)
                continue
        candidate = deepcopy(element)
        for field, level in refs:
            delta, old = offsets[field]
            candidate[field] = round((candidate.get(field) or 0) + delta, 3)
            building_levels[level]["elevation_struct"] = old
            evidence = structural_levels[level].get("evidence", ())
            if evidence:
                target = building_levels[level]
                target["evidence"] = list(dict.fromkeys([*target.get("evidence", ()),
                                                        *(f"{sheet_id}:{item}" for item in evidence)]))
            if level not in project_sheet["levels"]:
                project_sheet["levels"].append(level)
        candidate.setdefault("evidence", []).append("structural_framing:registered_axes_and_level_datums")
        if replaced:
            del group[replaced]
            if fragment_shapes:
                split_ids.add(replaced)
                for part, footprint in fragment_shapes:
                    part_id = part["id"]
                    used_ids.add(part_id)
                    group[part_id] = part, footprint
                    merged["elements"].append(part)
            else:
                remapped[replaced] = identifier
            label = "structural_wall" if category == "shear_wall" else "structural_column"
            candidate["evidence"].append(f"{label}:replaces:{replaced}")
        merged["elements"].append(candidate)
        used_ids.add(identifier)
        group[identifier] = candidate, shape

    merged["elements"] = [element for element in merged["elements"] if element["id"] not in remapped and element["id"] not in split_ids]
    for element in merged["elements"]:
        if element.get("host") in remapped:
            element["host"] = remapped[element["host"]]
    for relation in merged["relations"]:
        for key in ("a", "b"):
            if relation[key] in remapped:
                relation[key] = remapped[relation[key]]
    for entry in merged["issues"]:
        if entry.get("element") in remapped:
            entry["element"] = remapped[entry["element"]]
        if entry.get("element") in split_ids:
            del entry["element"]
    return merged
