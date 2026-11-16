import ezdxf

from tumo_engine.io.dxf_reader import analyze
from tumo_engine.pipeline import run
from tumo_engine.preprocess import primitives, unit_factor


def add_wall(drawing, x=0, *, layer="WALL", **attributes):
    return drawing.add_lwpolyline(
        [(x, 0), (x + 3000, 0), (x + 3000, 200), (x, 200)],
        close=True,
        dxfattribs={"layer": layer, **attributes},
    )


def test_recognizes_only_visible_walls_and_preserves_raw_analysis(tmp_path):
    doc = ezdxf.new(units=4)
    doc.layers.new("WALL").lock()
    doc.layers.new("WALL-BLOCK").off()
    doc.layers.new("WALL-LINE").freeze()
    drawing = doc.modelspace()
    visible = add_wall(drawing)
    add_wall(drawing, 4000, layer="wall-block")
    add_wall(drawing, 8000, layer="WALL-LINE")
    add_wall(drawing, 12000, invisible=1)
    path = tmp_path / "标准层平面图.dxf"
    doc.saveas(path)

    walls = [element for element in run(path)["elements"] if element["category"] == "wall"]

    assert [item["source"]["handles"] for item in walls] == [[visible.dxf.handle]]
    assert "| wall-block | 1 | LWPOLYLINE × 1" in analyze(path)
    assert "| WALL-LINE | 1 | LWPOLYLINE × 1" in analyze(path)


def test_hidden_parent_insert_hides_children_on_visible_layers():
    doc = ezdxf.new(units=4)
    doc.layers.new("WALL").on()
    doc.layers.new("FROZEN").freeze()
    doc.layers.new("PARENT-OFF").off()
    definition = doc.blocks.new("NESTED")
    add_wall(definition)
    assembly = doc.blocks.new("ASSEMBLY")
    assembly.add_blockref("NESTED", (0, 0), dxfattribs={"layer": "WALL"})
    assembly.add_blockref("NESTED", (4000, 0), dxfattribs={"layer": "FROZEN"})
    drawing = doc.modelspace()
    visible = drawing.add_blockref("ASSEMBLY", (0, 0))
    drawing.add_blockref("ASSEMBLY", (10000, 0), dxfattribs={"layer": "PARENT-OFF"})
    drawing.add_blockref("ASSEMBLY", (20000, 0), dxfattribs={"invisible": 1})

    items, _ = primitives(doc, 1)

    [item] = items
    assert item.points[0] == (0, 0)
    assert item.handles[0] == visible.dxf.handle


def test_layer_zero_children_inherit_the_visible_insert_layer():
    doc = ezdxf.new(units=4)
    doc.layers.get("0").off()
    doc.layers.new("WALL").on()
    definition = doc.blocks.new("PANEL")
    add_wall(definition, layer="0")
    drawing = doc.modelspace()
    drawing.add_blockref("PANEL", (0, 0), dxfattribs={"layer": "WALL"})
    drawing.add_blockref("PANEL", (4000, 0), dxfattribs={"layer": "0"})

    items, _ = primitives(doc, 1)

    [item] = items
    assert item.layer == "WALL"
    assert item.points[0] == (0, 0)


def test_skips_hidden_attributes_on_visible_insert():
    doc = ezdxf.new(units=4)
    doc.layers.new("TITLE").on()
    doc.layers.new("HIDDEN-TEXT").off()
    doc.blocks.new("LABEL")
    ref = doc.modelspace().add_blockref("LABEL", (0, 0), dxfattribs={"layer": "TITLE"})
    shown = ref.add_attrib("SHOWN", "显示文字", dxfattribs={"layer": "0"})
    hidden = ref.add_attrib("HIDDEN", "隐藏属性")
    hidden.is_invisible = True
    ref.add_attrib("HIDDEN_LAYER", "隐藏图层", dxfattribs={"layer": "HIDDEN-TEXT"})
    ref.add_attrib("HIDDEN_ENTITY", "隐藏图元", dxfattribs={"invisible": 1})

    items, _ = primitives(doc, 1)

    [item] = items
    assert item.text == "显示文字"
    assert item.layer == "TITLE"
    assert item.handles == (ref.dxf.handle, shown.dxf.handle)


def test_unit_inference_ignores_hidden_dimensions():
    doc = ezdxf.new(units=0)
    doc.layers.new("HIDDEN-DIM").off()
    drawing = doc.modelspace()
    for index in range(3):
        dim = drawing.add_linear_dim(
            base=(0, 500), p1=(0, 0), p2=(1000, 0), text="100",
            dxfattribs={"layer": "HIDDEN-DIM" if index != 2 else "0", "invisible": index == 2},
        )
        dim.render()

    assert unit_factor(doc) == 1.0


def test_hidden_standard_floor_title_cannot_override_visible_title(tmp_path):
    doc = ezdxf.new(units=4)
    doc.layers.new("TITLE-OFF").off()
    drawing = doc.modelspace()
    drawing.add_lwpolyline(
        [(0, 0), (12000, 0), (12000, 9000), (0, 9000)], close=True,
        dxfattribs={"layer": "FRAME"},
    )
    drawing.add_text("二层平面图", dxfattribs={"layer": "TITLE", "insert": (3000, 3000)})
    drawing.add_text("标准层平面图", dxfattribs={"layer": "TITLE-OFF", "insert": (3000, 3500)})
    path = tmp_path / "平面图.dxf"
    doc.saveas(path)

    [sheet] = run(path)["sheets"]

    assert sheet["title"] == "二层平面图"


def test_hidden_smaller_frame_does_not_replace_visible_frame(tmp_path):
    doc = ezdxf.new(units=4)
    doc.layers.new("FRAME-FROZEN").freeze()
    drawing = doc.modelspace()
    for corners, attributes in (
        ([(0, 0), (12000, 0), (12000, 9000), (0, 9000)], {"layer": "FRAME"}),
        ([(1000, 1000), (11000, 1000), (11000, 8000), (1000, 8000)], {"layer": "FRAME", "invisible": 1}),
        ([(2000, 2000), (10000, 2000), (10000, 7000), (2000, 7000)], {"layer": "FRAME-FROZEN"}),
    ):
        drawing.add_lwpolyline(corners, close=True, dxfattribs=attributes)
    drawing.add_text("二层平面图", dxfattribs={"layer": "TITLE", "insert": (3000, 3000)})
    path = tmp_path / "平面图.dxf"
    doc.saveas(path)

    [sheet] = run(path)["sheets"]

    assert sheet["bounds"] == [0, 0, 12000, 9000]


def test_directory_ignores_attributes_of_hidden_or_invisible_insert():
    from tumo_engine.sheets import directory_entries

    doc = ezdxf.new(units=4)
    doc.layers.new("TITLE").on()
    doc.layers.new("TITLE-HIDDEN").freeze()
    doc.blocks.new("CATALOG_ROW")
    drawing = doc.modelspace()
    visible = drawing.add_blockref("CATALOG_ROW", (0, 0), dxfattribs={"layer": "TITLE"})
    visible.add_attrib("NUMBER", "A101", (0, 0), dxfattribs={"layer": "0"})
    visible.add_attrib("TITLE", "二层平面图", (1000, 0), dxfattribs={"layer": "0"})
    hidden_attribute = visible.add_attrib("OTHER_TITLE", "错误平面图", (500, 0))
    hidden_attribute.is_invisible = True
    for x, number, attributes in (
        (3000, "A102", {"layer": "TITLE-HIDDEN"}),
        (6000, "A103", {"layer": "TITLE", "invisible": 1}),
    ):
        hidden = drawing.add_blockref("CATALOG_ROW", (x, 0), dxfattribs=attributes)
        hidden.add_attrib("NUMBER", number, (x, 2000), dxfattribs={"layer": "TITLE"})
        hidden.add_attrib("TITLE", "非可见平面图", (x + 1000, 2000), dxfattribs={"layer": "TITLE"})

    assert directory_entries(doc) == {"A101": "二层平面图"}
