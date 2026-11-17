import json

import ezdxf
import pytest

from tumo_engine.cli import main, read_job
from tumo_engine.tables.beams import BeamCandidate


def test_job_paths_are_relative_to_job_file(tmp_path):
    path = tmp_path / "run" / "job.json"
    path.parent.mkdir()
    path.write_text(json.dumps({"input": "../sample.dxf", "output_dir": ".", "bounds": [1, 2, 3, 4]}),
                    encoding="utf-8-sig")

    drawing, output, bounds = read_job(path)

    assert drawing == tmp_path / "sample.dxf"
    assert output == path.parent
    assert bounds == (1, 2, 3, 4)


@pytest.mark.parametrize("bounds", [[1, 2, 1, 4], [1, 2, 3], [1, 2, "3", 4]])
def test_job_rejects_invalid_bounds(tmp_path, bounds):
    path = tmp_path / "job.json"
    path.write_text(json.dumps({"input": "in.dxf", "output_dir": "out", "bounds": bounds}), encoding="utf-8")
    with pytest.raises(ValueError, match="范围"):
        read_job(path)


def test_structural_evidence_exports_only_the_requested_beam_sheet(tmp_path, monkeypatch):
    from tumo_engine.tables import beams

    doc = ezdxf.new(units=4)
    space = doc.modelspace()
    for x, number in ((0, "S4001"), (12000, "S4002")):
        space.add_lwpolyline([(x, 0), (x + 8000, 0), (x + 8000, 6000), (x, 6000)],
                             close=True, dxfattribs={"layer": "SHEET-BORDER"})
        for y, label in ((500, number), (700, "三层顶梁平法施工图")):
            space.add_text(label, dxfattribs={"layer": "TITLE", "insert": (x + 6600, y)})
    source = tmp_path / "梁结构图.dxf"
    doc.saveas(source)
    output = tmp_path / "out" / "evidence.json"

    def read(drawing, items, rules):
        assert [item.text for item in items if item.kind == "text" and item.text.startswith("S400")] == ["S4002"]
        return [BeamCandidate("KL1", (12000, 1000), (15000, 1000), 200, 600, ("AA",), ("AB",),
                              (("AC",), ("AD",)), ("AE",), (("AF",), ("AG",)))], []

    monkeypatch.setattr(beams, "read_beam_candidates", read)
    assert main(["structural-evidence", "beams", str(source), "--sheet", "S4002", "-o", str(output)]) == 0
    result = json.loads(output.read_text(encoding="utf-8"))
    assert result["sheet"]["number"] == "S4002"
    assert result["candidates"][0]["section_handles"] == ["AE"]
    assert result["candidates"][0]["height"] == 600


def test_structural_evidence_reports_column_sections_without_building_columns(tmp_path, monkeypatch):
    from tumo_engine.tables import columns

    source = tmp_path / "墙柱结构图.dxf"
    ezdxf.new(units=4).saveas(source)
    output = tmp_path / "column-report.json"
    records = [{"sheet": {"number": "S3010"}, "mark": "GBZ1", "scope": "wall_boundary_component"},
               {"sheet": {"number": "S3020"}, "mark": "KZ1", "scope": "column"}]
    monkeypatch.setattr(columns, "read_column_schedules", lambda *args, **kwargs: (records, []))

    assert main(["structural-evidence", "columns", str(source), "--sheet", "S3010", "-o", str(output)]) == 0

    result = json.loads(output.read_text(encoding="utf-8"))
    assert result["candidates"] == records[:1]
