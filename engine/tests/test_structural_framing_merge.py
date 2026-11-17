import json

from test_structural_slab_merge import _model

from tumo_engine.cli import main
from tumo_engine.export.model import TumoModel
from tumo_engine.recognize.structural_framing_merge import merge_structural_framing


def _column(sheet, discipline, shift=0, *, identifier=None):
    return {"id": identifier or f"{sheet}-C", "category": "column", "discipline": discipline, "base_level": "L3",
            "top_level": "L4", "geometry": {"kind": "point", "at": [1500 + shift, 1500], "rotation_deg": 0},
            "type": {"mark": "KZ1", "width": 850, "depth": 600},
            "confidence": .95, "source": {"sheet": sheet, "handles": ["A"]}}


def _beam(sheet, shift=0):
    return {"id": f"{sheet}-B", "category": "beam", "discipline": "struct", "base_level": "L3",
            "geometry": {"kind": "line", "start": [2000 + shift, 1500], "end": [4000 + shift, 1500]},
            "base_offset": 850, "type": {"mark": "KL1", "width": 200, "height": 1500},
            "confidence": .93, "source": {"sheet": sheet, "handles": ["B"]}}


def _inputs():
    arch, struct = _model("arch", "A1", 7800), _model("struct", "S1", 7750, shift=40000)
    for model, field, elevation in ((arch, "elevation_arch", 10800), (struct, "elevation_struct", 10750)):
        model["levels"].append({"id": "L4", "name": "4F", field: elevation})
        model["sheets"][0]["levels"].append("L4")
    return arch, struct


def test_replaces_the_matching_column_remaps_its_relationships_and_preserves_beam_top():
    arch, struct = _inputs()
    wall = {"id": "A1-W", "category": "wall", "discipline": "arch", "type": {"thickness": 200},
            "geometry": {"kind": "line", "start": [0, 1500], "end": [1500, 1500]},
            "base_level": "L3", "confidence": .9, "source": {"sheet": "A1", "handles": ["W"]}}
    arch["elements"] = [_column("A1", "arch"), wall]
    arch["relations"] = [{"kind": "join", "a": "A1-W", "b": "A1-C", "priority": "column_cuts_wall"}]
    arch["issues"] = [{"id": "I1", "severity": "warn", "message": "复核", "element": "A1-C"}]
    struct["elements"] = [_column("S1", "struct", shift=40000), _beam("S1", shift=40000)]

    result = merge_structural_framing(arch, struct)

    TumoModel.model_validate(result)
    assert {e["id"] for e in result["elements"]} == {"A1-W", "S1-C", "S1-B"}
    assert result["relations"][0]["b"] == result["issues"][0]["element"] == "S1-C"
    column, beam = [e for e in result["elements"] if e["discipline"] == "struct"]
    assert (column["base_offset"], column["top_offset"], beam["base_offset"]) == (-50, -50, 800)
    assert result["sheets"][1]["transform"] == {"dx": -40000, "dy": 0, "rot_deg": 0}


def test_leaves_unmatched_overlapping_column_as_a_locatable_issue():
    arch, struct = _inputs()
    arch["elements"] = [_column("A1", "arch")]
    struct["elements"] = [_column("S1", "struct", shift=40000)]
    struct["elements"][0]["geometry"]["at"][0] += 200

    result = merge_structural_framing(arch, struct)

    assert result["elements"] == arch["elements"]
    assert any("重叠" in issue["message"] and issue["where"]["sheet"] == "S1" for issue in result["issues"])


def test_building_job_selects_column_and_beam_drawings_independently(tmp_path, monkeypatch):
    from tumo_engine import building_pipeline, pipeline

    arch, structural = _inputs()
    column = _column("S1", "struct")
    beams = _model("struct", "S2", 7750)
    beams["elements"] = [_beam("S2")]
    structural["elements"] = [column]
    choices = {("S1", True): structural, ("S2", False): beams}
    sources = [tmp_path / name for name in ("A1.dxf", "S1.dxf", "S2.dxf")]
    for source in sources:
        source.touch()
    monkeypatch.setattr(building_pipeline, "run_building", lambda _: arch)
    monkeypatch.setattr(pipeline, "run", lambda _, **k: choices[(k["sheet_number"], k["repeat_standard_floors"])])
    job_file = tmp_path / "job.json"
    job_file.write_text(json.dumps({"input": "A1.dxf", "output_dir": "output", "structural_columns": "S1.dxf",
                                    "column_sheet": "S1", "structural_beams": "S2.dxf", "beam_sheet": "S2"}),
                        encoding="utf-8")

    assert main(["run", str(job_file), "--merge-floors"]) == 0
    merged = json.loads((tmp_path / "output" / "model.json").read_text(encoding="utf-8"))
    assert [e["id"] for e in merged["elements"]] == ["S1-C", "S2-B"]
    assert [s["file"] for s in merged["sheets"]] == ["A1.dxf", "S1.dxf", "S2.dxf"]
