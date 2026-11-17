"""Register independently verified structural columns and beams to the building levels."""

from collections import defaultdict
from copy import deepcopy
from math import isclose

from shapely.affinity import rotate, translate
from shapely.geometry import LineString, Point, box

from tumo_engine.recognize.geometry import rounded
from tumo_engine.recognize.plan_registration import register_plan
from tumo_engine.rules import defaults


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
    elif element["category"] == "beam" and geometry["kind"] == "line" and size.get("width"):
        shape = LineString((geometry["start"], geometry["end"])).buffer(size["width"] / 2, cap_style="flat")
    else:
        return None
    transform = sheet.get("transform") or {}
    if transform.get("rot_deg"):
        shape = rotate(shape, transform["rot_deg"], origin=(0, 0))
    return translate(shape, transform.get("dx", 0), transform.get("dy", 0))


def _level_shift(identifier, structural, architectural, tolerance):
    source, target = structural.get(identifier, {}), architectural.get(identifier, {})
    old, new = source.get("elevation_struct"), target.get("elevation_arch")
    if old is None or new is None or source.get("name") != target.get("name"):
        raise ValueError("缺少同名楼层对应的建筑与结构标高。")
    if (known := target.get("elevation_struct")) is not None and not isclose(known, old, abs_tol=tolerance, rel_tol=0):
        raise ValueError(f"{target['name']} 的结构标高冲突。")
    return old - new, old


def merge_structural_framing(building, structural):
    """Merge columns and beams after registering the original source frame and retaining project Z."""
    from tumo_engine.pipeline import _stable_id

    rules = defaults()["structural_framing_merge"]
    merged = deepcopy(building)

    def issue(message, sheet=None, at=None):
        entry = {"id": _stable_id("I", sheet, message, at), "severity": "warn", "message": message}
        if sheet is not None:
            entry["where"] = {"sheet": sheet, "at": at}
        if entry not in merged["issues"]:
            merged["issues"].append(entry)

    if len(structural["sheets"]) != 1 or structural["sheets"][0]["discipline"] != "struct":
        issue("结构柱梁合并需要一张独立结构平面图。")
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

    occupancies = defaultdict(dict)
    for element in merged["elements"]:
        if element["category"] not in {"column", "beam"} or element["source"]["sheet"] not in sheets:
            continue
        shape = _footprint(element, sheets[element["source"]["sheet"]])
        if shape is not None and not shape.is_empty:
            occupancies[(element["category"], element.get("base_level"))][element["id"]] = element, shape

    building_levels = {level["id"]: level for level in merged["levels"]}
    structural_levels = {level["id"]: level for level in structural["levels"]}
    used_ids = {element["id"] for element in merged["elements"]}
    remapped = {}
    for element in structural["elements"]:
        category, identifier = element["category"], element["id"]
        if category not in {"beam", "column"} or element.get("discipline") != "struct":
            continue
        shape = _footprint(element, project_sheet)
        mark = element["type"].get("mark") or identifier
        if shape is None or shape.is_empty or element["source"]["sheet"] != sheet_id:
            issue(f"{mark} 结构平面几何或来源无效。", sheet_id)
            continue
        at = rounded(translate(shape.centroid, -transform["dx"], -transform["dy"]).coords[0])
        base, top = element.get("base_level"), element.get("top_level")
        refs = [("base_offset", base), ("top_offset", top)] if category == "column" else [("base_offset", base)]
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
        group = occupancies[(category, base)]
        conflicts = [(existing, footprint) for existing, footprint in group.values()
                     if shape.intersection(footprint).area > rules["overlap_area"]]
        replaced = None
        if conflicts:
            existing, footprint = conflicts[0]
            if (len(conflicts) != 1 or category != "column" or existing.get("discipline") != "arch"
                    or existing.get("top_level") != top
                    or shape.hausdorff_distance(footprint) > rules["column_match_tolerance"]):
                issue(f"{mark} 与项目已有同层构件重叠，请核对位置或截面。", sheet_id, at)
                continue
            replaced = existing["id"]
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
            remapped[replaced] = identifier
            del group[replaced]
            candidate["evidence"].append(f"structural_column:replaces:{replaced}")
        merged["elements"].append(candidate)
        used_ids.add(identifier)
        group[identifier] = candidate, shape

    merged["elements"] = [element for element in merged["elements"] if element["id"] not in remapped]
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
    return merged
