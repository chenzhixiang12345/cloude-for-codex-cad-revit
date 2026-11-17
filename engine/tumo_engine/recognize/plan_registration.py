"""Register plan sheets with a translation verified against their shared CAD grids."""

from collections import defaultdict
from math import hypot, isfinite
from statistics import median
from unicodedata import normalize

from tumo_engine.recognize.geometry import direction, dot, parallel


_LABEL_TRACE = "axis:label:"
_ANGLE_TOLERANCE_DEG = 0.01
POSITION_TOLERANCE_MM = 1


def group_grids_by_original_label(grids):
    """Group CAD axis segments by source label. A conflicting label includes a None entry."""
    by_label = defaultdict(list)
    for grid in grids:
        recorded = {
            trace.removeprefix(_LABEL_TRACE)
            for trace in grid.get("evidence", ())
            if trace.startswith(_LABEL_TRACE)
        }
        labels = {normalize("NFKC", name).strip().upper() for name in recorded or (grid["label"],)} - {""}
        for label in labels:
            by_label[label].append(grid if len(labels) == 1 else None)
    return by_label


def _axis_segment(grid):
    if grid is None:
        return None
    start, end = grid.get("start"), grid.get("end")
    if any(not isinstance(point, (tuple, list)) or len(point) != 2
           or any(type(value) not in (int, float) or not isfinite(value) for value in point)
           for point in (start, end)):
        return None
    length = hypot(end[0] - start[0], end[1] - start[1])
    if not isfinite(length) or length < POSITION_TOLERANCE_MM:
        return None
    return start, end, direction(start, end)


def _match_segments(anchor, local):
    if len(anchor) != len(local):
        return None
    source = [_axis_segment(grid) for grid in local]
    targets = [_axis_segment(grid) for grid in anchor]
    if None in (*source, *targets):
        return None
    pairs = []
    for start, end, axis in targets:
        candidates = [entry for entry in source if parallel(axis, entry[2], _ANGLE_TOLERANCE_DEG)]
        if len(candidates) != 1:
            return None
        local_start, local_end, local_axis = candidates[0]
        source.remove(candidates[0])
        if dot(axis, local_axis) < 0:
            local_start, local_end = local_end, local_start
        pairs.append((axis, (start, end), (local_start, local_end)))
    return pairs


def register_plan(anchor_grids, local_grids):
    """Return the local-to-anchor translation and its diagnostic, or a refusal."""
    anchor_by_label = group_grids_by_original_label(anchor_grids)
    local_by_label = group_grids_by_original_label(local_grids)
    shared = sorted(anchor_by_label.keys() & local_by_label.keys())
    if len(shared) < 3:
        return None, f"共同轴号只有 {len(shared)} 个，配准至少需要 3 个。"

    axes, shifts = [], []
    for label in shared:
        pairs = _match_segments(anchor_by_label[label], local_by_label[label])
        if not pairs:
            return None, f"共同轴号 {label} 的轴段方向或数量不一致，无法确认平移。"
        for axis, targets, sources in pairs:
            axes.append(axis)
            shifts.extend((label, target[0] - source[0], target[1] - source[1])
                          for target, source in zip(targets, sources))
    if all(parallel(axes[0], axis, _ANGLE_TOLERANCE_DEG) for axis in axes[1:]):
        return None, "共同轴网只提供一个方向，无法确认平移。"

    dx = round(median(x for _, x, _ in shifts), 3)
    dy = round(median(y for _, _, y in shifts), 3)
    if not isfinite(dx) or not isfinite(dy):
        return None, "共同轴网的坐标差超出有效范围。"
    error, label = max((hypot(x - dx, y - dy), name) for name, x, y in shifts)
    if error > POSITION_TOLERANCE_MM:
        return None, f"共同轴号 {label} 的轴段端点不能重合，偏差 {error:.3f} mm。"
    return (
        {"dx": dx, "dy": dy, "rot_deg": 0},
        f"共同轴号 {len(shared)} 个，轴段 {len(axes)} 段，最大端点偏差 {error:.3f} mm。",
    )
