import ezdxf
import pytest

from tumo_engine.export.model import TumoModel
from tumo_engine.pipeline import run
from tumo_engine.recognize import structural_slabs


def _top_slab_drawing(tmp_path, *, structure=True):
    path = tmp_path / "顶板结构图.dxf"
    doc = ezdxf.new(units=4)
    space = doc.modelspace()
    space.add_text("二至四层顶板平法施工图", dxfattribs={"layer": "TITLE", "insert": (0, 7500)})
    elevation_heading = "结构标高(m)" if structure else "建筑标高(m)"
    for row, (name, elevation) in enumerate((("层号", elevation_heading), ("2", "4.750"), ("3", "7.750"),
                                             ("4", "10.750"), ("5", "13.750"), ("6", "16.750"))):
        for x, text in ((10000, name), (11500, elevation)):
            space.add_text(text, dxfattribs={"layer": "层高表", "insert": (x, 7200 - row * 350)})
    doc.saveas(path)
    return path


@pytest.fixture
def recognized_slab(monkeypatch):
    def recognize(drawing, items, rules, *, bounds=None):
        assert drawing.modelspace()
        assert any(item.text == "层号" for item in items)
        return [{
            "category": "slab", "type": {"mark": "LB2", "thickness": 150},
            "geometry": {"kind": "polygon", "points": [[0, 0], [1000, 0], [1000, 1200], [0, 1200]]},
            "base_offset": -500, "evidence": ["structural_slab:matched_hs_legend"],
            "confidence": 0.93, "handles": ["AA", "AB"],
        }], []

    monkeypatch.setattr(structural_slabs, "recognize_structural_slabs", recognize)


@pytest.mark.parametrize("repeat", [False, True])
def test_structural_top_slab_uses_the_upper_levels_and_keeps_top_offsets(tmp_path, recognized_slab, repeat):
    model = run(_top_slab_drawing(tmp_path), repeat_standard_floors=repeat)

    TumoModel.model_validate(model)
    slabs = model["elements"]
    assert [slab["base_level"] for slab in slabs] == (["L3", "L4", "L5"] if repeat else ["L3"])
    assert len({slab["id"] for slab in slabs}) == len(slabs)
    levels = {level["id"]: level for level in model["levels"]}
    tops = [levels[slab["base_level"]]["elevation_struct"] + slab["base_offset"] for slab in slabs]
    assert tops == ([7250, 10250, 13250] if repeat else [7250])
    assert all(slab["category"] == "slab" and slab["discipline"] == "struct"
               and set(slab["source"]["handles"]) >= {"AA", "AB"} for slab in slabs)


def test_structural_top_slab_requires_the_same_sheet_structural_datum(tmp_path, recognized_slab):
    model = run(_top_slab_drawing(tmp_path, structure=False))

    assert not model["elements"]
    assert any("缺少 3F 的明确结构标高" in entry["message"] for entry in model["issues"])
