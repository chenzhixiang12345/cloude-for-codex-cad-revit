"""Publish a verified planar beam only when its structural top is documented."""

from collections import defaultdict
from math import hypot, isfinite
import re
from unicodedata import normalize

from shapely.geometry import LineString, Point

from tumo_engine.index import SpatialIndex
from tumo_engine.preprocess import layer_key
from tumo_engine.recognize.geometry import dot, parallel, rounded
from tumo_engine.recognize.standard_floors import slab_top_floor_span
from tumo_engine.rules import defaults
from tumo_engine.tables.beams import _text_axes, read_beam_candidates
from tumo_engine.tables.levels import read_level_schedule


_PLAN_ISSUES = {
    "beam_span_unsupported": "跨数、悬挑或附注超出当前单跨规则。",
    "beam_leader_unconfirmed": "缺少唯一的集中标注引线。",
    "beam_edges_unconfirmed": "缺少唯一吻合的双虚线梁边。",
    "beam_section_unconfirmed": "梁跨内缺少唯一的同向同宽截面。",
    "beam_supports_unconfirmed": "两端支座证据不唯一。",
    "beam_mark_conflict": "同一梁跨关联多个梁号。",
}


def _text(item):
    return normalize("NFKC", item.text).replace("−", "-").strip()


def _note_sections(notes, offsets, axes, rules):
    section_index = SpatialIndex(notes, lambda item: item.shape)
    assigned = defaultdict(list)
    ambiguous = defaultdict(set)
    alongside = rules["section_alignment"]
    minimum, maximum = rules["next_row_distance"]
    angle = rules["angle_deg"]
    for note, offset in offsets:
        heading = axes.get((note.handles, note.points[0], note.text))
        if heading is None:
            continue
        possible = []
        for section in section_index.nearby(note.shape, hypot(alongside, maximum)):
            section_axis = axes.get((section.handles, section.points[0], section.text))
            if (section.handles[:-1] != note.handles[:-1] or section_axis is None
                    or not parallel(section_axis, heading, angle)):
                continue
            delta = (note.points[0][0] - section.points[0][0], note.points[0][1] - section.points[0][1])
            down = (section_axis[1], -section_axis[0])
            if abs(dot(delta, section_axis)) <= alongside and minimum <= dot(delta, down) <= maximum:
                possible.append(section)
        if len(possible) == 1:
            assigned[possible[0].handles].append((note, offset))
        else:
            for section in possible:
                ambiguous[section.handles].update(note.handles)
    return assigned, ambiguous


def beam_top_floor_span(title, rules):
    beam_title = normalize("NFKC", title).strip()
    slab_title, changed = re.subn(rules["structural_beam"]["top_title_token"], "顶板", beam_title)
    return slab_top_floor_span(slab_title) if changed else None


def recognize_structural_beams(doc, items, *, sheet_id, title, floor=None, rules=None):
    """Return schema 0.1 beams and located issues from an already-bound plan's local items."""
    from tumo_engine.pipeline import _stable_id

    rules = defaults() if rules is None else rules
    cfg = rules["structural_beam"]
    items = list(items)
    candidates, plan_issues = read_beam_candidates(doc, items, rules)
    result, issues = [], []

    def warn(mark, message, at, handles):
        provenance = "、".join(sorted(set(handles)))
        message = f"{mark}：{message} CAD 句柄 {provenance}。"
        issues.append({
            "id": _stable_id("I", sheet_id, message, at), "severity": "warn", "message": message,
            "where": {"sheet": sheet_id, "at": list(at)},
        })

    for issue in plan_issues:
        warn(issue.mark, _PLAN_ISSUES[issue.code], issue.at, issue.handles)
    if not candidates:
        return result, issues

    span = beam_top_floor_span(title, rules)
    target_floor = floor if floor is not None else (span[0] if span else None)
    rows, _ = read_level_schedule(items, discipline="struct", layer_pattern=rules["layers"]["level_table"],
                                  row_tolerance=rules["level"]["row_tolerance"])
    level = next((row for row in rows if row.number == target_floor), None) if target_floor is not None else None
    datum, conflicts, sections, offsets, marks = [], [], [], [], {}
    dimension_layer = re.compile(rules["beam_candidate"]["layers"]["section"])
    dimension_pattern = re.compile(rules["beam_candidate"]["section_pattern"])
    datum_layer = re.compile(cfg["reference_layer"])
    reference_pattern = re.compile(cfg["reference_pattern"])
    conflict_pattern = re.compile(cfg["conflicting_reference_pattern"])
    offset_pattern = re.compile(cfg["offset_pattern"])
    for item in items:
        if item.kind != "text" or not item.points or not item.handles:
            continue
        marks[item.handles] = item.points[0]
        key, text = layer_key(item.layer), _text(item)
        if datum_layer.fullmatch(key):
            if reference_pattern.search(text):
                datum.append(item)
            if conflict_pattern.search(text):
                conflicts.append(item)
        if dimension_layer.fullmatch(key):
            if dimension_pattern.fullmatch(text.upper()):
                sections.append(item)
            elif match := offset_pattern.fullmatch(text):
                offsets.append((item, round(float(match["value"]) * 1000, 3)))
    axes = _text_axes(doc, sections + [item for item, _ in offsets])
    assigned, ambiguous = _note_sections(sections, offsets, axes, cfg)
    for candidate in candidates:
        at = rounded(marks[candidate.mark_handles])
        handles = {*candidate.mark_handles, *candidate.leader_handles, *candidate.section_handles,
                   *(handle for chain in (*candidate.edge_handles, *candidate.support_handles) for handle in chain)}
        if not span or not span[0] <= target_floor <= span[1]:
            floor_name = f"{target_floor}F" if target_floor is not None else "指定楼层"
            warn(candidate.mark, f"{floor_name} 无法对应当前顶梁图明确的上层标高。", at, handles)
            continue
        if not level or level.elevation_struct is None or not isfinite(level.elevation_struct):
            warn(candidate.mark, f"楼层表缺少 {target_floor}F 的唯一结构标高。", at, handles)
            continue
        handles.update(level.handles)
        for item in (*datum, *conflicts):
            handles.update(item.handles)
        if len(datum) != 1 or conflicts:
            warn(candidate.mark, "缺少唯一的当层结构相对标高说明。", at, handles)
            continue
        associated = assigned[candidate.section_handles]
        ambiguous_notes = ambiguous[candidate.section_handles]
        if not associated and not ambiguous_notes:
            warn(candidate.mark, "未注明局部梁顶标高。图注要求按所属板面，需核对板区。", at, handles)
            continue
        for note, _ in associated:
            handles.update(note.handles)
        handles.update(ambiguous_notes)
        if len(associated) != 1 or ambiguous_notes:
            warn(candidate.mark, "同一截面附近有重复或归属不唯一的相对标高。", at, handles)
            continue
        (note, offset), = associated
        line = LineString((candidate.start, candidate.end))
        if not 0 < line.project(Point(note.points[0])) < line.length:
            warn(candidate.mark, "相对标高落点无法投影到该单跨梁。", at, handles)
            continue
        geometry = {"kind": "line", "start": list(candidate.start), "end": list(candidate.end)}
        result.append({
            "id": _stable_id("B", sheet_id, level.id, geometry), "category": "beam", "discipline": "struct",
            "type": {"mark": candidate.mark, "width": candidate.width, "height": candidate.height},
            "geometry": geometry, "base_level": level.id, "base_offset": offset, "confidence": 0.93,
            "source": {"sheet": sheet_id, "handles": sorted(handles)},
            "evidence": ["beam:matched_section_and_two_supports", "beam_top:explicit_structural_offset"],
        })
    return result, issues
