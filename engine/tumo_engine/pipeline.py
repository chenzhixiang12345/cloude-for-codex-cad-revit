"""Identify one architectural or structural plan and produce a TuMo model."""

from collections import Counter, defaultdict
from hashlib import blake2s
import json
import math
from pathlib import Path
import re
from unicodedata import normalize

from shapely.geometry import LineString, MultiPoint

from tumo_engine.io.dxf_reader import read_dxf
from tumo_engine.preprocess import Bounds, bind_xrefs, primitives, unit_factor
from tumo_engine.recognize.column import recognize_columns
from tumo_engine.recognize.elevation import read_elevation_drawing
from tumo_engine.recognize.geometry import rounded
from tumo_engine.recognize.grid import recognize_grids
from tumo_engine.recognize.level import floor_from_title, recognize_levels
from tumo_engine.recognize.opening import recognize_openings
from tumo_engine.recognize.ramp import identify_ramps
from tumo_engine.recognize.room import identify_rooms
from tumo_engine.recognize.slab import recognize_slabs
from tumo_engine.recognize.standard_floors import expand_standard_floors, standard_floor_span
from tumo_engine.recognize.wall import (
    apply_wall_end_extensions, pending_wall_end_extensions, recognize_walls, unbuilt_structural_wall_areas,
)
from tumo_engine.rules import defaults
from tumo_engine.sheets import NUMBER, catalog_title, discipline, drawing_sheets, input_drawing, sheet_kind
from tumo_engine.tables import OpeningScheduleRow, WallOpeningScheduleRow, read_opening_schedule
from tumo_engine.tables.levels import read_level_schedule


_OPENING_TABLE = re.compile(
    r"门窗(?:表|详图|大样)|(?:门|窗)表|墙(?:上)?洞(?:口)?表|(?:door|window|wall[-_ ]*opening)[-_ ]*schedule", re.I,
)
_ELEVATION_DRAWING = re.compile(r"立剖面|立面图|剖面图|(?:^|[-_ ])(?:elevation|section)(?:$|[-_ ])", re.I)
_OPENING_PREFIX = {"door": "D", "window": "WIN", "wall_opening": "WO"}


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


def _read_opening_companion(path, root):
    doc = read_dxf(path)
    warnings = bind_xrefs(doc, path, root)
    items, parsing_warnings = primitives(doc, unit_factor(doc))
    return read_opening_schedule(items), list(dict.fromkeys([*warnings, *parsing_warnings]))


def _opening_schedules(drawing, root, inline_items, issue, read_companion):
    inline_rows = read_opening_schedule(inline_items)
    tables = [(drawing.relative_to(root).as_posix(), inline_rows)]
    for path in sorted(drawing.parent.iterdir()):
        if (path == drawing or path.suffix.lower() != ".dxf" or not path.is_file()
                or not _OPENING_TABLE.search(path.stem) or not path.resolve().is_relative_to(root)):
            continue
        try:
            rows, warnings = read_companion(path, root)
        except ValueError as error:
            issue(str(error))
            continue
        relative = path.relative_to(root).as_posix()
        for warning in warnings:
            issue(f"门窗表 {relative}：{warning}")
        tables.append((relative, rows))

    rows = []
    evidence = defaultdict(list)
    for relative, table in tables:
        for row in table:
            rows.append(row)
            trace = f"schedule_row:{relative}:" + ",".join(row.handles)
            key = (row.mark, isinstance(row, WallOpeningScheduleRow))
            if trace not in evidence[key]:
                evidence[key].append(trace)
    inline_handles = {handle for row in inline_rows for handle in row.handles}
    return rows, evidence, inline_handles


def _restore_opening_hosts(model, candidates, hosts, issue):
    edited_hosts = Counter(wall for candidate in candidates for wall in candidate.get("wall_pair", ()))
    edited_hosts.update(candidate["wall_extension"]["host"] for candidate in candidates if "wall_extension" in candidate)
    remapped = {}
    rebuilt = []
    accepted = []
    for candidate in candidates:
        pair = candidate.pop("wall_pair", None)
        extension = candidate.pop("wall_extension", None)
        if pair:
            first, second = (hosts[identifier] for identifier in pair)
            if (any(edited_hosts[identifier] > 1 for identifier in pair)
                    or first["type"].get("material") != second["type"].get("material")):
                issue("两侧墙无法唯一合并为洞口宿主。请对照墙端和材料。", at=candidate["geometry"]["at"])
                continue
            start, end = first["geometry"]["start"], first["geometry"]["end"]
            dx, dy = end[0] - start[0], end[1] - start[1]
            length = math.hypot(dx, dy)
            ux, uy = dx / length, dy / length
            distances = [0, length, *((x - start[0]) * ux + (y - start[1]) * uy
                                      for x, y in (second["geometry"]["start"], second["geometry"]["end"]))]
            a, b = sorted(rounded((start[0] + distance * ux, start[1] + distance * uy))
                          for distance in (min(distances), max(distances)))
            geometry = {"kind": "line", "start": a, "end": b}
            sheet = first["source"]["sheet"]
            identifier = _stable_id("W", sheet, geometry)
            if identifier in hosts:
                issue("合并后的宿主墙与现有墙重复。请对照原图。", at=candidate["geometry"]["at"])
                continue
            evidence = sorted(set(first["evidence"] + second["evidence"] + ["opening:verified_wall_gap"]))
            handles = sorted(set(first["source"]["handles"] + second["source"]["handles"]))
            wall = {**first, "geometry": geometry, "id": identifier, "evidence": evidence,
                    "confidence": min(first["confidence"], second["confidence"]),
                    "source": {"sheet": sheet, "handles": handles}}
            for old in pair:
                remapped[old] = identifier
            rebuilt.append(wall)
            hosts[identifier] = wall
            candidate["host"] = identifier
        if extension:
            identifier = extension["host"]
            if edited_hosts[identifier] > 1:
                issue("多个洞口需要调整同一宿主墙。请对照原图。", at=candidate["geometry"]["at"])
                continue
            wall = hosts[identifier]
            wall["geometry"] = extension["geometry"]
            wall["evidence"] = sorted(set(wall["evidence"] + ["opening:verified_corner_host"]))
            wall["source"]["handles"] = sorted(set(wall["source"]["handles"] + extension["handles"]))
        accepted.append(candidate)

    if remapped:
        model["elements"] = [element for element in model["elements"] if element["id"] not in remapped] + rebuilt
        relations = []
        for relation in model["relations"]:
            updated = {**relation, **{key: remapped.get(relation[key], relation[key]) for key in ("a", "b")}}
            if updated not in relations:
                relations.append(updated)
        model["relations"] = relations
        for entry in model["issues"]:
            if (identifier := entry.get("element")) in remapped:
                entry["element"] = remapped[identifier]
    for candidate in accepted:
        candidate["host"] = remapped.get(candidate["host"], candidate["host"])
    return accepted


def _detail_trace(annotation):
    source = annotation["source"]
    return f"opening_detail:{source['file']}:" + ",".join(source["handles"])


def _detail_schedule(schedule, details):
    table_sizes = defaultdict(set)
    for row in schedule:
        if not isinstance(row, WallOpeningScheduleRow):
            table_sizes[row.mark].add((row.width, row.height))
    fallback = {}
    conflicts = set()
    for mark, entries in details.items():
        sizes = {(entry["category"], entry["width_mm"], entry["height_mm"]) for entry in entries}
        if len(sizes) != 1:
            conflicts.add(mark)
            continue
        (category, width, height), = sizes
        if table_sizes[mark]:
            if table_sizes[mark] != {(width, height)}:
                conflicts.add(mark)
        elif category == "door":
            handles = tuple(sorted({handle for entry in entries for handle in entry["source"]["handles"]}))
            fallback[mark] = OpeningScheduleRow(mark, width, height, None, handles)
    return fallback, conflicts


def _opening_host_ids(candidate):
    return set(candidate["wall_pair"]) if "wall_pair" in candidate else {candidate["host"]}


def _detail_openings(candidates, from_tables, issue):
    table_openings = defaultdict(list)
    table_edits = set()
    for candidate in from_tables:
        references = _opening_host_ids(candidate)
        for identifier in references:
            table_openings[identifier].append(candidate)
        if "wall_pair" in candidate or "wall_extension" in candidate:
            table_edits.update(references)
    for candidate in candidates:
        at = candidate["geometry"]["at"]
        mark = candidate["type"]["mark"]
        if candidate["category"] != "door" or "door:swing_and_jambs" not in candidate["evidence"]:
            issue(f"{mark} 已量得图型尺寸。还需核对门框外边和宿主墙。", at=at)
            continue
        references = _opening_host_ids(candidate)
        edits_wall = "wall_pair" in candidate or "wall_extension" in candidate
        same_host = [entry for identifier in references for entry in table_openings[identifier]]
        overlaps = any(math.dist(at, entry["geometry"]["at"]) <
                       (candidate["type"]["width"] + entry["type"]["width"]) / 2 for entry in same_host)
        if references & table_edits or (edits_wall and same_host) or overlaps:
            issue(f"{mark} 的候选门框与已核对门窗共用宿主或相交。请核对原图。", at=at)
            continue
        candidate["evidence"][0] = "detail:exact_mark"
        yield candidate


def _add_openings(model, items, sheet_items, drawing, root, rules, issue, read_companion, details):
    schedule, evidence, inline_handles = _opening_schedules(drawing, root, sheet_items, issue, read_companion)
    fallback, conflicts = _detail_schedule(schedule, details)
    plan_items = [
        item for item in items
        if item.kind != "text" or not item.handles or not set(item.handles) <= inline_handles
    ]
    for item in plan_items:
        if item.kind == "text" and (mark := normalize("NFKC", item.text).strip().upper()) in conflicts:
            trace = "、".join([*evidence[(mark, False)], *(_detail_trace(entry) for entry in details[mark])])
            issue(f"{mark} 的门窗表和图型尺寸冲突，或图型尺寸有冲突。请核对 CAD 来源 {trace}。",
                  at=rounded(item.points[0]))
    schedule = [row for row in schedule if isinstance(row, WallOpeningScheduleRow) or row.mark not in conflicts]
    if not (schedule or fallback):
        return
    hosts = {wall["id"]: wall for wall in model["elements"] if wall["category"] == "wall"}
    opening_rules = rules["opening"]
    def match(rows):
        candidates, issues = recognize_openings(
            plan_items, hosts.values(), rows, wall_pair_rules=opening_rules["wall_pair"],
            window_number_rules=opening_rules["window_number"], door_frame_rules=opening_rules["door_frame"],
            corner_rules=rules,
        )
        for entry in issues:
            issue(entry["message"], at=entry["at"])
        return candidates

    from_tables = match(schedule)
    from_details = list(_detail_openings(match(fallback.values()), from_tables, issue))

    sheet = model["sheets"][0]
    for candidate in _restore_opening_hosts(model, [*from_tables, *from_details], hosts, issue):
        at = candidate["geometry"]["at"]
        host = hosts[candidate["host"]]
        mark = candidate["type"]["mark"]
        category = candidate["category"]
        candidate.update({
            "id": _stable_id(_OPENING_PREFIX[category], sheet["id"], host["id"], at),
            "discipline": sheet["discipline"],
            "base_level": host["base_level"],
            "source": {"sheet": sheet["id"], "handles": candidate.pop("handles")},
        })
        candidate["evidence"].extend(evidence[(mark, category == "wall_opening")])
        candidate["evidence"].extend(dict.fromkeys(
            _detail_trace(entry) for entry in details.get(mark, ()) if entry["category"] == category))
        model["elements"].append(candidate)
        if category != "window":
            issue(f"请核对 {mark} 的尺寸和宿主墙。", candidate["id"], at)


def _opening_annotations(drawing, root, issue, read_elevations):
    details = defaultdict(list)
    sills = defaultdict(list)
    conflicts = set()
    for path in sorted(drawing.parent.iterdir()):
        if (path == drawing or path.suffix.lower() != ".dxf" or not path.is_file()
                or not _ELEVATION_DRAWING.search(path.stem) or not path.resolve().is_relative_to(root)):
            continue
        try:
            annotations, warnings = read_elevations(path, root)
        except ValueError as error:
            issue(str(error))
            continue
        for warning in warnings:
            issue(f"{path.name}：{warning['message']}")
            if warning["code"] == "conflicting_sill":
                conflicts.add((warning["level"], warning["mark"]))
        for annotation in annotations:
            if annotation["kind"] == "sill_height":
                sills[(annotation["level"], annotation["mark"])].append(annotation)
            elif annotation["kind"] == "opening_detail":
                details[annotation["mark"]].append(annotation)
    return details, sills, conflicts


def _apply_window_sills(model, details, sills, conflicts, issue):
    windows = defaultdict(list)
    for element in model["elements"]:
        if element["category"] == "window":
            windows[(element["base_level"], element["type"]["mark"])].append(element)

    for key, elements in windows.items():
        _, mark = key
        notes = sills.get(key, ())
        local_values = {note["value_mm"] for note in notes}
        for window in elements:
            size = window["type"]
            type_notes = [entry for entry in details.get(mark, ())
                          if entry["category"] == "window" and entry["width_mm"] == size["width"]
                          and entry["height_mm"] == size["height"] and entry.get("sill_height_mm") is not None]
            type_values = {entry["sill_height_mm"] for entry in type_notes if not entry["source"].get("hidden_sill_handles")}
            values = local_values | type_values
            matched = window.get("sill_height")
            at = window["geometry"]["at"]
            if key in conflicts or len(values) > 1 or (matched is not None and values and matched not in values):
                issue(f"{mark} 的窗台高度有冲突。请核对门窗表和立剖面。", window["id"], at)
            elif type_values or (notes and len(elements) == 1):
                window["sill_height"], = values
                if len(elements) == 1:
                    for entry in notes:
                        source = entry["source"]
                        window["evidence"].append(f"sill_height:{source['file']}:" + ",".join(source["handles"]))
            elif notes:
                issue(f"{mark} 同层有多个窗位。窗台标注不能唯一定位。", window["id"], at)
            hidden = sorted({entry["sill_height_mm"] for entry in type_notes if entry["source"].get("hidden_sill_handles")})
            if hidden and ("sill_height" not in window or window["sill_height"] not in hidden):
                candidates = "、".join(f"{value:g}" for value in hidden)
                issue(f"{mark} 图型的隐藏标注提示窗台高 {candidates} 毫米。请核对可见图纸。", window["id"], at)
            if "sill_height" not in window:
                issue(f"{mark} 缺少窗台高度。请对照立面或剖面。", window["id"], at)
            else:
                issue(f"请核对 {mark} 的尺寸和宿主墙。", window["id"], at)


def _add_rooms(model, items, rules, issue):
    sheet = model["sheets"][0]
    walls = [element for element in model["elements"] if element["category"] == "wall"]
    openings = [element for element in model["elements"] if element["category"] in _OPENING_PREFIX]
    base, top = model["levels"][:2]
    rooms, warnings = identify_rooms(items, walls, openings, sheet_id=sheet["id"],
                                     base_level=base["id"], top_level=top["id"], room_rules=rules["room"])
    model["elements"].extend(rooms)
    for entry in warnings:
        issue(entry["message"], at=entry["at"])
    for room in rooms:
        if room["confidence"] < rules["confidence"]["review"]:
            issue("请在 Revit 中复核房名和围合边界。", room["id"], room["params"]["label_point"])


def _add_slabs(model, items, rules, issue):
    sheet = model["sheets"][0]
    slabs, openings, warnings = recognize_slabs(items, rules)
    for message, at in warnings:
        issue(message, at=at)

    def publish(candidate, identifier):
        candidate.update({
            "id": identifier,
            "discipline": sheet["discipline"],
            "base_level": model["levels"][0]["id"],
            "source": {"sheet": sheet["id"], "handles": candidate.pop("handles")},
        })
        model["elements"].append(candidate)

    for slab in slabs:
        publish(slab, _stable_id("SL", sheet["id"], slab["geometry"], slab["base_offset"]))
    for opening, host in openings:
        opening["host"] = host["id"]
        publish(opening, _stable_id("SO", host["id"], opening["geometry"]))


def _level_schedule(items, discipline, rules):
    return read_level_schedule(items, discipline=discipline, layer_pattern=rules["layers"]["level_table"],
                               row_tolerance=rules["level"]["row_tolerance"])


def _plan_levels(items, title, discipline, rules):
    config = rules["level"]
    rows, warnings = _level_schedule(items, discipline, rules)
    if not rows:
        levels, legacy_warnings = recognize_levels(items, title, discipline, rules)
        return levels, rows, warnings + legacy_warnings

    numbered = {row.number: row for row in rows if row.number is not None}
    span, _ = standard_floor_span(title)
    floor = span[0] if span else floor_from_title(title)
    if floor is None:
        floor = min((number for number in numbered if number > 0), default=1)
    top_floor = 1 if floor == -1 else floor + 1
    base_row, top_row = numbered.get(floor), numbered.get(top_floor)
    own = "elevation_struct" if discipline == "struct" else "elevation_arch"
    other = "elevation_arch" if discipline == "struct" else "elevation_struct"

    def elevation(level):
        return level.get(own, level.get(other))

    def level(row, number, source, values):
        if row is not None and elevation(data := row.to_level()) is not None:
            data["evidence"] = ["level_schedule:" + ",".join(row.handles)]
            return data
        identifier = f"LB{-number}" if number < 0 else f"L{number}"
        name = f"B{-number}F" if number < 0 else f"{number}F"
        return {"id": identifier, "name": name, "source": source, "confidence": 0.5, **values}

    base = level(base_row, floor, "默认楼层", {own: (floor - 1) * config["default_height"]})
    if base["source"] == "默认楼层":
        warnings.append("楼层表缺少当前层的明确标高，暂用默认楼层标高。")
    height = base_row.height if base_row and base["source"] == "楼层表" else None
    top = level(top_row, top_floor, "楼层表层高" if height is not None else "默认层高", {
        key: base[key] + (height if height is not None else config["default_height"])
        for key in (own, other) if key in base
    })
    if top["source"] == "默认层高":
        warnings.append(f"楼层表缺少上层标高，暂用 {config['default_height']} mm 层高。")
    if elevation(top) <= elevation(base):
        warnings.append("楼层表的上层标高不高于当前层。请核对后再生成该层。")
    elif height is not None and top["source"] == "楼层表" and abs(elevation(top) - elevation(base) - height) > 1:
        warnings.append("相邻楼层表的标高差与所列层高不一致。按明确标高保留这两层，待复核。")
    return [base, top], rows, warnings


def run(input_path: Path, bounds: Bounds | None = None, *, repeat_standard_floors: bool = False) -> dict:
    path = Path(input_path).resolve()
    drawing = input_drawing(path)
    root = path if path.is_dir() else path.parent
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
    return _recognize_plan(path, drawing, root, doc, rules, factor, sheet_info,
                           bounds=tuple(bounds) if bounds is not None else sheet_info.bounds,
                           xref_warnings=bind_xrefs(doc, drawing, root), repeat_standard_floors=repeat_standard_floors)


def _recognize_plan(path, drawing, root, doc, rules, factor, sheet_info, *, bounds, xref_warnings,
                    repeat_standard_floors=False, verified_levels=None, selected_items=None,
                    read_companion=_read_opening_companion, read_elevations=read_elevation_drawing):
    relative = drawing.relative_to(root).as_posix()
    subject = discipline(drawing)
    if subject == "unknown" and sheet_kind(sheet_info.title) == "floor_plan":
        subject = "arch"
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
    if bounds is not None:
        sheet["bounds"] = list(bounds)

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

    for warning in xref_warnings:
        issue(warning)
    items, warnings = primitives(doc, factor, bounds) if selected_items is None else selected_items
    for message in warnings:
        issue(message)
    level_items = items
    if sheet_info.bounds is not None and sheet_info.bounds != bounds:
        level_items, _ = primitives(doc, factor, sheet_info.bounds)
    levels, level_rows, warnings = _plan_levels(level_items, sheet_info.title, subject, rules) if verified_levels is None else (verified_levels, [], [])
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
    if subject == "arch":
        details, sills, sill_conflicts = _opening_annotations(drawing, root, issue, read_elevations)
        wall_ends = pending_wall_end_extensions(items, model["elements"], model["relations"], rules)
        _add_openings(model, items, level_items, drawing, root, rules, issue, read_companion, details)
        apply_wall_end_extensions(model["elements"], columns, wall_ends)
        _add_rooms(model, items, rules, issue)
        _add_slabs(model, items, rules, issue)
        ramps, ramp_warnings = identify_ramps(items, sheet_id=sheet_id, base_level=levels[0]["id"],
                                               top_level=levels[1]["id"], rules=rules)
        model["elements"].extend(ramps)
        for entry in ramp_warnings:
            issue(entry["message"], at=entry["at"])
    if repeat_standard_floors:
        expanded, repeat_warnings = expand_standard_floors(model, sheet_info.title, level_rows)
        if expanded is model:
            message = "\n".join(repeat_warnings) or "图名缺少明确的标准层范围。请选择含楼层范围和楼层表的平面图。"
            raise ValueError(message)
        model = expanded
        for message in repeat_warnings:
            issue(message)
    if subject == "arch":
        _apply_window_sills(model, details, sills, sill_conflicts, issue)
    for category, count in (("轴网", len(model["grids"])), ("柱", len(columns)), ("墙", len(walls))):
        if count == 0 and (category != "墙" or subject != "struct"):
            issue(f"当前图纸范围内没有识别到{category}。")
    return model
