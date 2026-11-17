import ezdxf

from tumo_engine import pipeline
from tumo_engine.export.model import TumoModel
from tumo_engine.pipeline import run
from tumo_engine.recognize import structural_columns
from tumo_engine.tables import columns, shear_walls


def test_structural_plan_publishes_each_floor_only_after_checking_its_column_table(tmp_path, monkeypatch):
    doc = ezdxf.new(units=4)
    space = doc.modelspace()
    space.add_text("三至四层墙柱平面图", dxfattribs={"layer": "TITLE", "insert": (0, 7500)})
    for row, (name, elevation) in enumerate((("层号", "结构标高(m)"), ("3", "7.750"),
                                             ("4", "10.750"), ("5", "13.750"))):
        for x, text in ((10000, name), (11500, elevation)):
            space.add_text(text, dxfattribs={"layer": "层高表", "insert": (x, 7200 - row * 350)})
    path = tmp_path / "墙柱结构图.dxf"
    doc.saveas(path)
    monkeypatch.setattr(columns, "read_column_schedules", lambda *a, **k: ([{"fixture": True}], []))
    checked = []

    def recognize(items, table, *, plan_title, floor_number, rules):
        checked.append(floor_number)
        assert table == [{"fixture": True}]
        if floor_number == 4:
            return [], [("4F 的截面未确认。", [0, 0])]
        return [({"category": "column", "type": {"mark": "KZ1", "width": 850, "depth": 600},
                  "geometry": {"kind": "point", "at": [0, 0]}, "confidence": .95,
                  "handles": ["CDE"], "evidence": ["column_schedule:S3010:ABC"]}, None)], []

    monkeypatch.setattr(structural_columns, "recognize_structural_columns", recognize)
    monkeypatch.setattr(shear_walls, "read_shear_wall_schedule", lambda *a, **k: ([{"fixture": "shear"}], []))

    def recognize_shear(items, table, *, floor_number, legend_items, rules):
        assert table == [{"fixture": "shear"}] and legend_items
        if floor_number == 3:
            return [], [("Q2a 未确认。", [0, 300])]
        return [({"category": "shear_wall", "type": {"mark": "Q2a", "thickness": 300},
                  "geometry": {"kind": "line", "start": [0, 300], "end": [3900, 300]}, "confidence": .95,
                  "handles": ["DEF"], "evidence": ["shear_wall_schedule:ABC"]}, None)], []

    monkeypatch.setattr(pipeline.structural_shear_walls, "recognize_structural_shear_walls", recognize_shear)

    model = run(path, repeat_standard_floors=True)

    TumoModel.model_validate(model)
    assert checked == [3, 4]
    column, shear = model["elements"]
    assert (column["base_level"], column["top_level"], column["discipline"]) == ("L3", "L4", "struct")
    assert (shear["base_level"], shear["top_level"], shear["discipline"]) == ("L4", "L5", "struct")
    assert shear["source"]["handles"] == ["DEF"]
    assert [level["id"] for level in model["levels"]] == ["L3", "L4", "L5"]
    assert any("截面未确认" in issue["message"] for issue in model["issues"])
    assert any("Q2a 未确认" in issue["message"] and issue["where"]["at"] == [0, 300] for issue in model["issues"])
