from shapely.geometry import Polygon

from tumo_engine.preprocess import Primitive
from tumo_engine.recognize.slab import recognize_slabs


def rectangle(layer, extents, handle, *, kind="polyline"):
    x0, y0, x1, y1 = extents
    points = ((x0, y0), (x1, y0), (x1, y1), (x0, y1))
    return Primitive(kind, layer, points, (handle,), (), closed=True)


def label(value, at, handle, layer="A-SLAB-TEXT"):
    return Primitive("text", layer, (at,), (handle,), (), text=value)


def test_publishes_a_closed_slab_with_local_thickness_and_provenance():
    outline = rectangle("XREF$0$A-SLAB", (0, 0, 6000, 4000), "AA")
    thickness = label("LB2 h=120", (1500, 1700), "AB", "S_FLOR_TEXT_BOT")

    [slab], openings, warnings = recognize_slabs([outline, thickness])

    assert slab["category"] == "slab"
    assert slab["type"]["thickness"] == 120
    assert slab["type"]["mark"] == "LB2"
    assert slab["base_offset"] == 0
    assert slab["geometry"]["kind"] == "polygon"
    assert Polygon(slab["geometry"]["points"]).equals(Polygon(outline.points))
    assert set(slab["handles"]) == {"AA", "AB"}
    assert openings == warnings == []


def test_marks_sunken_slab_at_an_explicit_offset_and_preserves_adjacent_slab():
    main = rectangle("A-SLAB", (0, 0, 6000, 4000), "AA")
    lowered = rectangle("A-SLAB-DROP", (6000, 0, 8000, 2000), "BA", kind="hatch")
    items = [main, lowered, label("板厚 120 mm", (1500, 1700), "AB"),
             label("板厚120\nH-0.050", (6900, 800), "BB")]

    slabs, openings, warnings = recognize_slabs(items)

    assert [(slab["base_offset"], slab["type"]["thickness"]) for slab in slabs] == [(0, 120), (-50, 120)]
    assert set(slabs[1]["handles"]) == {"BA", "BB"}
    assert openings == warnings == []
    assert recognize_slabs(list(reversed(items))) == (slabs, openings, warnings)


def test_attaches_dedicated_opening_to_the_containing_slab():
    outline = rectangle("A-SLAB", (0, 0, 6000, 4000), "AA")
    hole = rectangle("A-SLAB-OPENING", (1000, 1000, 1300, 1400), "AC")
    ventilation = rectangle("AE-HOLE", (2000, 1000, 2300, 1400), "AD")
    touching = rectangle("SLAB-HOLE", (5900, 2000, 6000, 2500), "AE")
    items = [outline, hole, ventilation, touching, label("板厚=120", (1500, 1700), "AB")]

    [slab], [(opening, host)], warnings = recognize_slabs(items)

    assert host is slab
    assert opening["category"] == "slab_opening"
    assert opening["handles"] == ["AC"]
    assert Polygon(opening["geometry"]["points"]).area == 120_000
    assert len(warnings) == 1
    assert "洞口" in warnings[0][0]


def test_ignores_room_floor_finishes_and_sheet_frame_even_when_they_contain_a_note():
    shape = (0, 0, 6000, 4000)
    items = [rectangle("AD-NAME-ROOM", shape, "A"), rectangle("AE-FLOR", shape, "B"),
             rectangle("SHEET-SLAB-BORDER", shape, "C"), label("板厚 120", (1500, 1700), "D")]

    assert recognize_slabs(items) == ([], [], [])


def test_reports_unsupported_thickness_and_offset_instead_of_using_legend_or_guess():
    items = [rectangle("A-SLAB", (0, 0, 6000, 4000), "AA"),
             rectangle("SLAB-DROP", (6000, 0, 8000, 2000), "BA"),
             label("未注明板均为 LB1，板厚为120mm。", (1500, 1700), "AB"),
             label("板厚 100", (7000, 900), "BB")]

    slabs, openings, warnings = recognize_slabs(items)

    assert slabs == openings == []
    assert len(warnings) == 2
    assert "板厚" in warnings[0][0]
    assert "标高" in warnings[1][0]


def test_rejects_conflicting_labels_or_overlapping_slab_regions():
    first = rectangle("SLAB", (0, 0, 6000, 4000), "AA")
    second = rectangle("SLAB-DROP", (5000, 0, 8000, 2000), "BA")
    first_thickness = label("厚120", (1500, 1700), "AB")
    second_thickness = label("板厚100\n降板50 mm", (7000, 900), "BB")

    slabs, _, warnings = recognize_slabs([first, first_thickness, label("板厚=130", (2000, 1800), "AC")])
    assert slabs == []
    assert "板厚" in warnings[0][0]

    slabs, _, warnings = recognize_slabs([first, second, first_thickness, second_thickness])
    assert slabs == []
    assert len(warnings) == 2
    assert all("重叠" in message for message, _ in warnings)


def test_accepts_configured_boundary_layer_and_thickness_note_in_metres():
    outline = rectangle("PROJECT-FLOOR-BND", (0, 0, 6000, 4000), "AA")
    items = [outline, label("板厚 0.15 m", (1500, 1700), "AB")]

    assert recognize_slabs(items) == ([], [], [])
    rules = {"layers": {"slab_boundaries": "^PROJECT-FLOOR-BND$"}}
    [slab], _, _ = recognize_slabs(items, rules)
    assert slab["type"]["thickness"] == 150


def test_specific_opening_layer_takes_precedence_over_a_broad_configured_boundary():
    outline = rectangle("PROJECT-SLAB", (0, 0, 6000, 4000), "AA")
    hole = rectangle("A-SLAB-OPENING", (1000, 1000, 1300, 1400), "AC")
    items = [outline, hole, label("板厚为120mm。", (1500, 1700), "AB")]

    [slab], [(opening, host)], warnings = recognize_slabs(items, {"layers": {"slab_boundaries": "SLAB"}})

    assert host is slab
    assert opening["handles"] == ["AC"]
    assert warnings == []


def test_duplicate_boundary_paths_merge_without_duplicate_slabs():
    outline = rectangle("SLAB", (0, 0, 6000, 4000), "AA")
    same_outline = rectangle("SLAB", (0, 0, 6000, 4000), "AB", kind="hatch")
    items = [outline, same_outline, label("板厚 120 mm", (1500, 1700), "AC")]

    [slab], _, warnings = recognize_slabs(items)

    assert set(slab["handles"]) == {"AA", "AB", "AC"}
    assert warnings == []
