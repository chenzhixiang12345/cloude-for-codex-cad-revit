import ezdxf
import pytest

from tumo_engine.preprocess import primitives


def drawing(problem=None):
    doc = ezdxf.new(units=4)
    plan = doc.modelspace()
    mark = plan.add_text("KL29(2)" if problem == "multiple_spans" else "KL29(1)",
                         dxfattribs={"layer": "S_BEAM_TEXT_HORZ", "insert": (1700, 690)})
    leader = None
    if problem != "missing_leader":
        leader = plan.add_line((1650, 0), (1650, 950), dxfattribs={"layer": "S_BEAM_TEXT_HORZ"})
    source = doc.blocks.new("STRUCTURE")
    beam_layer = {"layer": "S_BEAM", "linetype": "DASHED"}
    left = source.add_line((0, -100), (3400, -100), dxfattribs=beam_layer)
    right = source.add_line((0, 100), (3400, 100),
                            dxfattribs=beam_layer if problem != "solid" else {"layer": "S_BEAM"})
    if problem == "other_section_reference":
        sizes = doc.blocks.new("ANOTHER_STRUCTURE")
        plan.add_blockref(sizes.name, (0, 0))
    else:
        sizes = source
    note = sizes.add_text("250×1500" if problem == "wrong_width" else "200×1500", dxfattribs={
        "layer": "S_BEAM_TEXT_VERT", "insert": (1400, 270), "rotation": 90 if problem == "wrong_rotation" else 0,
    })
    if problem == "multiple_sections":
        sizes.add_text("200×900", dxfattribs={"layer": "S_BEAM_TEXT_VERT", "insert": (1800, 290)})
    wall_layer = {"layer": "S_WALL"}
    first = source.add_lwpolyline([(-300, -150), (-1, -150), (-1, 150), (-300, 150)],
                                  close=True, dxfattribs=wall_layer)
    last = None
    if problem != "missing_support":
        wall_source = source
        if problem == "other_support_reference":
            wall_source = doc.blocks.new("SOME_OTHER_FLOOR")
            plan.add_blockref(wall_source.name, (0, 0))
        last = wall_source.add_lwpolyline([(3401, -150), (3700, -150), (3700, 150), (3401, 150)],
                                          close=True, dxfattribs=wall_layer)
    instance = plan.add_blockref(source.name, (0, 0))
    return doc, (mark, leader, left, right, note, first, last, instance)


def recognize(doc):
    from tumo_engine.tables.beams import read_beam_candidates

    items, _ = primitives(doc, 1)
    return read_beam_candidates(doc, items)


def test_reads_one_single_span_beam_with_section_and_two_distinct_supports():
    doc, (mark, leader, first, last, note, support_a, support_b, instance) = drawing()

    candidates, issues = recognize(doc)

    [result] = candidates
    assert (result.mark, result.width, result.height) == ("KL29", 200, 1500)
    assert result.start == (0, 0)
    assert result.end == (3400, 0)
    assert result.mark_handles == (mark.dxf.handle,)
    assert result.leader_handles == (leader.dxf.handle,)
    assert result.edge_handles == ((instance.dxf.handle, first.dxf.handle), (instance.dxf.handle, last.dxf.handle))
    assert result.section_handles == (instance.dxf.handle, note.dxf.handle)
    assert result.support_handles == ((instance.dxf.handle, support_a.dxf.handle),
                                      (instance.dxf.handle, support_b.dxf.handle))
    assert not issues


@pytest.mark.parametrize(("problem", "code"), [
    ("multiple_spans", "beam_span_unsupported"),
    ("missing_leader", "beam_leader_unconfirmed"),
    ("solid", "beam_edges_unconfirmed"),
    ("wrong_width", "beam_section_unconfirmed"),
    ("wrong_rotation", "beam_section_unconfirmed"),
    ("other_section_reference", "beam_section_unconfirmed"),
    ("multiple_sections", "beam_section_unconfirmed"),
    ("missing_support", "beam_supports_unconfirmed"),
    ("other_support_reference", "beam_supports_unconfirmed"),
])
def test_incomplete_or_conflicting_beam_evidence_remains_locatable(problem, code):
    doc, (mark, *_) = drawing(problem)

    candidates, issues = recognize(doc)

    assert not candidates
    [issue] = issues
    assert issue.code == code
    assert issue.at == (1700, 690)
    assert mark.dxf.handle in issue.handles
