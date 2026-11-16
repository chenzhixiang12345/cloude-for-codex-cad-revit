from eval.score import compare, recall_regressions


def axis(identifier, x, label=None, reverse=False):
    points = [[x, 0], [x, 1000]]
    if reverse:
        points.reverse()
    return {"id": identifier, "label": label, "start": points[0], "end": points[1]}


def test_score_maximizes_matches_before_comparing_attributes():
    actual = {"grids": [axis("G1", 25, "B", reverse=True), axis("G2", -40, "A")]}
    expected = {"grids": [axis("G3", 0, "A"), axis("G4", 60, "B"), axis("G5", 200, "C")]}

    result = compare(actual, expected)["grid"]

    assert result["matched"] == 2
    assert result["precision"] == 1
    assert result["recall"] == 2 / 3
    assert result["attribute_accuracy"] == 1


def test_empty_truth_reports_undefined_recall():
    result = compare({"grids": [axis("G1", 0)]}, {})["grid"]
    assert result["precision"] == 0
    assert result["recall"] is None


def test_regression_ignores_undefined_baseline_and_checks_missing_categories():
    older = {"grid": {"recall": .90}, "column": {"recall": None}, "wall": {"recall": .80}}
    newer = {"grid": {"recall": .89}, "column": {"recall": .45}}

    assert recall_regressions(newer, older) == ["wall"]
