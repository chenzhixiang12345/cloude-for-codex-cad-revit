"""Read a DXF and report the information available for recognition."""

from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

import ezdxf
from ezdxf import units as dxf_units
from ezdxf.document import Drawing
from ezdxf.entities import DXFEntity


TEXT_TYPES = {"TEXT", "MTEXT", "ATTRIB", "ATTDEF"}
UNIT_LABELS = {1: "英寸", 2: "英尺", 4: "毫米", 5: "厘米", 6: "米"}


@dataclass
class LayerSummary:
    layout_entities: Counter[str] = field(default_factory=Counter)
    block_entities: Counter[str] = field(default_factory=Counter)
    text_samples: list[str] = field(default_factory=list)


@dataclass
class DrawingSummary:
    layers: dict[str, LayerSummary]
    blocks: Counter[str] = field(default_factory=Counter)
    external_refs: set[str] = field(default_factory=set)
    text_count: int = 0
    proxy_count: int = 0

    def add(self, entity: DXFEntity, *, in_definition: bool = False) -> None:
        kind = entity.dxftype()
        layer_name = entity.dxf.layer if entity.dxf.is_supported("layer") else "0"
        layer = self.layers.setdefault(layer_name, LayerSummary())
        counts = layer.block_entities if in_definition else layer.layout_entities
        counts[kind] += 1

        if kind == "INSERT":
            self.blocks[entity.dxf.name] += 1
            for attrib in entity.attribs:
                self.add(attrib, in_definition=in_definition)
        elif kind in TEXT_TYPES:
            content = " ".join(entity.plain_text().split())
            if content:
                self.text_count += 1
                sample = content[:96] + ("…" if len(content) > 96 else "")
                if len(layer.text_samples) < 5 and sample not in layer.text_samples:
                    layer.text_samples.append(sample)
        elif kind == "ACAD_PROXY_ENTITY":
            self.proxy_count += 1


def read_dxf(path: Path) -> Drawing:
    path = Path(path)
    if path.suffix.lower() != ".dxf":
        raise ValueError(f"需要 DXF 文件，请先把 {path.name} 另存为 DXF。")
    try:
        return ezdxf.readfile(path)
    except (OSError, ezdxf.DXFError, UnicodeError) as error:
        raise ValueError(f"无法读取 DXF 文件 {path.name}：{error}") from error


def _entity_types(counts: Counter[str]) -> str:
    ordered = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    return "、".join(f"{name} × {count}" for name, count in ordered) or "—"


def _cell(value: str) -> str:
    return (value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace("|", "&#124;").replace("\\", "&#92;")
            .replace("[", "&#91;").replace("]", "&#93;").replace(chr(96), "&#96;"))


def _unit_label(code: int) -> str:
    if code in UNIT_LABELS:
        return f"{UNIT_LABELS[code]}（$INSUNITS={code}）"
    if code <= 0:
        return f"未指定绘图单位（$INSUNITS={code}）"
    try:
        unit = dxf_units.decode(code)
    except IndexError:
        unit = None
    return f"{unit or '未知绘图单位'}（$INSUNITS={code}）"


def analyze(path: Path) -> str:
    """Return a Chinese Markdown inventory of layouts, entities, and missing data."""
    path = Path(path)
    doc = read_dxf(path)
    summary = DrawingSummary({layer.dxf.name: LayerSummary() for layer in doc.layers})
    layout_counts: list[tuple[str, int]] = []
    viewports: list[tuple[str, int, int, float]] = []

    # Block definitions are listed separately from their placements.
    for layout in doc.layouts:
        layout_counts.append((layout.name, len(layout)))
        for entity in layout:
            summary.add(entity)
            # ID 1 is the paperspace canvas itself. Other viewports show modelspace.
            if entity.dxftype() == "VIEWPORT" and entity.dxf.id > 1:
                viewports.append((layout.name, entity.dxf.id, entity.dxf.status, entity.get_scale()))

    for block in doc.blocks:
        if block.is_any_layout:
            continue
        if block.block.is_xref or block.block.is_xref_overlay:
            summary.external_refs.add(block.name)
        for entity in block:
            summary.add(entity, in_definition=True)

    total_layout_entities = sum(sum(layer.layout_entities.values()) for layer in summary.layers.values())
    proxy_note = ("未检测到代理实体。"
                  if not summary.proxy_count
                  else f"检测到 {summary.proxy_count} 个代理实体。"
                       "如果图纸来自天正 CAD，请先导出为 T3 格式，再另存为 DXF。")
    lines = [
        f"# DXF 图层分析：{_cell(path.name)}",
        "",
        "## 这张图有什么",
        "",
        f"- DXF 版本：{doc.dxfversion}",
        f"- 绘图单位：{_unit_label(doc.units)}",
        f"- 布局图元：{total_layout_entities} 个（含附加的块属性）。",
        f"- 可直接读取的文字和块属性：{summary.text_count} 个（含块定义）。",
        f"- {proxy_note}",
        "",
        "## 这张图缺什么",
        "",
    ]
    if doc.units <= 0:
        lines.append("- 未指定绘图单位。识别前需从标注或项目设置确认毫米换算。")
    if not summary.text_count:
        lines.append("- 没有可直接读取的文字，图名和轴号等信息可能无法从文字识别。")
    if not viewports:
        lines.append("- 没有可供分析的布局视口。图框可能直接画在模型空间。")
    if not total_layout_entities:
        lines.append("- 模型和图纸布局都没有可读取的图元。")
    if summary.external_refs:
        lines.append("- 图纸含外部参照。参照中的完整内容取决于对应文件是否可用。")
    if summary.proxy_count:
        lines.append("- 代理实体的业务属性可能缺失，需核对 T3 导出结果。")
    if lines[-1] == "":
        lines.append("- 基础信息均有记录。构件位置、编号和尺寸仍需识别后核对。")

    lines.extend(["", "## 布局和视口", "",
                  "| 布局 | 图元数 |", "| --- | ---: |"])
    for name, count in layout_counts:
        lines.append(f"| {_cell(name)} | {count} |")
    if viewports:
        lines.extend(["", "| 布局 | 视口 ID | 状态值 | 视口与模型高度比 |",
                      "| --- | ---: | ---: | --- |"])
        for name, viewport_id, status, scale in viewports:
            ratio = f"1:{1 / scale:.6g}" if scale > 0 else "未定义"
            lines.append(f"| {_cell(name)} | {viewport_id} | {status} | {ratio} |")

    lines.extend([
        "", "## 图层", "",
        "| 图层 | 布局图元数 | 布局图元类型 | 块定义内类型 | 文字样例 |",
        "| --- | ---: | --- | --- | --- |",
    ])
    for name, layer in sorted(summary.layers.items(), key=lambda item: item[0].casefold()):
        layout_total = sum(layer.layout_entities.values())
        text_samples = _cell(" / ".join(layer.text_samples)) or "—"
        lines.append(f"| {_cell(name)} | {layout_total} | {_entity_types(layer.layout_entities)} | "
                     f"{_entity_types(layer.block_entities)} | {text_samples} |")

    lines.extend(["", "## 块和外部参照", ""])
    if summary.blocks:
        lines.extend(["| 块名 | 引用次数（含嵌套引用） |", "| --- | ---: |"])
        for name, count in sorted(summary.blocks.items(), key=lambda item: item[0].casefold()):
            lines.append(f"| {_cell(name)} | {count} |")
    else:
        lines.append("未发现块引用。")
    if summary.external_refs:
        lines.extend(["", "外部参照：" + "、".join(_cell(name) for name in sorted(summary.external_refs))])
    return "\n".join(lines) + "\n"
