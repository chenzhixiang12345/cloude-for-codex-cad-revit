import ezdxf

from tumo_engine.export.model import TumoModel
from tumo_engine.pipeline import run
from tumo_engine.recognize import structural_beams


def test_structural_beam_plan_uses_top_floor_and_retains_reference(tmp_path, monkeypatch):
    doc = ezdxf.new(units=4)
    space = doc.modelspace()
    space.add_text("二至六层顶梁平法施工图", dxfattribs={"layer": "TITLE", "insert": (0, 7500)})
    for row, (name, elevation) in enumerate((("层号", "结构标高(m)"), ("2", "4.750"), ("3", "7.750"), ("4", "10.750"))):
        for x, text in ((10000, name), (11500, elevation)):
            space.add_text(text, dxfattribs={"layer": "层高表", "insert": (x, 7200 - row * 350)})
    path = tmp_path / "梁结构图.dxf"
    doc.saveas(path)

    def recognize(doc, items, *, sheet_id, title, floor, rules, **_):
        assert floor == 3 and "顶梁" in title
        return [{"id": "B-1", "category": "beam", "discipline": "struct", "base_level": "L3", "base_offset": 850,
                 "type": {"mark": "KL1", "width": 200, "height": 1500},
                 "geometry": {"kind": "line", "start": [0, 0], "end": [2000, 0]}, "confidence": .93,
                 "source": {"sheet": sheet_id, "handles": ["A", "B"]}}], []

    monkeypatch.setattr(structural_beams, "recognize_structural_beams", recognize)
    model = run(path)

    TumoModel.model_validate(model)
    [beams] = model["elements"]
    assert beams["category"] == "beam"
    assert [level["id"] for level in model["levels"]] == ["L3", "L4"]
    assert model["levels"][0]["elevation_struct"] + beams["base_offset"] == 8600
