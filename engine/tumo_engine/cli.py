"""Command line interface for DXF analysis, recognition, and review."""

from __future__ import annotations

import argparse
import json
import sys
from io import TextIOWrapper
from math import isfinite
from pathlib import Path


def _bounds(values):
    if values is None:
        return None
    if (not isinstance(values, (list, tuple)) or len(values) != 4
            or not all(type(value) in (int, float) and isfinite(value) for value in values)
            or values[0] >= values[2] or values[1] >= values[3]):
        raise ValueError("范围必须是有效的 xmin ymin xmax ymax，单位为毫米。")
    return tuple(values)


def read_job(path: Path):
    job = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(job, dict):
        raise ValueError("任务文件必须包含 JSON 对象。")
    for field in ("input", "output_dir"):
        if not isinstance(job.get(field), str) or not job[field].strip():
            raise ValueError(f"任务文件需要 {field} 路径。")
    return (
        (path.parent / job["input"]).resolve(),
        (path.parent / job["output_dir"]).resolve(),
        _bounds(job.get("bounds")),
    )


def _write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
                    encoding="utf-8")


def _analyze(args):
    from tumo_engine.io.dxf_reader import analyze

    report = analyze(args.drawing)
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(report, encoding="utf-8")
    else:
        print(report)


def _schedule(args):
    from dataclasses import asdict

    from tumo_engine.io.dxf_reader import read_dxf
    from tumo_engine.preprocess import bind_xrefs, primitives, unit_factor
    from tumo_engine.tables import read_opening_schedule

    source = args.drawing.resolve()
    drawing = read_dxf(source)
    warnings = bind_xrefs(drawing, source, source.parent)
    items, parsing_warnings = primitives(drawing, unit_factor(drawing))
    rows = read_opening_schedule(items)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    _write_json(args.output, {
        "units": "mm",
        "openings": [asdict(row) for row in rows],
        "warnings": list(dict.fromkeys([*warnings, *parsing_warnings])),
    })
    print(args.output)


def _run(args):
    from tumo_engine.export.model import TumoModel
    from tumo_engine.pipeline import run

    if args.input.suffix.lower() == ".json" and not args.input.is_dir():
        if args.output or args.bounds:
            raise ValueError("请在任务文件中指定输出位置和范围。")
        source, output, bounds = read_job(args.input)
    else:
        source = args.input.resolve()
        output = (args.output or Path("out")).resolve()
        bounds = _bounds(args.bounds)
    model = TumoModel.model_validate(run(source, bounds=bounds)).model_dump(mode="json", exclude_unset=True)
    output.mkdir(parents=True, exist_ok=True)
    _write_json(output / "model.json", model)
    _write_json(output / "issues.json", {"issues": model["issues"]})
    _write_json(output / "sources.json", {"root": str(source if source.is_dir() else source.parent)})
    print(output / "model.json")


def _render(args):
    from tumo_engine.export.model import TumoModel
    from tumo_engine.viz.review import render

    model = TumoModel.model_validate_json(args.model.read_text(encoding="utf-8-sig"))
    root = args.source
    source_manifest = args.model.with_name("sources.json")
    if root is None and source_manifest.is_file():
        root = Path(json.loads(source_manifest.read_text(encoding="utf-8"))["root"])
    render(model.model_dump(mode="json", exclude_unset=True), args.output, root)


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        if isinstance(stream, TextIOWrapper):
            stream.reconfigure(encoding="utf-8", errors="backslashreplace")
    parser = argparse.ArgumentParser(description="图模一致 DXF 识别引擎")
    commands = parser.add_subparsers(required=True)
    analyze = commands.add_parser("analyze", help="检查 DXF 图层、文字、块和布局。")
    analyze.add_argument("drawing", type=Path)
    analyze.add_argument("-o", "--output", type=Path, help="保存中文报告。")
    analyze.set_defaults(handler=_analyze)
    schedule = commands.add_parser("schedule", help="读取门窗表，并输出毫米尺寸和来源句柄。")
    schedule.add_argument("drawing", type=Path)
    schedule.add_argument("-o", "--output", type=Path, default=Path("out/opening-schedule.json"))
    schedule.set_defaults(handler=_schedule)
    run = commands.add_parser("run", help="识别 DXF、目录或 JSON 任务。")
    run.add_argument("input", type=Path)
    run.add_argument("-o", "--output", type=Path)
    run.add_argument("--bounds", type=float, nargs=4, metavar=("XMIN", "YMIN", "XMAX", "YMAX"))
    run.set_defaults(handler=_run)
    render = commands.add_parser("render", help="输出 SVG 和 PNG 识别结果图。")
    render.add_argument("model", type=Path)
    render.add_argument("-o", "--output", type=Path, default=Path("out/viz"))
    render.add_argument("--source", type=Path, help="原始图纸或图纸根目录。")
    render.set_defaults(handler=_render)
    args = parser.parse_args(argv)
    try:
        args.handler(args)
    except (OSError, ValueError) as error:
        parser.exit(1, f"图模一致：{error}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
