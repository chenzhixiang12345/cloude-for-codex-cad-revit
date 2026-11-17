from math import cos, radians, sin

import ezdxf
import pytest

from tumo_engine.preprocess import primitives


def _schedule(mark="KZ1", width=850, depth=600, *, floors="三~四层", scope="column", number="S3010"):
    return {"mark": mark, "floors": floors, "scope": scope, "sheet": {"number": number},
            "section": {"width_mm": width, "depth_mm": depth}, "handles": ["SHEET", "WIDTH", "DEPTH"]}


def _column(space, x, *, angle=0, mark="KZ1", hatch=True, connected=True, clip=False):
    c, s = cos(radians(angle)), sin(radians(angle))

    def world(u, v):
        return x + c * u - s * v, 4000 + s * u + c * v

    points = [world(0, 0), world(850, 0), world(850, 600), world(100 if clip else 0, 600)]
    if clip:
        points.append(world(0, 500))
    outline = space.add_lwpolyline(points, close=True, dxfattribs={"layer": "COLU_OLIN"})
    text_at = (x + 1450, 4800)
    caption = space.add_text(mark, dxfattribs={"layer": "TAB_TEXT", "insert": text_at})
    tip = points[2] if connected else (x + 1100, 4300)
    leader = space.add_lwpolyline([tip, text_at, (text_at[0] + 800, text_at[1])], dxfattribs={"layer": "COLU_DE_TEXT"})
    sources = [outline, caption, leader]
    if hatch:
        fill = space.add_hatch(dxfattribs={"layer": "COLU_HATCH"})
        fill.paths.add_polyline_path(points, is_closed=True, flags=1)
        sources.append(fill)
    return {entity.dxf.handle for entity in sources}


def _run(doc, rows, *, floor=3, title="三~四层墙柱平面图"):
    from tumo_engine.recognize.structural_columns import recognize_structural_columns

    items, _ = primitives(doc, 1)
    return recognize_structural_columns(items, rows, plan_title=title, floor_number=floor)


@pytest.mark.parametrize("angle", [0, 30, 90])
def test_matches_plan_column_to_table_and_keeps_real_section_rotation(angle):
    doc = ezdxf.new(units=4)
    sources = _column(doc.modelspace(), 0, angle=angle)
    items = [_schedule(floors="五~八层", width=650, number="S3012"), _schedule()]

    result, issues = _run(doc, items)

    [(column, outline)] = result
    assert issues == []
    assert column["type"]["width"] == 850
    assert column["type"]["depth"] == 600
    assert column["geometry"]["rotation_deg"] == pytest.approx(angle, abs=0.001)
    assert column["geometry"]["at"] == pytest.approx(list(outline.centroid.coords[0]), abs=0.001)
    assert set(column["handles"]) == sources
    assert any(evidence.startswith("column_schedule:S3010:") for evidence in column["evidence"])


def test_keeps_same_type_for_two_distinct_positions():
    doc = ezdxf.new(units=4)
    _column(doc.modelspace(), 0)
    _column(doc.modelspace(), 5000)

    result, issues = _run(doc, [_schedule()])

    assert issues == []
    assert [column["geometry"]["at"] for column, _ in result] == [[425, 4300], [5425, 4300]]


def test_preserves_matching_column_reference_geometry_and_fill_handles():
    doc = ezdxf.new(units=4)
    space = doc.modelspace()
    sources = _column(space, 0)
    points = [(0.01, 4000.01), (850, 4000), (850, 4600), (0, 4600)]
    reference = space.add_lwpolyline(points, close=True, dxfattribs={"layer": "S_COLU"})
    fill = space.add_hatch(dxfattribs={"layer": "S_COLU_HACH"})
    fill.paths.add_polyline_path(points, is_closed=True, flags=1)

    [(column, _)], issues = _run(doc, [_schedule()])

    assert issues == []
    assert set(column["handles"]) == {*sources, reference.dxf.handle, fill.dxf.handle}


@pytest.mark.parametrize("evidence", ["wrong_size", "wrong_table_floor", "multiple_rows", "fill_missing", "bad_leader", "clipped"])
def test_keeps_unsafe_placements_as_located_issues(evidence):
    doc = ezdxf.new(units=4)
    _column(doc.modelspace(), 0, hatch=evidence != "fill_missing", connected=evidence != "bad_leader", clip=evidence == "clipped")
    rows = [_schedule(width=750 if evidence == "wrong_size" else 850,
                      floors="五~八层" if evidence == "wrong_table_floor" else "三~四层")]
    if evidence == "multiple_rows":
        rows.append(_schedule(width=800, floors="三层", number="S3020"))

    result, issues = _run(doc, rows)

    assert result == []
    assert len(issues) == 1
    assert issues[0][1] == [1450, 4800]


def test_ignores_wall_boundary_component_even_with_a_matching_outline():
    doc = ezdxf.new(units=4)
    _column(doc.modelspace(), 0, mark="GBZ1")

    assert _run(doc, [_schedule(mark="GBZ1", scope="wall_boundary_component")]) == ([], [])
