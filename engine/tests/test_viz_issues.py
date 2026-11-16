from tumo_engine.viz.review import render


def test_uses_explicit_issue_sheet_and_draws_round_columns(tmp_path):
    model = {
        "sheets": [{"id": "S1", "file": "a.dxf"}, {"id": "S2", "file": "b.dxf"}],
        "grids": [],
        "elements": [{"id": "C-0001", "category": "column", "type": {"diameter": 500},
                      "geometry": {"kind": "point", "at": [0, 0]}, "source": {"sheet": "S1"}}],
        "issues": [{"id": "I-0001", "severity": "warn", "element": "C-0001",
                    "where": {"sheet": "S2", "at": [500, 500]}}],
    }

    render(model, tmp_path)

    first = (tmp_path / "S1.svg").read_text(encoding="utf-8")
    second = (tmp_path / "S2.svg").read_text(encoding="utf-8")
    assert "I-0001" not in first
    assert "I-0001" in second
    assert "outline-C-0001" in first
