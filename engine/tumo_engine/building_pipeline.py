"""Assemble the plans with verified levels and axes from one architectural DXF."""

from functools import cache
from pathlib import Path
import re
from unicodedata import normalize

from shapely.geometry import box

from tumo_engine.index import SpatialIndex
from tumo_engine.io.dxf_reader import read_dxf
from tumo_engine.pipeline import _level_schedule, _read_opening_companion, _recognize_plan, _stable_id
from tumo_engine.preprocess import bind_xrefs, primitives, unit_factor
from tumo_engine.recognize.elevation import read_elevation_drawing
from tumo_engine.recognize.level import floor_from_title
from tumo_engine.recognize.standard_floors import standard_floor_span
from tumo_engine.rules import defaults
from tumo_engine.sheets import discipline, drawing_sheets, input_drawing, sheet_kind


def _verified_level_pair(title, schedule):
    number = floor_from_title(title)
    name = normalize("NFKC", title).strip()
    bases = [row for row in schedule if (number is not None and number == row.number)
             or (number is None and row.number is None and re.search(re.escape(row.name) + r"\s*(?:建筑)?平面图$", name))]
    if len(bases) != 1 or bases[0].elevation_arch is None:
        return None
    base = bases[0]
    higher = [row for row in schedule if row.elevation_arch is not None and row.elevation_arch > base.elevation_arch]
    if not higher:
        return None
    nearest = min(row.elevation_arch for row in higher)
    options = [row for row in higher if abs(row.elevation_arch - nearest) <= 0.001]
    if len(options) != 1:
        return None
    top = options[0]
    if base.height is not None:
        if abs(nearest - base.elevation_arch - base.height) > 0.001:
            return None
    elif number is None or top.number != (1 if number == -1 else number + 1):
        return None

    result = []
    for row in (base, top):
        level = row.to_level()
        level["evidence"] = [f"level_schedule:{handle}" for handle in row.handles]
        result.append(level)
    return result


def run_building(input_path: Path) -> dict:
    """Use the unique standard floor as the plan and datum reference."""
    from tumo_engine.recognize.building import merge_building_models
    from tumo_engine.recognize.plan_registration import register_plan

    path = Path(input_path).resolve()
    drawing = input_drawing(path)
    root = path if path.is_dir() else path.parent
    if discipline(drawing) == "struct":
        raise ValueError("楼层合并需要建筑平面图。")
    doc = read_dxf(drawing)
    rules = defaults()
    factor = unit_factor(doc)
    plans = [sheet for sheet in drawing_sheets(doc, drawing, factor, rules["layers"])
             if sheet.number and sheet.bounds and sheet_kind(sheet.title) in {"floor_plan", "special_plan"}]
    if len(plans) < 2 or len({sheet.number for sheet in plans}) != len(plans):
        raise ValueError("合并楼层需要同一 DXF 有两张以上独立图框和唯一图号。")
    anchors = [sheet for sheet in plans if standard_floor_span(sheet.title)[0] is not None]
    if len(anchors) != 1:
        raise ValueError("合并楼层需要唯一的标准层图框和明确楼层范围。")

    seed = anchors[0]
    xref_warnings = bind_xrefs(doc, drawing, root)
    all_items, preprocessing_warnings = primitives(doc, factor)
    spatial = SpatialIndex(all_items, lambda item: item.shape)
    read_companion = cache(_read_opening_companion)
    read_elevations = cache(read_elevation_drawing)

    def recognize(sheet, items, **options):
        return _recognize_plan(path, drawing, root, doc, rules, factor, sheet, bounds=sheet.bounds,
                               xref_warnings=xref_warnings, selected_items=(items, preprocessing_warnings),
                               read_companion=read_companion, read_elevations=read_elevations, **options)

    seed_items = spatial.nearby(box(*seed.bounds), 0)
    schedule, _ = _level_schedule(seed_items, "arch", rules)
    anchor = recognize(seed, seed_items, repeat_standard_floors=True)
    aligned = []
    notices = []
    for sheet in plans:
        if sheet is seed:
            continue
        caption = f"{sheet.number} {sheet.title}"
        levels = _verified_level_pair(sheet.title, schedule)
        if levels is None:
            notices.append(f"{caption}：未找到唯一的本层与顶部建筑标高，保留单图核对。")
            continue

        items = spatial.nearby(box(*sheet.bounds), 0)
        local_rows, local_warnings = _level_schedule(items, "arch", rules)
        local = {row.id: row for row in local_rows}
        if local_warnings or any(level["id"] in local and (
                local[level["id"]].elevation_arch is None
                or abs(local[level["id"]].elevation_arch - level["elevation_arch"]) > 0.001) for level in levels):
            notices.append(f"{caption}：本图楼层表与项目楼层表不一致，保留单图核对。")
            continue
        plan = recognize(sheet, items, verified_levels=levels)
        transform, note = register_plan(anchor["grids"], plan["grids"])
        if transform is None:
            notices.append(f"{caption}：{note}")
            continue
        aligned.append((plan, transform))

    model, merge_warnings = merge_building_models(anchor, aligned)
    for message in [*notices, *merge_warnings]:
        model["issues"].append({"id": _stable_id("I", seed.number, message), "severity": "warn",
                                "message": message, "where": {"sheet": seed.number}})
    return model
