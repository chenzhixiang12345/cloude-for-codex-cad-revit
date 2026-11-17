import pytest

from tumo_engine.preprocess import Primitive
from tumo_engine.recognize.door_hosts import corner_door_host
from tumo_engine.recognize.geometry import Segment
from tumo_engine.rules import defaults


def _wall(identifier, start, end):
    return {
        "id": identifier,
        "category": "wall",
        "geometry": {"kind": "line", "start": start, "end": end},
        "type": {"thickness": 200},
    }


def _outline(x1, x2, y1=-100, y2=100, layer="AE-WALL", handle="RETURN"):
    points = ((x1, y1), (x2, y1), (x2, y2), (x1, y2))
    return Primitive("polyline", layer, points, (handle,), (), closed=True)


@pytest.mark.parametrize("reverse_symbol", [False, True])
def test_extends_a_single_wall_to_the_measured_corner(reverse_symbol):
    ends = ((0, 0), (950, 0))
    symbol = Segment(*(ends[::-1] if reverse_symbol else ends), ())
    original = _wall("host", [-825, 0], [0, 0])
    corner = _wall("return-wall", [1125, -400], [1125, 400])
    closed_return = _outline(949.6, 1025, layer="PLAN$0$0-S_WALL")

    result = corner_door_host(symbol, 950, [original, corner], [closed_return], defaults())

    assert result == {
        "host": "host",
        "geometry": {"kind": "line", "start": [-825, 0], "end": [949.6, 0]},
        "at": [475, 0],
        "handles": ["RETURN"],
    }
    assert original["geometry"]["end"] == [0, 0]


@pytest.mark.parametrize("obstacle", ["model", "cad", "column"])
def test_rejects_an_extension_that_crosses_another_constructed_or_drawn_element(obstacle):
    symbol = Segment((0, 0), (950, 0), ())
    walls = [_wall("host", [0, 0], [-825, 0])]
    items = [_outline(950, 1025)]
    if obstacle == "model":
        walls.append(_wall("crossing", [500, 300], [500, -300]))
    else:
        layer = "AE-WALL" if obstacle == "cad" else "AE-COLUMN"
        items.append(_outline(400, 600, -300, 300, layer=layer))

    assert corner_door_host(symbol, 950, walls, items, defaults()) is None


@pytest.mark.parametrize("return_outline", [
    _outline(950, 1025, 0, 100),
    _outline(875, 950),
    _outline(950, 1025, layer="DETAIL"),
])
def test_requires_a_wall_face_with_full_thickness_facing_the_gap(return_outline):
    symbol = Segment((0, 0), (950, 0), ())
    walls = [_wall("host", [-825, 0], [0, 0])]

    assert corner_door_host(symbol, 950, walls, [return_outline], defaults()) is None


def test_does_not_extend_if_either_existing_wall_could_be_the_host():
    symbol = Segment((0, 0), (950, 0), ())
    walls = [_wall("left", [-825, 0], [0, 0]), _wall("right", [950, 0], [1900, 0])]

    assert corner_door_host(symbol, 950, walls, [_outline(950, 1025)], defaults()) is None
