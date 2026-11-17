import ezdxf

from tumo_engine.recognize.standard_floors import slab_top_floor_span
from tumo_engine.rules import defaults
from tumo_engine.sheets import drawing_sheets, sheet_kind


def test_slab_plan_construction_titles_take_priority_over_plan_notes(tmp_path):
    doc = ezdxf.new(units=4)
    drawing = doc.modelspace()
    for x, number, title in (
        (0, "S5010", "二至四层顶板平法施工图"),
        (10000, "S5011", "机房层顶板平法施工图"),
    ):
        drawing.add_lwpolyline([(x, 0), (x + 8000, 0), (x + 8000, 6000), (x, 6000)],
                               close=True, dxfattribs={"layer": "SHEET-BORDER"})
        drawing.add_text(number, dxfattribs={"layer": "C-SHET-SHET", "insert": (x + 7000, 500)})
        drawing.add_text(title, dxfattribs={"layer": "C-SHET-SHET", "insert": (x + 7000, 700)})
        drawing.add_text("本层模板需结合墙身详图中剖切大样同时施工。",
                         dxfattribs={"layer": "S_PLAN_TEXT", "insert": (x + 5000, 1500)})

    sheets = drawing_sheets(doc, tmp_path / "板结构图.dxf", 1, defaults()["layers"])

    assert [(sheet.number, sheet.title, sheet_kind(sheet.title)) for sheet in sheets] == [
        ("S5010", "二至四层顶板平法施工图", "floor_plan"),
        ("S5011", "机房层顶板平法施工图", "special_plan"),
    ]


def test_template_notes_keep_their_classification():
    assert sheet_kind("顶板平法施工图设计说明") == "notes"
    assert sheet_kind("二层顶板平面施工图") == "floor_plan"


def test_a_top_slab_uses_the_level_above_the_floor_named_in_the_title():
    assert slab_top_floor_span("二~二十三层顶板平法施工图") == (3, 24)
    assert slab_top_floor_span("首层顶板平面施工图") == (2, 2)
    assert slab_top_floor_span("地下一层顶板平法施工图") == (1, 1)
    assert slab_top_floor_span("机房层顶板平法施工图") is None
    assert slab_top_floor_span("二至四层及六层顶板平法施工图") is None
    assert slab_top_floor_span("二至四层顶板平法施工图（不含三层）") is None
