from copy import deepcopy
import json

from tumo_engine.cli import main
from tumo_engine.export.model import TumoModel
from tumo_engine.recognize.structural_slab_merge import merge_structural_slabs


def _grid(identifier, index, start, end, sheet, shift=0):
    return {"id": identifier, "label": index, "start": [start[0] + shift, start[1]],
            "end": [end[0] + shift, end[1]], "confidence": 1, "source": {"sheet": sheet, "handles": []}}


def _model(discipline, sheet, elevation, shift=0):
    return {
        "schema_version": "0.1", "project": {"name": "fixture", "units": "mm", "origin": {}},
        "sheets": [{"id": sheet, "file": sheet + ".dxf", "title": sheet, "discipline": discipline,
                    "kind": "floor_plan", "levels": ["L3"]}],
        "levels": [{"id": "L3", "name": "3F", f"elevation_{discipline}": elevation}],
        "grids": [_grid(f"{sheet}-G1", "1", (0, 0), (0, 6000), sheet, shift),
                  _grid(f"{sheet}-G2", "2", (4000, 0), (4000, 6000), sheet, shift),
                  _grid(f"{sheet}-GA", "A", (0, 1500), (4000, 1500), sheet, shift)],
        "elements": [], "relations": [], "issues": [],
    }


def _slab(sheet, discipline, *, shift=0, offset=-500):
    return {"id": f"{sheet}-SL", "category": "slab", "discipline": discipline, "base_level": "L3",
            "geometry": {"kind": "polygon", "points": [[500 + shift, 500], [2000 + shift, 500],
                                                         [2000 + shift, 1800], [500 + shift, 1800]]},
            "base_offset": offset, "type": {"mark": "LB1", "thickness": 150},
            "confidence": .93, "source": {"sheet": sheet, "handles": ["AA"]}}


def test_registered_structural_slab_preserves_absolute_elevation_and_source_frame():
    building = _model("arch", "A1", 7800)
    structural = _model("struct", "S1", 7750, shift=40000)
    structural["elements"].append(_slab("S1", "struct", shift=40000))
    original = deepcopy((building, structural))

    model = merge_structural_slabs(building, structural)

    TumoModel.model_validate(model)
    assert (building, structural) == original
    assert model["levels"] == [{"id": "L3", "name": "3F", "elevation_arch": 7800, "elevation_struct": 7750}]
    assert model["sheets"][1]["transform"] == {"dx": -40000, "dy": 0, "rot_deg": 0}
    [slab] = model["elements"]
    assert slab["base_offset"] == -550
    assert slab["geometry"] == structural["elements"][0]["geometry"]
    assert slab["source"]["sheet"] == "S1"
    assert model["grids"] == building["grids"]


def test_unregistered_structure_does_not_add_sheets_levels_or_slabs():
    building = _model("arch", "A1", 7800)
    structural = _model("struct", "S1", 7750, shift=40000)
    structural["grids"].pop()
    structural["elements"].append(_slab("S1", "struct", shift=40000))

    model = merge_structural_slabs(building, structural)

    assert model["elements"] == []
    assert model["sheets"] == building["sheets"]
    assert model["levels"] == building["levels"]
    assert "共同轴号" in model["issues"][0]["message"]


def test_registered_structural_slab_reports_overlap_on_same_floor_without_duplicating_slabs():
    building = _model("arch", "A1", 7800)
    building["elements"].append(_slab("A1", "arch"))
    structural = _model("struct", "S1", 7750, shift=40000)
    structural["elements"].append(_slab("S1", "struct", shift=40000))

    model = merge_structural_slabs(building, structural)

    assert model["elements"] == building["elements"]
    [issue] = model["issues"]
    assert "重叠" in issue["message"]
    assert issue["where"]["sheet"] == "S1"


def test_conflicting_structural_datum_rejects_only_the_affected_floor():
    building = _model("arch", "A1", 7800)
    building["levels"][0]["elevation_struct"] = 7770
    structural = _model("struct", "S1", 7750)
    structural["elements"].append(_slab("S1", "struct"))

    model = merge_structural_slabs(building, structural)

    assert not model["elements"]
    assert model["levels"] == building["levels"]
    assert "结构标高冲突" in model["issues"][0]["message"]


def test_merge_command_retains_both_sources_and_local_coordinates(tmp_path, monkeypatch):
    from tumo_engine import building_pipeline, pipeline

    building = _model("arch", "A1", 7800)
    structural = _model("struct", "S1", 7750, shift=40000)
    structural["elements"].append(_slab("S1", "struct", shift=40000))
    arch_file = tmp_path / "建筑" / "A1.dxf"
    struct_file = tmp_path / "结构" / "S1.dxf"
    for path in (arch_file, struct_file):
        path.parent.mkdir()
        path.touch()
    monkeypatch.setattr(building_pipeline, "run_building", lambda source: building)
    monkeypatch.setattr(pipeline, "run", lambda source, **kwargs: structural)
    output = tmp_path / "output"

    assert main(["run", str(arch_file), "--merge-floors", "--structural-slab", str(struct_file), "-o", str(output)]) == 0

    model = json.loads((output / "model.json").read_text(encoding="utf-8"))
    manifest = json.loads((output / "sources.json").read_text(encoding="utf-8"))
    assert manifest["root"] == str(tmp_path)
    assert [sheet["file"] for sheet in model["sheets"]] == ["建筑/A1.dxf", "结构/S1.dxf"]
    assert model["elements"][0]["base_offset"] == -550

    job = tmp_path / "job.json"
    job.write_text(json.dumps({"input": "建筑/A1.dxf", "structural_slab": "结构/S1.dxf", "output_dir": "job-output"}),
                   encoding="utf-8")
    assert main(["run", str(job), "--merge-floors"]) == 0
    from_job = json.loads((tmp_path / "job-output" / "model.json").read_text(encoding="utf-8"))
    assert from_job == model
