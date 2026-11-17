"""Combine verified, aligned floor plans into one model with local CAD coordinates."""

from collections import defaultdict
from collections.abc import Iterable
from copy import deepcopy
from math import cos, dist, radians, sin

from pydantic import ValidationError

from tumo_engine.export.model import SheetTransform
from tumo_engine.recognize.geometry import world
from tumo_engine.recognize.plan_registration import POSITION_TOLERANCE_MM, group_grids_by_original_label


class _MergeConflict(ValueError):
    pass


def _by_id(rows, label):
    indexed = {}
    for row in rows:
        identifier = row["id"]
        if identifier in indexed:
            raise _MergeConflict(f"{label}编号 {identifier} 重复，无法合并。")
        indexed[identifier] = row
    return indexed


def _base_sheets(model):
    bases = defaultdict(set)
    for sheet in model["sheets"]:
        if sheet["levels"]:
            bases[sheet["levels"][0]].add(sheet["id"])
    for element in model["elements"]:
        if level := element.get("base_level"):
            bases[level].add(element["source"]["sheet"])
    return bases


def _merge_levels(levels, new_levels):
    for identifier, incoming in new_levels.items():
        current = levels.get(identifier)
        if current is None:
            levels[identifier] = deepcopy(incoming)
            continue
        for field, kind in (("elevation_arch", "建筑"), ("elevation_struct", "结构")):
            value = incoming.get(field)
            if value is None:
                continue
            if current.get(field) is None:
                current[field] = value
            elif current[field] != value:
                raise _MergeConflict(f"{identifier} 的{kind}标高冲突，无法合并。")
        if evidence := incoming.get("evidence"):
            current["evidence"] = list(dict.fromkeys([*current.get("evidence", ()), *evidence]))


def _same_endpoints(start, end, target):
    a, b = target["start"], target["end"]
    return min(max(dist(start, a), dist(end, b)), max(dist(start, b), dist(end, a))) <= POSITION_TOLERANCE_MM


def _axis_matches(local, anchor, aligned):
    if not local or len(local) != len(anchor) or None in (*local, *anchor):
        return {}
    if len(local) == 1:
        # Registration already verified shared labels with one segment.
        return {local[0]["id"]: anchor[0]["id"]}

    rotation = radians(aligned["rot_deg"])
    axis = (cos(rotation), sin(rotation))

    def transform(point):
        x, y = world(*point, axis)
        return (x + aligned["dx"], y + aligned["dy"])

    matches = {}
    used = set()
    for grid in local:
        start, end = transform(grid["start"]), transform(grid["end"])
        candidates = [target["id"] for target in anchor if _same_endpoints(start, end, target)]
        if len(candidates) != 1 or candidates[0] in used:
            return {}
        matches[grid["id"]] = candidates[0]
        used.add(candidates[0])
    return matches


def _append_plan(result, plan, transform, levels, bases, grids_by_label, relations):
    if len(plan["sheets"]) != 1:
        raise _MergeConflict("每组配准变换需要对应一张局部图纸。")
    local_sheet = plan["sheets"][0]
    sheet = local_sheet["id"]
    if any(other["id"] == sheet for other in result["sheets"]):
        raise _MergeConflict(f"图纸编号 {sheet} 重复，无法合并。")
    try:
        aligned = SheetTransform.model_validate(transform).model_dump()
    except ValidationError as error:
        raise _MergeConflict(f"{sheet} 的坐标变换不完整或含无效值。") from error

    plan_levels = _by_id(plan["levels"], f"{sheet} 标高")
    _merge_levels(levels, plan_levels)
    plan_bases = _base_sheets(plan)
    if overlap := sorted(plan_bases.keys() & bases.keys()):
        level = overlap[0]
        existing = "、".join(sorted(bases[level]))
        raise _MergeConflict(f"{sheet} 与 {existing} 有相同的基底标高 {level}，无法合并。")

    elements = _by_id(plan["elements"], f"{sheet} 构件")
    old_elements = {element["id"] for element in result["elements"]}
    if duplicate := sorted(elements.keys() & old_elements):
        raise _MergeConflict(f"构件编号 {duplicate[0]} 重复，无法合并。")
    issues = _by_id(plan["issues"], f"{sheet} 疑点")
    old_issues = {issue["id"] for issue in result["issues"]}
    if duplicate := sorted(issues.keys() & old_issues):
        raise _MergeConflict(f"疑点编号 {duplicate[0]} 重复，无法合并。")

    local_grids = _by_id(plan["grids"], f"{sheet} 轴网")
    grids_by_original_label = group_grids_by_original_label(local_grids.values())
    original_labels = {grid["id"]: label for label, group in grids_by_original_label.items()
                       for grid in group if grid is not None}
    matched_axes = {}
    if collisions := sorted((elements.keys() & plan_levels.keys()) | (local_grids.keys() & (elements.keys() | plan_levels.keys()))):
        raise _MergeConflict(f"{sheet} 的引用编号 {collisions[0]} 重复，无法合并。")

    def reference(identifier):
        if grid := local_grids.get(identifier):
            if identifier not in matched_axes:
                label = original_labels.get(identifier)
                matched_axes.update(_axis_matches(
                    grids_by_original_label.get(label, ()), grids_by_label.get(label, ()), aligned,
                ))
                if identifier not in matched_axes:
                    raise _MergeConflict(f"{sheet} 的轴号 {label or grid['label']} 无法唯一对应到主图轴网。")
            return matched_axes[identifier]
        if identifier in elements or identifier in plan_levels:
            return identifier
        raise _MergeConflict(f"{sheet} 引用了未知构件、标高或轴网 {identifier}。")

    if missing := sorted(set(local_sheet["levels"]) - plan_levels.keys()):
        raise _MergeConflict(f"{sheet} 引用了未提供的标高 {missing[0]}。")
    for element in elements.values():
        identifier = element["id"]
        if element["source"]["sheet"] != sheet:
            raise _MergeConflict(f"构件 {identifier} 的来源图纸与 {sheet} 不符。")
        if (host := element.get("host")) and host not in elements:
            raise _MergeConflict(f"构件 {identifier} 缺少同图宿主 {host}。")
        for field in ("base_level", "top_level"):
            if (level := element.get(field)) and level not in plan_levels:
                raise _MergeConflict(f"构件 {identifier} 引用了未提供的标高 {level}。")

    for issue in issues.values():
        if (where := issue.get("where")) and where["sheet"] != sheet:
            raise _MergeConflict(f"疑点 {issue['id']} 的来源图纸与 {sheet} 不符。")
        entry = deepcopy(issue)
        if identifier := entry.get("element"):
            entry["element"] = reference(identifier)
        result["issues"].append(entry)

    for relation in plan["relations"]:
        entry = {**relation, "a": reference(relation["a"]), "b": reference(relation["b"])}
        if (identity := _relation_key(entry)) not in relations:
            result["relations"].append(entry)
            relations.add(identity)
    result["sheets"].append({**deepcopy(local_sheet), "transform": aligned})
    result["elements"].extend(deepcopy(plan["elements"]))
    bases.update(plan_bases)


def _relation_key(relation):
    return relation["kind"], relation["a"], relation["b"], relation.get("priority")


def merge_building_models(anchor_model: dict, aligned_plans: Iterable[tuple[dict, dict]]) -> tuple[dict, list[str]]:
    """Merge one model per aligned sheet, or return the unchanged anchor on conflict.

    Each transform maps local millimetres to the anchor drawing coordinates.
    Grid fit is a prerequisite. The anchor supplies all published grids and
    the project origin. Sources, bounds and element coordinates stay local.
    """
    merged = deepcopy(anchor_model)
    try:
        levels = _by_id(merged["levels"], "主图标高")
        bases = _base_sheets(merged)
        grids_by_label = group_grids_by_original_label(_by_id(merged["grids"], "主图轴网").values())
        _by_id(merged["sheets"], "主图图纸")
        _by_id(merged["elements"], "主图构件")
        _by_id(merged["issues"], "主图疑点")
        relations = {_relation_key(relation) for relation in merged["relations"]}
        for plan, transform in aligned_plans:
            _append_plan(merged, plan, transform, levels, bases, grids_by_label, relations)
        merged["levels"] = list(levels.values())
    except _MergeConflict as error:
        return anchor_model, [str(error)]
    return merged, []
