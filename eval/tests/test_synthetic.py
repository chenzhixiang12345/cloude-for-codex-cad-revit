"""Execute the installed engine CLI against a known synthetic floor plan."""

import os
import subprocess
import sys

from eval.synthetic import check_fixture, write_fixture


def test_synthetic_floor_plan_scores_perfectly_through_cli(tmp_path):
    job = write_fixture(tmp_path)
    process = subprocess.run(
        [sys.executable, "-m", "tumo_engine", "run", str(job)],
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        capture_output=True, encoding="utf-8", check=False, timeout=30,
    )
    assert process.returncode == 0, process.stderr
    metrics = check_fixture(tmp_path)
    assert set(metrics) == {"column", "grid", "level", "wall"}
    for values in metrics.values():
        assert values["precision"] == 1
        assert values["recall"] == 1
        assert values["attribute_accuracy"] == 1
