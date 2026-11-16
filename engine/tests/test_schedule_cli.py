import json

import ezdxf

from tumo_engine.cli import main


def test_schedule_cli_exports_evidence_in_millimeters(tmp_path):
    source = tmp_path / "schedule.dxf"
    target = tmp_path / "results" / "schedule.json"
    doc = ezdxf.new(units=4)
    space = doc.modelspace()
    for text, at in (("门窗编号", (0, 1000)), ("洞口尺寸", (1000, 1000)),
                     ("C01", (0, 500)), ("1.2m×150cm", (1000, 500))):
        space.add_text(text, dxfattribs={"insert": at})
    doc.saveas(source)

    assert main(["schedule", str(source), "-o", str(target)]) == 0

    report = json.loads(target.read_text(encoding="utf-8"))
    row, = report["openings"]
    assert report["units"] == "mm"
    assert (row["mark"], row["width"], row["height"]) == ("C01", 1200, 1500)
    assert row["handles"]
