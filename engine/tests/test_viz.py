import ezdxf

from tumo_engine.viz.review import render


def test_review_renders_source_and_identifiers(tmp_path):
    source = tmp_path / "plan.dxf"
    drawing = ezdxf.new()
    drawing.modelspace().add_line((0, 0), (1000, 1000))
    drawing.modelspace().add_line((50000, 50000), (51000, 51000))
    drawing.saveas(source)
    model = {
        "sheets": [{"id": "S1", "file": source.name, "bounds": [-100, -100, 1100, 1100]}],
        "grids": [{"id": "G-0001", "label": "1", "start": [100, 0], "end": [100, 1000],
                   "source": {"sheet": "S1"}}],
        "elements": [{"id": "W-0001", "category": "wall", "type": {"thickness": 200},
                      "geometry": {"kind": "line", "start": [0, 0], "end": [1000, 1000]},
                      "source": {"sheet": "S1"}}],
        "issues": [{"id": "I-0001", "severity": "warn", "where": {"sheet": "S1", "at": [500, 500]}}],
    }

    images = tmp_path / "viz"
    render(model, images, tmp_path)

    svg = (images / "S1.svg").read_text(encoding="utf-8")
    assert all(value in svg for value in ("G-0001", "W-0001", "I-0001", "#b8bec6", "#b91c1c"))
    assert svg.count("#b8bec6") == 1
    assert (images / "S1.png").read_bytes().startswith(b"\x89PNG")
