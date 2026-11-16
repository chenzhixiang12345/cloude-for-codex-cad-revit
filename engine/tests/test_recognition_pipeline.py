from pathlib import Path

import ezdxf
import pytest

from tumo_engine.pipeline import run


def make_plan(path: Path) -> None:
    doc = ezdxf.new(units=4)
    msp = doc.modelspace()
    msp.add_text("标准层平面图", dxfattribs={"insert": (-300, -1600), "layer": "TITLE"})
    msp.add_text("图号 A101", dxfattribs={"insert": (3000, -1600), "layer": "TITLE"})
    for x, label in [(0, "1"), (5000, "2")]:
        msp.add_line((x, -1000), (x, 4000), dxfattribs={"layer": "GRID"})
        msp.add_circle((x, -1000), 250, dxfattribs={"layer": "GRID-BUBBLE"})
        msp.add_text(label, dxfattribs={"insert": (x, -1000), "layer": "GRID-BUBBLE"})
    msp.add_line((-1000, 0), (6000, 0), dxfattribs={"layer": "GRID"})
    msp.add_circle((-1000, 0), 250, dxfattribs={"layer": "GRID-BUBBLE"})
    msp.add_text("A", dxfattribs={"insert": (-1000, 0), "layer": "GRID-BUBBLE"})

    block = doc.blocks.new("PILLAR")
    block.add_lwpolyline(
        [(-250, -200), (250, -200), (250, 200), (-250, 200)], close=True,
    )
    msp.add_blockref("PILLAR", (0, 0), dxfattribs={"layer": "COLUMN"})
    msp.add_text("KZ1", dxfattribs={"insert": (200, 100), "layer": "COLUMN-ID"})
    for y in (-100, 100):
        for start, end in [(255, 1500), (1530, 4500)]:
            msp.add_line((start, y), (end, y), dxfattribs={"layer": "WALL"})

    for x, text in [(100, "楼层"), (2000, "建筑标高"), (4000, "结构标高")]:
        msp.add_text(text, dxfattribs={"insert": (x, 3300), "layer": "层高表"})
    for y, row in [(3000, ("3F", "7.200", "7.150")), (2700, ("4F", "10.200", "10.150"))]:
        for x, text in zip((100, 2000, 4000), row):
            msp.add_text(text, dxfattribs={"insert": (x, y), "layer": "层高表"})
    doc.saveas(path)


def test_recognizes_grid_columns_walls_and_level_table(tmp_path):
    path = tmp_path / "三层平面图.dxf"
    make_plan(path)
    model = run(path)
    assert model == run(path)
    assert model["project"]["units"] == "mm"
    assert model["project"]["origin"]["grid"] == ["1", "A"]
    assert {grid["label"] for grid in model["grids"]} == {"1", "2", "A"}
    assert model["sheets"][0]["id"] == "A101"
    assert model["sheets"][0]["kind"] == "floor_plan"
    assert model["levels"][0]["elevation_arch"] == pytest.approx(7200)
    assert model["levels"][0]["elevation_struct"] == pytest.approx(7150)
    column, wall = model["elements"]
    assert column["category"] == "column"
    assert column["type"]["width"] == pytest.approx(500)
    assert column["type"]["depth"] == pytest.approx(400)
    assert column["type"]["mark"] == "KZ1"
    assert wall["category"] == "wall"
    assert wall["type"]["thickness"] == pytest.approx(200)
    assert wall["geometry"]["start"] == pytest.approx([250, 0])
    assert wall["geometry"]["end"] == pytest.approx([4500, 0])
    assert wall["base_level"] == "L3"
    assert wall["top_level"] == "L4"
    assert model["relations"][0]["a"] == wall["id"]
    assert model["relations"][0]["b"] == column["id"]
    assert all(el["source"]["handles"] for el in model["elements"])


def test_accepts_model_space_bounds_and_converts_units(tmp_path):
    doc = ezdxf.new(units=5)
    msp = doc.modelspace()
    for x in (0, 1000):
        msp.add_lwpolyline(
            [(x, 0), (x + 500, 0), (x + 500, 20), (x, 20)], close=True,
            dxfattribs={"layer": "WALL"},
        )
    path = tmp_path / "标准层平面图.dxf"
    doc.saveas(path)
    model = run(path, bounds=(-100, -100, 6000, 1000))
    assert len(model["elements"]) == 1
    assert model["elements"][0]["type"]["thickness"] == pytest.approx(200)
    assert model["elements"][0]["geometry"]["end"] == pytest.approx([5000, 100])



def test_prefers_repeated_story_plan_to_building_number(tmp_path):
    doc = ezdxf.new(units=4)
    msp = doc.modelspace()
    frames = [(0, "1-1号楼 首层平面图", "A101"), (10000, "1-1号楼 三-八层平面图", "A102")]
    for offset, title, number in frames:
        msp.add_lwpolyline(
            [(offset, 0), (offset + 8000, 0), (offset + 8000, 6000), (offset, 6000)], close=True,
            dxfattribs={"layer": "FRAME"},
        )
        msp.add_text(title, dxfattribs={"insert": (offset + 1000, 1000), "layer": "TITLE"})
        msp.add_text(number, dxfattribs={"insert": (offset + 2000, 1000), "layer": "TITLE"})
    path = tmp_path / "楼层平面图.dxf"
    doc.saveas(path)
    model = run(path)
    assert model["sheets"][0]["number"] == "A102"
    assert model["sheets"][0]["bounds"] == [10000, 0, 18000, 6000]
    assert model["levels"][0]["id"] == "L3"
