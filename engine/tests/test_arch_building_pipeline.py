import ezdxf

from tumo_engine.export.model import TumoModel
from tumo_engine.pipeline import run


def test_plan_publishes_rooms_without_creating_unlabelled_rooms(tmp_path):
    drawing = tmp_path / "三层平面图.dxf"
    doc = ezdxf.new(units=4)
    space = doc.modelspace()
    for y in (0, 3000):
        for offset in (-100, 100):
            space.add_line((0, y + offset), (4000, y + offset), dxfattribs={"layer": "WALL"})
    for x in (0, 4000):
        for offset in (-100, 100):
            space.add_line((x + offset, 0), (x + offset, 3000), dxfattribs={"layer": "WALL"})
    label = space.add_text("卧室", dxfattribs={"insert": (1200, 1500), "layer": "AD-NAME-ROOM"})
    doc.saveas(drawing)

    model = run(drawing)

    TumoModel.model_validate(model)
    assert run(drawing) == model
    [room] = [item for item in model["elements"] if item["category"] == "room"]
    assert room["type"]["name"] == "卧室"
    assert (room["base_level"], room["top_level"]) == ("L3", "L4")
    assert room["params"]["seed"] == [1200, 1500]
    assert label.dxf.handle in room["source"]["handles"]


def test_plan_publishes_slabs_and_assigns_opening_to_containing_host(tmp_path):
    drawing = tmp_path / "三层平面图.dxf"
    doc = ezdxf.new(units=4)
    space = doc.modelspace()
    for layer, polygon in (
        ("A-SLAB", [(0, 0), (6000, 0), (6000, 4000), (0, 4000)]),
        ("A-SLAB-DROP", [(6000, 0), (8000, 0), (8000, 2000), (6000, 2000)]),
        ("A-SLAB-OPENING", [(1000, 1000), (1400, 1000), (1400, 1500), (1000, 1500)]),
        ("A-SLAB-OPENING", [(5900, 2500), (6100, 2500), (6100, 2700), (5900, 2700)]),
    ):
        space.add_lwpolyline(polygon, close=True, dxfattribs={"layer": layer})
    space.add_text("板厚120", dxfattribs={"insert": (3000, 1000), "layer": "A-SLAB-TEXT"})
    space.add_text("板厚100", dxfattribs={"insert": (7000, 1000), "layer": "A-SLAB-TEXT"})
    space.add_text("降板50 mm", dxfattribs={"insert": (7000, 1200), "layer": "A-SLAB-TEXT"})
    doc.saveas(drawing)

    model = run(drawing)

    TumoModel.model_validate(model)
    assert run(drawing) == model
    slab, lower, opening = model["elements"]
    assert (slab["type"]["thickness"], lower["type"]["thickness"]) == (120, 100)
    assert (slab["base_offset"], lower["base_offset"]) == (0, -50)
    assert opening["category"] == "slab_opening"
    assert opening["host"] == slab["id"]
    assert {item["base_level"] for item in model["elements"]} == {"L3"}
    assert any("洞口" in issue["message"] and issue["where"].get("at") for issue in model["issues"])


def test_plan_prefers_inline_level_table_and_current_row_height(tmp_path):
    drawing = tmp_path / "三层平面图.dxf"
    doc = ezdxf.new(units=4)
    space = doc.modelspace()
    for x, y, caption in ((0, 0, "层数"), (1500, 0, "标高H(m)"), (3000, 0, "层高(mm)"),
                          (0, -500, "3F"), (1500, -500, "7"), (3000, -500, "3200")):
        space.add_text(caption, dxfattribs={"insert": (x, y), "layer": "AD-层高表"})
    doc.saveas(drawing)

    model = run(drawing)

    assert [item["elevation_arch"] for item in model["levels"]] == [7000, 10200]
    assert model["levels"][1]["source"] == "楼层表层高"
    assert not any("默认" in issue["message"] for issue in model["issues"])


def test_standard_floor_mode_uses_range_and_keeps_single_floor_as_default(tmp_path):
    drawing = tmp_path / "三至四层平面图.dxf"
    doc = ezdxf.new(units=4)
    space = doc.modelspace()
    for x, y, caption in ((0, 0, "层数"), (1500, 0, "标高H(m)"), (3000, 0, "层高(mm)"),
                          (0, -500, "3F"), (1500, -500, "7.800"), (3000, -500, "3000"),
                          (0, -1000, "4F"), (1500, -1000, "10.800"), (3000, -1000, "3000"),
                          (0, -1500, "5F"), (1500, -1500, "13.800")):
        space.add_text(caption, dxfattribs={"insert": (x, y), "layer": "AD-层高表"})
    for y in (1900, 2100):
        space.add_line((0, y), (4000, y), dxfattribs={"layer": "WALL"})
    doc.saveas(drawing)

    single = run(drawing)
    expanded = run(drawing, repeat_standard_floors=True)

    TumoModel.model_validate(expanded)
    assert [level["id"] for level in single["levels"]] == ["L3", "L4"]
    assert [level["id"] for level in expanded["levels"]] == ["L3", "L4", "L5"]
    assert [wall["base_level"] for wall in expanded["elements"]] == ["L3", "L4"]
    assert expanded["elements"][0]["id"] == single["elements"][0]["id"]
