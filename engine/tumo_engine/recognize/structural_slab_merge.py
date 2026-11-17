"""Register structural floor regions in an architectural project without changing their absolute height."""

from collections import defaultdict
from copy import deepcopy
from math import isclose

from shapely.affinity import translate
from shapely.geometry import Polygon

from tumo_engine.recognize.geometry import rounded
from tumo_engine.recognize.plan_registration import register_plan


def _project_shape(element, sheet):
    geometry = element.get("geometry") or {}
    if geometry.get("kind") != "polygon":
        return None
    polygon = Polygon(geometry["points"])
    if not polygon.is_valid or polygon.is_empty:
        return None
    transform = sheet.get("transform") or {}
    if transform.get("rot_deg", 0):
        from shapely.affinity import rotate

        polygon = rotate(polygon, transform["rot_deg"], origin=(0, 0))
    return translate(polygon, transform.get("dx", 0), transform.get("dy", 0))


def merge_structural_slabs(building, structural):
    """Attach one axis-registered structural slab drawing to matching architectural levels."""
    from tumo_engine.pipeline import _stable_id

    merged = deepcopy(building)

    def issue(message, sheet=None, at=None):
        entry = {"id": _stable_id("I", sheet, message, at), "severity": "warn", "message": message}
        if sheet is not None:
            entry["where"] = {"sheet": sheet}
            if at is not None:
                entry["where"]["at"] = at
        if entry not in merged["issues"]:
            merged["issues"].append(entry)

    candidates = [element for element in structural["elements"]
                  if element["category"] == "slab" and element.get("discipline") == "struct"]
    if len(structural["sheets"]) != 1 or structural["sheets"][0]["discipline"] != "struct":
        issue("结构降板合并需要一张独立的结构板图。")
        return merged
    source = structural["sheets"][0]
    sheet_id = source["id"]
    existing_sheets = {sheet["id"]: sheet for sheet in merged["sheets"]}
    if sheet_id in existing_sheets:
        issue(f"结构图号 {sheet_id} 已存在，无法再次合并。")
        return merged
    transform, message = register_plan(merged["grids"], structural["grids"])
    if transform is None:
        issue(f"{sheet_id} 无法与建筑配准。{message}")
        return merged

    levels = {level["id"]: level for level in merged["levels"]}
    source_levels = {level["id"]: level for level in structural["levels"]}
    existing = defaultdict(list)
    for element in merged["elements"]:
        if element["category"] in {"slab", "slab_opening"} and (sheet := existing_sheets.get(element["source"]["sheet"])):
            if shape := _project_shape(element, sheet):
                existing[element.get("base_level")].append(shape)
    project_sheet = {**deepcopy(source), "transform": transform, "levels": []}
    merged["sheets"].append(project_sheet)
    for entry in structural["issues"]:
        where = entry.get("where") or {}
        if where.get("sheet") == sheet_id:
            issue(entry["message"], sheet_id, where.get("at"))

    used_ids = {element["id"] for element in merged["elements"]}
    for source_slab in candidates:
        shape = _project_shape(source_slab, project_sheet)
        if shape is None:
            issue("结构降板边界无效，无法合并。", sheet_id)
            continue
        at = rounded(Polygon(source_slab["geometry"]["points"]).representative_point().coords[0])
        level_id = source_slab.get("base_level")
        reference, target = source_levels.get(level_id, {}), levels.get(level_id, {})
        structural_height, architectural_height = reference.get("elevation_struct"), target.get("elevation_arch")
        mark = source_slab["type"].get("mark") or source_slab["id"]
        if (source_slab["source"]["sheet"] != sheet_id or structural_height is None or architectural_height is None
                or reference.get("name") != target.get("name")):
            issue(f"{mark} 缺少同名楼层的建筑与结构标高，无法合并。", sheet_id, at)
            continue
        known = target.get("elevation_struct")
        if known is not None and not isclose(known, structural_height, rel_tol=0, abs_tol=0.001):
            issue(f"{target['name']} 的结构标高冲突。{mark} 暂留单图核对。", sheet_id, at)
            continue
        if any(shape.intersection(other).area > 0 for other in existing[level_id]):
            issue(f"{mark} 的结构板区与项目已有同层板区或洞口重叠。请核对边界。", sheet_id, at)
            continue
        if source_slab["id"] in used_ids:
            issue(f"结构降板编号 {source_slab['id']} 重复，无法合并。", sheet_id, at)
            continue
        candidate = deepcopy(source_slab)
        candidate["base_offset"] = round(structural_height + (candidate.get("base_offset") or 0) - architectural_height, 3)
        candidate.setdefault("evidence", []).append("structural_slab:registered_axes_and_level_datums")
        merged["elements"].append(candidate)
        used_ids.add(candidate["id"])
        existing[level_id].append(shape)
        target["elevation_struct"] = structural_height
        if evidence := reference.get("evidence"):
            target["evidence"] = list(dict.fromkeys([*target.get("evidence", ()),
                                                    *(f"{sheet_id}:{entry}" for entry in evidence)]))
        if level_id not in project_sheet["levels"]:
            project_sheet["levels"].append(level_id)
    return merged
