import math

import ezdxf

from tumo_engine.preprocess import primitives, unit_factor


def _read(doc):
    from tumo_engine.tables import read_opening_schedule

    items, _ = primitives(doc, unit_factor(doc))
    return read_opening_schedule(items)


def test_reads_opening_table_with_separate_width_height_and_optional_sill():
    doc = ezdxf.new(units=4)
    drawing = doc.modelspace()
    for index, heading in enumerate(("门窗编号", "洞口宽(mm)", "洞口高(mm)", "窗台高度")):
        drawing.add_text(heading, dxfattribs={"insert": (index * 1000, 1000)})
    mark = drawing.add_text("ｍ－０１", dxfattribs={"insert": (0, 500)})
    width = drawing.add_mtext("0.9m", dxfattribs={"insert": (1000, 520)})
    height = drawing.add_text("2100", dxfattribs={"insert": (2000, 480)})
    sill = drawing.add_text("0", dxfattribs={"insert": (3000, 500)})
    drawing.add_text("C-02", dxfattribs={"insert": (0, 0)})
    drawing.add_text("1200", dxfattribs={"insert": (1000, 0)})

    row, = _read(doc)

    assert (row.mark, row.width, row.height, row.sill_height) == ("M-01", 900, 2100, 0)
    assert set(row.handles) >= {mark.dxf.handle, width.dxf.handle, height.dxf.handle, sill.dxf.handle}


def test_reads_combined_dimensions_with_units_in_cell_and_header():
    doc = ezdxf.new(units=6)
    drawing = doc.modelspace()
    drawing.add_text("门窗编号", dxfattribs={"insert": (0, 2)})
    drawing.add_text("洞口尺寸（m）", dxfattribs={"insert": (1.5, 2)})
    drawing.add_text("C12", dxfattribs={"insert": (0, 1.5)})
    drawing.add_text("1.2×1.5", dxfattribs={"insert": (1.5, 1.5)})
    drawing.add_text("C13", dxfattribs={"insert": (0, 1)})
    drawing.add_text("125cm x 1.45m", dxfattribs={"insert": (1.5, 1)})

    assert [(row.mark, row.width, row.height) for row in _read(doc)] == [
        ("C12", 1200, 1500),
        ("C13", 1250, 1450),
    ]


def test_reads_adjacent_rotated_opening_cards_in_inserted_blocks():
    doc = ezdxf.new(units=4)
    card = doc.blocks.new("OPENING CARD")
    for text, position in (
        ("设计编号", (0, 340)),
        ("PX205（示例）", (-1030, 368)),
        ("洞口尺寸", (0, 0)),
        ("775mm×1985mm", (-980, 38)),
    ):
        card.add_text(text, dxfattribs={"insert": position})
    drawing = doc.modelspace()
    reference = drawing.add_blockref("OPENING CARD", (12500, -4700), dxfattribs={"rotation": 30})
    # A neighboring card with the nearest mark on the opposite side must keep its own dimensions.
    angle = math.radians(30)
    adjacent = (12500 + 4500 * math.cos(angle), -4700 + 4500 * math.sin(angle))
    other = doc.blocks.new("OTHER OPENING CARD")
    for text, position in (
        ("设计编号", (0, 340)),
        ("CX217带限位", (950, 380)),
        ("洞口尺寸", (0, 0)),
        ("1330mm×1675mm", (930, 35)),
    ):
        other.add_text(text, dxfattribs={"insert": position})
    drawing.add_blockref("OTHER OPENING CARD", adjacent, dxfattribs={"rotation": 30})

    rows = {row.mark: row for row in _read(doc)}

    assert rows.keys() == {"PX205", "CX217"}
    assert (rows["PX205"].width, rows["PX205"].height) == (775, 1985)
    assert (rows["CX217"].width, rows["CX217"].height) == (1330, 1675)
    assert reference.dxf.handle in rows["PX205"].handles


def test_does_not_treat_plan_labels_or_notes_as_a_schedule():
    doc = ezdxf.new(units=4)
    drawing = doc.modelspace()
    drawing.add_text("参见门窗表", dxfattribs={"insert": (0, 1000)})
    drawing.add_text("m1021", dxfattribs={"insert": (0, 500)})
    drawing.add_text("900×2100", dxfattribs={"insert": (1000, 500)})

    assert _read(doc) == []


def test_keeps_conflicting_dimensions_for_the_same_mark_and_stops_at_gap():
    doc = ezdxf.new(units=4)
    drawing = doc.modelspace()
    drawing.add_text("门窗编号", dxfattribs={"insert": (0, 5000)})
    drawing.add_text("洞口尺寸", dxfattribs={"insert": (1000, 5000)})
    for mark, size, height in (
        ("C01", "950×1300", 4500),
        ("C01", "980×1300", 4000),
        ("C02", "1200×1420", 0),
    ):
        drawing.add_text(mark, dxfattribs={"insert": (0, height)})
        drawing.add_text(size, dxfattribs={"insert": (1000, height)})

    assert [(row.mark, row.width) for row in _read(doc)] == [("C01", 950), ("C01", 980)]
