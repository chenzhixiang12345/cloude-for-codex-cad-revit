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


def read_job(path: Path, *, include_structural=False):
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
    if not include_structural:
        return base
    structural = {}
    for key in ("structural_slab", "structural_columns", "structural_beams"):
        if (value := job.get(key)) is not None:
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"任务文件需要有效的 {key} 路径。")
            structural[key] = (path.parent / value).resolve()
    choices = {}
    for key in ("column_sheet", "beam_sheet"):
        if (value := job.get(key)) is not None:
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"任务文件需要有效的 {key} 图号。")
            choices[key] = value.strip()
    return *base, structural, choices


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


def _structural_evidence(args):
    from dataclasses import asdict

    from tumo_engine.io.dxf_reader import read_dxf
    from tumo_engine.preprocess import bind_xrefs, primitives, unit_factor
    from tumo_engine.rules import defaults
    from tumo_engine.sheets import drawing_sheets
    from tumo_engine.tables.beams import read_beam_candidates
    from tumo_engine.tables.columns import read_column_schedules

    source = args.drawing.resolve()
    drawing = read_dxf(source)
    xref_warnings = bind_xrefs(drawing, source, source.parent)
    rules = defaults()
    metadata = {}
    if args.category == "columns":
        candidates, issues = read_column_schedules(drawing, rules=rules)
        if args.sheet:
            candidates = [candidate for candidate in candidates if candidate["sheet"]["number"] == args.sheet]
            issues = [issue for issue in issues if issue["sheet"]["number"] == args.sheet]
    else:
        factor = unit_factor(drawing)
        sheets = drawing_sheets(drawing, source, factor, rules["layers"])
        selected = [sheet for sheet in sheets if (not args.sheet or sheet.number == args.sheet) and sheet.bounds]
        if len(selected) != 1:
            raise ValueError("梁证据需要唯一图框。请用 --sheet 指定梁图图号。")
        sheet = selected[0]
        items, warnings = primitives(drawing, factor, sheet.bounds)
        xref_warnings.extend(warnings)
        beams, notes = read_beam_candidates(drawing, items, rules)
        candidates, issues = [asdict(beam) for beam in beams], [asdict(note) for note in notes]
        metadata["sheet"] = asdict(sheet)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    _write_json(args.output, {"units": "mm", "category": args.category, "source": source.name, **metadata,
                              "candidates": candidates, "issues": issues, "warnings": list(dict.fromkeys(xref_warnings))})
    print(args.output)


def _run(args):
    from tumo_engine.export.model import TumoModel
    from tumo_engine.pipeline import run

    structural = {key: getattr(args, key) for key in ("structural_slab", "structural_columns", "structural_beams")
                  if getattr(args, key) is not None}
    choices = {key: getattr(args, key) for key in ("column_sheet", "beam_sheet") if getattr(args, key) is not None}
    if args.input.suffix.lower() == ".json" and not args.input.is_dir():
        if args.output or args.bounds:
            raise ValueError("请在任务文件中指定输出位置和范围。")
        source, output, bounds, drawings, job_choices = read_job(args.input, include_structural=True)
        if structural.keys() & drawings.keys() or choices.keys() & job_choices.keys():
            raise ValueError("每种结构图只使用一个输入路径和图号。")
        structural.update(drawings)
        choices.update(job_choices)
    else:
        source = args.input.resolve()
        output = (args.output or Path("out")).resolve()
        bounds = _bounds(args.bounds)
    source_root = source if source.is_dir() else source.parent
    if structural and not args.merge_floors:
        raise ValueError("合并结构图时需要同时指定 --merge-floors。")
    for drawing, sheet in (("structural_columns", "column_sheet"), ("structural_beams", "beam_sheet")):
        if (drawing in structural) != (sheet in choices):
            raise ValueError("合并柱梁时需要同时指定结构图和对应图号。")
    if args.merge_floors:
        if bounds is not None or args.sheet is not None:
            raise ValueError("合并楼层需要读取完整 DXF，不能同时指定单张图的范围或图号。")
        from tumo_engine.building_pipeline import run_building

        result = run_building(source)
        if structural:
            from tumo_engine.recognize.standard_floors import slab_top_floor_span
            from tumo_engine.recognize.structural_framing_merge import merge_structural_framing
            from tumo_engine.recognize.structural_slab_merge import merge_structural_slabs

            drawing_paths = {key: value.resolve() for key, value in structural.items()}
            if any(not path.is_file() or path.suffix.lower() != ".dxf" for path in drawing_paths.values()):
                raise ValueError("请选择有效的结构 DXF。")
            try:
                combined_root = Path(commonpath((source_root, *(path.parent for path in drawing_paths.values()))))
            except ValueError as error:
                raise ValueError("建筑图与结构图需要位于同一磁盘。") from error
            def rebase(project, root):
                sheets = [{**sheet, "file": (root / sheet["file"]).relative_to(combined_root).as_posix()}
                          for sheet in project["sheets"]]
                return {**project, "sheets": sheets}

            result = rebase(result, source_root)
            if slab_file := drawing_paths.get("structural_slab"):
                slab = run(slab_file)
                span = slab_top_floor_span(slab["sheets"][0]["title"])
                if span and span[0] < span[1]:
                    slab = run(slab_file, repeat_standard_floors=True)
                result = merge_structural_slabs(result, rebase(slab, slab_file.parent))
            for key, sheet, repeat in (("structural_columns", "column_sheet", True),
                                       ("structural_beams", "beam_sheet", False)):
                if framing_file := drawing_paths.get(key):
                    framing = run(framing_file, sheet_number=choices[sheet], repeat_standard_floors=repeat)
                    result = merge_structural_framing(result, rebase(framing, framing_file.parent))
            source_root = combined_root
    else:
        result = run(source, bounds=bounds, repeat_standard_floors=args.repeat_standard_floors, sheet_number=args.sheet)
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
    evidence = commands.add_parser("structural-evidence", help="输出保留 CAD 句柄的柱表和梁候选证据。")
    evidence.add_argument("category", choices=("columns", "beams"))
    evidence.add_argument("drawing", type=Path)
    evidence.add_argument("--sheet", help="按图号选择图框。多图框梁图必须指定。")
    evidence.add_argument("-o", "--output", type=Path, default=Path("out/structural-evidence.json"))
    evidence.set_defaults(handler=_structural_evidence)
    run = commands.add_parser("run", help="识别 DXF、目录或 JSON 任务。")
    run.add_argument("input", type=Path)
    run.add_argument("-o", "--output", type=Path)
    run.add_argument("--bounds", type=float, nargs=4, metavar=("XMIN", "YMIN", "XMAX", "YMAX"))
    run.add_argument("--sheet", help="按原图号选定 DXF 内的一张图。")
    building_mode = run.add_mutually_exclusive_group()
    building_mode.add_argument("--repeat-standard-floors", action="store_true", help="按图名和明确的楼层表复用标准层。")
    building_mode.add_argument("--merge-floors", action="store_true", help="对齐同一 DXF 中建筑平面的标高和共有轴网。")
    run.add_argument("--structural-slab", type=Path, help="在 --merge-floors 中加入轴网已配准的结构降板图。")
    run.add_argument("--structural-columns", type=Path, help="合并带柱表的结构墙柱 DXF。")
    run.add_argument("--column-sheet", help="合并结构柱使用的墙柱图号。")
    run.add_argument("--structural-beams", type=Path, help="合并带梁顶依据的结构梁 DXF。")
    run.add_argument("--beam-sheet", help="合并结构梁使用的梁平面图号。")
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
