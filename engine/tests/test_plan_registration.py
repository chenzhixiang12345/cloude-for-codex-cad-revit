from math import cos, radians, sin

import pytest

from tumo_engine.recognize.plan_registration import register_plan


def _grid(label, start, end, source_label=None):
    trace = f"axis:label:{source_label or label}"
    return {"label": label, "start": start, "end": end, "evidence": [trace]}


def _reference():
    return [
        _grid("1", (0, -800), (0, 1200)),
        _grid("2", (2000, -500), (2000, 1200)),
        _grid("A", (-800, 0), (2400, 0)),
        _grid("A-2", (2400, 0), (6000, 1500), "A"),
    ]


def _alter(grids, point):
    return [{**grid, "start": point(grid["start"]), "end": point(grid["end"])} for grid in grids]


def test_registers_translation_using_original_axis_names_and_bent_segments():
    anchor = _reference()
    local = _alter(anchor, lambda p: (p[0] - 20000, p[1] + 50000))
    local[-1] = {**local[-1], "label": "A-5", "start": local[-1]["end"], "end": local[-1]["start"]}
    local.append(_grid("C", (0, 5000), (2000, 5000)))

    transform, diagnostic = register_plan(anchor, local)

    assert transform == {"dx": 20000, "dy": -50000, "rot_deg": 0}
    assert "3" in diagnostic and "4" in diagnostic


def test_requires_three_original_axis_names_and_independent_directions():
    anchor = _reference()
    transform, _ = register_plan(anchor, [anchor[0], *anchor[2:]])
    assert transform is None

    parallel = [
        _grid("1", (0, 0), (0, 1000)),
        _grid("2", (3000, 0), (3000, 1000)),
        _grid("3", (6000, 0), (6000, 1000)),
    ]
    transform, _ = register_plan(parallel, parallel)
    assert transform is None


def test_rejects_a_single_conflicting_shared_axis():
    anchor = _reference()
    local = list(anchor)
    local[1] = {**local[1], "start": (2200, -500), "end": (2200, 1200)}

    transform, diagnostic = register_plan(anchor, local)

    assert transform is None
    assert diagnostic


def test_rejects_reflection_even_when_infinite_supporting_lines_coincide():
    anchor = _reference()[:3]
    local = _alter(anchor, lambda p: (p[0], -p[1]))

    transform, diagnostic = register_plan(anchor, local)

    assert transform is None
    assert diagnostic


def test_rejects_rotated_plan():
    anchor = _reference()
    angle = radians(15)
    local = _alter(anchor, lambda p: (p[0] * cos(angle) - p[1] * sin(angle),
                                      p[0] * sin(angle) + p[1] * cos(angle)))

    transform, diagnostic = register_plan(anchor, local)

    assert transform is None
    assert diagnostic


@pytest.mark.parametrize("position", [120, float("nan")])
def test_rejects_ambiguous_or_invalid_geometry_for_a_shared_label(position):
    anchor = _reference()
    local = list(anchor)
    local.append(_grid("1-2", (position, -800), (position, 1200), "1"))

    transform, diagnostic = register_plan(anchor, local)

    assert transform is None
    assert "1" in diagnostic


def test_does_not_count_two_unverified_names_on_the_same_axis_as_distinct_evidence():
    anchor = _reference()[:3]
    aliases = {"label": "1", "start": (0, -800), "end": (0, 1200),
               "evidence": ["axis:label:1", "axis:label:2"]}

    transform, _ = register_plan([aliases, anchor[-1]], [aliases, anchor[-1]])

    assert transform is None


def test_rejects_translation_outside_a_finite_coordinate_range():
    anchor = [
        _grid("1", (1e308, 0), (1e308, 1000)),
        _grid("2", (1e308, 0), (1e308, 10000)),
        _grid("A", (1e308, 0), (1.001e308, 0)),
    ]
    local = _alter(anchor, lambda p: (-p[0], p[1]))

    transform, _ = register_plan(anchor, local)

    assert transform is None
