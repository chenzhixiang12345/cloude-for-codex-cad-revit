import ezdxf
import pytest

from tumo_engine.export.model import Element, Issue
from tumo_engine.preprocess import primitives


def drawing(problem=None):
    doc = ezdxf.new(units=4)
    plan = doc.modelspace()
    plan.add_text("KL29(1)", dxfattribs={"layer": "S_BEAM_TEXT_HORZ", "insert": (1700, 690)})
    plan.add_line((1650, 0), (1650, 950), dxfattribs={"layer": "S_BEAM_TEXT_HORZ"})
    source = doc.blocks.new("STRUCTURE")
    for y in (-100, 100):
        source.add_line((0, y), (3400, y), dxfattribs={"layer": "S_BEAM", "linetype": "DASHED"})
    for a, b in ((-300, -1), (3401, 3700)):
        source.add_lwpolyline([(a, -150), (b, -150), (b, 150), (a, 150)], close=True,
                              dxfattribs={"layer": "S_WALL"})
    note = source.add_text("200×1500", dxfattribs={"layer": "S_BEAM_TEXT_VERT", "insert": (1400, 270)})
    level_note = None
    if problem != "missing_offset":
        notes = source
        if problem == "other_reference":
            notes = doc.blocks.new("DIFFERENT_STRUCTURE")
            plan.add_blockref(notes.name, (0, 0))
        level_note = notes.add_text("(-0.850)" if problem == "negative" else "(+0.850)", dxfattribs={
            "layer": "S_BEAM_TEXT_VERT", "insert": (1405, 20), "rotation": 90 if problem == "wrong_rotation" else 0,
        })
    if problem == "conflicting_offset":
        source.add_text("(+0.500)", dxfattribs={"layer": "S_BEAM_TEXT_VERT", "insert": (1400, 25)})
    if problem == "ambiguous_section":
        source.add_text("300×600", dxfattribs={"layer": "S_BEAM_TEXT_VERT", "insert": (1400, 268)})
    plan.add_blockref(source.name, (0, 0))
    reference = None
    if problem != "undefined_datum":
        reference = plan.add_text("图中( )中的相对标高均为相对于本层结构层高表中的标高。",
                                  dxfattribs={"layer": "S_PLAN_TEXT", "insert": (5000, 1930)})
    if problem == "conflicting_datum":
        plan.add_text("图中括号内相对标高相对于本层建筑标高。",
                      dxfattribs={"layer": "S_PLAN_TEXT", "insert": (5000, 1600)})
    for x, value in ((0, "层号"), (800, "标高(m)"), (1600, "层高(m)")):
        plan.add_text(value, dxfattribs={"layer": "S_TABL_NUMB", "insert": (x, 7000)})
    elevation = None
    for floor in range(2, 8):
        if problem == "missing_structural_floor" and floor == 3:
            continue
        y = 7000 - (floor - 1) * 400
        plan.add_text(str(floor), dxfattribs={"layer": "S_TABL_NUMB", "insert": (0, y)})
        value = plan.add_text(f"{4.750 + 3 * (floor - 2):.3f}",
                              dxfattribs={"layer": "S_TABL_NUMB", "insert": (800, y)})
        plan.add_text("3.000", dxfattribs={"layer": "S_TABL_NUMB", "insert": (1600, y)})
        if floor == 3:
            elevation = value
    return doc, note, level_note, reference, elevation


def read(doc, floor=None):
    from tumo_engine.recognize.structural_beams import recognize_structural_beams

    items, _ = primitives(doc, 1)
    return recognize_structural_beams(doc, items, sheet_id="S4004", title="二~六层顶梁平法施工图", floor=floor)


@pytest.mark.parametrize(("problem", "offset"), [(None, 850), ("negative", -850)])
def test_resolves_explicit_beam_top_relative_to_correct_structural_floor(problem, offset):
    doc, note, level_note, reference, elevation = drawing(problem)

    elements, issues = read(doc)

    [source] = elements
    beam = Element.model_validate(source)
    assert beam.category == "beam" and beam.discipline == "struct"
    assert beam.base_level == "L3" and beam.base_offset == offset
    assert beam.type.mark == "KL29" and beam.type.width == 200 and beam.type.height == 1500
    assert beam.geometry.start == (0, 0) and beam.geometry.end == (3400, 0)
    assert {entity.dxf.handle for entity in (note, level_note, reference, elevation)} <= set(beam.source.handles)
    assert not issues


@pytest.mark.parametrize("problem", [
    "missing_offset", "other_reference", "wrong_rotation", "conflicting_offset",
    "ambiguous_section", "undefined_datum", "conflicting_datum", "missing_structural_floor",
])
def test_locates_uncertain_vertical_evidence_without_publishing_a_beam(problem):
    doc, *_ = drawing(problem)

    elements, issues = read(doc)

    assert not elements
    [reported] = issues
    issue = Issue.model_validate(reported)
    assert issue.where.sheet == "S4004"
    assert issue.where.at == (1700, 690)
    assert "KL29" in issue.message


def test_refuses_floor_outside_the_explicit_top_beam_title():
    doc, *_ = drawing()

    elements, issues = read(doc, floor=8)

    assert not elements
    [issue] = issues
    assert "8F" in issue["message"]
