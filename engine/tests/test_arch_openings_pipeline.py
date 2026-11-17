from pathlib import Path

import ezdxf
import pytest

from tumo_engine.export.model import TumoModel
from tumo_engine.pipeline import run


def _table(layout, mark, width, *, offset=0):
    for row, y in ((("门窗编号", "洞口尺寸"), 5000), ((mark, f"{width}×2100"), 4700)):
        for column, text in enumerate(row):
            layout.add_text(text, dxfattribs={"insert": (offset + column * 1500, y)})


def _plan(path: Path, *, inline_width=None):
    doc = ezdxf.new(units=4)
    space = doc.modelspace()
    space.add_text("三层平面图", dxfattribs={"insert": (0, -1000), "layer": "TITLE"})
    for y in (-100, 100):
        space.add_line((0, y), (4000, y), dxfattribs={"layer": "WALL"})
    label = space.add_text("M01", dxfattribs={"insert": (1250, 250)})
    door = space.add_line((800, 0), (1700, 0), dxfattribs={"layer": "A-DOOR"})
    if inline_width is not None:
        _table(space, "M01", inline_width)
    doc.saveas(path)
    return {label.dxf.handle, door.dxf.handle}


def _schedule(path: Path, *, width=900):
    doc = ezdxf.new(units=4)
    _table(doc.modelspace(), "M01", width)
    doc.saveas(path)


@pytest.mark.parametrize("inline", [True, False])
def test_run_publishes_openings_with_a_stable_host_and_drawing_evidence(tmp_path, inline):
    drawing = tmp_path / "三层平面图.dxf"
    source_handles = _plan(drawing, inline_width=900 if inline else None)
    schedule_file = tmp_path / "门窗表.dxf"
    if not inline:
        _schedule(schedule_file)

    model = run(drawing)

    assert run(drawing) == model
    TumoModel.model_validate(model)
    wall, door = model["elements"]
    assert (wall["category"], door["category"], door["host"]) == ("wall", "door", wall["id"])
    assert door["geometry"]["at"] == [1250, 0]
    assert door["type"] == {"mark": "M01", "width": 900, "height": 2100}
    assert door["base_level"] == wall["base_level"] == "L3"
    assert door["source"]["sheet"] == model["sheets"][0]["id"]
    assert set(door["source"]["handles"]) == source_handles
    expected_file = drawing if inline else schedule_file
    assert any(entry.startswith(f"schedule_row:{expected_file.name}:") for entry in door["evidence"])


def test_run_reports_schedule_conflict_without_publishing_opening(tmp_path):
    drawing = tmp_path / "三层平面图.dxf"
    _plan(drawing, inline_width=900)
    _schedule(tmp_path / "门窗详图.dxf", width=1200)

    model = run(drawing)

    assert {item["category"] for item in model["elements"]} == {"wall"}
    assert any("存在不同的尺寸" in item["message"] and item["where"]["at"] == [1250, 250]
               for item in model["issues"])


def test_run_limits_companion_schedule_to_the_plan_directory(tmp_path):
    building = tmp_path / "建筑" / "1号楼"
    other = tmp_path / "建筑" / "2号楼"
    building.mkdir(parents=True)
    other.mkdir(parents=True)
    drawing = building / "三层平面图.dxf"
    _plan(drawing)
    _schedule(other / "门窗表.dxf")
    (building / "门窗表.dxf").write_text("invalid DXF", encoding="utf-8")

    model = run(tmp_path)

    assert {item["category"] for item in model["elements"]} == {"wall"}
    assert any("门窗表.dxf" in item["message"] and "读取" in item["message"]
               for item in model["issues"])


def test_run_reconnects_confirmed_split_wall_for_native_opening(tmp_path):
    drawing = tmp_path / "三层平面图.dxf"
    doc = ezdxf.new(units=4)
    space = doc.modelspace()
    for y in (-100, 100):
        space.add_line((0, y), (1200, y), dxfattribs={"layer": "WALL"})
        space.add_line((2100, y), (4000, y), dxfattribs={"layer": "WALL"})
    space.add_text("M01", dxfattribs={"insert": (1650, 250)})
    space.add_line((1200, 0), (2100, 0), dxfattribs={"layer": "A-DOOR"})
    _table(space, "M01", 900)
    doc.saveas(drawing)

    model = run(drawing)

    TumoModel.model_validate(model)
    assert run(drawing) == model
    wall, door = model["elements"]
    assert wall["geometry"] == {"kind": "line", "start": [0, 0], "end": [4000, 0]}
    assert len(wall["source"]["handles"]) == 4
    assert "opening:verified_wall_gap" in wall["evidence"]
    assert "wall_pair" not in door
    assert door["host"] == wall["id"]
    assert door["geometry"]["at"] == [1650, 0]
