from math import cos, radians, sin

import ezdxf
import pytest

from tumo_engine.preprocess import primitives


def _plan(*, angle=0, missing=None):
    doc = ezdxf.new(units=4)
    model = doc.modelspace()
    c, s = cos(radians(angle)), sin(radians(angle))

    def world(point):
        x, y = point
        return x * c - y * s + 70000, x * s + y * c + 54000

    def boundary(points, layer):
        return model.add_lwpolyline([world(point) for point in points], close=True, dxfattribs={"layer": layer})

    def fill(points, layer):
        entity = model.add_hatch(dxfattribs={"layer": layer})
        entity.paths.add_polyline_path([world(point) for point in points], flags=1)
        return entity

    def text(value, position, layer="S_COLU_TEXT"):
        return model.add_text(value, dxfattribs={"insert": world(position), "layer": layer})

    wall = boundary([(-1000, -400), (0, -400), (0, 0), (4500, 0), (4500, 300), (-1000, 300)], "S_WALL")
    mark = text("Q2a", (2100, -1.2))
    notes = text("9、图例 (无填充)表示现浇墙体，为现浇墙体构造边缘构件范围；", (10000, -5000), "S_PLAN_TEXT")
    sources = {entity.dxf.handle for entity in (wall, mark, notes)}
    for m, (start, end, lower) in enumerate(((-1000, 0, -400), (3900, 4500, 0))):
        if m == 1 and missing == "cross_section":
            lower = 100
        ring = [(start, lower), (end, lower), (end, 300), (start, 300)]
        outline = boundary(ring, "COLU_OLIN")
        sources.add(outline.dxf.handle)
        if not (m == 1 and missing == "fill"):
            sources.add(fill(ring, "COLU_HATCH").dxf.handle)
    if missing == "legend":
        notes.dxf.text = "剪力墙边缘构件详图"
    if missing == "second_wall":
        boundary([(1000, -10), (3500, -10), (3500, -310), (1000, -310)], "S_WALL")
    if missing == "other_mark":
        text("Q5", (1700, 1))
    if missing == "middle_fill":
        fill([(1700, -20), (1900, -20), (1900, 325), (1700, 325)], "COLU_HATCH")
    if missing == "opening":
        model.add_line(world((2000, -50)), world((2000, 0.1)), dxfattribs={"layer": "S_HOLE"})
    return doc, sources, world


def _table(*, span=(3, 4), thickness=300):
    return {"sheet": "S3009", "mark": "Q2A", "floor_span": span, "thickness_mm": thickness,
            "handles": ["TABLE", "THICKNESS"]}


def _read(doc, table, floor=3):
    from tumo_engine.recognize.structural_shear_walls import recognize_structural_shear_walls

    items, _ = primitives(doc, 1)
    return recognize_structural_shear_walls(items, table, floor_number=floor)


@pytest.mark.parametrize(("angle", "floor"), [(0, 3), (0, 4), (37, 4)])
def test_extracts_only_the_unfilled_section_between_verified_edge_components(angle, floor):
    doc, handles, world = _plan(angle=angle)

    walls, issues = _read(doc, [_table()], floor)

    [(wall, footprint)] = walls
    assert not issues
    assert wall["category"] == "shear_wall"
    assert wall["type"]["thickness"] == 300
    assert wall["type"]["mark"] == "Q2a"
    assert wall["geometry"]["start"] == pytest.approx(world((0, 150)), abs=0.001)
    assert wall["geometry"]["end"] == pytest.approx(world((3900, 150)), abs=0.001)
    assert footprint.area == pytest.approx(3900 * 300, abs=0.5)
    assert set(wall["handles"]) == handles | {"TABLE", "THICKNESS"}


@pytest.mark.parametrize("missing", ["fill", "legend", "second_wall", "other_mark", "middle_fill", "opening", "cross_section"])
def test_refuses_to_publish_when_local_wall_body_evidence_is_missing(missing):
    doc, _, _ = _plan(missing=missing)

    walls, issues = _read(doc, [_table()])

    assert not walls
    assert issues


@pytest.mark.parametrize("table", [[_table(thickness=350)], [_table(span=(5, 8))], [_table(), _table()]])
def test_requires_one_compatible_table_row_in_this_floor(table):
    doc, _, _ = _plan()

    walls, issues = _read(doc, table)

    assert not walls
    assert issues


def test_other_number_on_same_source_wall_outside_local_body_does_not_block_the_proved_segment():
    doc, _, world = _plan()
    doc.modelspace().add_text("Q5", dxfattribs={"layer": "S_COLU_TEXT", "insert": world((4200, 0))})

    walls, issues = _read(doc, [_table()])

    [(wall, _)] = walls
    assert wall["type"]["mark"] == "Q2a"
    [(message, at)] = issues
    assert "Q5" in message
    assert at == list(world((4200, 0)))


def test_uses_the_current_sheet_legend_outside_the_axis_footprint():
    from tumo_engine.recognize.structural_shear_walls import recognize_structural_shear_walls

    doc, _, _ = _plan()
    sheet, _ = primitives(doc, 1)
    plan = [item for item in sheet if item.layer != "S_PLAN_TEXT"]
    assert not recognize_structural_shear_walls(plan, [_table()], floor_number=3)[0]

    walls, issues = recognize_structural_shear_walls(plan, [_table()], floor_number=3, legend_items=sheet)

    assert len(walls) == 1
    assert not issues
