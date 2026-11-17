import ezdxf
import pytest

from tumo_engine.preprocess import primitives
from tumo_engine.rules import defaults
from tumo_engine.tables import OpeningScheduleRow


def _wall(identifier, y=0, start=0, end=4000):
    return {
        "id": identifier,
        "category": "wall",
        "type": {"thickness": 200},
        "geometry": {"kind": "line", "start": (start, y), "end": (end, y)},
    }


def _plan(mark="M01", layer="A-DOOR", width=900, y=70):
    doc = ezdxf.new(units=4)
    drawing = doc.modelspace()
    tag = drawing.add_text(mark, dxfattribs={"insert": (1220, y + 250)})
    symbol = drawing.add_line((800, y), (800 + width, y), dxfattribs={"layer": layer})
    return doc, tag.dxf.handle, symbol.dxf.handle


def _match(doc, walls, schedule, **kwargs):
    from tumo_engine.recognize.opening import recognize_openings

    items, _ = primitives(doc, 1)
    return recognize_openings(items, walls, schedule, **kwargs)


def test_matches_door_evidence_to_wall_and_snaps_to_its_axis():
    doc, tag, symbol = _plan("ｍ０１")
    schedule = [OpeningScheduleRow("M01", 900, 2075, None, ("table-1",))]

    openings, issues = _match(doc, [_wall("W1")], schedule)

    opening, = openings
    assert opening["category"] == "door"
    assert opening["host"] == "W1"
    assert opening["geometry"] == {"kind": "point", "at": [1250, 0]}
    assert opening["type"] == {"mark": "M01", "width": 900, "height": 2075}
    assert set(opening["handles"]) == {tag, symbol}
    assert issues == []


def test_matching_window_keeps_sill_height_and_merges_coincident_symbol_lines():
    doc, _, _ = _plan("C55", "AE-WIND", width=900, y=70)
    doc.modelspace().add_line((1700, -70), (800, -70), dxfattribs={"layer": "AE-WIND"})
    sheet = [OpeningScheduleRow("C55", 900, 1450, 825, ())]

    openings, issues = _match(doc, [_wall("W1")], sheet)

    opening, = openings
    assert opening["category"] == "window"
    assert opening["sill_height"] == 825
    assert len(opening["handles"]) == 3
    assert issues == []


@pytest.mark.parametrize(
    ("layer", "width", "walls", "expected"),
    [
        ("A-WALL", 900, [_wall("W1")], "opening_evidence_missing"),
        ("A-DOOR", 680, [_wall("W1")], "opening_evidence_missing"),
        ("A-DOOR", 900, [_wall("W1", start=1400)], "host_missing"),
        ("A-DOOR", 900, [_wall("W1"), _wall("W2", y=120)], "host_ambiguous"),
    ],
)
def test_requires_measured_opening_and_unique_spanning_host(layer, width, walls, expected):
    doc, _, _ = _plan(layer=layer, width=width)
    schedule = [OpeningScheduleRow("M01", 900, 2100, None, ())]

    openings, issues = _match(doc, walls, schedule)

    assert openings == []
    assert [issue["code"] for issue in issues] == [expected]


def test_reports_conflicting_schedule_rows_even_when_width_agrees():
    doc, _, _ = _plan()
    schedule = [
        OpeningScheduleRow("M01", 900, 2100, None, ()),
        OpeningScheduleRow("M01", 900, 2400, None, ()),
    ]

    openings, issues = _match(doc, [_wall("W1")], schedule)

    assert openings == []
    assert [issue["code"] for issue in issues] == ["schedule_conflict"]


def test_nearby_different_tags_do_not_place_two_doors_in_one_opening():
    doc, _, _ = _plan()
    doc.modelspace().add_text("M02", dxfattribs={"insert": (1240, 440)})
    schedule = [
        OpeningScheduleRow("M01", 900, 2100, None, ()),
        OpeningScheduleRow("M02", 900, 2100, None, ()),
    ]

    openings, issues = _match(doc, [_wall("W1")], schedule)

    assert openings == []
    assert {issue["mark"] for issue in issues} == {"M01", "M02"}
    assert {issue["code"] for issue in issues} == {"mark_ambiguous"}


def _wall_fragment(identifier, start, end, *, y=0, thickness=200, floor="L3"):
    result = _wall(identifier, y, start, end)
    result["type"]["thickness"] = thickness
    result["base_level"] = floor
    return result


def _match_wall_pair(walls):
    doc, _, _ = _plan("C01", "AE-WIND")
    return _match(doc, walls, [OpeningScheduleRow("C01", 900, 1400, None, ())],
                  wall_pair_rules=defaults()["opening"]["wall_pair"])


def test_matches_window_to_unique_pair_of_segments_on_either_side_of_the_gap():
    left = _wall_fragment("WZ", 0, 797)
    right = _wall_fragment("WA", 1704, 4000)
    [opening], issues = _match_wall_pair([right, left])

    assert opening["wall_pair"] == ["WA", "WZ"]
    assert "host" not in opening
    assert opening["geometry"] == {"kind": "point", "at": [1250.5, 0]}
    assert "wall:paired_ends" in opening["evidence"]
    assert issues == []


def test_prefers_existing_continuous_host_over_endpoint_pair():
    walls = [_wall_fragment("W1", 0, 800), _wall_fragment("W2", 1700, 4000), _wall("W3")]
    [opening], issues = _match_wall_pair(walls)

    assert opening["host"] == "W3"
    assert "wall_pair" not in opening
    assert issues == []


@pytest.mark.parametrize("different", [
    _wall_fragment("W2", 1700, 4000, thickness=220),
    _wall_fragment("W2", 1700, 4000, floor="L4"),
    _wall_fragment("W2", 1700, 4000, y=20),
    _wall_fragment("W2", 1770, 4000),
])
def test_requires_compatible_wall_fragments_and_aligned_endpoints(different):
    openings, issues = _match_wall_pair([_wall_fragment("W1", 0, 800), different])

    assert openings == []
    assert [issue["code"] for issue in issues] == ["host_missing"]


def test_rejects_competing_wall_pairs_without_selecting_one():
    walls = [_wall_fragment("W1", 0, 800), _wall_fragment("W2", 1700, 4000),
             _wall_fragment("W3", 0, 800, y=120), _wall_fragment("W4", 1700, 4000, y=120)]
    openings, issues = _match_wall_pair(walls)

    assert openings == []
    assert [issue["code"] for issue in issues] == ["host_ambiguous"]
