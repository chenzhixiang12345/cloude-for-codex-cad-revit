import pytest

from tumo_engine.export.model import Element
from tumo_engine.preprocess import Primitive
from tumo_engine.recognize.ramp import identify_ramps


def outline(points=((0, 0), (3600, 0), (3600, 1200), (0, 1200)), *, handle="B", closed=True):
    return Primitive("polyline", "XREF$0$A-RAMP-OUTLINE", tuple(points), (handle,), (), closed=closed)


def note(text, at, handle):
    return Primitive("text", "A-NOTE", (at,), (handle,), (), text=text)


def labels(slope="i = 1:12", high="坡顶标高 = ±0.000m", high_at=(3600, 600)):
    return [
        note("坡底标高 = -300 mm", (0, 600), "LOW"),
        note(high, high_at, "HIGH"),
        note(slope, (1800, 600), "GRADE"),
    ]


def in_sheet(items):
    return identify_ramps(items, sheet_id="A101", base_level="B1", top_level="L1")


def test_uses_confirmed_low_high_endpoints_and_consistent_slope():
    items = [outline(), *labels()]

    ramps, issues = in_sheet(items)

    [ramp] = ramps
    element = Element.model_validate(ramp)
    assert element.category == "ramp"
    assert element.geometry.kind == "polygon"
    assert element.base_level == "B1" and element.top_level == "L1"
    assert element.type.width == 1200
    assert element.params == {
        "low_point": [0, 600], "high_point": [3600, 600],
        "low_elevation": -300, "high_elevation": 0,
        "run_length": 3600, "rise": 300, "slope": pytest.approx(1 / 12),
    }
    assert element.source.handles == ["B", "GRADE", "HIGH", "LOW"]
    [issue] = issues
    assert issue["code"] == "ramp_native_unverified"
    assert issue["at"] == [1800, 600]
    assert issue["handles"] == element.source.handles
    assert in_sheet(list(reversed(items))) == (ramps, issues)


@pytest.mark.parametrize(
    ("items", "code"),
    [
        ([outline(), labels()[0], labels()[-1]], "ramp_levels_missing"),
        ([outline(), *labels(slope="坡度 10%")], "ramp_slope_mismatch"),
        ([outline(), *labels(high="坡顶标高 = -0.400m")], "ramp_levels_conflict"),
        ([outline(), *labels(high_at=(0, 900))], "ramp_direction_unclear"),
    ],
)
def test_insufficient_or_inconsistent_evidence_is_located(items, code):
    ramps, issues = in_sheet(items)

    assert not ramps
    [issue] = issues
    assert issue["code"] == code
    assert issue["at"] == [1800, 600]
    assert "B" in issue["handles"]


def test_selects_the_axis_of_ascent_even_when_it_is_the_shorter_side():
    items = [
        outline(((0, 0), (1200, 0), (1200, 3600), (0, 3600))),
        note("坡底标高 -0.120m", (1200, 1800), "LOW"),
        note("坡顶标高 0mm", (0, 1800), "HIGH"),
        note("SLOPE 10%", (600, 1800), "GRADE"),
    ]

    [ramp], _ = in_sheet(items)

    assert ramp["params"]["low_point"] == [1200, 1800]
    assert ramp["params"]["high_point"] == [0, 1800]
    assert ramp["params"]["run_length"] == 1200
    assert ramp["type"]["width"] == 3600


def test_corner_notes_on_a_square_do_not_establish_a_unique_ascent_axis():
    items = [
        outline(((0, 0), (3600, 0), (3600, 3600), (0, 3600))),
        note("坡底标高 -0.300m", (0, 0), "LOW"),
        note("坡顶标高 0mm", (3600, 3600), "HIGH"),
        note("坡度 1:12", (1800, 1800), "GRADE"),
    ]

    ramps, issues = in_sheet(items)

    assert not ramps
    assert [issue["code"] for issue in issues] == ["ramp_direction_unclear"]


def test_conflicting_grade_notes_do_not_place_a_ramp():
    items = [outline(), *labels(), note("坡度 10%", (1600, 600), "OTHER-GRADE")]

    ramps, issues = in_sheet(items)

    assert not ramps
    [issue] = issues
    assert issue["code"] == "ramp_slope_conflict"
    assert {"GRADE", "OTHER-GRADE"}.issubset(issue["handles"])


def test_ramp_annotation_without_a_confirmed_outline_is_located():
    ramps, issues = in_sheet([note("无障碍坡道", (2000, 900), "TAG")])

    assert not ramps
    assert [(issue["code"], issue["at"], issue["handles"]) for issue in issues] == [
        ("ramp_outline_missing", [2000, 900], ["TAG"]),
    ]


def test_open_ramp_outline_is_reported_without_guessing_the_closure():
    ramps, issues = in_sheet([outline(closed=False), *labels()])

    assert not ramps
    assert [issue["code"] for issue in issues] == ["ramp_outline_incomplete"]


def test_non_rectangular_ramp_is_retained_only_as_a_located_issue():
    polyline = outline(((0, 0), (3600, 0), (3600, 1200), (1800, 1200), (1800, 2100), (0, 2100)))
    ramps, issues = in_sheet([polyline, *labels()])

    assert not ramps
    [issue] = issues
    assert issue["code"] == "ramp_outline_unsupported"
    assert issue["at"]


def test_scale_annotation_without_a_slope_prefix_is_not_grade_evidence():
    ramps, issues = in_sheet([outline(), *labels(slope="1:12")])

    assert not ramps
    assert [issue["code"] for issue in issues] == ["ramp_slope_missing"]
