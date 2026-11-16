"""Overlay model IDs and issue markers on a gray DXF reference."""

from __future__ import annotations

from math import cos, hypot, radians, sin
from pathlib import Path

import ezdxf
import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt
from ezdxf import units
from ezdxf.addons.drawing import Frontend, RenderContext
from ezdxf.addons.drawing.config import (
    BackgroundPolicy, ColorPolicy, Configuration, HatchPolicy, ImagePolicy, LinePolicy,
)
from ezdxf.addons.drawing.matplotlib import MatplotlibBackend
from ezdxf.addons.drawing.recorder import Recorder
from matplotlib import font_manager
from matplotlib.lines import Line2D
from matplotlib.patches import Circle, Polygon
from ezdxf.math import Matrix44

GRID = "#2563a6"
COLUMN = "#9a3412"
WALL = "#087869"
ISSUE = "#b91c1c"
BACKGROUND = "#b8bec6"


def _source_path(root, file):
    if root is None:
        return None
    if root.is_file():
        return root
    drawing = (root / file).resolve()
    if not drawing.is_relative_to(root.resolve()):
        raise ValueError("图纸路径必须在原始图纸目录内。")
    return drawing


def _background(ax, source, sheet):
    drawing = ezdxf.readfile(source)
    layout = drawing.layout(sheet["layout"]) if sheet.get("layout") else drawing.modelspace()
    unit = layout.units if sheet.get("layout") and layout.units else drawing.units
    scale = units.conversion_factor(unit, units.MM) if unit else 1
    scale = sheet.get("units_to_mm", scale)
    config = Configuration(
        color_policy=ColorPolicy.CUSTOM,
        custom_fg_color=BACKGROUND,
        background_policy=BackgroundPolicy.WHITE,
        image_policy=ImagePolicy.IGNORE,
        hatch_policy=HatchPolicy.SHOW_OUTLINE,
        line_policy=LinePolicy.SOLID,
        lineweight_scaling=.7,
    )
    recorder = Recorder()
    Frontend(RenderContext(drawing), recorder, config).draw_layout(layout)
    player = recorder.player()
    if scale != 1:
        player.transform(Matrix44.scale(scale, scale, 1))
    if sheet.get("bounds"):
        x0, y0, x1, y1 = _padded_bounds(sheet["bounds"])
        player.crop_rect((x0, y0), (x1, y1), distance=1)
    player.replay(MatplotlibBackend(ax, adjust_figure=False))


def _padded_bounds(bounds):
    x0, y0, x1, y1 = bounds
    pad = .035 * max(x1 - x0, y1 - y0)
    return x0 - pad, y0 - pad, x1 + pad, y1 + pad


def _own_sheet(item, sheet_id, first_sheet):
    return (item.get("source") or {}).get("sheet", first_sheet) == sheet_id


def _annotate(ax, label, point, color, order):
    ax.annotate(
        label, point, xytext=(2, 2), textcoords="offset points", size=6.3,
        color=color, weight="bold", zorder=order, annotation_clip=True,
        bbox={"facecolor": "white", "alpha": .82, "edgecolor": "none", "pad": .5},
    )


def _segment(ax, start, end, color, *, thickness=0, zorder=5):
    x1, y1 = start
    x2, y2 = end
    length = hypot(x2 - x1, y2 - y1)
    if thickness and length:
        dx, dy = (y1 - y2) * thickness / (2 * length), (x2 - x1) * thickness / (2 * length)
        path = [(x1 + dx, y1 + dy), (x2 + dx, y2 + dy), (x2 - dx, y2 - dy), (x1 - dx, y1 - dy)]
        ax.add_patch(Polygon(path, facecolor=color, edgecolor=color, linewidth=.6, alpha=.55, zorder=zorder))
    else:
        ax.plot((x1, x2), (y1, y2), color=color, linewidth=.85, linestyle=(0, (5, 3)), zorder=zorder)
    return ((x1 + x2) / 2, (y1 + y2) / 2)


def _element(ax, element):
    category = element["category"]
    color = COLUMN if category == "column" else WALL
    geo = element["geometry"]
    if geo["kind"] == "line":
        point = _segment(ax, geo["start"], geo["end"], color,
                         thickness=(element.get("type") or {}).get("thickness") or 0)
    elif geo["kind"] == "polygon":
        points = geo["points"]
        ax.add_patch(Polygon(points, facecolor=color, edgecolor=color, linewidth=.9, alpha=.65, zorder=6))
        point = tuple(sum(coordinate) / len(points) for coordinate in zip(*points))
    elif geo["kind"] == "point":
        point = geo["at"]
        outline = element.get("type") or {}
        width, depth = outline.get("width"), outline.get("depth")
        diameter = outline.get("diameter")
        if category == "column" and diameter:
            circle = Circle(point, diameter / 2, facecolor=color, edgecolor=color,
                            linewidth=.9, alpha=.65, zorder=6)
            circle.set_gid(f"outline-{element['id']}")
            ax.add_patch(circle)
        elif category == "column" and width and depth:
            angle = radians(geo.get("rotation_deg") or 0)
            corners = [(-width/2, -depth/2), (width/2, -depth/2), (width/2, depth/2), (-width/2, depth/2)]
            points = [(point[0] + x*cos(angle) - y*sin(angle), point[1] + x*sin(angle) + y*cos(angle))
                      for x, y in corners]
            ax.add_patch(Polygon(points, facecolor=color, edgecolor=color, linewidth=.9, alpha=.65, zorder=6))
        else:
            ax.scatter(*point, s=20, color=color, zorder=6)
    else:
        return
    _annotate(ax, element["id"], point, color, 7)
    return point


def _draw_sheet(model, sheet, output, source):
    first = model["sheets"][0]["id"]
    identifier = sheet["id"]
    cjk = next((font for font in ("Microsoft YaHei", "Noto Sans CJK SC", "SimHei")
                if font in font_manager.get_font_names()), None)
    labels = ("轴网", "柱", "墙", "疑点") if cjk else ("Grid", "Column", "Wall", "Issue")
    legend = [Line2D([], [], color=color, lw=2, label=label)
              for color, label in zip((GRID, COLUMN, WALL, ISSUE), labels)]
    with plt.rc_context({"svg.fonttype": "none", "font.family": cjk or "DejaVu Sans"}):
        fig, ax = plt.subplots(figsize=(16, 12), layout="constrained")
        try:
            if source is not None:
                _background(ax, source, sheet)
            points = {}
            for grid in model["grids"]:
                if _own_sheet(grid, identifier, first):
                    points[grid["id"]] = _segment(ax, grid["start"], grid["end"], GRID, zorder=4)
                    _annotate(ax, f"{grid['id']} / {grid['label']}", grid["end"], GRID, 7)
            for element in model["elements"]:
                if _own_sheet(element, identifier, first):
                    points[element["id"]] = _element(ax, element)
            for issue in model["issues"]:
                where = issue.get("where") or {}
                element_id = issue.get("element")
                explicit_sheet = where.get("sheet")
                issue_sheet = explicit_sheet or (identifier if element_id in points else None)
                if issue_sheet != identifier:
                    continue
                point = where.get("at") or points.get(element_id)
                if point is not None:
                    ax.scatter(*point, marker="x", color=ISSUE, linewidth=1.4, s=24, zorder=8)
                    _annotate(ax, issue["id"], point, ISSUE, 9)
            if sheet.get("bounds"):
                x0, y0, x1, y1 = _padded_bounds(sheet["bounds"])
                ax.set(xlim=(x0, x1), ylim=(y0, y1))
            else:
                ax.autoscale()
            ax.set_aspect("equal", adjustable="box")
            ax.set_axis_off()
            fig.legend(handles=legend, loc="outside lower center", ncols=4, frameon=False)
            title = sheet.get("title") if cjk else None
            fig.suptitle(f"{identifier}  {title or ''}".rstrip(), fontsize=15)
            for extension in ("svg", "png"):
                fig.savefig(output / f"{identifier}.{extension}", dpi=175, facecolor="white")
        finally:
            plt.close(fig)


def render(model: dict, output: Path, source_root: Path | None = None):
    output.mkdir(parents=True, exist_ok=True)
    used = {item["source"]["sheet"] for item in (*model["elements"], *model["grids"])
            if item.get("source")}
    used.update(issue["where"]["sheet"] for issue in model["issues"]
                if (issue.get("where") or {}).get("sheet"))
    for index, sheet in enumerate(model["sheets"]):
        if index and sheet["id"] not in used:
            continue
        identifier = sheet["id"]
        if identifier in (".", "..") or any(char in identifier for char in '/\\:*?"<>|'):
            raise ValueError(f"图纸 ID 含有不能用于文件名的字符：{identifier}")
        source = _source_path(source_root, sheet["file"])
        _draw_sheet(model, sheet, output, source)
