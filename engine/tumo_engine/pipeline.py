"""Identify one architectural or structural plan and produce a TuMo model."""

from collections import Counter
from hashlib import blake2s
import json
import math
from pathlib import Path
import re

from shapely.geometry import LineString, MultiPoint

from tumo_engine.io.dxf_reader import read_dxf
from tumo_engine.preprocess import Bounds, bind_xrefs, primitives, unit_factor
from tumo_engine.recognize.column import recognize_columns
from tumo_engine.recognize.geometry import rounded
from tumo_engine.recognize.grid import recognize_grids
from tumo_engine.recognize.level import recognize_levels
from tumo_engine.recognize.wall import recognize_walls, unbuilt_structural_wall_areas
from tumo_engine.rules import defaults
from tumo_engine.sheets import NUMBER, catalog_title, discipline, drawing_sheets, input_drawing, sheet_kind


def _stable_id(prefix, *values):
    payload = json.dumps(values, ensure_ascii=False, sort_keys=True).encode("utf-8")
    suffix = blake2s(payload, digest_size=5).hexdigest().upper()
    return f"{prefix}-{suffix}"


def _origin(grids):
    """Prefer the smallest numeric and alphabetic axes that cross."""
    x_axes = []
    y_axes = []
    for grid in grids:
        prefix, _, designation = grid["label"].rpartition("-")
        if designation.isdigit():
            x_axes.append((prefix, int(designation), grid))
        elif re.fullmatch(r"[A-Z]{1,2}", designation):
            y_axes.append((prefix, len(designation), designation, grid))
    for x_prefix, _, x in sorted(x_axes, key=lambda row: row[:2]):
        for y_prefix, _, _, y in sorted(y_axes, key=lambda row: row[:3]):
            if x_prefix != y_prefix:
                continue
            intersection = LineString((x["start"], x["end"])).intersection(LineString((y["start"], y["end"])))
            if not intersection.is_empty and intersection.geom_type == "Point":
                return {"grid": [x["label"], y["label"]], "at": rounded(intersection.coords[0])}
    return {}


def run(input_path: Path, bounds: Bounds | None = None) -> dict:
    path = Path(input_path).resolve()
    drawing = input_drawing(path)
    root = path if path.is_dir() else path.parent
    relative = drawing.relative_to(root).as_posix()
    doc = read_dxf(drawing)
    rules = defaults()
    factor = unit_factor(doc)
    detected = drawing_sheets(doc, drawing, factor, rules["layers"])
    if bounds is not None:
        if len(bounds) != 4 or not all(math.isfinite(value) for value in bounds) or bounds[0] >= bounds[2] or bounds[1] >= bounds[3]:
            raise ValueError("图纸范围必须是递增的四个毫米坐标。")
        cx, cy = (bounds[0] + bounds[2]) / 2, (bounds[1] + bounds[3]) / 2
        sheet_info = next((sheet for sheet in detected if sheet.bounds
                           and sheet.bounds[0] <= cx <= sheet.bounds[2]
                           and sheet.bounds[1] <= cy <= sheet.bounds[3]), detected[0])
    else:
        sheet_info = detected[0]
    if sheet_kind(sheet_info.title) == "unknown":
        number = sheet_info.number
        if number is None and (match := NUMBER.fullmatch(drawing.stem)):
            number = match[1].upper()
        if number and (title := catalog_title(drawing, root, number)):
            sheet_info.number = number
            sheet_info.title = title

    subject = discipline(drawing)
    if subject == "unknown" and sheet_kind(sheet_info.title) == "floor_plan":
        subject = "arch"
    chosen_bounds = tuple(bounds) if bounds is not None else sheet_info.bounds
    sheet_id = sheet_info.number or _stable_id("S", relative, sheet_info.title)
    model = {
        "schema_version": "0.1",
        "project": {"name": path.stem, "units": "mm", "origin": {}},
        "sheets": [{
            "id": sheet_id, "file": relative, "discipline": subject,
            "kind": sheet_kind(sheet_info.title), "title": sheet_info.title,
            "layout": "Model",
            "units_to_mm": factor,
            "transform": {"dx": 0, "dy": 0, "rot_deg": 0},
        }],
        "levels": [], "grids": [], "elements": [], "relations": [], "issues": [],
    }
    sheet = model["sheets"][0]
    if sheet_info.number:
        sheet["number"] = sheet_info.number
    if sheet_info.scale:
        sheet["scale"] = sheet_info.scale
    if chosen_bounds is not None:
        sheet["bounds"] = list(chosen_bounds)

    def issue(message, element=None, at=None):
        entry = {
            "id": _stable_id("I", sheet_id, message, element, at),
            "severity": "warn",
            "message": message,
            "where": {"sheet": sheet_id},
        }
        if element:
            entry["element"] = element
        if at:
            entry["where"]["at"] = at
        if entry not in model["issues"]:
            model["issues"].append(entry)

    for warning in bind_xrefs(doc, drawing, root):
        issue(warning)
    items, warnings = primitives(doc, factor, chosen_bounds)
    for message in warnings:
        issue(message)
    level_items = items
    if sheet_info.bounds is not None and sheet_info.bounds != chosen_bounds:
        level_items, _ = primitives(doc, factor, sheet_info.bounds)
    levels, warnings = recognize_levels(level_items, sheet_info.title, subject, rules)
    model["levels"] = levels
    sheet["levels"] = [level["id"] for level in levels]
    for message in warnings:
        issue(message)

    grids = recognize_grids(items, rules)
    labels = Counter()
    original_labels = {grid["label"] for grid in grids}
    used_labels = set(original_labels)
    for grid in sorted(grids, key=lambda row: (-row["confidence"], row["label"], row["start"], row["end"])):
        label = grid["label"]
        labels[label] += 1
        if labels[label] > 1:
            alias = f"{label}-{labels[label]}"
            while alias in used_labels:
                labels[label] += 1
                alias = f"{label}-{labels[label]}"
            used_labels.add(alias)
            grid["label"] = alias
            issue(f"轴号 {label} 有多段直线。当前段在 Revit 中命名为 {alias}。", at=grid["start"])
        grid["id"] = _stable_id("G", sheet_id, grid["label"])
        grid["source"] = {"sheet": sheet_id, "handles": grid.pop("handles")}
        model["grids"].append(grid)
    model["project"]["origin"] = _origin(model["grids"])

    if len(model["grids"]) >= 2:
        axes = [p for grid in model["grids"] for p in (grid["start"], grid["end"])]
        footprint = MultiPoint(axes).convex_hull
        if footprint.area > 0:
            scope = footprint.buffer(rules["grid"]["plan_margin"])
            items = [item for item in items if scope.intersects(item.shape)]

    columns = recognize_columns(items, rules)
    walls = recognize_walls(items, columns, rules) if subject != "struct" else []
    if subject != "struct":
        for handle, at in unbuilt_structural_wall_areas(items, walls, columns, rules):
            source = f"（CAD 句柄 {handle}）" if handle else ""
            issue(f"结构墙轮廓仍有未建区域{source}。请对照底图复核。", at=at)
    ordered = sorted(columns, key=lambda item: item[0]["geometry"]["at"])
    ordered += sorted(walls, key=lambda item: (item[0]["geometry"]["start"], item[0]["geometry"]["end"]))
    for element, joined in ordered:
        category = element["category"]
        geom = element["geometry"]
        code = "C" if category == "column" else "W"
        if element["confidence"] < rules["confidence"]["publish"]:
            issue(f"{category} 候选证据不足。", at=geom.get("at", geom.get("start")))
            continue
        element.update({
            "id": _stable_id(code, sheet_id, geom),
            "discipline": subject,
            "base_level": levels[0]["id"], "top_level": levels[1]["id"],
            "source": {"sheet": sheet_id, "handles": element.pop("handles")},
        })
        model["elements"].append(element)
        if element["confidence"] < rules["confidence"]["review"]:
            label = "柱" if category == "column" else "墙"
            issue(f"请复核{label}的形状和尺寸。", element["id"], geom.get("at", geom.get("start")))
        if category == "wall":
            model["relations"].extend(
                {"kind": "join", "a": element["id"], "b": identifier, "priority": "column_cuts_wall"}
                for identifier in sorted({column["id"] for column in joined if "id" in column})
            )
    for category, count in (("轴网", len(model["grids"])), ("柱", len(columns)), ("墙", len(walls))):
        if count == 0 and (category != "墙" or subject != "struct"):
            issue(f"当前图纸范围内没有识别到{category}。")
    return model
