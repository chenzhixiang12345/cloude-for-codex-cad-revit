from dataclasses import replace

import ezdxf
import pytest

from tumo_engine.preprocess import Primitive, primitives
from tumo_engine.recognize.opening import recognize_openings
from tumo_engine.rules import defaults
from tumo_engine.tables import OpeningScheduleRow, read_opening_schedule


def _wall(identifier="W1", start=0, end=4500, y=0):
    return {
        "id": identifier, "category": "wall", "type": {"thickness": 200},
        "geometry": {"kind": "line", "start": [start, y], "end": [end, y]},
        "base_level": "L3", "top_level": "L4", "source": {"sheet": "A103", "handles": ["WB"]},
    }


def _add_text(space, text, x, y):
    return space.add_text(text, dxfattribs={"insert": (x, y)})


def _evidence(*, layer="A-WALL-OPENING", mark_heading="墙洞编号", bottom_heading="洞底相对标高(m)", bottom="H+0.825"):
    doc = ezdxf.new(units=4)
    space = doc.modelspace()
    tag = _add_text(space, "WD01", 1400, 250)
    outline = space.add_line((1000, 40), (1800, 40), dxfattribs={"layer": layer})
    heading = _add_text(space, mark_heading, 8000, 2200)
    _add_text(space, "墙洞尺寸(mm)", 9400, 2200)
    _add_text(space, bottom_heading, 11000, 2200)
    row_mark = _add_text(space, "WD01", 8000, 1900)
    size = _add_text(space, "800×600", 9400, 1900)
    level = _add_text(space, bottom, 11000, 1900)
    items, _ = primitives(doc, 1)
    inline_handles = tuple(entity.dxf.handle for entity in (heading, row_mark, size, level))
    return items, (tag.dxf.handle, outline.dxf.handle), inline_handles


def _match(items, walls, schedule=None):
    rows = read_opening_schedule(items) if schedule is None else schedule
    table_handles = {handle for row in rows for handle in row.handles}
    plan = [item for item in items if not set(item.handles) <= table_handles and item.points[0][0] < 8000]
    return recognize_openings(plan, walls, rows, wall_pair_rules=defaults()["opening"]["wall_pair"])


@pytest.mark.parametrize("layer", ["A-WALL-OPENING", "墙上洞口"])
def test_publishes_a_scheduled_wall_opening_with_relative_bottom_and_source_handles(layer):
    items, plan_handles, table_handles = _evidence(layer=layer)
    [row] = read_opening_schedule(items)
    [opening], issues = _match(items, [_wall()])

    assert row.category == "wall_opening"
    assert row.sill_height == 825
    assert set(row.handles) >= set(table_handles)
    assert opening["category"] == "wall_opening"
    assert opening["host"] == "W1"
    assert opening["geometry"] == {"kind": "point", "at": [1400, 0]}
    assert opening["type"] == {"mark": "WD01", "width": 800, "height": 600}
    assert opening["sill_height"] == 825
    assert set(opening["handles"]) == set(plan_handles)
    assert not issues


@pytest.mark.parametrize(("heading", "value"), [("洞底标高(m)", "0.825"), ("洞底相对标高(m)", "-")])
def test_reports_missing_or_ambiguous_wall_opening_bottom_without_guessing(heading, value):
    items, *_ = _evidence(bottom_heading=heading, bottom=value)
    openings, issues = _match(items, [_wall()])

    assert not openings
    [issue] = issues
    assert issue["code"] == "wall_opening_bottom_missing"
    assert issue["at"] == [1400, 250]


@pytest.mark.parametrize(("heading", "value", "offset"), [
    ("洞底相对标高(mm)", "0", 0),
    ("洞底相对楼层标高(m)", "-0.025", -25),
])
def test_keeps_explicit_zero_and_signed_relative_bottom(heading, value, offset):
    items, *_ = _evidence(bottom_heading=heading, bottom=value)
    [row] = read_opening_schedule(items)

    assert row.sill_height == offset


@pytest.mark.parametrize(("layer", "heading"), [
    ("AE-HOLE", "墙洞编号"),
    ("A-WALL-OPENING", "编号"),
    ("A-WINDOW", "墙洞编号"),
])
def test_requires_both_a_dedicated_plan_layer_and_wall_opening_schedule(layer, heading):
    items, *_ = _evidence(layer=layer, mark_heading=heading)
    openings, _ = _match(items, [_wall()])

    assert not openings


@pytest.mark.parametrize(("walls", "code"), [
    ([_wall(), _wall("W2", y=60)], "host_ambiguous"),
    ([_wall(end=1000), _wall("W2", start=1800)], "host_missing"),
])
def test_does_not_use_competing_or_split_walls_as_the_wall_opening_host(walls, code):
    items, *_ = _evidence()
    openings, issues = _match(items, walls)

    assert not openings
    assert [issue["code"] for issue in issues] == [code]


def test_reports_conflicting_wall_opening_bottoms_in_same_numbered_table_rows():
    items, *_ = _evidence()
    [row] = read_opening_schedule(items)
    openings, issues = _match(items, [_wall()], [row, replace(row, sill_height=925)])

    assert not openings
    assert [issue["code"] for issue in issues] == ["schedule_conflict"]


def test_same_numbered_wall_and_window_schedules_require_unambiguous_plan_evidence():
    items, *_ = _evidence()
    schedule = [*read_opening_schedule(items), OpeningScheduleRow("WD01", 800, 900, 650, ())]
    [opening], issues = _match(items, [_wall()], schedule)

    assert opening["category"] == "wall_opening"
    assert not issues

    window = Primitive("line", "A-WINDOW", ((1000, 70), (1800, 70)), ("WW",), ())
    openings, issues = _match([*items, window], [_wall()], schedule)

    assert not openings
    assert [issue["code"] for issue in issues] == ["opening_ambiguous"]
