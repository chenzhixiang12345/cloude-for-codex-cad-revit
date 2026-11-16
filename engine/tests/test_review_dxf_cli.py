import hashlib

import ezdxf

from eval.synthetic import write_fixture
from tumo_engine.cli import main


def test_exports_vector_review_using_source_manifest(tmp_path):
    sample = tmp_path / "sample"
    job = write_fixture(sample)
    source = sample / "arch" / "standard-floor.dxf"
    original = hashlib.sha256(source.read_bytes()).digest()
    assert main(["run", str(job)]) == 0

    model = sample / "recognized" / "model.json"
    target = tmp_path / "review" / "vector.dxf"
    assert main(["render-dxf", str(model), "-o", str(target)]) == 0

    result = ezdxf.readfile(target)
    assert result.layers.has_entry("TUMO_REVIEW_WALL")
    assert result.layers.has_entry("TUMO_REVIEW_LABELS")
    assert hashlib.sha256(source.read_bytes()).digest() == original
