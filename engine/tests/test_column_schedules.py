import ezdxf
import pytest


def _rectangle(x0, y0, x1, y1):
    return [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]


def _table_column(space, x0, x1, y):
    corners = _rectangle(x0, y, x1, y + 12000)
    rules = [(x0, y + 800), (x1, y + 800), (x1, y + 1600), (x0, y + 1600), (x0, y + 2400),
             (x1, y + 2400), (x1, y + 3200), (x0, y + 3200)]
    return space.add_lwpolyline([*corners, corners[0], *rules], dxfattribs={"layer": "TAB"})


def _sheet(doc, number, floor, x, *, no_horizontal=False, override=False, duplicate=False):
    if not doc.blocks.get("THAPE_test"):
        detail = doc.blocks.new("THAPE_test")
        detail.add_lwpolyline(_rectangle(0, 0, 30000, 20000), close=True, dxfattribs={"layer": "内框"})
    space = doc.modelspace()
    anchor = space.add_blockref("THAPE_test", (x, 0), dxfattribs={"layer": "内框"})
    title = anchor.add_attrib("内框名称", f"{floor}墙柱柱表", (x + 500, 19800))
    stamp = space.add_text(number, dxfattribs={"insert": (x + 29000, 1000), "layer": "C-SHET-SHET"})
    headers = _table_column(space, x + 2000, x + 3500, 3000)
    column = _table_column(space, x + 3500, x + 9500, 3000)
    for caption, y in (("截面", 10500), ("编号", 5600), ("标高", 4800), ("纵筋", 4000), ("箍筋", 3200)):
        space.add_text(caption, dxfattribs={"insert": (x + 2500, y), "layer": "TAB_TEXT"})
    number_cell = space.add_text("GBZ1", dxfattribs={"insert": (x + 6500, 5600), "layer": "TAB_TEXT"})
    floor_cell = space.add_text(floor, dxfattribs={"insert": (x + 6500, 4800), "layer": "TAB_TEXT"})
    if duplicate:
        space.add_text("GBZ2", dxfattribs={"insert": (x + 6800, 5600), "layer": "TAB_TEXT"})
    outline = space.add_lwpolyline(_rectangle(x + 5900, 9800, x + 7100, 11400), close=True,
                                  dxfattribs={"layer": "WCOL_DE"})
    only = [anchor, title, stamp, headers, column, number_cell, floor_cell, outline]

    def dimension(start, end, angle, base, text="<>"):
        dimension = space.add_linear_dim(p1=start, p2=end, angle=angle, base=base, text=text,
                                         override={"dimlfac": 0.25}, dxfattribs={"layer": "WCOL_DE_DIM"})
        dimension.render()
        only.append(dimension.dimension)

    if not no_horizontal:
        dimension((x + 5900, 9000), (x + 7100, 9000), 0, (x + 5900, 8200), "350" if override else "<>")
    dimension((x + 7100, 9800), (x + 7100, 11400), 90, (x + 7900, 9800))
    return {item.dxf.handle for item in only}


def _read(doc):
    from tumo_engine.tables.columns import read_column_schedules

    return read_column_schedules(doc)


def test_reads_wall_boundary_section_in_the_real_schedule_cells():
    doc = ezdxf.new(units=4)
    handles = _sheet(doc, "S3010", "三~四层", 0)
    _sheet(doc, "S3012", "五~八层", 60000)

    records, issues = _read(doc)

    first, second = records
    assert issues == []
    assert first["sheet"]["number"] == "S3010"
    assert first["mark"] == second["mark"] == "GBZ1"
    assert first["scope"] == "wall_boundary_component"
    assert [first["floors"], second["floors"]] == ["三~四层", "五~八层"]
    assert first["section"]["width_mm"] == 300
    assert first["section"]["depth_mm"] == 400
    assert first["section"]["outline_mm"] == [[0, 0], [0, 400], [300, 400], [300, 0]]
    assert handles <= set(first["handles"])


@pytest.mark.parametrize("case", ["no_horizontal", "override", "duplicate"])
def test_leaves_incomplete_or_conflicting_captioned_section_as_an_issue(case):
    doc = ezdxf.new(units=4)
    _sheet(doc, "S3010", "三~四层", 0, **{case: True})

    records, issues = _read(doc)

    assert not records
    assert issues
    assert issues[0]["sheet"]["number"] == "S3010"


def test_unresolved_duplicate_mark_blocks_the_other_same_floor_section():
    doc = ezdxf.new(units=4)
    _sheet(doc, "S3010", "三~四层", 0)
    drawing = doc.modelspace()
    _table_column(drawing, 9500, 15500, 3000)
    drawing.add_text("GBZ1", dxfattribs={"insert": (12500, 5600), "layer": "TAB_TEXT"})
    drawing.add_text("三~四层", dxfattribs={"insert": (12500, 4800), "layer": "TAB_TEXT"})

    records, issues = _read(doc)

    assert records == []
    assert len(issues) == 2
