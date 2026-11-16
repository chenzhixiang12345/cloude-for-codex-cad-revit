import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator
from pydantic import ValidationError

from tumo_engine.export.model import TumoModel


def sample_model():
    return {
        "schema_version": "0.1",
        "project": {"name": "合成项目", "units": "mm", "origin": {"grid": ["1", "A"]}},
        "sheets": [{
            "id": "S-A-01", "file": "arch/example.dxf", "discipline": "arch",
            "kind": "floor_plan", "title": "标准层", "levels": ["L1", "L2"],
            "scale": 100, "transform": {"dx": 50, "dy": 25, "rot_deg": 0},
        }],
        "levels": [
            {"id": "L1", "name": "1F", "elevation_arch": 0, "elevation_struct": -50},
            {"id": "L2", "name": "2F", "elevation_arch": 3100},
        ],
        "grids": [{"id": "G-1", "label": "1", "start": [0, 0], "end": [0, 6000],
                   "confidence": 0.98}],
        "elements": [
            {
                "id": "W-0012", "category": "wall", "discipline": "arch",
                "type": {"name": "砌块墙", "thickness": 200},
                "geometry": {"kind": "line", "start": [0, 0], "end": [6000, 0]},
                "base_level": "L1", "top_level": "L2", "params": {"mark": None},
                "confidence": 0.93, "evidence": ["parallel_pair:200"],
                "source": {"sheet": "S-A-01", "handles": ["1A", "1B"]},
            },
            {
                "id": "C-0001", "category": "column",
                "type": {"name": "矩形柱", "width": 450, "depth": 400, "mark": "KZ1"},
                "geometry": {"kind": "point", "at": [0, 0], "rotation_deg": 90},
                "confidence": 0.9, "source": {"sheet": "S-A-01", "handles": ["2B"]},
            },
            {
                "id": "D-0007", "category": "door", "host": "W-0012",
                "type": {"mark": "M1021", "width": 1000, "height": 2100},
                "geometry": {"kind": "point", "at": [2500, 0], "facing": [0, 1], "hand": "left"},
                "sill_height": 0, "confidence": 0.88,
                "source": {"sheet": "S-A-01", "handles": ["3B"]},
            },
        ],
        "relations": [{"kind": "join", "a": "W-0012", "b": "C-0001",
                       "priority": "column_cuts_wall"}],
        "issues": [{"id": "I-0004", "severity": "warn", "element": "D-0007",
                    "message": "门高需要核对", "where": {"sheet": "S-A-01", "at": [2500, 0]}}],
    }


@pytest.fixture
def model_schema():
    path = Path(__file__).resolve().parents[2] / "schema" / "tumo-model.schema.json"
    return json.loads(path.read_text(encoding="utf-8"))


def test_model_matches_committed_schema(model_schema):
    Draft202012Validator.check_schema(model_schema)
    assert model_schema == TumoModel.model_json_schema()


def test_model_round_trip_preserves_contract_fields(model_schema):
    data = sample_model()
    model = TumoModel.model_validate(data)
    output = json.loads(model.model_dump_json(exclude_unset=True))
    Draft202012Validator(model_schema).validate(output)
    assert output == data


@pytest.mark.parametrize(
    ("path", "invalid"),
    [
        (("schema_version",), "0.2"),
        (("project", "units"), "m"),
        (("sheets", 0, "scale"), 0),
        (("grids", 0, "end"), [10, 20, 30]),
        (("elements", 0, "confidence"), 1.1),
        (("elements", 0, "type", "thickness"), True),
        (("elements", 0, "type", "thickness"), 0),
        (("elements", 0, "category"), "air_duct"),
        (("elements", 0, "geometry", "start"), ["10", 20]),
        (("issues", 0, "severity"), "unknown"),
    ],
)
def test_contract_rejects_invalid_fields(model_schema, path, invalid):
    data = sample_model()
    container = data
    for field in path[:-1]:
        container = container[field]
    container[path[-1]] = invalid
    with pytest.raises(ValidationError):
        TumoModel.model_validate(data)
    assert not Draft202012Validator(model_schema).is_valid(data)
