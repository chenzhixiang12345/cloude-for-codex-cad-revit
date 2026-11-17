import ezdxf
import pytest
from shapely.geometry import Point

from tumo_engine.preprocess import primitives
from tumo_engine.recognize.geometry import segments, source_handles


def _frame_parts(block, radius=890, *, angle=90):
    layer = {"layer": "AE-DOOR-INSD"}
    sweep = block.add_arc((0, 0), radius, 360 - angle, 360, dxfattribs=layer)
    leaf = block.add_lwpolyline([(0, 0), (30, 0), (30, -radius), (0, -radius)],
                                close=True, dxfattribs=layer)
    hinge = block.add_lwpolyline([(0, 30), (0, 0), (-30, 0), (-30, 60), (30, 60), (30, 30)],
                                 close=True, dxfattribs=layer)
    catch = block.add_lwpolyline([(radius, 30), (radius, 0), (radius + 30, 0),
                                 (radius + 30, 60), (radius - 30, 60), (radius - 30, 30)],
                                 close=True, dxfattribs=layer)
    return sweep, leaf, hinge, catch


def _opening_segments(doc):
    from tumo_engine.recognize.door_symbols import door_frame_openings

    items, _ = primitives(doc, 1)
    return list(door_frame_openings(items))


def test_arc_uses_its_curved_bounds_and_does_not_enter_generic_segments():
    doc = ezdxf.new(units=4)
    arc = doc.modelspace().add_arc((100, 100), 75, 10, 160, dxfattribs={"layer": "A-DOOR"})
    items, _ = primitives(doc, 1, (99, 174.5, 101, 175.5))

    [curve] = items
    assert curve.kind == "arc"
    assert curve.handles == (arc.dxf.handle,)
    assert curve.shape.distance(Point(100, 175)) < 0.001
    assert not list(segments(items))


@pytest.mark.parametrize("radius", [790, 890, 1040])
@pytest.mark.parametrize("mirror", [1, -1])
def test_measures_both_jambs_and_preserves_handles_in_a_nested_door_insert(radius, mirror):
    doc = ezdxf.new(units=4)
    door = doc.blocks.new("DOOR")
    parts = _frame_parts(door, radius)
    unit = doc.blocks.new("UNIT")
    inner = unit.add_blockref("DOOR", (200, 300), dxfattribs={"rotation": 30, "xscale": mirror})
    outer = doc.modelspace().add_blockref("UNIT", (5000, 8000))

    [result] = _opening_segments(doc)

    assert result.length == pytest.approx(radius + 60, abs=0.01)
    expected = [(anchor.x + 5000, anchor.y + 8000)
                for anchor in inner.matrix44().transform_vertices([(-30, 30, 0), (radius + 30, 30, 0)])]
    assert result.a == pytest.approx(expected[0], abs=0.01)
    assert result.b == pytest.approx(expected[1], abs=0.01)
    assert source_handles(result.primitives) == sorted([outer.dxf.handle, inner.dxf.handle,
                                                        *(part.dxf.handle for part in parts)])


@pytest.mark.parametrize("reason", ["missing_leaf", "wide_sweep", "missing_frame", "two_sweeps", "extra_frame",
                                     "offset_frame", "decorations", "different_instance"])
def test_rejects_door_shapes_without_unique_same_instance_evidence(reason):
    doc = ezdxf.new(units=4)
    door = doc.blocks.new("DOOR")
    sweep, leaf, hinge, catch = _frame_parts(door, angle=60 if reason == "wide_sweep" else 90)
    if reason == "missing_leaf":
        door.delete_entity(leaf)
    if reason == "missing_frame":
        door.delete_entity(catch)
    if reason == "two_sweeps":
        door.add_arc((3, 0), 890, 270, 360, dxfattribs={"layer": "AE-DOOR-INSD"})
    if reason == "extra_frame":
        door.add_lwpolyline([(890, 35), (890, 0), (925, 0), (925, 65), (860, 65), (860, 35)],
                            close=True, dxfattribs={"layer": "AE-DOOR-INSD"})
    if reason == "offset_frame":
        catch.translate(0, 25, 0)
    if reason == "decorations":
        for part in (sweep, leaf, hinge, catch):
            part.dxf.layer = "DECORATIVE-ARCS"
    if reason == "different_instance":
        other = doc.blocks.new("OTHER")
        other.add_entity(catch.copy())
        door.delete_entity(catch)
        doc.modelspace().add_blockref("OTHER", (1000, 1000))
    doc.modelspace().add_blockref("DOOR", (1000, 1000))

    assert not _opening_segments(doc)


@pytest.mark.parametrize("corner,extra_frame", [
    (False, None), (True, None), (True, "unhosted"), (True, "closer_number"), (True, "unrelated_number"),
])
def test_pipeline_uses_measured_frame_width_for_a_scheduled_door(tmp_path, corner, extra_frame):
    from tumo_engine.export.model import TumoModel
    from tumo_engine.pipeline import run

    doc = ezdxf.new(units=4)
    block = doc.blocks.new("DOOR")
    parts = _frame_parts(block)
    drawing = doc.modelspace()
    insert = drawing.add_blockref("DOOR", (1300, 0))
    label = drawing.add_text("WM0924A", dxfattribs={"insert": (1745, 500), "layer": "AD-NUMB"})
    if extra_frame == "unhosted":
        drawing.add_blockref("DOOR", (1850, 0), dxfattribs={"rotation": 90})
    if extra_frame == "unrelated_number":
        drawing.add_text("FM乙0924A", dxfattribs={"insert": (1745, -100), "layer": "AD-NUMB"})
    if extra_frame == "closer_number":
        drawing.add_blockref("DOOR", (1300, 820))
        drawing.add_text("FM乙0924A", dxfattribs={"insert": (1745, 850), "layer": "AD-NUMB"})
        for y in (750, 950):
            drawing.add_line((0, y), (4000, y), dxfattribs={"layer": "WALL"})
    for y in (-70, 130):
        drawing.add_line((0, y), (1270 if corner else 4000, y), dxfattribs={"layer": "WALL"})
    if corner:
        wall_face = drawing.add_lwpolyline([(2220, -70), (2290, -70), (2290, 130), (2220, 130)],
                                           close=True, dxfattribs={"layer": "AE-WALL"})
    for x, y, value in ((0, 4500, "门窗编号"), (1500, 4500, "洞口尺寸"),
                        (0, 4000, "WM0924A"), (1500, 4000, "950×2400")):
        drawing.add_text(value, dxfattribs={"insert": (x, y)})
    path = tmp_path / "三层建筑平面图.dxf"
    doc.saveas(path)

    model = run(path)

    TumoModel.model_validate(model)
    [door] = [element for element in model["elements"] if element["category"] == "door"]
    wall = next(element for element in model["elements"] if element["id"] == door["host"])
    assert wall["category"] == "wall"
    assert door["host"] == wall["id"]
    assert door["type"] == {"mark": "WM0924A", "width": 950, "height": 2400}
    assert door["geometry"]["at"] == [1745, 30]
    assert "door:swing_and_jambs" in door["evidence"]
    assert set(door["source"]["handles"]) == {insert.dxf.handle, label.dxf.handle, *(part.dxf.handle for part in parts)}
    if corner:
        assert wall["geometry"]["end"] == [2220, 30]
        assert "wall:verified_corner_face" in door["evidence"]
        assert "opening:verified_corner_host" in wall["evidence"]
        assert wall_face.dxf.handle in wall["source"]["handles"]
