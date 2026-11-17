import ezdxf
import pytest

from tumo_engine.recognize.elevation import read_elevation_drawing


def _card(drawing, mark, width, height, *, sill=None, evidence="valid"):
    block = drawing.blocks.new(f"Bound$0${mark}")
    frame = block.add_lwpolyline([(0, 0), (width, 0), (width, height), (0, height)], close=True)
    if evidence == "hidden_geometry":
        drawing.layers.new("AuxiliaryFrame").off()
        frame.dxf.layer = "AuxiliaryFrame"
    datum = drawing.blocks.new("RelativeDatum")
    datum.add_line((0, 0), (300, 0))
    scope = drawing.blocks.new("Bound")
    root = drawing.modelspace().add_blockref("Bound", (10000, 20000))
    bottom = sill or 0
    diagram = scope.add_blockref(block.name, (0, bottom), dxfattribs={"layer": "AE-ELEV-4"})
    caption = scope.add_text("C9999" if evidence == "caption" else mark,
                             dxfattribs={"insert": (0, -600), "layer": "AD-NOTE-CRCY"})

    def dimension(start, end, angle, base):
        drawing_dimension = scope.add_linear_dim(base=base, p1=start, p2=end, angle=angle,
                                                 dxfattribs={"layer": "AD-DIMS-OTSD"})
        drawing_dimension.render()
        return drawing_dimension.dimension

    top = bottom + height
    width_end = width - 50 if evidence == "width_witness" else width
    horizontal = dimension((0, top), (width_end, top), 0, (0, top + 500))
    vertical = dimension((0, bottom), (0, top), 90, (-250, bottom))
    if evidence == "width_override":
        horizontal.dxf.text = str(width - 50)
    handles = {root.dxf.handle, diagram.dxf.handle, caption.dxf.handle, horizontal.dxf.handle, vertical.dxf.handle}
    if sill is not None:
        floor = scope.add_blockref(datum.name, (-1000, 50 if evidence == "datum_witness" else 0),
                                   dxfattribs={"layer": "AD-LEVL-HIGH"})
        scope_type = floor.add_attrib("标高类型", "绝对标高" if evidence == "absolute_datum" else "相对于本层")
        height_type = floor.add_attrib("标高", "H")
        sill_dim = dimension((0, 0), (0, bottom), 90, (-250, bottom))
        handles.update([floor.dxf.handle, scope_type.dxf.handle, height_type.dxf.handle, sill_dim.dxf.handle])
        if evidence == "adjacent_datum":
            neighbor = scope.add_blockref(datum.name, (width + 900, 0), dxfattribs={"layer": "AD-LEVL-HIGH"})
            neighbor.add_attrib("标高类型", "相对于本层")
            neighbor.add_attrib("标高", "H")
    drawing.layers.new("AE-ELEV-4")
    if evidence == "hidden":
        drawing.layers.get("AE-ELEV-4").off()
    if evidence == "hidden_support":
        drawing.layers.new("AD-DIMS-OTSD").off()
        drawing.layers.new("AD-LEVL-HIGH").freeze()
    return handles


@pytest.mark.parametrize(("mark", "category", "width", "height", "sill"), [
    ("M0824a", "door", 850, 2400, None),
    ("FM乙0924a", "door", 950, 2400, None),
    ("TC2118", "window", 2100, 1800, 500),
])
def test_reads_native_dimensions_from_captioned_opening_detail(tmp_path, mark, category, width, height, sill):
    drawing = ezdxf.new(units=4)
    handles = _card(drawing, mark, width, height, sill=sill)
    path = tmp_path / "立剖面图.dxf"
    drawing.saveas(path)

    records, _ = read_elevation_drawing(path)

    [detail] = [item for item in records if item["kind"] == "opening_detail"]
    assert detail["category"] == category
    assert detail["mark"] == mark.upper()
    assert detail["text"] == mark
    assert detail["width_mm"] == width
    assert detail["height_mm"] == height
    assert detail["sill_height_mm"] == sill
    assert detail["at"] == [10000, 19400]
    assert detail["source"]["file"] == path.name
    assert set(detail["source"]["handles"]) == handles


@pytest.mark.parametrize("evidence", ["caption", "width_witness", "width_override", "hidden", "hidden_geometry"])
def test_requires_visible_matching_caption_and_real_witness_endpoints(tmp_path, evidence):
    drawing = ezdxf.new(units=4)
    _card(drawing, "M0824a", 850, 2400, evidence=evidence)
    path = tmp_path / "立剖面图.dxf"
    drawing.saveas(path)

    records, _ = read_elevation_drawing(path)

    assert not any(item["kind"] == "opening_detail" for item in records)


@pytest.mark.parametrize("evidence", ["datum_witness", "absolute_datum"])
def test_window_detail_needs_relative_floor_datum_at_the_sill_witness(tmp_path, evidence):
    drawing = ezdxf.new(units=4)
    _card(drawing, "TC2118", 2100, 1800, sill=500, evidence=evidence)
    path = tmp_path / "立剖面图.dxf"
    drawing.saveas(path)

    records, _ = read_elevation_drawing(path)

    [detail] = [item for item in records if item["kind"] == "opening_detail"]
    assert detail["sill_height_mm"] is None


def test_uses_hidden_dimensions_only_as_witnesses_for_a_visible_detail(tmp_path):
    drawing = ezdxf.new(units=4)
    handles = _card(drawing, "TC2118", 2100, 1800, sill=500, evidence="hidden_support")
    path = tmp_path / "立剖面图.dxf"
    drawing.saveas(path)

    records, _ = read_elevation_drawing(path)

    [detail] = [item for item in records if item["kind"] == "opening_detail"]
    assert detail["sill_height_mm"] == 500
    assert set(detail["source"]["handles"]) == handles
    assert len(detail["source"]["hidden_size_handles"]) == 2
    assert len(detail["source"]["hidden_sill_handles"]) == 4
    expected_hidden = {handle for handle in handles if drawing.entitydb[handle].dxf.layer
                       in {"AD-DIMS-OTSD", "AD-LEVL-HIGH"}}
    assert set(detail["source"]["hidden_size_handles"] + detail["source"]["hidden_sill_handles"]) == expected_hidden


def test_sill_witness_stays_with_datum_on_the_same_side_of_its_diagram(tmp_path):
    drawing = ezdxf.new(units=4)
    handles = _card(drawing, "TC2118", 2100, 1800, sill=500, evidence="adjacent_datum")
    path = tmp_path / "立剖面图.dxf"
    drawing.saveas(path)

    records, _ = read_elevation_drawing(path)

    [detail] = [item for item in records if item["kind"] == "opening_detail"]
    assert detail["sill_height_mm"] == 500
    assert set(detail["source"]["handles"]) == handles
