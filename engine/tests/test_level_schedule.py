from tumo_engine.preprocess import Primitive
from tumo_engine.tables.levels import read_level_schedule


def cells(rows, *, x=0, y=0, layer="AD-层高表"):
    return [
        Primitive("text", layer, ((x + dx, y + dy),), (handle,), (), text=text)
        for dx, dy, text, handle in rows
    ]


def test_reads_explicit_architectural_elevation_and_height_units():
    items = cells([
        (0, 0, "层数", "A"), (1750, 0, "标高H(m)", "B"), (3100, 0, "层高(mm)", "C"),
        (50, -500, "R1F", "D"), (1743, -505, "73.800", "E"), (3100, -500, "-", "F"),
        (50, -925, "24F", "G"), (1743, -915, "70.800", "H"), (3100, -925, "3000", "I"),
        (50, -1350, "23F", "J"), (1743, -1340, "67", "K"), (3100, -1350, "3000", "L"),
    ])

    rows, warnings = read_level_schedule(items, discipline="arch", layer_pattern=r"层高表")

    assert [(row.id, row.elevation_arch, row.height) for row in rows] == [
        ("L23", 67000, 3000), ("L24", 70800, 3000), ("LR1F", 73800, None),
    ]
    assert rows[1].to_level() == {
        "id": "L24", "name": "24F", "elevation_arch": 70800, "source": "楼层表", "confidence": 0.9,
    }
    assert set(rows[1].handles) >= {"G", "H", "I"}
    assert not warnings


def test_reads_structural_bare_floor_numbers_and_rows_above_heading():
    items = cells([
        (0, 0, "层号", "A"), (1205, 0, "标高(m)", "B"), (2440, 0, "层高(m)", "C"),
        (0, 940, "-1", "D"), (1205, 784, "-5.250", "E"), (2445, 934, "4.000", "F"),
        (0, 1640, "架空层（商业）", "G"), (1205, 1484, "0.550", "H"), (2445, 1634, "5.800", "I"),
        (0, 2340, "1", "J"), (1205, 2184, "4.750", "K"), (2445, 2334, "3.000", "L"),
    ], layer="S_TABL_NUMB")

    rows, warnings = read_level_schedule(items, discipline="struct", layer_pattern=r"层高表")

    by_name = {row.name: row for row in rows}
    assert by_name["B1F"].elevation_struct == -5250
    assert by_name["架空层(商业)"].elevation_struct == 550
    assert by_name["1F"].height == 3000
    assert all(row.elevation_arch is None for row in rows)
    assert not warnings


def test_keeps_architectural_and_structural_columns_distinct():
    items = cells([
        (0, 0, "楼层", "A"), (1200, 0, "建筑标高(mm)", "B"), (2400, 0, "结构标高(m)", "C"),
        (0, -500, "三层", "D"), (1200, -500, "7800", "E"), (2400, -500, "7.750", "F"),
    ])

    [row], warnings = read_level_schedule(items, discipline="struct")

    assert (row.number, row.name, row.elevation_arch, row.elevation_struct) == (3, "3F", 7800, 7750)
    assert not warnings


def test_conflicting_architectural_elevations_do_not_overwrite_reliable_structural_value():
    first = cells([
        (0, 0, "楼层", "A"), (1200, 0, "建筑标高(mm)", "B"), (2400, 0, "结构标高(m)", "C"),
        (0, -500, "三层", "D"), (1200, -500, "7800", "E"), (2400, -500, "7.750", "F"),
    ])
    second = cells([
        (0, 0, "楼层", "G"), (1200, 0, "建筑标高(mm)", "H"),
        (0, -500, "3F", "I"), (1200, -500, "7900", "J"),
    ], x=5500)

    [row], [warning] = read_level_schedule([*first, *second], discipline="arch")

    assert row.elevation_arch is None
    assert row.elevation_struct == 7750
    assert "3F" in warning and "7800" in warning and "7900" in warning
    assert "E" in row.handles and "J" in row.handles


def test_notes_without_a_complete_header_do_not_form_a_level_schedule():
    items = cells([
        (0, 0, "建筑标高说明", "A"), (1200, 0, "2F", "B"),
        (0, -500, "楼层", "C"), (1200, -500, "标准层高度", "D"),
        (0, -1000, "3F", "E"), (1200, -1000, "7800", "F"),
    ], layer="AD-NOTE")

    assert read_level_schedule(items, discipline="arch") == ([], [])


def test_does_not_read_distant_unrelated_floor_numbers_as_contiguous_schedule():
    items = cells([
        (0, 0, "层数", "A"), (1200, 0, "建筑标高(mm)", "B"),
        (0, -500, "1F", "C"), (1200, -500, "0", "D"),
        (0, -4000, "2F", "E"), (1200, -4000, "3000", "F"),
    ])

    [row], warnings = read_level_schedule(items, discipline="arch")

    assert row.number == 1
    assert not warnings
