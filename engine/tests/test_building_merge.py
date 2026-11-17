from copy import deepcopy

import pytest

from tumo_engine.export.model import TumoModel
from tumo_engine.recognize.building import merge_building_models


TRANSFORM = {"dx": 12500, "dy": -3600, "rot_deg": 90}


def _plan(sheet, base):
    lower = f"L{base}"
    upper = f"L{base + 1}"
    wall = f"W-{sheet}"
    column = f"C-{sheet}"
    axis = f"G-{sheet}-A"
    return {
        "schema_version": "0.1",
        "project": {"units": "mm", "name": "建筑", "origin": {"grid": ["A", "1"], "at": [45, 78]}},
        "sheets": [{
            "id": sheet, "file": "建筑平面.dxf", "kind": "floor_plan", "discipline": "arch",
            "title": f"{base}F 平面图", "bounds": [0, 0, 6000, 6000], "levels": [lower, upper],
            "transform": {"dx": 0, "dy": 0, "rot_deg": 0},
        }],
        "levels": [
            {"id": lower, "name": f"{base}F", "elevation_arch": base * 3000},
            {"id": upper, "name": f"{base + 1}F", "elevation_arch": (base + 1) * 3000},
        ],
        "grids": [
            {"id": axis, "label": "A", "start": [0, 0], "end": [5500, 0], "confidence": .9},
            {"id": f"G-{sheet}-1", "label": "1", "start": [0, 0], "end": [0, 5500], "confidence": .9},
        ],
        "elements": [
            {"id": wall, "category": "wall", "geometry": {"kind": "line", "start": [20, 20], "end": [5500, 20]},
             "type": {}, "base_level": lower, "top_level": upper, "confidence": .9,
             "source": {"sheet": sheet, "handles": ["A"]}},
            {"id": column, "category": "column", "geometry": {"kind": "point", "at": [25, 20]},
             "type": {}, "base_level": lower, "top_level": upper, "confidence": .9,
             "source": {"sheet": sheet, "handles": ["B"]}},
            {"id": f"D-{sheet}", "category": "door", "geometry": {"kind": "point", "at": [800, 20]},
             "type": {}, "base_level": lower, "host": wall, "confidence": .9,
             "source": {"sheet": sheet, "handles": ["C"]}},
            {"id": f"R-{sheet}", "category": "room",
             "geometry": {"kind": "polygon", "points": [[20, 20], [1000, 20], [300, 1400]]},
             "type": {}, "params": {"seed": [100, 80]}, "base_level": lower, "top_level": upper,
             "confidence": .9, "source": {"sheet": sheet, "handles": ["D"]}},
        ],
        "relations": [
            {"kind": "join", "a": wall, "b": column, "priority": "column_cuts_wall"},
            {"kind": "aligned", "a": wall, "b": axis},
        ],
        "issues": [{"id": f"I-{sheet}", "severity": "warn", "element": wall, "message": "请复核墙。",
                    "where": {"sheet": sheet, "at": [20, 20]}}],
    }


def test_merges_independent_local_plans_preserving_anchor_and_audit_coordinates():
    anchor = _plan("A103", 3)
    aligned = _plan("A104", 4)
    aligned["project"]["origin"]["at"] = [1000, 800]
    aligned["levels"][0].update(elevation_struct=11950, evidence=["level_schedule:304"])
    original = deepcopy((anchor, aligned))

    merged, warnings = merge_building_models(anchor, iter([(aligned, TRANSFORM)]))

    TumoModel.model_validate(merged)
    assert not warnings
    assert (anchor, aligned) == original
    assert merged["project"] == anchor["project"]
    assert merged["grids"] == anchor["grids"]
    assert merged["sheets"] == [anchor["sheets"][0], {**aligned["sheets"][0], "transform": TRANSFORM}]
    assert [level["id"] for level in merged["levels"]] == ["L3", "L4", "L5"]
    assert merged["levels"][1]["elevation_struct"] == 11950
    assert merged["levels"][1]["evidence"] == ["level_schedule:304"]
    assert merged["elements"] == anchor["elements"] + aligned["elements"]
    assert merged["issues"] == anchor["issues"] + aligned["issues"]
    assert merged["relations"] == anchor["relations"] + [
        aligned["relations"][0], {"kind": "aligned", "a": "W-A104", "b": "G-A103-A"},
    ]


@pytest.mark.parametrize("base", (2, 3))
def test_different_top_constraint_values_reject_entire_merge(base):
    anchor = _plan("A103", base)
    plan = _plan("A104", base + 1)
    plan["levels"][0]["elevation_arch"] += 50
    earlier = _plan("A101", 1)

    merged, warnings = merge_building_models(anchor, [(earlier, TRANSFORM), (plan, TRANSFORM)])

    assert merged is anchor
    assert any(f"L{base + 1}" in warning and "标高" in warning for warning in warnings)


def test_second_plan_at_same_base_is_not_merged_even_without_recognized_elements():
    anchor = _plan("A103", 3)
    next_plan = _plan("A104", 3)
    next_plan["elements"] = []
    next_plan["relations"] = []
    next_plan["issues"] = []

    merged, warnings = merge_building_models(anchor, [(next_plan, TRANSFORM)])

    assert merged is anchor
    assert any("L3" in warning and "基底" in warning for warning in warnings)


def test_repeat_standard_floors_occupy_every_actual_element_base():
    anchor = _plan("A103", 3)
    added = _plan("A104", 4)
    next_level = added["levels"][1]
    anchor["levels"].append(next_level)
    anchor["sheets"][0]["levels"].append(next_level["id"])
    anchor["elements"].append({
        **anchor["elements"][0], "id": "W-REPEAT-L4", "base_level": "L4", "top_level": "L5",
    })

    merged, warnings = merge_building_models(anchor, [(added, TRANSFORM)])

    assert merged is anchor
    assert any("L4" in warning and "基底" in warning for warning in warnings)


def test_detects_same_cad_identity_and_rejects_atomically():
    anchor = _plan("A103", 3)
    plan = _plan("A104", 4)
    plan["elements"][0]["id"] = anchor["elements"][0]["id"]

    merged, warnings = merge_building_models(anchor, [(plan, TRANSFORM)])

    assert merged is anchor
    assert any("W-A103" in warning and "重复" in warning for warning in warnings)


def test_refuses_a_referenced_local_axis_without_a_unique_anchor_match():
    anchor = _plan("A103", 3)
    plan = _plan("A104", 4)
    plan["grids"][0]["label"] = "B"

    merged, warnings = merge_building_models(anchor, [(plan, TRANSFORM)])

    assert merged is anchor
    assert any("B" in warning and "轴" in warning for warning in warnings)


def test_allows_unreferenced_local_axes_to_be_omitted():
    anchor = _plan("A103", 3)
    plan = _plan("A104", 4)
    plan["grids"][1]["label"] = "2"

    merged, warnings = merge_building_models(anchor, [(plan, TRANSFORM)])

    assert merged["grids"] == anchor["grids"]
    assert not warnings


def _source_axis(identifier, alias, start, end):
    return {"id": identifier, "label": alias, "start": start, "end": end, "confidence": .9,
            "evidence": ["axis:label:A"]}


def test_redirects_axis_references_by_transformed_endpoints_when_aliases_swap():
    anchor = _plan("A103", 3)
    plan = _plan("A104", 4)
    anchor["grids"][0] = _source_axis("G-A103-A", "A", [0, 0], [5500, 0])
    anchor["grids"].append(_source_axis("G-A103-BEND", "A-2", [5500, 0], [6500, 2200]))
    # With TRANSFORM these endpoints become [6500, 2200]–[5500, 0], in reverse order.
    plan["grids"][0] = _source_axis("G-A104-A", "A", [5800, 6000], [3600, 7000])
    plan["grids"].append(_source_axis("G-A104-START", "A-2", [3600, 12500], [3600, 7000]))
    plan["issues"][0].update(element="G-A104-START")

    merged, warnings = merge_building_models(anchor, [(plan, TRANSFORM)])

    TumoModel.model_validate(merged)
    assert not warnings
    assert merged["relations"][-1] == {"kind": "aligned", "a": "W-A104", "b": "G-A103-BEND"}
    assert merged["issues"][-1] == {**plan["issues"][0], "element": "G-A103-A"}
    assert merged["grids"] == anchor["grids"]


def test_rejects_equidistant_segments_with_the_same_original_axis_label():
    anchor = _plan("A103", 3)
    plan = _plan("A104", 4)
    anchor["grids"][0] = _source_axis("G-A103-A", "A", [0, -.5], [5500, -.5])
    anchor["grids"].append(_source_axis("G-A103-A-OTHER", "A-2", [0, .5], [5500, .5]))
    plan["grids"][0] = _source_axis("G-A104-A", "A", [0, 0], [5500, 0])
    plan["grids"].append(_source_axis("G-A104-BEND", "A-2", [0, .7], [5500, .7]))

    merged, warnings = merge_building_models(anchor, [(plan, {"dx": 0, "dy": 0, "rot_deg": 0})])

    assert merged is anchor
    assert any("轴号 A" in warning and "唯一" in warning for warning in warnings)


def test_rejects_conflicting_original_axis_label_instead_of_guessing_from_alias():
    anchor = _plan("A103", 3)
    plan = _plan("A104", 4)
    plan["grids"][0]["evidence"] = ["axis:label:A", "axis:label:B"]

    merged, warnings = merge_building_models(anchor, [(plan, {"dx": 0, "dy": 0, "rot_deg": 0})])

    assert merged is anchor
    assert any("轴号 A" in warning and "唯一" in warning for warning in warnings)


@pytest.mark.parametrize("transform", ({"dx": 0, "dy": 0}, {"dx": float("nan"), "dy": 0, "rot_deg": 0}))
def test_rejects_incomplete_or_nonfinite_alignment(transform):
    anchor = _plan("A103", 3)

    merged, warnings = merge_building_models(anchor, [(_plan("A104", 4), transform)])

    assert merged is anchor
    assert any("变换" in warning for warning in warnings)


def test_rejects_dangling_host_in_the_candidate_graph():
    anchor = _plan("A103", 3)
    next_plan = _plan("A104", 4)
    next_plan["elements"][2]["host"] = "W-missing"

    merged, warnings = merge_building_models(anchor, [(next_plan, TRANSFORM)])

    assert merged is anchor
    assert any("W-missing" in warning and "宿主" in warning for warning in warnings)


def test_rejects_duplicate_sheet_id_even_when_contents_differ():
    anchor = _plan("A103", 3)
    next_plan = _plan("A104", 4)
    next_plan["sheets"][0]["id"] = anchor["sheets"][0]["id"]

    merged, warnings = merge_building_models(anchor, [(next_plan, TRANSFORM)])

    assert merged is anchor
    assert any("A103" in warning and "图纸" in warning for warning in warnings)
