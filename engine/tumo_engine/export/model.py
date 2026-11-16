"""Versioned JSON contract shared by the recognition engine and Revit."""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue


Coordinate = Annotated[float, Field(strict=True, allow_inf_nan=False)]
Point = tuple[Coordinate, Coordinate]
Bounds = tuple[Coordinate, Coordinate, Coordinate, Coordinate]
Identifier = Annotated[str, Field(min_length=1)]
PositiveNumber = Annotated[Coordinate, Field(gt=0)]
Confidence = Annotated[Coordinate, Field(ge=0, le=1)]
Discipline = Literal["arch", "struct", "unknown"]
Category = Literal[
    "grid", "level", "column", "wall", "shear_wall", "beam", "slab",
    "slab_opening", "door", "window", "wall_opening", "room", "stair",
    "railing", "roof", "parapet", "pile", "pile_cap", "raft", "footing",
    "strip_footing", "pit", "cushion", "ramp", "ring_beam", "lintel",
    "constructional_column", "site_element",
]


class ContractObject(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Origin(ContractObject):
    grid: tuple[Identifier, Identifier] | None = None
    at: Point | None = None


class Project(ContractObject):
    name: Identifier
    units: Literal["mm"]
    origin: Origin | None = None


class SheetTransform(ContractObject):
    """Rotate counterclockwise about (0, 0), then add dx and dy in mm."""

    dx: Coordinate
    dy: Coordinate
    rot_deg: Coordinate


class Sheet(ContractObject):
    id: Identifier
    file: Identifier
    discipline: Discipline
    kind: Identifier
    title: str
    number: Identifier | None = None
    layout: Identifier | None = None
    bounds: Bounds | None = Field(
        default=None, description="Local drawing bounds [min x, min y, max x, max y] in mm."
    )
    levels: list[Identifier]
    scale: PositiveNumber | None = None
    units_to_mm: PositiveNumber = 1
    transform: SheetTransform | None = None


class Source(ContractObject):
    sheet: Identifier
    handles: list[Identifier]


class Level(ContractObject):
    id: Identifier
    name: Identifier
    elevation_arch: Coordinate | None = None
    elevation_struct: Coordinate | None = None
    source: str | None = None
    confidence: Confidence | None = None
    evidence: list[str] = Field(default_factory=list)


class Grid(ContractObject):
    id: Identifier
    label: Identifier
    start: Point
    end: Point
    confidence: Confidence
    source: Source | None = None
    evidence: list[str] = Field(default_factory=list)


class LineGeometry(ContractObject):
    kind: Literal["line"]
    start: Point
    end: Point


class PointGeometry(ContractObject):
    kind: Literal["point"]
    at: Point
    facing: Point | None = None
    hand: Literal["left", "right"] | None = None
    rotation_deg: Coordinate | None = None


class PolygonGeometry(ContractObject):
    kind: Literal["polygon"]
    points: Annotated[list[Point], Field(min_length=3)]


Geometry = Annotated[LineGeometry | PointGeometry | PolygonGeometry, Field(discriminator="kind")]


class ElementType(ContractObject):
    """Common BIM type fields plus category-specific JSON properties."""

    __pydantic_extra__: dict[str, JsonValue] = Field(init=False)
    model_config = ConfigDict(extra="allow")

    name: str | None = None
    material: str | None = None
    mark: str | None = None
    thickness: PositiveNumber | None = None
    width: PositiveNumber | None = None
    depth: PositiveNumber | None = None
    height: PositiveNumber | None = None
    diameter: PositiveNumber | None = None


class Element(ContractObject):
    id: Identifier
    category: Category
    discipline: Discipline | None = None
    type: ElementType
    geometry: Geometry
    base_level: Identifier | None = None
    top_level: Identifier | None = None
    base_offset: Coordinate | None = None
    top_offset: Coordinate | None = None
    host: Identifier | None = None
    sill_height: Coordinate | None = None
    params: dict[str, JsonValue] = Field(default_factory=dict)
    confidence: Confidence
    evidence: list[str] = Field(default_factory=list)
    source: Source


class Relation(ContractObject):
    kind: Identifier
    a: Identifier
    b: Identifier
    priority: Identifier | None = None


class IssueLocation(ContractObject):
    sheet: Identifier
    at: Point | None = None


class Issue(ContractObject):
    id: Identifier
    severity: Literal["info", "warn", "error"]
    element: Identifier | None = None
    message: Identifier
    where: IssueLocation | None = None


class TumoModel(ContractObject):
    model_config = ConfigDict(
        json_schema_extra={"$schema": "https://json-schema.org/draft/2020-12/schema"}
    )

    schema_version: Literal["0.1"]
    project: Project
    sheets: list[Sheet]
    levels: list[Level]
    grids: list[Grid]
    elements: list[Element]
    relations: list[Relation]
    issues: list[Issue]
