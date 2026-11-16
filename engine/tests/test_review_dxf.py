from pathlib import Path

import ezdxf
import pytest

from tumo_engine.viz.review_dxf import write_review_dxf


def _review_model():
    return {
        "sheets": [{
            "id": "S1", "layout": "Model", "file": "plan.dxf",
            "units_to_mm": 10, "bounds": [-100, -100, 2100, 1100],
        }],
        "grids": [{
            "id": "G-1", "label": "A", "start": [0, 0], "end": [2000, 0],
            "source": {"sheet": "S1"},
        }],
        "elements": [
            {"id": "W-1", "category": "wall", "type": {"thickness": 200},
             "geometry": {"kind": "line", "start": [0, 500], "end": [2000, 500]},
             "source": {"sheet": "S1"}},
            {"id": "C-1", "category": "column", "type": {"diameter": 400},
             "geometry": {"kind": "point", "at": [1000, 800]},
             "source": {"sheet": "S1"}},
        ],
        "issues": [
            {"id": "I-1", "element": "W-1"},
            {"id": "I-2", "element": "W-1", "where": {"sheet": "S2", "at": [800, 500]}},
        ],
    }


def _source(path: Path):
    document = ezdxf.new("R2010", units=5)
    document.layers.new("CAD_BASE")
    block = document.blocks.new("BASE_BLOCK")
    block.add_circle((0, 0), 10)
    layout = document.modelspace()
    layout.add_line((0, 0), (200, 0), dxfattribs={"layer": "CAD_BASE"})
    layout.add_blockref("BASE_BLOCK", (75, 25), dxfattribs={"layer": "CAD_BASE"})
    document.saveas(path)


def test_writes_aligned_review_entities_and_preserves_the_source_dxf(tmp_path):
    source = tmp_path / "plan.dxf"
    _source(source)
    original = source.read_bytes()
    output = tmp_path / "review.dxf"

    assert write_review_dxf(_review_model(), source, output) == output

    assert source.read_bytes() == original
    drawing = ezdxf.readfile(output)
    assert drawing.units == 5
    layout = drawing.modelspace()
    base, = layout.query('LINE[layer=="CAD_BASE"]')
    axis, = layout.query('LINE[layer=="TUMO_REVIEW_GRID"]')
    assert axis.dxf.start == base.dxf.start
    assert axis.dxf.end == base.dxf.end
    assert len(layout.query('INSERT[layer=="CAD_BASE"]')) == 1
    assert len(drawing.blocks.get("BASE_BLOCK").query("CIRCLE")) == 1
    wall, = layout.query('LWPOLYLINE[layer=="TUMO_REVIEW_WALL"]')
    assert wall.closed
    assert {tuple(point) for point in wall.get_points("xy")} == {
        (0, 40), (200, 40), (200, 60), (0, 60),
    }
    column, = layout.query('CIRCLE[layer=="TUMO_REVIEW_COLUMN"]')
    assert column.dxf.center.isclose((100, 80, 0))
    assert column.dxf.radius == 20
    assert len(layout.query('LINE[layer=="TUMO_REVIEW_ISSUE"]')) == 2
    labels = {text.dxf.text for text in layout.query('TEXT[layer=="TUMO_REVIEW_LABELS"]')}
    assert labels == {"A / G-1", "W-1", "C-1", "I-1"}
    assert drawing.layers.get("TUMO_REVIEW_GRID").color != drawing.layers.get("TUMO_REVIEW_ISSUE").color


def test_refuses_to_overwrite_the_source(tmp_path):
    source = tmp_path / "plan.dxf"
    _source(source)
    original = source.read_bytes()
    with pytest.raises(ValueError, match="原始"):
        write_review_dxf(_review_model(), source, source)
    assert source.read_bytes() == original


def test_crops_clear_geometry_but_retains_crossing_or_unbounded_entities(tmp_path):
    source = tmp_path / "plan.dxf"
    _source(source)
    drawing = ezdxf.readfile(source)
    layout = drawing.modelspace()
    layer = {"layer": "CAD_BASE"}
    layout.add_line((1000, 1000), (1200, 1000), dxfattribs=layer)
    layout.add_line((-200, 60), (500, 60), dxfattribs=layer)
    layout.add_blockref("BASE_BLOCK", (1000, 1000), dxfattribs=layer)
    layout.add_ray((1000, 1000), (1, 0), dxfattribs=layer)
    layout.add_text("unknown text boundary", dxfattribs={"insert": (1000, 1000), **layer})
    drawing.saveas(source)

    output = write_review_dxf(_review_model(), source, tmp_path / "review.dxf")

    layout = ezdxf.readfile(output).modelspace()
    remaining = [tuple(line.dxf.start)[:2] for line in layout.query('LINE[layer=="CAD_BASE"]')]
    assert remaining == [(0, 0), (-200, 60)]
    assert len(layout.query('INSERT[layer=="CAD_BASE"]')) == 1
    assert len(layout.query('RAY[layer=="CAD_BASE"]')) == 1
    assert len(layout.query('TEXT[layer=="CAD_BASE"]')) == 1


def test_records_inferred_millimeters_in_a_previously_unitless_copy(tmp_path):
    source = tmp_path / "plan.dxf"
    _source(source)
    document = ezdxf.readfile(source)
    document.units = 0
    document.saveas(source)
    model = _review_model()
    model["sheets"][0]["units_to_mm"] = 1

    output = write_review_dxf(model, source, tmp_path / "review.dxf")

    assert ezdxf.readfile(source).units == 0
    assert ezdxf.readfile(output).units == 4
