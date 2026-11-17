import ezdxf

from tumo_engine.preprocess import primitives


def _table(document, *, origin=(0, 0), rows=(), caption="剪力墙身表"):
    space = document.modelspace()
    left, bottom = origin
    width, row_height = 5000, 600
    header_y = bottom + (len(rows) + 1) * row_height
    top = header_y + row_height
    limits = (0, 1000, 2500, 3800, width)
    space.add_lwpolyline([(left, bottom), (left, top), (left + width, top), (left + width, bottom)],
                          dxfattribs={"layer": "S_TABL"})
    for y in range(bottom, top, row_height):
        space.add_line((left, y), (left + width, y), dxfattribs={"layer": "S_TABL"})
    for x in limits[1:-1]:
        space.add_line((left + x, bottom), (left + x, header_y), dxfattribs={"layer": "S_TABL"})

    def label(text, x, y):
        return space.add_text(text, dxfattribs={"layer": "S_TABL_NUMB", "insert": (left + x, y)})

    title = label(caption, 3000, top - 300)
    columns = (500, 1750, 3150, 4400)
    headings = [label(value, x, header_y - 300)
                for value, x in zip(("编号", "标高", "墙厚", "分布筋排数"), columns)]
    cells = [[label(value, x + offset * 50, header_y - row_height * (row + 1) - 300)
              for values, x in zip(entries, columns)
              for offset, value in enumerate(values if isinstance(values, tuple) else (values,)) if value]
             for row, entries in enumerate(rows)]
    return title, headings, cells


def _read(document):
    from tumo_engine.tables.shear_walls import read_shear_wall_schedule

    items, _ = primitives(document, 1)
    return read_shear_wall_schedule(items, sheet_id="S3009")


def test_reads_thickness_only_from_the_captioned_table_and_correct_cell():
    document = ezdxf.new(units=4)
    title, headings, cells = _table(document, rows=[("Q2a", "三～四层", "300", "900")])
    _table(document, origin=(7000, 0), rows=[("Q2a", "三～四层", "400")], caption="墙柱柱表")
    document.modelspace().add_text("未注明墙身均为Q1", dxfattribs={"layer": "S_TABL_NUMB", "insert": (100, -250)})

    records, issues = _read(document)

    assert issues == []
    (record,) = records
    assert record["sheet"] == "S3009"
    assert record["mark"] == "Q2a"
    assert record["floors"] == "三~四层"
    assert record["floor_span"] == (3, 4)
    assert record["thickness_mm"] == 300
    assert {item.dxf.handle for item in (title, *headings[:3], *cells[0][:3])} <= set(record["handles"])


def test_reports_ambiguous_thickness_or_missing_floor_without_using_neighboring_values():
    document = ezdxf.new()
    _, _, cells = _table(document, rows=[("Q1", "三~四层", ("200", "300")),
                                        ("Q2a", "+7.750", "300"),
                                        ("Q3", "5F", "250毫米")])

    records, issues = _read(document)

    assert [(row["mark"], row["floor_span"], row["thickness_mm"]) for row in records] == [("Q3", (5, 5), 250)]
    assert {issue["mark"] for issue in issues} == {"Q1", "Q2a"}
    assert {item.dxf.handle for row in cells[:2] for item in row} <= {
        handle for issue in issues for handle in issue["handles"]
    }


def test_overlapping_same_wall_mark_blocks_both_rows_but_distinct_floor_ranges_are_kept():
    document = ezdxf.new()
    _table(document, rows=[("Q2a", "三~四层", "300"), ("Q2A", "4~5层", "350"), ("Q2a", "六层", "200")])

    records, issues = _read(document)

    assert [row["floor_span"] for row in records] == [(6, 6)]
    assert {issue["mark"] for issue in issues} == {"Q2a", "Q2A"}


def test_incomplete_duplicate_thickness_does_not_leave_another_same_floor_row_as_confirmed():
    document = ezdxf.new()
    _table(document, rows=[("Q2a", "三~四层", "300"), ("Q2a", "4F", "")])

    records, issues = _read(document)

    assert records == []
    assert len(issues) == 2


def test_invalid_chinese_floor_number_is_an_issue():
    document = ezdxf.new()
    _table(document, rows=[("Q2a", "十十层", "300")])

    records, issues = _read(document)

    assert records == []
    assert [issue["mark"] for issue in issues] == ["Q2a"]


def test_changed_cell_borders_do_not_claim_the_heading_of_a_split_cell():
    document = ezdxf.new()
    _table(document, rows=[("Q2a", "三~四层", "300")])
    document.modelspace().add_line((3400, 0), (3400, 600), dxfattribs={"layer": "S_TABL"})

    records, issues = _read(document)

    assert records == []
    assert len(issues) == 1
