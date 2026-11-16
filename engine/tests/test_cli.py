import json

import pytest

from tumo_engine.cli import read_job


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
