import ezdxf
import pytest

from tumo_engine.io.dxf_reader import analyze


def test_analyze_identifies_layers_text_blocks_and_viewports(tmp_path):
    doc = ezdxf.new("R2013", units=4)
    doc.layers.new("轴网")
    doc.layers.new("未使用")
    block = doc.blocks.new("柱样例")
    block.add_text("KZ1")
    block.add_attdef("MARK", insert=(0, 0))
    model = doc.modelspace()
    model.add_line((0, 0), (0, 5000), dxfattribs={"layer": "轴网"})
    model.add_mtext(r"标准层\PA 区", dxfattribs={"layer": "轴网"})
    model.add_blockref("柱样例", (0, 0)).add_auto_attribs({"MARK": "KZ2"})
    layout = doc.layouts.new("首层布置")
    layout.add_viewport(center=(100, 100), size=(180, 180), view_center_point=(0, 0),
                        view_height=18000, status=2)
    path = tmp_path / "合成图纸.dxf"
    doc.saveas(path)

    report = analyze(path)

    for label in ("轴网", "未使用", "LINE", "MTEXT", "标准层 A 区", "柱样例",
                  "KZ1", "KZ2", "首层布置", "1:100", "毫米", "未检测到代理实体"):
        assert label in report


def test_analyze_reports_missing_content_and_proxy_entities(tmp_path):
    doc = ezdxf.new("R2013", units=0)
    doc.modelspace().new_entity("ACAD_PROXY_ENTITY", dxfattribs={"layer": "0"})
    path = tmp_path / "合成代理.dxf"
    doc.saveas(path)

    report = analyze(path)

    for label in ("未指定绘图单位", "没有可直接读取的文字", "1 个代理实体", "T3",
                  "没有可供分析的布局视口"):
        assert label in report


def test_analyze_rejects_non_dxf(tmp_path):
    path = tmp_path / "合成图纸.dwg"
    path.write_text("dummy", encoding="utf-8")
    with pytest.raises(ValueError, match="DXF"):
        analyze(path)
