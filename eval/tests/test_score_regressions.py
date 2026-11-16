import json

from eval.score import compare, main


def test_uses_structural_elevation_when_architectural_elevation_is_null():
    model = {"levels": [{"id": "L1", "elevation_arch": None, "elevation_struct": 3600, "source": "table"}]}

    assert compare(model, model)["level"]["matched"] == 1


def test_does_not_match_coincident_walls_on_different_floors():
    wall = {
        "id": "W-1", "category": "wall", "base_level": "L1",
        "geometry": {"kind": "line", "start": [0, 0], "end": [6000, 0]},
    }
    expected = {"elements": [wall]}
    actual = {"elements": [{**wall, "base_level": "L2"}]}

    assert compare(actual, expected)["wall"]["matched"] == 0


def test_reads_baseline_before_replacing_output(tmp_path):
    path = tmp_path / "report.json"
    path.write_text(json.dumps({"sample": {"categories": {"wall": {"recall": 1}}}}), encoding="utf-8")

    assert main(["--smoke", "--baseline", str(path), "--output", str(path)]) == 1
