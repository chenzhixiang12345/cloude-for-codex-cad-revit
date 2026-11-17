from math import isclose

import ezdxf
from shapely.geometry import LineString, Point, Polygon

from tumo_engine.pipeline import run


def wall_footprint(wall):
    geometry = wall["geometry"]
    return LineString((geometry["start"], geometry["end"])).buffer(
        wall["type"]["thickness"] / 2, cap_style=2,
    )


def test_recovers_wall_from_structural_layer_in_architectural_plan(tmp_path):
    doc = ezdxf.new(units=4)
    plan = doc.modelspace()
    plan.add_lwpolyline([(0, 0), (4000, 0), (4000, 200), (0, 200)], close=True,
                        dxfattribs={"layer": "PLAN$0$0-S_WALL"})
    column = plan.add_lwpolyline([(6000, 0), (6450, 0), (6450, 800), (6000, 800)], close=True,
                                dxfattribs={"layer": "0-S_WALL"})
    outline = plan.add_lwpolyline([(0, 3000), (4000, 3000), (4000, 3200), (0, 3200)], close=True,
                                 dxfattribs={"layer": "AE-WALL"})
    plan.add_lwpolyline([(0, 3000), (4000, 3000), (4000, 3200), (0, 3200)], close=True,
                        dxfattribs={"layer": "0-S_WALL"})
    path = tmp_path / "三层平面图.dxf"
    doc.saveas(path)

    model = run(path)
    assert any(element["category"] == "column" and column.dxf.handle in element["source"]["handles"]
               for element in model["elements"])
    walls = [element for element in model["elements"] if element["category"] == "wall"]
    assert len(walls) == 2
    assert [wall["geometry"]["start"] for wall in walls] == [[0, 100], [0, 3100]]
    assert [wall["geometry"]["end"] for wall in walls] == [[4000, 100], [4000, 3100]]
    assert all(wall["type"]["thickness"] == 200 for wall in walls)
    assert walls[1]["source"]["handles"] == [outline.dxf.handle]
    assert not any("轮廓仍有未建区域" in issue["message"] for issue in model["issues"])


def test_recovers_branches_without_building_in_outline_notch(tmp_path):
    doc = ezdxf.new(units=4)
    points = [(0, 0), (3500, 0), (3500, 2400), (3300, 2400), (3300, 200),
              (200, 200), (200, 2400), (0, 2400)]
    outline = doc.modelspace().add_lwpolyline(points, close=True, dxfattribs={"layer": "S_WALL"})
    path = tmp_path / "三层平面图.dxf"
    doc.saveas(path)

    walls = run(path)["elements"]
    assert len(walls) == 3
    assert all(wall["source"]["handles"] == [outline.dxf.handle] for wall in walls)
    footprints = [wall_footprint(wall) for wall in walls]
    assert all(Polygon(points).covers(footprint) for footprint in footprints)
    assert isclose(footprints[0].union(footprints[1]).union(footprints[2]).area, Polygon(points).area)


def test_recovers_short_returns_of_a_compound_architectural_wall(tmp_path):
    doc = ezdxf.new(units=4)
    points = [(0, 0), (200, 0), (200, 800), (4000, 800), (4000, 400), (4300, 400),
              (4300, 1400), (4000, 1400), (4000, 1000), (200, 1000), (200, 1800), (0, 1800)]
    doc.modelspace().add_lwpolyline(points, close=True, dxfattribs={"layer": "AE-WALL"})
    path = tmp_path / "三层平面图.dxf"
    doc.saveas(path)

    walls = run(path)["elements"]
    assert len(walls) == 3
    assert {tuple(wall["geometry"]["start"]) for wall in walls} == {(100, 0), (200, 900), (4150, 400)}
    assert all(Polygon(points).covers(wall_footprint(wall)) for wall in walls)


def test_recovers_unbuilt_branch_from_rectangular_fill_inside_a_wall_outline(tmp_path):
    doc = ezdxf.new(units=4)
    plan = doc.modelspace()
    points = [(-50, 1200), (-725, 1200), (-725, 1100), (-50, 1100), (-50, 0), (50, 0),
              (50, 3100), (650, 3100), (650, 3400), (-450, 3400), (-450, 3100), (-50, 3100)]
    outline = plan.add_lwpolyline(points, close=True, dxfattribs={"layer": "AE-WALL"})
    branch = [(-50, 0), (50, 0), (50, 1100), (-50, 1100)]
    fill = plan.add_lwpolyline(branch, close=True, dxfattribs={"layer": "AE-WALL-HACH"})
    plan.add_lwpolyline([(-50, 0.1), (50, 0.1), (50, 1099.9), (-50, 1099.9)],
                        close=True, dxfattribs={"layer": "AE-WALL-HACH"})
    unsupported = plan.add_lwpolyline([(950, 0), (1050, 0), (1050, 1100), (950, 1100)],
                                      close=True, dxfattribs={"layer": "AE-WALL-HACH"})
    path = tmp_path / "三层平面图.dxf"
    doc.saveas(path)

    walls = run(path)["elements"]

    [recovered] = [wall for wall in walls if wall_footprint(wall).covers(Point(0, 550))]
    assert recovered["geometry"] == {"kind": "line", "start": [0, 0], "end": [0, 1100]}
    assert recovered["type"]["thickness"] == 100
    assert set(recovered["source"]["handles"]) == {outline.dxf.handle, fill.dxf.handle}
    assert all(unsupported.dxf.handle not in wall["source"]["handles"] for wall in walls)
    assert all(wall_footprint(wall).intersection(wall_footprint(recovered)).area == 0
               for wall in walls if wall is not recovered)


def test_cleans_tiny_end_overlap_with_existing_wall(tmp_path):
    doc = ezdxf.new(units=4)
    plan = doc.modelspace()
    plan.add_lwpolyline([(0, 0), (1000, 0), (1000, 100), (0, 100)], close=True,
                        dxfattribs={"layer": "AE-WALL"})
    plan.add_lwpolyline([(999.5, -100), (4000, -100), (4000, 100), (999.5, 100)], close=True,
                        dxfattribs={"layer": "S_WALL"})
    path = tmp_path / "三层平面图.dxf"
    doc.saveas(path)

    existing, added = run(path)["elements"]
    assert existing["geometry"]["end"] == [1000, 50]
    assert 1000 <= added["geometry"]["start"][0] < 1000.5
    assert wall_footprint(existing).intersection(wall_footprint(added)).area == 0


def test_avoids_duplicate_outline_over_several_existing_walls(tmp_path):
    doc = ezdxf.new(units=4)
    plan = doc.modelspace()
    for begin, end in [(0, 1800), (2000, 3800)]:
        for y in (0, 200):
            plan.add_line((begin, y), (end, y), dxfattribs={"layer": "AE-WALL"})
    plan.add_lwpolyline([(0, 0), (4000, 0), (4000, 200), (0, 200)], close=True,
                        dxfattribs={"layer": "S_WALL"})
    path = tmp_path / "三层平面图.dxf"
    doc.saveas(path)

    walls = run(path)["elements"]
    assert len(walls) == 2
    assert all("closed_wall_outline" not in wall["evidence"] for wall in walls)


def test_replaces_short_wall_covered_by_a_longer_wall_from_its_own_outline(tmp_path):
    doc = ezdxf.new(units=4)
    points = [(0, 0), (900, 0), (900, 300), (660, 300), (660, 200),
              (240, 200), (240, 300), (0, 300)]
    plan = doc.modelspace()
    outline = plan.add_lwpolyline(points, close=True, dxfattribs={"layer": "AE-WALL"})
    extra = plan.add_line((260, 200), (640, 200), dxfattribs={"layer": "AE-WALL"})
    path = tmp_path / "三层平面图.dxf"
    doc.saveas(path)

    walls = [wall for wall in run(path)["elements"] if wall["category"] == "wall"]

    [wall] = walls
    assert wall["geometry"] == {"kind": "line", "start": [0, 100], "end": [900, 100]}
    assert wall["type"]["thickness"] == 200
    assert wall["source"]["handles"] == sorted([outline.dxf.handle, extra.dxf.handle])
    assert "closed_outline" in wall["evidence"]


def test_shared_block_insert_is_not_proof_of_the_same_wall_outline(tmp_path):
    doc = ezdxf.new(units=4)
    floor = doc.blocks.new("WALLS")
    short = floor.add_lwpolyline([(240, 0), (660, 0), (660, 200), (240, 200)], close=True,
                                 dxfattribs={"layer": "AE-WALL"})
    long = floor.add_lwpolyline([(0, 0), (900, 0), (900, 200), (0, 200)], close=True,
                                dxfattribs={"layer": "S_WALL"})
    insert = doc.modelspace().add_blockref("WALLS", (0, 0))
    path = tmp_path / "三层平面图.dxf"
    doc.saveas(path)

    walls = [wall for wall in run(path)["elements"] if wall["category"] == "wall"]

    assert len(walls) == 2
    assert {tuple(wall["source"]["handles"]) for wall in walls} == {
        tuple(sorted([long.dxf.handle, insert.dxf.handle])),
        tuple(sorted([short.dxf.handle, insert.dxf.handle])),
    }


def test_substitution_does_not_drop_an_existing_wall_to_column_join(tmp_path):
    doc = ezdxf.new(units=4)
    points = [(0, 0), (900, 0), (900, 300), (660, 300), (660, 200),
              (240, 200), (240, 300), (0, 300)]
    plan = doc.modelspace()
    plan.add_lwpolyline(points, close=True, dxfattribs={"layer": "AE-WALL"})
    plan.add_lwpolyline([(220, 100), (420, 100), (420, 300), (220, 300)], close=True,
                        dxfattribs={"layer": "COLUMN"})
    path = tmp_path / "三层平面图.dxf"
    doc.saveas(path)

    model = run(path)
    walls = [wall for wall in model["elements"] if wall["category"] == "wall"]

    assert len(walls) == 2
    short = next(wall for wall in walls if wall["geometry"]["start"] == [240, 100])
    [join] = model["relations"]
    assert (join["kind"], join["a"]) == ("join", short["id"])


def test_marks_unverified_stepped_wall_without_guessing_its_build(tmp_path):
    doc = ezdxf.new(units=4)
    points = [(0, 0), (700, 0), (700, 950), (300, 950), (300, 1250), (0, 1250)]
    doc.modelspace().add_lwpolyline(points, close=True, dxfattribs={"layer": "S_WALL"})
    path = tmp_path / "三层平面图.dxf"
    doc.saveas(path)

    model = run(path)
    assert model["elements"] == []
    [issue] = [issue for issue in model["issues"] if "轮廓仍有未建区域" in issue["message"]]
    assert Polygon(points).contains(Point(issue["where"]["at"]))


def test_reports_unbuilt_wall_outline_at_a_locatable_point(tmp_path):
    doc = ezdxf.new(units=4)
    points = [(0, 0), (3000, 0), (3000, 700), (0, 700)]
    outline = doc.modelspace().add_lwpolyline(points, close=True, dxfattribs={"layer": "S_WALL"})
    path = tmp_path / "三层平面图.dxf"
    doc.saveas(path)

    model = run(path)
    assert not model["elements"]
    [issue] = [issue for issue in model["issues"] if "轮廓仍有未建区域" in issue["message"]]
    assert outline.dxf.handle in issue["message"]
    assert issue["where"]["at"] == [1500, 350]
