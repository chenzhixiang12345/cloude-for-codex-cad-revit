import ezdxf

from tumo_engine.export.model import TumoModel
from tumo_engine.building_pipeline import run_building


def _frame(layout, title, sheet, dy, *, grid_shift=0):
    def shift(point):
        return (point[0], point[1] + dy)

    def label(text, point, layer):
        layout.add_text(text, dxfattribs={"insert": shift(point), "layer": layer})

    frame = ((-5000, -5000), (10000, -5000), (10000, 9000), (-5000, 9000))
    layout.add_lwpolyline([shift(point) for point in frame], close=True, dxfattribs={"layer": "FRAME"})
    label(title, (200, -2700), "TITLE")
    label("图号 " + sheet, (4500, -2700), "TITLE")
    for mark, start, end in (
        ("1", (0, -1000), (0, 5300)), ("2", (6000 + grid_shift, -1000), (6000 + grid_shift, 5300)),
        ("A", (-1000, 0), (7000, 0)), ("B", (-1000, 4000), (7000, 4000)),
    ):
        layout.add_line(shift(start), shift(end), dxfattribs={"layer": "GRID"})
        layout.add_circle(shift(start), 200, dxfattribs={"layer": "GRID-BUBBLE"})
        label(mark, start, "GRID-BUBBLE")
    for y in (900, 1100):
        layout.add_line(shift((200, y)), shift((3900, y)), dxfattribs={"layer": "WALL"})
    for index, row in enumerate((
        ("楼层", "建筑标高(m)", "层高(mm)"),
        ("1F", "0", "3000"), ("2F", "3", "3000"), ("3F", "6", "3000"), ("4F", "9", "3000"),
        ("5F", "12", "3000"),
    )):
        for x, text in zip((200, 2000, 4000), row):
            label(text, (x, 7800 - index * 400), "楼层表")


def test_merges_shifted_first_floor_with_standard_floor_and_skips_unknown_roof(tmp_path):
    drawing = tmp_path / "建筑平面图.dxf"
    doc = ezdxf.new(units=4)
    space = doc.modelspace()
    _frame(space, "三至四层平面图", "A103", 20000)
    _frame(space, "首层平面图", "A101", 0)
    _frame(space, "屋顶层平面图", "A105", 40000)
    doc.saveas(drawing)

    model = run_building(drawing)

    TumoModel.model_validate(model)
    assert [sheet["id"] for sheet in model["sheets"]] == ["A103", "A101"]
    assert model["sheets"][1]["transform"] == {"dx": 0, "dy": 20000, "rot_deg": 0}
    assert len(model["grids"]) == 4
    assert {wall["base_level"] for wall in model["elements"]} == {"L1", "L3", "L4"}
    assert any("A105" in issue["message"] for issue in model["issues"])


def test_building_skips_floor_with_conflicting_grid_positions(tmp_path):
    drawing = tmp_path / "建筑平面图.dxf"
    doc = ezdxf.new(units=4)
    space = doc.modelspace()
    _frame(space, "三至四层平面图", "A103", 20000)
    _frame(space, "首层平面图", "A101", 0, grid_shift=500)
    doc.saveas(drawing)

    model = run_building(drawing)

    assert len(model["sheets"]) == 1
    assert any("A101" in issue["message"] and "轴" in issue["message"] for issue in model["issues"])
