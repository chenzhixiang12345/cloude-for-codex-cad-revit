"""Select a plan and read title block metadata from DXF."""

from dataclasses import dataclass
from pathlib import Path
import re
from unicodedata import normalize

from ezdxf import bbox

from tumo_engine.preprocess import Bounds, layer_key, point, viewport_bounds

TITLE = re.compile(r"(?:平面图|立面图|剖面图|详图|图纸目录|图纸清单|floor\s*plan|schedule)", re.I)
STANDARD = re.compile(r"标准层|典型层|(?:[一二三四五六七八九十\d]+)\s*[-~～至—–]\s*[一二三四五六七八九十\d]+\s*层|typical", re.I)
NUMBER = re.compile(r"(?:(?:图号|DRAWING\s*NO\.?)\s*[:：]?\s*)?([A-Z]{1,3}[-.]?\d{2,5}(?:[-.]\d{1,4})?)", re.I)


@dataclass
class SheetInfo:
    title: str
    number: str | None = None
    bounds: Bounds | None = None
    scale: float | None = None


def discipline(path):
    for part in reversed(path.parts[:-1]):
        if part.lower() in {"arch", "architecture", "建筑"}:
            return "arch"
        if part.lower() in {"struct", "structure", "结构"}:
            return "struct"
    if re.search(r"结构|配筋|_[sS]\d\d", path.stem):
        return "struct"
    if re.search(r"建筑|平面图|_[aA]\d\d", path.stem):
        return "arch"
    return "unknown"


def sheet_kind(name):
    if re.search(r"(?:目录|图纸清单|index)", name, re.I):
        return "directory"
    if re.search(r"(?:基础|屋顶|机房|屋面).*(?:平面图|plan)", name, re.I):
        return "special_plan"
    if re.search(r"(?:平面图|floor\s*plan)", name, re.I):
        return "floor_plan"
    if re.search(r"(?:立面|剖面|elevation|section)", name, re.I):
        return "section_elevation"
    if re.search(r"(?:详图|detail)", name, re.I):
        return "detail"
    if re.search(r"(?:说明|notes)", name, re.I):
        return "notes"
    return "unknown"


def input_drawing(path):
    if path.is_file() and path.suffix.lower() == ".dxf":
        return path
    if not path.is_dir():
        raise ValueError("请选择 DXF 文件或包含 DXF 的样例文件夹。")
    candidates = [
        file for file in path.rglob("*")
        if file.is_file() and file.suffix.lower() == ".dxf" and "ground_truth" not in file.parts
    ]
    if not candidates:
        raise ValueError("选定文件夹中没有找到 DXF。")

    def order(file):
        label = file.stem
        subject = discipline(file.relative_to(path))
        kind = sheet_kind(label)
        return (
            kind not in {"floor_plan", "unknown"},
            subject == "struct",
            kind != "floor_plan",
            not bool(STANDARD.search(label)),
            "_A10" not in label.upper(),
            file.as_posix(),
        )

    return min(candidates, key=order)


def top_level_text(layout):
    for entity in layout.query("TEXT MTEXT ATTRIB INSERT"):
        contents = entity.attribs if entity.dxftype() == "INSERT" else [entity]
        for item in contents:
            text = normalize("NFKC", " ".join(item.plain_text().split()))
            if text:
                yield item.dxf.get("layer", "0"), item.dxf.insert, text


def _frames(layout, factor, pattern):
    for entity in layout.query("INSERT LWPOLYLINE"):
        key = layer_key(entity.dxf.layer)
        if entity.dxftype() == "INSERT":
            key += " " + entity.dxf.name.upper()
        if not re.search(pattern, key):
            continue
        # Names like title block and border distinguish paper outlines from grids.
        if entity.dxftype() == "INSERT" and not re.search(r"(?:FRAME|THAPE|BORDER|内框|图框)", key):
            continue
        extent = bbox.extents([entity], fast=True)
        if not extent.has_data:
            continue
        start, end = point(extent.extmin, factor), point(extent.extmax, factor)
        w, h = end[0] - start[0], end[1] - start[1]
        if min(w, h) > 1000 and 0.35 < w / h < 3.5:
            yield (*start, *end)


def _in_frame(pos, bounds):
    return bounds[0] <= pos[0] <= bounds[2] and bounds[1] <= pos[1] <= bounds[3]


def _compact_frames(frames):
    selected = []
    for frame in sorted(frames, key=lambda b: (b[2] - b[0]) * (b[3] - b[1])):
        center = ((frame[2] + frame[0]) / 2, (frame[3] + frame[1]) / 2)
        if not any(_in_frame(center, other) for other in selected):
            selected.append(frame)
    return selected


def _metadata(lines, name, patterns):
    titles = [(layer, text) for layer, _, text in lines if len(text) <= 100 and TITLE.search(text)]
    if titles:
        _, title = min(titles, key=lambda item: (
            not re.search(patterns["title"], layer_key(item[0])),
            not bool(STANDARD.search(item[1])),
            len(item[1]),
        ))
    else:
        title = name
    number = None
    scale = None
    for layer, _, text in lines:
        if not re.search(patterns["title"], layer_key(layer)) and not text.startswith("图号"):
            continue
        if match := NUMBER.fullmatch(text):
            number = match[1].upper()
        if match := re.fullmatch(r"1\s*:\s*(\d+(?:\.\d+)?)", text):
            scale = float(match[1]) or None
    return title, number, scale


def drawing_sheets(doc, path, factor, patterns):
    layout = doc.modelspace()
    lines = [(layer, point(pos, factor), text) for layer, pos, text in top_level_text(layout)]
    frame_infos = []
    for frame in _compact_frames(_frames(layout, factor, patterns["frame"])):
        contained = [row for row in lines if _in_frame(row[1], frame)]
        title, number, scale = _metadata(contained, "", patterns)
        if title:
            frame_infos.append(SheetInfo(title, number, frame, scale))
    if frame_infos:
        return sorted(frame_infos, key=lambda sheet: (
            sheet_kind(sheet.title) != "floor_plan",
            not bool(STANDARD.search(sheet.title)),
            sheet.number or "",
            sheet.title,
            sheet.bounds,
        ))
    paper_lines = [
        (layer, point(pos, 1), text)
        for paper in doc.layouts if not paper.is_modelspace
        for layer, pos, text in top_level_text(paper)
    ]
    title, number, scale = _metadata(lines + paper_lines, path.stem, patterns)
    return [SheetInfo(title, number, viewport_bounds(doc, factor), scale)]


def directory_entries(doc):
    """Read drawing numbers and their titles from a DXF drawing list."""
    entries = {}
    for layout in doc.layouts:
        texts = list(top_level_text(layout))
        names = [(pos, text) for _, pos, text in texts if TITLE.search(text)]
        for _, pos, text in texts:
            number = NUMBER.fullmatch(text)
            if number is None or not names:
                continue
            title_pos, title = min(names, key=lambda row: (abs(row[0].y - pos.y), abs(row[0].x - pos.x)))
            if abs(title_pos.y - pos.y) < max(1.0, abs(title_pos.x - pos.x) * 0.15):
                entries[number[1].upper()] = title
    return entries
