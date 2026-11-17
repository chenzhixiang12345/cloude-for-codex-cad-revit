import ezdxf

from tumo_engine.export.model import TumoModel
from tumo_engine.pipeline import run
from tumo_engine.preprocess import unit_factor


def _dimensions(space, layer, y, *, ratio, explicit):
    for x in (1000, 3000, 5000):
        attributes = {"layer": layer}
        dim = space.add_linear_dim(base=(x, y + 400), p1=(x, y), p2=(x + 800, y),
                                   text=f"{800 * ratio:g}" if explicit else "<>",
                                   override={"dimlfac": ratio}, dxfattribs=attributes)
        dim.render()


def test_unitless_structural_sheet_uses_its_own_witnessed_scale(tmp_path):
    doc = ezdxf.new(units=0)
    space = doc.modelspace()
    for base, number in ((0, "S3008"), (14000, "S3009")):
        space.add_lwpolyline([(0, base), (12000, base), (12000, base + 9000), (0, base + 9000)],
                             close=True, dxfattribs={"layer": "SHEET-BORDER"})
        space.add_text("三至四层墙柱平面图", dxfattribs={"insert": (7000, base + 1000), "layer": "TITLE"})
        space.add_text(number, dxfattribs={"insert": (7000, base + 500), "layer": "TITLE"})
    _dimensions(space, "WCOL_DE_DIM", 1800, ratio=.25, explicit=True)
    _dimensions(space, "COLU_DE_DIM", 17000, ratio=1, explicit=False)
    path = tmp_path / "墙柱结构平面图.dxf"
    doc.saveas(path)

    assert unit_factor(doc) == .25
    model = run(path, sheet_number="S3009")

    TumoModel.model_validate(model)
    [sheet] = model["sheets"]
    assert sheet["id"] == "S3009"
    assert sheet["units_to_mm"] == 1
    assert sheet["bounds"] == [0, 14000, 12000, 23000]


def test_sheet_select_rejects_an_unknown_number(tmp_path):
    import pytest

    path = tmp_path / "平面图.dxf"
    ezdxf.new(units=4).saveas(path)
    with pytest.raises(ValueError, match="找不到唯一图号"):
        run(path, sheet_number="S-404")
