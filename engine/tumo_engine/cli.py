"""Command line interface for DXF analysis, recognition, and review."""

from __future__ import annotations

import argparse
import json
import sys
from io import TextIOWrapper
from math import isfinite
from os.path import commonpath
from pathlib import Path


def _bounds(values):
    if values is None:
        return None
    if (not isinstance(values, (list, tuple)) or len(values) != 4
            or not all(type(value) in (int, float) and isfinite(value) for value in values)
            or values[0] >= values[2] or values[1] >= values[3]):
        raise ValueError("范围必须是有效的 xmin ymin xmax ymax，单位为毫米。")
    return tuple(values)


def read_job(path: Path, *, with_structural_slab=False):
    job = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(job, dict):
        raise ValueError("任务文件必须包含 JSON 对象。")
    for field in ("input", "output_dir"):
        if not isinstance(job.get(field), str) or not job[field].strip():
            raise ValueError(f"任务文件需要 {field} 路径。")
    base = (
        (path.parent / job["input"]).resolve(),
        (path.parent / job["output_dir"]).resolve(),
        _bounds(job.get("bounds")),
    )
    if not with_structural_slab:
        return base
    structural = job.get("structural_slab")
    if structural is not None and (not isinstance(structural, str) or not structural.strip()):
        raise ValueError("任务文件需要有效的 structural_slab 路径。")
    return *base, (path.parent / structural).resolve() if structural is not None else None


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


def _elevations(args):
    from tumo_engine.recognize.elevation import read_elevation_drawing

    annotations, issues = read_elevation_drawing(args.drawing, default_elevation_unit=args.unit)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    _write_json(args.output, {"units": "mm", "annotations": annotations, "issues": issues})
    print(args.output)


def _run(args):
    from tumo_engine.export.model import TumoModel
    from tumo_engine.pipeline import run

    structural_slab = args.structural_slab
    if args.input.suffix.lower() == ".json" and not args.input.is_dir():
        if args.output or args.bounds:
            raise ValueError("请在任务文件中指定输出位置和范围。")
        source, output, bounds, job_slab = read_job(args.input, with_structural_slab=True)
        if structural_slab is not None and job_slab is not None:
            raise ValueError("结构板图使用一个输入路径。")
        structural_slab = structural_slab or job_slab
    else:
        source = args.input.resolve()
        output = (args.output or Path("out")).resolve()
        bounds = _bounds(args.bounds)
    source_root = source if source.is_dir() else source.parent
    if structural_slab and not args.merge_floors:
        raise ValueError("合并结构板图时需要同时指定 --merge-floors。")
    if args.merge_floors:
        if bounds is not None:
            raise ValueError("合并楼层需要读取完整 DXF，不能同时指定单张图的范围。")
        from tumo_engine.building_pipeline import run_building

        result = run_building(source)
        if structural_slab:
            from tumo_engine.recognize.standard_floors import slab_top_floor_span
            from tumo_engine.recognize.structural_slab_merge import merge_structural_slabs

            structural_file = structural_slab.resolve()
            if not structural_file.is_file() or structural_file.suffix.lower() != ".dxf":
                raise ValueError("请选择一份结构板 DXF。")
            try:
                combined_root = Path(commonpath((source_root, structural_file.parent)))
            except ValueError as error:
                raise ValueError("建筑图与结构板图需要位于同一磁盘。") from error
            structural = run(structural_file)
            span = slab_top_floor_span(structural["sheets"][0]["title"])
            if span and span[0] < span[1]:
                structural = run(structural_file, repeat_standard_floors=True)
            def rebase(project, root):
                sheets = [{**sheet, "file": (root / sheet["file"]).relative_to(combined_root).as_posix()}
                          for sheet in project["sheets"]]
                return {**project, "sheets": sheets}

            result = merge_structural_slabs(rebase(result, source_root), rebase(structural, structural_file.parent))
            source_root = combined_root
    else:
        result = run(source, bounds=bounds, repeat_standard_floors=args.repeat_standard_floors)
    model = TumoModel.model_validate(result)
    model = model.model_dump(mode="json", exclude_unset=True)
    output.mkdir(parents=True, exist_ok=True)
    _write_json(output / "model.json", model)
    _write_json(output / "issues.json", {"issues": model["issues"]})
    _write_json(output / "sources.json", {"root": str(source_root)})
    print(output / "model.json")


def _review_inputs(args):
    from tumo_engine.export.model import TumoModel

    model = TumoModel.model_validate_json(args.model.read_text(encoding="utf-8-sig"))
    root = args.source
    source_manifest = args.model.with_name("sources.json")
    if root is None and source_manifest.is_file():
        root = Path(json.loads(source_manifest.read_text(encoding="utf-8"))["root"])
    return model.model_dump(mode="json", exclude_unset=True), root


def _render(args):
    from tumo_engine.viz.review import render

    model, root = _review_inputs(args)
    render(model, args.output, root)


def _render_dxf(args):
    from tumo_engine.viz.review_dxf import write_review_dxf

    model, root = _review_inputs(args)
    if root is None:
        raise ValueError("请用 --source 指定原始 DXF 或图纸目录。")
    sheet = next((item for item in model["sheets"] if args.sheet in (None, item["id"])), None)
    if sheet is None:
        raise ValueError("识别结果中没有选定的图纸。")
    source = root.resolve()
    if source.is_dir():
        source = (root / sheet["file"]).resolve()
        if not source.is_relative_to(root.resolve()):
            raise ValueError("图纸路径必须在原始图纸目录内。")
    print(write_review_dxf(model, source, args.output, sheet_id=sheet["id"]))


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
    elevations = commands.add_parser("elevations", help="读取立剖面标高及窗台，保留 CAD 来源。")
    elevations.add_argument("drawing", type=Path)
    elevations.add_argument("-o", "--output", type=Path, default=Path("out/elevations.json"))
    elevations.add_argument("--unit", choices=("m", "cm", "mm"), help="指定标高中省略的单位。")
    elevations.set_defaults(handler=_elevations)
    run = commands.add_parser("run", help="识别 DXF、目录或 JSON 任务。")
    run.add_argument("input", type=Path)
    run.add_argument("-o", "--output", type=Path)
    run.add_argument("--bounds", type=float, nargs=4, metavar=("XMIN", "YMIN", "XMAX", "YMAX"))
    building_mode = run.add_mutually_exclusive_group()
    building_mode.add_argument("--repeat-standard-floors", action="store_true", help="按图名和明确的楼层表复用标准层。")
    building_mode.add_argument("--merge-floors", action="store_true", help="对齐同一 DXF 中建筑平面的标高和共有轴网。")
    run.add_argument("--structural-slab", type=Path, help="在 --merge-floors 中加入轴网已配准的结构降板图。")
    run.set_defaults(handler=_run)
    render = commands.add_parser("render", help="输出 SVG 和 PNG 识别结果图。")
    render.add_argument("model", type=Path)
    render.add_argument("-o", "--output", type=Path, default=Path("out/viz"))
    render.add_argument("--source", type=Path, help="原始图纸或图纸根目录。")
    render.set_defaults(handler=_render)
    vector = commands.add_parser("render-dxf", help="导出可在 Revit 里直接导入的矢量审图 DXF。")
    vector.add_argument("model", type=Path)
    vector.add_argument("-o", "--output", type=Path, default=Path("out/review.dxf"))
    vector.add_argument("--source", type=Path, help="原始 DXF 或图纸根目录。")
    vector.add_argument("--sheet", help="选择识别结果中的图纸 ID。默认用第一张。")
    vector.set_defaults(handler=_render_dxf)
    args = parser.parse_args(argv)
    try:
        args.handler(args)
    except (OSError, ValueError) as error:
        parser.exit(1, f"图模一致：{error}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
