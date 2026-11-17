from math import isclose

import ezdxf
from shapely.geometry import LineString, Polygon

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
