"""Create a synthetic floor plan and hand-coded reference for end-to-end tests."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import ezdxf

from eval.score import compare
from tumo_engine.export.model import TumoModel

DRAWING = Path("arch/standard-floor.dxf")
TRUTH = Path("ground_truth/model.json")
RESULT = Path("recognized/model.json")
SHEET = "A901"
TITLE = "三层建筑平面图"


def _draw(path: Path) -> None:
    doc = ezdxf.new(units=4)
    model = doc.modelspace()
    for layer in ("TITLE", "GRID", "GRID-BUBBLE", "COLUMN", "COLUMN-ID", "WALL", "楼层表"):
        doc.layers.new(layer)

    model.add_text(TITLE, dxfattribs={"insert": (0, -1800), "layer": "TITLE"})
    model.add_text("图号 A901", dxfattribs={"insert": (4200, -1800), "layer": "TITLE"})
    for label, start, end in (
        ("1", (0, -1000), (0, 5000)),
        ("2", (6000, -1000), (6000, 5000)),
        ("A", (-1000, 0), (7000, 0)),
        ("B", (-1000, 4000), (7000, 4000)),
    ):
        model.add_line(start, end, dxfattribs={"layer": "GRID"})
        model.add_circle(start, 200, dxfattribs={"layer": "GRID-BUBBLE"})
        model.add_text(label, dxfattribs={"insert": start, "layer": "GRID-BUBBLE"})

    block = doc.blocks.new("PILLAR")
    block.add_lwpolyline(
        [(-250, -200), (250, -200), (250, 200), (-250, 200)], close=True,
    )
    model.add_blockref("PILLAR", (0, 0), dxfattribs={"layer": "COLUMN"})
    model.add_text("KZ1", dxfattribs={"insert": (240, 100), "layer": "COLUMN-ID"})
    model.add_circle((6000, 4000), 225, dxfattribs={"layer": "COLUMN"})
    model.add_text("KZ2", dxfattribs={"insert": (6225, 4000), "layer": "COLUMN-ID"})

    for y in (-100, 100):
        for start, end in ((270, 2400), (2430, 5200)):
            model.add_line((start, y), (end, y), dxfattribs={"layer": "WALL"})

    for y, row in (
        (3400, ("楼层", "建筑标高", "结构标高")),
        (3100, ("3F", "7.200", "7.150")),
        (2800, ("4F", "10.200", "10.150")),
    ):
        for x, cell in zip((100, 2000, 4200), row):
            model.add_text(cell, dxfattribs={"insert": (x, y), "layer": "楼层表"})
    doc.saveas(path)


def _truth() -> dict:
    source = {"sheet": SHEET, "handles": []}
    return {
        "schema_version": "0.1",
        "project": {"name": "合成标准层", "units": "mm", "origin": {"grid": ["1", "A"], "at": [0, 0]}},
        "sheets": [{
            "id": SHEET, "file": DRAWING.as_posix(), "discipline": "arch",
            "kind": "floor_plan", "title": TITLE, "levels": ["L3", "L4"],
        }],
        "levels": [
            {"id": "L3", "name": "3F", "elevation_arch": 7200, "elevation_struct": 7150},
            {"id": "L4", "name": "4F", "elevation_arch": 10200, "elevation_struct": 10150},
        ],
        "grids": [
            {"id": "G-1", "label": "1", "start": [0, -1000], "end": [0, 5000], "confidence": 1, "source": source},
            {"id": "G-2", "label": "2", "start": [6000, -1000], "end": [6000, 5000], "confidence": 1, "source": source},
            {"id": "G-A", "label": "A", "start": [-1000, 0], "end": [7000, 0], "confidence": 1, "source": source},
            {"id": "G-B", "label": "B", "start": [-1000, 4000], "end": [7000, 4000], "confidence": 1, "source": source},
        ],
        "elements": [
            {
                "id": "C-0001", "category": "column",
                "type": {"width": 500, "depth": 400, "mark": "KZ1"},
                "geometry": {"kind": "point", "at": [0, 0]},
                "base_level": "L3", "top_level": "L4", "confidence": 1, "source": source,
            },
            {
                "id": "C-0002", "category": "column",
                "type": {"diameter": 450, "mark": "KZ2"},
                "geometry": {"kind": "point", "at": [6000, 4000]},
                "base_level": "L3", "top_level": "L4", "confidence": 1, "source": source,
            },
            {
                "id": "W-0001", "category": "wall",
                "type": {"thickness": 200},
                "geometry": {"kind": "line", "start": [250, 0], "end": [5200, 0]},
                "base_level": "L3", "top_level": "L4", "confidence": 1, "source": source,
            },
        ],
        "relations": [{"kind": "join", "a": "W-0001", "b": "C-0001", "priority": "column_cuts_wall"}],
        "issues": [],
    }


def write_fixture(directory: Path) -> Path:
    """Write the drawing, reference model and portable job; return the job path."""
    drawing = directory / DRAWING
    truth = directory / TRUTH
    drawing.parent.mkdir(parents=True, exist_ok=True)
    truth.parent.mkdir(parents=True, exist_ok=True)
    _draw(drawing)
    model = TumoModel.model_validate(_truth())
    truth.write_text(model.model_dump_json(indent=2, exclude_unset=True) + "\n", encoding="utf-8")
    job = directory / "job.json"
    job.write_text(json.dumps({"input": ".", "output_dir": RESULT.parent.as_posix()}) + "\n",
                   encoding="utf-8")
    return job


def check_fixture(directory: Path) -> dict:
    actual = json.loads((directory / RESULT).read_text(encoding="utf-8-sig"))
    expected = json.loads((directory / TRUTH).read_text(encoding="utf-8-sig"))
    return compare(actual, expected)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="生成或检查合成标准层验收样例。")
    parser.add_argument("directory", type=Path, help="合成图纸、任务和识别结果所在的目录。")
    parser.add_argument("--check", action="store_true", help="对比已有识别结果与合成标准答案。")
    args = parser.parse_args(argv)
    if not args.check:
        print(write_fixture(args.directory))
        return 0

    expected_categories = {"column", "grid", "level", "wall"}
    scores = check_fixture(args.directory)
    print(json.dumps(scores, ensure_ascii=False, indent=2))
    return int(set(scores) != expected_categories or any(
        values[key] != 1
        for values in scores.values()
        for key in ("precision", "recall", "attribute_accuracy")
    ))


if __name__ == "__main__":
    raise SystemExit(main())
