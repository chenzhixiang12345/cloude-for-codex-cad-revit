from copy import deepcopy

import pytest

from tumo_engine.export.model import TumoModel
from tumo_engine.recognize.standard_floors import expand_standard_floors
from tumo_engine.tables.levels import LevelScheduleRow


TITLE = "1-1#住宅楼 三-五层平面图"


def scheduled_floor(number, elevation, *, height=3000, identifier=None, name=None):
    identifier = identifier or f"L{number}"
    return LevelScheduleRow(identifier, number, name or f"{number}F",
                            elevation, None, height, (f"{identifier}-table",))


def schedule():
    return [scheduled_floor(n, 7800 + 3000 * (n - 3)) for n in range(3, 7)]


def source_element(identifier, category, geometry, *, top=None, host=None):
    element = {
        "id": identifier, "category": category, "discipline": "arch",
        "geometry": geometry, "type": {}, "base_level": "L3",
        "source": {"sheet": "A101", "handles": [identifier + "-cad"]},
        "evidence": ["original"], "confidence": 0.9,
    }
    if top is not None:
        element["top_level"] = top
    if host:
        element["host"] = host
    return element


def fixture(title=TITLE):
    point = {"kind": "point", "at": [1500, 1500]}
    line = {"kind": "line", "start": [0, 0], "end": [5000, 0]}
    polygon = {"kind": "polygon", "points": [[0, 0], [5000, 0], [5000, 3000], [0, 3000]]}
    elements = [
        source_element("W-01", "wall", line, top="L4"),
        source_element("C-01", "column", point, top="L4"),
        source_element("D-01", "door", point, host="W-01"),
        source_element("SL-01", "slab", polygon),
        source_element("SO-01", "slab_opening", polygon, host="SL-01"),
        source_element("R-01", "room", polygon, top="L4"),
        source_element("RP-01", "ramp", polygon, top="L4"),
    ]
    elements[-1]["params"] = {"low_elevation": 7800, "high_elevation": 8400}
    return {
        "schema_version": "0.1",
        "project": {"units": "mm", "name": "合成样例"},
        "sheets": [{
            "id": "A101", "file": "plan.dxf", "discipline": "arch", "kind": "floor_plan",
            "title": title, "levels": ["L3", "L4"],
        }],
        "levels": [row.to_level() for row in schedule()[:2]],
        "grids": [{"id": "G-01", "label": "2", "confidence": 0.9, "start": [0, 0], "end": [5000, 0]}],
        "elements": elements,
        "relations": [{"kind": "join", "a": "W-01", "b": "C-01", "priority": "column_cuts_wall"}],
        "issues": [],
    }


def on_floor(model, level):
    return {element["category"]: element for element in model["elements"] if element["base_level"] == level}


def test_copies_only_reusable_elements_and_remaps_dependencies():
    model = fixture()
    original = deepcopy(model)
    expanded, warnings = expand_standard_floors(model, TITLE, schedule())

    TumoModel.model_validate(expanded)
    assert model == original
    assert expanded["grids"] == original["grids"]
    assert expanded["elements"][:len(original["elements"])] == original["elements"]
    assert [item["id"] for item in expanded["levels"]] == ["L3", "L4", "L5", "L6"]
    assert expanded["sheets"][0]["levels"] == ["L3", "L4", "L5", "L6"]
    assert sum(element["category"] == "ramp" for element in expanded["elements"]) == 1
    assert len({element["id"] for element in expanded["elements"]}) == len(expanded["elements"])
    for current, upper in (("L4", "L5"), ("L5", "L6")):
        copied = on_floor(expanded, current)
        assert set(copied) == {"wall", "column", "door", "slab", "slab_opening", "room"}
        assert [copied[kind]["top_level"] for kind in ("wall", "column", "room")] == [upper] * 3
        assert "top_level" not in copied["door"]
        assert copied["door"]["host"] == copied["wall"]["id"]
        assert copied["slab_opening"]["host"] == copied["slab"]["id"]
        assert copied["wall"]["source"] == original["elements"][0]["source"]
        assert any("standard_floor_reuse" in evidence for evidence in copied["wall"]["evidence"])
        assert {"kind": "join", "a": copied["wall"]["id"], "b": copied["column"]["id"],
                "priority": "column_cuts_wall"} in expanded["relations"]
    assert expand_standard_floors(model, TITLE, list(reversed(schedule()))) == (expanded, warnings)
    assert expand_standard_floors(expanded, TITLE, schedule())[0] == expanded


def test_uses_the_immediately_higher_named_level_when_last_height_matches():
    rows = [*schedule()[:3], scheduled_floor(None, 16800, identifier="LR1F", name="R1F", height=None)]
    expanded, _ = expand_standard_floors(fixture(), TITLE, rows)

    assert expanded["sheets"][0]["levels"][-1] == "LR1F"
    assert on_floor(expanded, "L5")["wall"]["top_level"] == "LR1F"


@pytest.mark.parametrize(
    "rows",
    [
        [schedule()[0], *schedule()[2:]],
        schedule()[:3],
        [*schedule()[:2], scheduled_floor(5, 10800), schedule()[3]],
        [*schedule()[:3], scheduled_floor(None, 16700, identifier="LR1F", name="R1F")],
        [*schedule()[:3], scheduled_floor(None, 16800, identifier="LR1F", name="R1F"),
         scheduled_floor(None, 15800, identifier="L-MECH", name="机房层")],
        [*schedule()[:2], scheduled_floor(5, 13800, height=None),
         scheduled_floor(None, 16800, identifier="LR1F", name="R1F")],
        [*schedule()[:3], scheduled_floor(None, 16800, identifier="LR1F", name="R1F"),
         scheduled_floor(None, 16800, identifier="L-ROOF", name="屋面")],
    ],
)
def test_rejects_the_entire_expansion_when_schedule_evidence_is_incomplete(rows):
    model = fixture()

    expanded, warnings = expand_standard_floors(model, TITLE, rows)

    assert expanded == model
    assert warnings


@pytest.mark.parametrize(
    "title",
    [
        "五-三层平面图", "B1-3F平面图", "-3-5F平面图", "3-5层平面图（不含4层）",
        "3-5层、8-10层平面图", "3-5层和7层平面图",
    ],
)
def test_rejects_invalid_or_ambiguous_floor_ranges(title):
    model = fixture(title)
    expanded, warnings = expand_standard_floors(model, title, schedule())

    assert expanded == model
    assert warnings


def test_does_not_infer_a_range_from_a_single_floor_title():
    model = fixture("1-1#住宅楼 三层平面图")

    assert expand_standard_floors(model, model["sheets"][0]["title"], schedule()) == (model, [])


def test_does_not_copy_other_sheet_elements():
    model = fixture()
    model["sheets"].append({**model["sheets"][0], "id": "A202", "title": "另一个三层平面图"})
    other = deepcopy(model["elements"][0])
    other["id"] = "W-other"
    other["source"]["sheet"] = "A202"
    model["elements"].append(other)

    expanded, _ = expand_standard_floors(model, TITLE, schedule())

    assert expanded["sheets"][1] == model["sheets"][1]
    assert [element for element in expanded["elements"] if element["source"]["sheet"] == "A202"] == [other]
    assert on_floor(expanded, "L4")["wall"]["source"]["sheet"] == "A101"


def test_leaves_multi_floor_host_and_its_opening_at_the_source_level():
    model = fixture()
    model["elements"][0]["top_level"] = "L5"

    expanded, warnings = expand_standard_floors(model, TITLE, schedule())

    assert {element["category"] for element in expanded["elements"] if element["base_level"] == "L4"} == {
        "column", "slab", "slab_opening", "room",
    }
    assert expanded["relations"] == model["relations"]
    assert any("W-01" in warning for warning in warnings)
    assert any("D-01" in warning for warning in warnings)
