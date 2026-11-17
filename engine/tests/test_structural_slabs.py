import ezdxf
import pytest

from tumo_engine.preprocess import primitives


def _rectangle(x, y, width, height):
    return [(x, y), (x + width, y), (x + width, y + height), (x, y + height)]


def _fill(layout, layer, points, pattern="FLEX", scale=750, angle=0, *, hole=False):
    hatch = layout.add_hatch(dxfattribs={"layer": layer})
    hatch.set_pattern_fill(pattern, scale=scale, angle=angle)
    hatch.paths.add_polyline_path(points, is_closed=True, flags=1)
    if hole:
        x, y = points[0]
        hatch.paths.add_polyline_path(_rectangle(x + 30, y + 30, 20, 30), is_closed=True)
    return hatch


def _legend(drawing, y=4000, text="填充区域板顶标高为 Hs-0.500,"):
    layer = "S_PLAN_TEXT"
    points = _rectangle(450, y + 40, 620, 360)
    outline = drawing.add_lwpolyline(points, close=True, dxfattribs={"layer": layer})
    hatch = _fill(drawing, layer, points, scale=300)
    label = drawing.add_text(text, dxfattribs={"layer": layer, "insert": (0, y)})
    return {outline.dxf.handle, hatch.dxf.handle, label.dxf.handle}


def _drawing(case=None):
    doc = ezdxf.new(units=4)
    drawing = doc.modelspace()
    layer = "S_PLAN_HACH"
    points = _rectangle(5000, 4000, 2600, 1200)
    hatch = _fill(drawing, layer, points, angle=30, hole=case == "multiple_paths")
    if case == "open_hatch":
        hatch.paths.paths[0].is_closed = False
    if case == "pattern_conflict":
        hatch.pattern.lines[0].dash_length_items[0] *= 0.7
    outline = drawing.add_lwpolyline(points if case != "wrong_outline" else _rectangle(5000, 4000, 2300, 1200),
                                     close=True, dxfattribs={"layer": layer})
    at = (5000, 4500) if case == "outside_label" else (5300, 4500)
    label = drawing.add_text("LB2 h=150", dxfattribs={"layer": "S_FLOR_TEXT_BOT", "insert": at})
    source = {hatch.dxf.handle, outline.dxf.handle, label.dxf.handle}
    source.update(_legend(drawing, text="填充区域为飘窗板，板顶标高详见墙身详图。"
                          if case == "no_explicit_level" else "填充区域板顶标高为 Hs-0.500,"))
    if case != "no_hs_definition":
        definition = drawing.add_text("Hs:本层结构标高。", dxfattribs={"layer": "S_PLAN_TEXT", "insert": (0, 3200)})
        source.add(definition.dxf.handle)
    if case == "conflicting_legend":
        _legend(drawing, y=4500, text="填充区域板顶标高为 Hs-0.050,")
    if case == "overlapping_board":
        _fill(drawing, layer, _rectangle(7000, 4200, 900, 1400), pattern="ANSI31")
    if case == "duplicate_board":
        _fill(drawing, layer, points)
    if case == "curved_board_overlap":
        fill = drawing.add_hatch(dxfattribs={"layer": layer})
        fill.set_pattern_fill("ANSI31")
        path = fill.paths.add_edge_path(flags=1)
        path.add_arc((5000, 4700), 300, 0, 360)
    if case == "touching_hole":
        drawing.add_line((5000, 4900), (4500, 4900), dxfattribs={"layer": "SE_HOLE"})
    if case == "multiple_notes":
        drawing.add_text("LB7 h=160", dxfattribs={"layer": "S_FLOR_TEXT_BOT", "insert": (5400, 4500)})
    return doc, source


def _run(doc, *, bounds=None):
    from tumo_engine.recognize.structural_slabs import recognize_structural_slabs

    items, _ = primitives(doc, 1, bounds)
    return recognize_structural_slabs(doc, items, bounds=bounds)


def test_uses_verified_row_pattern_outline_and_local_board_thickness():
    doc, source = _drawing()

    slabs, issues = _run(doc, bounds=(-100, 2000, 9000, 6000))

    [slab] = slabs
    assert issues == []
    assert slab["category"] == "slab"
    assert slab["type"]["mark"] == "LB2"
    assert slab["type"]["thickness"] == 150
    assert slab["base_offset"] == -500
    assert set(map(tuple, slab["geometry"]["points"])) == {(5000, 4000), (7600, 4000), (5000, 5200), (7600, 5200)}
    assert set(slab["handles"]) == source


@pytest.mark.parametrize("case", ["multiple_paths", "open_hatch", "pattern_conflict", "wrong_outline", "outside_label",
                                  "no_explicit_level", "no_hs_definition", "conflicting_legend", "overlapping_board", "touching_hole",
                                  "multiple_notes", "duplicate_board", "curved_board_overlap"])
def test_leaves_board_for_review_if_evidence_is_missing_or_conflicts(case):
    doc, _ = _drawing(case)

    slabs, issues = _run(doc, bounds=(-100, 2000, 9000, 6000))

    assert slabs == []
    assert issues


def test_does_not_reuse_legend_from_a_different_sheet():
    doc, _ = _drawing()

    slabs, issues = _run(doc, bounds=(4500, 3500, 8500, 5500))

    assert slabs == []
    assert issues
