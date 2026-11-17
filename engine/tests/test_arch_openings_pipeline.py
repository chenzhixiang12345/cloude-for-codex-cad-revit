from pathlib import Path

import ezdxf
import pytest

from tumo_engine.export.model import TumoModel
from tumo_engine import pipeline
from tumo_engine.pipeline import run
from tumo_engine.recognize import elevation


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


def test_plan_uses_uniquely_located_window_sill_and_preserves_section_source(tmp_path):
    drawing = tmp_path / "三层平面图.dxf"
    doc = ezdxf.new(units=4)
    space = doc.modelspace()
    for y in (-100, 100):
        space.add_line((0, y), (4000, y), dxfattribs={"layer": "WALL"})
    space.add_line((800, 0), (1700, 0), dxfattribs={"layer": "A-WINDOW"})
    space.add_text("C0914", dxfattribs={"insert": (1250, 250)})
    _table(space, "C0914", 900)
    doc.saveas(drawing)
    section = ezdxf.new(units=4)
    note = section.modelspace().add_text("3F C0914 窗台高 750 mm", dxfattribs={"insert": (0, 0)})
    section.saveas(tmp_path / "剖面图.dxf")

    model = run(drawing)

    TumoModel.model_validate(model)
    [window] = [element for element in model["elements"] if element["category"] == "window"]
    assert window["sill_height"] == 750
    assert f"sill_height:剖面图.dxf:{note.dxf.handle}" in window["evidence"]
    assert not any("缺少窗台高度" in issue["message"] for issue in model["issues"])


@pytest.mark.parametrize("detail_width,hidden", [(900, False), (950, False), (900, True)])
def test_plan_applies_agreeing_window_detail_to_repeated_instances(tmp_path, monkeypatch, detail_width, hidden):
    drawing = tmp_path / "三层平面图.dxf"
    doc = ezdxf.new(units=4)
    space = doc.modelspace()
    for y in (-100, 100):
        space.add_line((0, y), (4500, y), dxfattribs={"layer": "WALL"})
    for x in (1250, 2850):
        space.add_line((x - 450, 0), (x + 450, 0), dxfattribs={"layer": "A-WINDOW"})
        space.add_text("C0914", dxfattribs={"insert": (x, 250), "layer": "AD-NUMB"})
    _table(space, "C0914", 900)
    doc.saveas(drawing)
    ezdxf.new(units=4).saveas(tmp_path / "剖面图.dxf")
    annotation = {"kind": "opening_detail", "mark": "C0914", "category": "window", "width_mm": detail_width,
                  "height_mm": 2100, "sill_height_mm": 500, "source": {"file": "剖面图.dxf", "handles": ["D1"]}}
    if hidden:
        annotation["source"]["hidden_sill_handles"] = ["D1"]
    monkeypatch.setattr(elevation, "recognize_elevation_annotations", lambda *args, **kwargs: ([annotation], []))

    model = pipeline.run(drawing)

    TumoModel.model_validate(model)
    windows = [element for element in model["elements"] if element["category"] == "window"]
    if detail_width != 900:
        assert not windows
        assert sum("门窗表和图型尺寸冲突" in entry["message"] for entry in model["issues"]) == 2
    elif hidden:
        assert len(windows) == 2
        assert all("sill_height" not in window for window in windows)
        assert sum("隐藏标注提示窗台高 500" in entry["message"] for entry in model["issues"]) == 2
    else:
        assert [window["sill_height"] for window in windows] == [500, 500]
        assert all("opening_detail:剖面图.dxf:D1" in window["evidence"] for window in windows)


def test_plan_reads_wall_opening_schedule_and_binds_its_bottom_height(tmp_path):
    drawing = tmp_path / "三层平面图.dxf"
    doc = ezdxf.new(units=4)
    space = doc.modelspace()
    for y in (-100, 100):
        space.add_line((0, y), (4000, y), dxfattribs={"layer": "WALL"})
    space.add_line((800, 0), (1400, 0), dxfattribs={"layer": "A-WALL-OPENING"})
    space.add_text("DK1", dxfattribs={"insert": (1100, 250)})
    space.add_line((2200, 0), (3100, 0), dxfattribs={"layer": "A-DOOR"})
    space.add_text("DK1", dxfattribs={"insert": (2650, 250)})
    doc.saveas(drawing)
    schedule = ezdxf.new(units=4)
    table = schedule.modelspace()
    for index, row in enumerate((("墙洞编号", "洞口尺寸", "洞底高(mm)"), ("DK1", "600×700", "1200"))):
        for x, text in zip((0, 1500, 3000), row):
            table.add_text(text, dxfattribs={"insert": (x, 1000 - 500 * index)})
    schedule.saveas(tmp_path / "墙洞口表.dxf")
    door_schedule = ezdxf.new(units=4)
    _table(door_schedule.modelspace(), "DK1", 900)
    door_schedule.saveas(tmp_path / "门窗表.dxf")

    model = run(drawing)

    TumoModel.model_validate(model)
    wall, opening, door = model["elements"]
    assert opening["category"] == "wall_opening"
    assert opening["host"] == wall["id"]
    assert opening["type"] == {"mark": "DK1", "width": 600, "height": 700}
    assert opening["sill_height"] == 1200
    assert door["category"] == "door"
    assert {entry.split(":")[1] for entry in opening["evidence"] if entry.startswith("schedule_row:")} == {"墙洞口表.dxf"}
    assert {entry.split(":")[1] for entry in door["evidence"] if entry.startswith("schedule_row:")} == {"门窗表.dxf"}
