"""Read LDraw .ldr/.mpd files and flatten them into a list of placed parts.

LDraw basics this module relies on:
  * One line per element. Line type 1 places a part or a submodel:
        1 <colour> x y z a b c d e f g h i <file>
  * An .mpd holds several models, each introduced by "0 FILE <name>".
    The first one is the main model.
  * Colour 16 means "inherit the colour of whoever placed me".
  * The Y axis points down: a smaller Y is higher up.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

Matrix = tuple[float, float, float, float, float, float, float, float, float]
Vec = tuple[float, float, float]

IDENTITY: Matrix = (1, 0, 0, 0, 1, 0, 0, 0, 1)
INHERIT_COLOUR = 16


def mat_mul(a: Matrix, b: Matrix) -> Matrix:
    return tuple(
        sum(a[r * 3 + k] * b[k * 3 + c] for k in range(3))
        for r in range(3)
        for c in range(3)
    )  # type: ignore[return-value]


def mat_vec(m: Matrix, v: Vec) -> Vec:
    return (
        m[0] * v[0] + m[1] * v[1] + m[2] * v[2],
        m[3] * v[0] + m[4] * v[1] + m[5] * v[2],
        m[6] * v[0] + m[7] * v[1] + m[8] * v[2],
    )


@dataclass
class Ref:
    """A type-1 line: a part or submodel placed inside a model."""

    colour: int
    pos: Vec
    matrix: Matrix
    target: str  # file name as written, e.g. "3001.dat" or "wing.ldr"

    @property
    def key(self) -> str:
        return normalise_name(self.target)

    def to_line(self) -> str:
        nums = [*self.pos, *self.matrix]
        return f"1 {self.colour} " + " ".join(fmt(n) for n in nums) + f" {self.target}"


@dataclass
class Line:
    raw: str
    ref: Ref | None = None

    @property
    def is_step(self) -> bool:
        parts = self.raw.split()
        return len(parts) >= 2 and parts[0] == "0" and parts[1].upper() == "STEP"


@dataclass
class Model:
    name: str
    lines: list[Line] = field(default_factory=list)

    @property
    def refs(self) -> list[Ref]:
        return [ln.ref for ln in self.lines if ln.ref]

    @property
    def step_count(self) -> int:
        return sum(1 for ln in self.lines if ln.is_step)


@dataclass
class Document:
    models: dict[str, Model]  # keyed by normalised name, insertion-ordered
    main: str

    @property
    def main_model(self) -> Model:
        return self.models[self.main]


@dataclass
class PlacedPart:
    """One physical piece in world coordinates."""

    part: str  # LDraw part id without ".dat", lower case, e.g. "3001"
    colour: int
    pos: Vec
    matrix: Matrix
    path: tuple[str, ...]  # chain of submodels this piece sits inside
    step: int  # step number within its own submodel (1-based)
    label: str = ""  # the "0 // ..." comment written just above it, if any
    local: Vec = (0.0, 0.0, 0.0)  # position inside its own submodel

    def to_line(self) -> str:
        return Ref(self.colour, self.pos, self.matrix, f"{self.part}.dat").to_line()


def fmt(n: float) -> str:
    """Format a number the way LDraw files usually carry them."""
    text = f"{n:.5f}".rstrip("0").rstrip(".")
    return "0" if text in ("-0", "") else text


def normalise_name(name: str) -> str:
    return name.strip().replace("\\", "/").lower()


def parse_colour(token: str) -> int:
    """LDraw colours are decimal codes, or 0x2RRGGBB direct colours."""
    return int(token, 16) if token.lower().startswith("0x") else int(token)


def parse_ref(raw: str) -> Ref | None:
    tokens = raw.split()
    if len(tokens) < 15 or tokens[0] != "1":
        return None
    try:
        colour = parse_colour(tokens[1])
        nums = [float(t) for t in tokens[2:14]]
    except ValueError:
        return None
    target = " ".join(tokens[14:])
    return Ref(colour, tuple(nums[:3]), tuple(nums[3:]), target)  # type: ignore[arg-type]


def parse(text: str, default_name: str = "main.ldr") -> Document:
    models: dict[str, Model] = {}
    current: Model | None = None
    main: str | None = None

    for raw in text.splitlines():
        stripped = raw.strip()
        if not stripped:
            continue
        tokens = stripped.split()
        if tokens[0] == "0" and len(tokens) >= 3 and tokens[1].upper() == "FILE":
            name = " ".join(tokens[2:])
            current = Model(name)
            models[normalise_name(name)] = current
            main = main or normalise_name(name)
            continue
        if tokens[0] == "0" and len(tokens) >= 2 and tokens[1].upper() == "NOFILE":
            current = None
            continue
        if current is None:
            if main is not None:
                continue  # stray text between NOFILE and the next FILE
            current = Model(default_name)
            models[normalise_name(default_name)] = current
            main = normalise_name(default_name)
        current.lines.append(Line(stripped, parse_ref(stripped)))

    if main is None:
        raise ValueError("No LDraw content found")
    return Document(models, main)


def load(path: str | Path) -> Document:
    path = Path(path)
    return parse(path.read_text(encoding="utf-8", errors="replace"), path.name)


def dump(doc: Document) -> str:
    """Write a Document back out as .mpd text."""
    out: list[str] = []
    for model in doc.models.values():
        out.append(f"0 FILE {model.name}")
        out.extend(ln.raw for ln in model.lines)
        out.append("0 NOFILE")
    return "\n".join(out) + "\n"


def is_part_file(key: str) -> bool:
    return key.endswith(".dat")


def flatten(doc: Document) -> tuple[list[PlacedPart], list[str]]:
    """Resolve every submodel and return (pieces, problems)."""
    pieces: list[PlacedPart] = []
    problems: list[str] = []

    def walk(model: Model, pos: Vec, matrix: Matrix, colour: int, path: tuple[str, ...]):
        step = 1
        label = ""
        for line in model.lines:
            if line.is_step:
                step += 1
                continue
            ref = line.ref
            if ref is None:
                if line.raw.startswith("0 //"):
                    label = line.raw[4:].strip()
                continue
            this_label, label = label, ""
            world_pos = tuple(p + o for p, o in zip(pos, mat_vec(matrix, ref.pos)))
            world_mat = mat_mul(matrix, ref.matrix)
            ref_colour = colour if ref.colour == INHERIT_COLOUR else ref.colour
            key = ref.key
            if key in doc.models:
                if key in path:
                    problems.append(f"Submodel loop: {' > '.join(path)} > {key}")
                    continue
                walk(doc.models[key], world_pos, world_mat, ref_colour, path + (key,))
            elif is_part_file(key):
                pieces.append(PlacedPart(
                    key[:-4], ref_colour, world_pos, world_mat, path, step,
                    this_label, ref.pos,
                ))
            else:
                problems.append(
                    f"'{ref.target}' is used in {model.name} but is not defined in the file"
                )

    walk(doc.main_model, (0.0, 0.0, 0.0), IDENTITY, INHERIT_COLOUR, (doc.main,))
    return pieces, problems
