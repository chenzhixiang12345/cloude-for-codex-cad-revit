import ezdxf

from tumo_engine.preprocess import Primitive
from tumo_engine.recognize import elevation
from tumo_engine.recognize.elevation import recognize_elevation_annotations


def _text(handle, value, layer="AD-NOTE-CRCY", at=(0, 0), parent=()):
    return Primitive("text", layer, (at,), (*parent, handle), (), text=value)


def test_explicit_window_sill_keeps_floor_mark_value_and_drawing_source():
    note = _text("12", "三层 C0814c 窗台高度：0.900 米", at=(140, 230))

    annotations, issues = recognize_elevation_annotations([note], "section.dxf")

    assert issues == []
    sill, = annotations
    assert sill["kind"] == "sill_height"
    assert (sill["level"], sill["mark"], sill["value_mm"]) == ("L3", "C0814C", 900)
    assert (sill["source_unit"], sill["unit_basis"]) == ("m", "explicit")
    assert sill["source"] == {"file": "section.dxf", "handles": ["12"]}
    assert sill["at"] == [140, 230]
    assert sill["text"] == note.text


def test_unlocated_range_and_multiple_window_sill_notes_are_reported():
    notes = [
        _text("1", "C0814 窗台高 900 mm"),
        _text("2", "3F/4F C0814 窗台高 900 mm"),
        _text("3", "3F C0814/C1014 窗台高 900 mm"),
        _text("4", "3～5层 C0814 窗台高 900 mm"),
        _text("5", "3F 窗台高 900 mm"),
    ]

    annotations, issues = recognize_elevation_annotations(notes, "section.dxf")

    assert annotations == []
    assert [issue["code"] for issue in issues] == ["unlocated_sill"] * 5
    assert [issue["source"]["handles"] for issue in issues] == [["1"], ["2"], ["3"], ["4"], ["5"]]


def test_incompatible_annotations_for_same_window_report_both_sources():
    notes = [
        _text("1", "3F C0814 窗台高 900 毫米"),
        _text("2", "3F C0814 窗台高 950 mm"),
        _text("3", "4F C0814 窗台高 900 mm"),
    ]

    annotations, issues = recognize_elevation_annotations(notes, "section.dxf")

    assert [(note["level"], note["value_mm"]) for note in annotations] == [("L4", 900)]
    issue, = issues
    assert issue["code"] == "conflicting_sill"
    assert (issue["level"], issue["mark"]) == ("L3", "C0814")
    assert issue["source"]["handles"] == ["1", "2"]


def test_relative_heights_and_absolute_elevations_are_distinct():
    notes = [
        _text("1", "B1F TC2118 窗台标高 −3.200m"),
        _text("2", "女儿墙高 1200mm"),
        _text("3", "女儿墙顶标高 +18.400m"),
        _text("4", "第二层建筑标高 3.600m"),
        _text("5", "±0.000", "AD-LEVL-HIGH"),
        _text("6", "4.200", "AD-NOTE-CRCY"),
        _text("7", "女儿墙", "AD-LEVL-HIGH"),
    ]

    annotations, issues = recognize_elevation_annotations(notes, "section.dxf")

    assert [(note["kind"], note["level"], note["mark"], note["value_mm"]) for note in annotations] == [
        ("sill_elevation", "LB1", "TC2118", -3200),
        ("parapet_height", None, None, 1200),
        ("parapet_elevation", None, None, 18400),
        ("elevation", "L2", None, 3600),
        ("elevation", None, None, 0),
    ]
    assert annotations[-1]["unit_basis"] == "elevation_format"
    assert [issue["source"]["handles"] for issue in issues] == [["7"]]


def test_unitless_sill_is_reported_and_explicit_drawing_units_override_fallback():
    notes = [
        _text("1", "3F C0814 窗台高 900"),
        _text("2", "标高单位为米"),
        _text("3", "3F 标高 8.2"),
    ]

    annotations, issues = recognize_elevation_annotations(notes, "section.dxf", default_elevation_unit="mm")

    elevation, = annotations
    assert (elevation["value_mm"], elevation["source_unit"], elevation["unit_basis"]) == (8200, "m", "drawing_note")
    assert elevation["source"]["handles"] == ["3", "2"]
    assert [issue["code"] for issue in issues] == ["annotation_unit_unknown"]


def test_level_block_pairs_value_with_unique_label_and_preserves_parapet_elevation():
    notes = [
        _text("2", "(24F)", "AD-LEVL-HIGH", (900, 100), ("A",)),
        _text("3", "70.800", at=(0, 90), parent=("A",)),
        _text("4", "(RF)", "AD-LEVL-HIGH", (900, 3100), ("A",)),
        _text("5", "73.800", at=(0, 3090), parent=("A",)),
        _text("6", "女儿墙", "AD-LEVL-HIGH", (900, 3800), ("A",)),
        _text("7", "74.500", at=(0, 3790), parent=("A",)),
        _text("8", "75.000", at=(200, 3800), parent=("B",)),
    ]

    annotations, issues = recognize_elevation_annotations(notes, "section.dxf")

    assert issues == []
    assert [(note["kind"], note["level"], note["value_mm"]) for note in annotations] == [
        ("elevation", "L24", 70800),
        ("elevation", "LRF", 73800),
        ("parapet_elevation", None, 74500),
    ]
    assert annotations[-1]["source"] == {"file": "section.dxf", "handles": ["A", "6", "7"]}
    assert annotations[-1]["at"] == [0, 3790]


def test_competing_same_block_values_are_not_attached_to_a_floor():
    notes = [
        _text("1", "(2F)", "AD-LEVL-HIGH", (900, 100), ("A",)),
        _text("2", "3.600", at=(0, 110), parent=("A",)),
        _text("3", "3.650", at=(100, 90), parent=("A",)),
    ]

    annotations, issues = recognize_elevation_annotations(notes, "section.dxf")

    assert annotations == []
    issue, = issues
    assert issue["code"] == "ambiguous_elevation_row"
    assert issue["source"]["handles"] == ["A", "1", "2", "3"]


def test_drawing_layer_legend_numbers_are_not_elevation_readings():
    notes = [
        _text("1", "61", "AD-LEVL-HIGH"),
        _text("2", "61000 mm", "AD-LEVL-HIGH"),
    ]

    annotations, issues = recognize_elevation_annotations(notes, "section.dxf", default_elevation_unit="mm")

    assert [(note["value_mm"], note["source"]["handles"]) for note in annotations] == [(61000, ["2"])]
    assert issues == []


def test_basement_sill_resolves_negative_chinese_floor_and_does_not_approximate_height():
    notes = [
        _text("1", "负一层 C0814 窗台高 900mm"),
        _text("2", "地下二层 C0814 窗台高约900mm"),
    ]

    annotations, issues = recognize_elevation_annotations(notes, "section.dxf")

    sill, = annotations
    assert (sill["level"], sill["value_mm"]) == ("LB1", 900)
    assert [issue["code"] for issue in issues] == ["annotation_no_value"]


def test_sign_and_bare_value_need_elevation_context_and_contradictory_units_are_reported():
    notes = [
        _text("1", "本图标高均以米为单位。"),
        _text("2", "结构标高单位为毫米。"),
        _text("3", "2F 标高 5.6"),
        _text("4", "2F 结构标高 5580 mm"),
    ]

    annotations, issues = recognize_elevation_annotations(notes, "section.dxf", default_elevation_unit="mm")

    level, = annotations
    assert (level["value_mm"], level["elevation_type"]) == (5580, "struct")
    assert [issue["code"] for issue in issues] == ["annotation_unit_unknown"]


def test_same_block_dimension_text_is_not_an_elevation_row_candidate():
    notes = [
        _text("1", "(2F)", "AD-LEVL-HIGH", (900, 100), ("A",)),
        _text("2", "3.600", at=(0, 110), parent=("A",)),
        _text("3", "3250mm", "AD-DIMS-OTSD", (100, 90), ("A",)),
    ]

    annotations, issues = recognize_elevation_annotations(notes, "section.dxf")

    level, = annotations
    assert (level["level"], level["value_mm"]) == ("L2", 3600)
    assert issues == []


def test_repeated_equal_sill_keeps_both_sources_in_one_unambiguous_record():
    notes = [
        _text("1", "3F C0814 窗台高 900 mm"),
        _text("2", "3F C0814 窗台高 0.900 m"),
    ]

    annotations, issues = recognize_elevation_annotations(notes, "section.dxf")

    sill, = annotations
    assert sill["value_mm"] == 900
    assert sill["source"]["handles"] == ["1", "2"]
    assert issues == []


def test_mark_digits_are_not_used_as_a_floor_number():
    notes = [_text("1", "C0814层 窗台高 900 mm")]

    annotations, issues = recognize_elevation_annotations(notes, "section.dxf")

    assert annotations == []
    assert [issue["code"] for issue in issues] == ["unlocated_sill"]


def test_explicit_story_height_keeps_its_floor_and_absolute_elevation_is_separate():
    notes = [
        _text("1", "首层层高 4.2m"),
        _text("2", "首层标高 +0.600m"),
    ]

    annotations, issues = recognize_elevation_annotations(notes, "section.dxf")

    assert [(note["kind"], note["level"], note["value_mm"]) for note in annotations] == [
        ("story_height", "L1", 4200),
        ("elevation", "L1", 600),
    ]
    assert issues == []


def test_read_dxf_keeps_relative_path_and_normalizes_drawing_coordinates(tmp_path):
    directory = tmp_path / "drawings"
    directory.mkdir()
    path = directory / "section.dxf"
    drawing = ezdxf.new(units=6)
    note = drawing.modelspace().add_text(
        "3F C0814 窗台高 900mm", dxfattribs={"insert": (0.14, 0.23)},
    )
    drawing.saveas(path)

    annotations, issues = elevation.read_elevation_drawing(path, tmp_path)

    assert issues == []
    sill, = annotations
    assert sill["source"] == {"file": "drawings/section.dxf", "handles": [note.dxf.handle]}
    assert sill["at"] == [140, 230]
