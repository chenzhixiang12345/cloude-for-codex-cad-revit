import pytest
from shapely.geometry import Point, Polygon

from tumo_engine.export.model import Element
from tumo_engine.preprocess import Primitive
from tumo_engine.recognize.room import identify_rooms


def wall(identifier, start, end, *, sheet="A101", level="L3", width=200):
    return {
        "id": identifier,
        "category": "wall",
        "type": {"thickness": width},
        "geometry": {"kind": "line", "start": start, "end": end},
        "base_level": level,
        "source": {"sheet": sheet, "handles": [identifier + "-cad"]},
    }


def label(name, at, handle):
    return Primitive("text", "AD-NAME-ROOM", (at,), (handle,), (), text=name)


def area(value, at=(1500, 1160), *, layer="AD-AREA-USNO"):
    return Primitive("text", layer, (at,), ("AREA",), (), text=value)


def rectangle():
    return [
        wall("W1", (0, 0), (4000, 0)),
        wall("W2", (4000, 0), (4000, 3000)),
        wall("W3", (4000, 3000), (0, 3000)),
        wall("W4", (0, 3000), (0, 0)),
    ]


def split_wall_room():
    return [
        wall("W1", (0, 0), (1200, 0)),
        wall("W5", (2100, 0), (4000, 0)),
        *rectangle()[1:],
    ]


def in_sheet(items, walls, openings=()):
    return identify_rooms(items, walls, openings, sheet_id="A101", base_level="L3", top_level="L4")


def door(host, at=(1650, 0), width=900):
    return {
        "id": "D1",
        "category": "door",
        "type": {"width": width, "height": 2100},
        "geometry": {"kind": "point", "at": at},
        "host": host,
        "base_level": "L3",
        "source": {"sheet": "A101", "handles": ["DOOR-CAD"]},
    }


def test_output_is_a_stable_0_1_room_with_source_level_and_seed():
    walls = rectangle()
    items = [label("卧室", (1500, 1500), "NAME-CAD")]

    rooms, issues = in_sheet(items, walls)

    [room] = rooms
    parsed = Element.model_validate(room)
    assert parsed.category == "room"
    assert parsed.type.name == "卧室"
    assert parsed.base_level == "L3"
    assert parsed.top_level == "L4"
    assert parsed.params["seed"] == [1500, 1500]
    assert parsed.source.sheet == "A101"
    assert parsed.source.handles == ["NAME-CAD", "W1-cad", "W2-cad", "W3-cad", "W4-cad"]
    assert not issues
    assert in_sheet(items, list(reversed(walls))) == (rooms, issues)


def test_confirmed_door_with_a_unique_host_side_closes_the_wall_gap():
    items = [label("卧室", (1500, 1500), "NAME")]
    rooms, issues = in_sheet(items, split_wall_room(), [door("W1")])

    [room] = rooms
    assert Polygon(room["geometry"]["points"]).area == 12_000_000
    assert "DOOR-CAD" in room["source"]["handles"]
    assert "opening_boundary" in room["evidence"]
    assert not issues


def test_confirmed_point_opening_on_a_continuous_host_does_not_change_the_room():
    items = [label("卧室", (1500, 1500), "NAME")]
    walls = rectangle()

    assert in_sheet(items, walls, [door("W1")]) == in_sheet(items, walls)


def test_confirmed_line_opening_uses_both_wall_ends_without_an_assigned_host():
    opening = {
        "category": "wall_opening",
        "type": {"width": 900},
        "geometry": {"kind": "line", "start": (1200, 0), "end": (2100, 0)},
        "source": {"sheet": "A101", "handles": ["VOID-CAD"]},
    }

    rooms, issues = in_sheet([label("卧室", (1500, 1500), "NAME")], split_wall_room(), [opening])

    [room] = rooms
    assert room["type"]["name"] == "卧室"
    assert "VOID-CAD" in room["source"]["handles"]
    assert not issues


def test_rejects_conflicting_opening_dimensions_instead_of_guessing():
    items = [label("卧室", (1500, 1500), "NAME")]
    rooms, issues = in_sheet(items, split_wall_room(), [door("W1", width=1400)])

    assert not rooms
    [issue] = issues
    assert issue["code"] == "room_opening_conflict"
    assert issue["at"] == [1650, 0]


def test_rejects_competing_walls_at_the_end_of_a_confirmed_opening():
    walls = [*split_wall_room(), wall("W6", (2100, 0), (3000, 0))]
    rooms, issues = in_sheet([label("卧室", (1500, 1500), "NAME")], walls, [door("W1")])

    assert not rooms
    assert [issue["code"] for issue in issues] == ["room_opening_conflict"]


def test_connects_only_corners_where_the_wall_footprints_touch():
    walls = [
        wall("W1", (100, 0), (3900, 0)),
        wall("W2", (4000, 100), (4000, 2900)),
        wall("W3", (3900, 3000), (100, 3000)),
        wall("W4", (0, 2900), (0, 100)),
    ]
    items = [label("卧室", (1500, 1500), "NAME")]
    rooms, issues = in_sheet(items, walls)

    [room] = rooms
    assert Polygon(room["geometry"]["points"]).area == 12_000_000
    assert "wall_junction" in room["evidence"]
    assert not issues

    walls[0] = wall("W1", (180, 0), (3900, 0))
    assert in_sheet(items, walls)[0] == []


def test_unbacked_collinear_door_gap_stays_open():
    rooms, _ = in_sheet([label("卧室", (1500, 1500), "NAME")], split_wall_room())

    assert not rooms


def test_different_room_names_in_one_boundary_are_reported_for_review():
    items = [label("厨房", (1000, 1500), "K"), label("餐厅", (3000, 1500), "D")]

    rooms, issues = in_sheet(items, rectangle())

    assert not rooms
    [issue] = issues
    assert issue["code"] == "room_label_conflict"
    assert "厨房" in issue["message"] and "餐厅" in issue["message"]
    assert Polygon([(0, 0), (4000, 0), (4000, 3000), (0, 3000)]).contains(Point(issue["at"]))


def test_moves_seed_out_of_wall_thickness_but_keeps_label_point():
    rooms, issues = in_sheet([label("厨房", (50, 1500), "K")], rectangle())

    [room] = rooms
    assert not issues
    assert room["params"]["label_point"] == [50, 1500]
    assert Polygon([(100, 100), (3900, 100), (3900, 2900), (100, 2900)]).contains(
        Point(room["params"]["seed"])
    )


def test_does_not_close_rooms_with_walls_from_other_levels_or_sheets():
    items = [label("厨房", (1000, 1500), "K")]
    walls = [
        *rectangle()[1:],
        wall("W5", (0, 0), (4000, 0), level="L2"),
        wall("W6", (0, 0), (4000, 0), sheet="A102"),
    ]

    assert in_sheet(items, walls)[0] == []


@pytest.mark.parametrize("walls", [[], split_wall_room()], ids=["no-walls", "open-boundary"])
@pytest.mark.parametrize("value", [r"14.00m\U+00B2", "14.00 m²", "14.00㎡"])
def test_unique_positive_area_marks_unconfirmed_room_at_original_name(walls, value):
    items = iter([label("卧室", (1500, 1500), "NAME"), area(value)])

    rooms, issues = in_sheet(items, walls)

    assert not rooms
    assert len(issues) == 1
    [issue] = issues
    assert issue["code"] == "room_area_unconfirmed"
    assert issue["at"] == [1500, 1500]
    assert "卧室" in issue["message"]
    assert "14.00 平方米" in issue["message"]


def test_area_marker_is_suppressed_if_the_name_is_in_a_confirmed_room():
    items = [label("卧室", (1500, 1500), "NAME")]

    assert in_sheet([*items, area("99.00m²")], rectangle()) == in_sheet(items, rectangle())


@pytest.mark.parametrize("other", [
    label("厨房", (1500, 1000), "SECOND-NAME"),
    area("16.00m²", at=(1700, 1500)),
])
def test_area_marker_requires_a_unique_neighbour_in_both_directions(other):
    items = [label("卧室", (1500, 1500), "NAME"), area("14.00m²"), other]

    assert in_sheet(items, []) == ([], [])


@pytest.mark.parametrize("annotation", [
    area("14.00m²", layer="AD-AREA-USNO-NOTES"),
    area("14.00"),
    area("14.00m"),
    area("0.00m²"),
    area("-14.00m²"),
    area("14.00m²", at=(1500, 1000)),
])
def test_area_marker_requires_designated_layer_positive_square_metres_and_proximity(annotation):
    items = [label("卧室", (1500, 1500), "NAME"), annotation]

    assert in_sheet(items, []) == ([], [])
