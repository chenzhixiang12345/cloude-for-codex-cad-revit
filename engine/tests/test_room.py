from shapely.geometry import Polygon

from tumo_engine.preprocess import Primitive
from tumo_engine.recognize.room import recognize_rooms


def wall(start, end, handle):
    return {
        "category": "wall",
        "geometry": {"kind": "line", "start": start, "end": end},
        "source": {"sheet": "A101", "handles": [handle]},
    }


def label(name, at, handle, layer="XREF$0$AD-NAME-ROOM"):
    return Primitive("text", layer, (at,), (handle,), (), text=name)


def rectangle(x0, y0, x1, y1, prefix):
    corners = [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
    return [wall(a, b, f"{prefix}{i}") for i, (a, b) in enumerate(zip(corners, corners[1:] + corners[:1]))]


def test_identifies_labeled_faces_on_each_side_of_shared_wall():
    walls = rectangle(0, 0, 6000, 4000, "A")
    partition = wall((3000, 0), (3000, 4000), "B")
    items = [
        label("厨房", (1400, 1500), "C1"),
        label("餐厅", (4100, 1200), "C2"),
        label("说明", (4400, 1000), "D1", "NOTES"),
        label("室外", (7500, 2000), "D2"),
    ]

    rooms = recognize_rooms(items, [*walls, partition])

    assert [room["type"]["name"] for room in rooms] == ["厨房", "餐厅"]
    assert all(room["category"] == "room" for room in rooms)
    kitchen, dining = rooms
    assert Polygon(kitchen["geometry"]["points"]).equals(
        Polygon([(0, 0), (3000, 0), (3000, 4000), (0, 4000)])
    )
    assert Polygon(dining["geometry"]["points"]).equals(
        Polygon([(3000, 0), (6000, 0), (6000, 4000), (3000, 4000)])
    )
    assert kitchen["params"]["label_point"] == [1400, 1500]
    assert kitchen["handles"] == ["A0", "A2", "A3", "B", "C1"]


def test_only_closes_doorway_with_an_explicit_boundary():
    walls = [
        *rectangle(0, 0, 4000, 3000, "A")[1:],
        wall((0, 0), (1200, 0), "B1"),
        wall((2100, 0), (4000, 0), "B2"),
    ]
    items = [label("卧室", (1500, 1500), "C")]
    doorway = Primitive("line", "DOOR", ((1200, 0), (2100, 0)), ("D",), ())

    assert recognize_rooms(items, walls) == []

    [room] = recognize_rooms(items, walls, opening_boundaries=[doorway])

    assert room["type"]["name"] == "卧室"
    assert Polygon(room["geometry"]["points"]).area == 12_000_000
    assert "D" in room["handles"]
    assert "opening_boundary" in room["evidence"]


def test_rejects_measurements_and_labels_on_the_boundary():
    walls = rectangle(0, 0, 4000, 3000, "A")
    items = [label("12.0 m²", (1000, 1000), "B"), label("卧室", (0, 1500), "C")]
    assert recognize_rooms(items, walls) == []


def test_normalizes_repeated_room_name_and_keeps_its_provenance():
    walls = rectangle(0, 0, 4000, 3000, "A")
    items = [label("  客厅\n12.50 ㎡ ", (2000, 1800), "B"), label("客厅", (1700, 1400), "C")]

    [room] = recognize_rooms(items, walls)

    assert room["type"]["name"] == "客厅"
    assert room["params"]["label_point"] == [1700, 1400]
    assert set(room["handles"]) == {"A0", "A1", "A2", "A3", "B", "C"}
    assert recognize_rooms(list(reversed(items)), list(reversed(walls))) == [room]


def test_does_not_guess_between_different_names_in_one_face():
    walls = rectangle(0, 0, 4000, 3000, "A")
    items = [label("厨房", (1000, 1500), "B1"), label("餐厅", (3000, 1500), "B2")]
    assert recognize_rooms(items, walls) == []


def test_does_not_drop_inner_boundary_from_the_room_geometry():
    walls = [*rectangle(0, 0, 8000, 6000, "A"), *rectangle(3000, 2000, 5000, 4000, "B")]
    items = [label("大厅", (1000, 1500), "C1"), label("储藏室", (4200, 3100), "C2")]

    [room] = recognize_rooms(items, walls)

    assert room["type"]["name"] == "储藏室"
    assert Polygon(room["geometry"]["points"]).area == 4_000_000


def test_accepts_configured_room_label_layer():
    walls = rectangle(0, 0, 4000, 3000, "A")
    items = [label("厨房", (1500, 1800), "B", "FLOOR-NAME")]

    assert recognize_rooms(items, walls) == []
    [room] = recognize_rooms(items, walls, room_layers=r"^FLOOR-NAME$")
    assert room["type"]["name"] == "厨房"
