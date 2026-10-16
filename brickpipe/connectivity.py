"""Do the pieces actually clip together?

Two sources of truth:

* The LDraw part library (official geometry). A brick's studs are not listed
  anywhere as studs; they are references to the `stud.dat` primitive (and its
  relatives) inside the part file, so the part file is read and followed.
* LDCad's shadow library (shipped with LDraw Nova as data/offLibShadow, CC
  BY-SA 4.0): one file per part or primitive with `!LDCAD SNAP_*` records that
  say where connectors are and which way they face. Primitives like `stud.dat`
  carry the male stud; parts carry their undersides as female records with
  grids.

Every connector is a segment: a point, an axis and a length (a stud is 4 LDU
long, a Technic pin 40, a bar whatever the part says). Two connectors mate
when they are on different pieces, their axes are parallel and on the same
line (within 1.5 LDU), their extents overlap, and their kinds fit:

* `SNAP_CYL` male into `SNAP_CYL` female of the same radius: studs into
  sockets, pins and axles into holes, bars into holders. Overlap along the
  line is what matters, so a pin half-way through a hole, or a bar held
  anywhere along its length, counts.
* `SNAP_CYL` male bar into `SNAP_CLP` clip of the same radius.
* `SNAP_FGR` hinge fingers into fingers of the same group and the opposite
  gender offset.
* `SNAP_GEN` male into female of the same group at the same point (minifig
  hands into torsos, horses onto bases and the like).

Pieces are joined into groups by their connections; the biggest group is the
model. Pieces whose geometry overlaps another piece's (a tyre on a rim) are
treated as press-fitted.
"""
from __future__ import annotations

import re
import zipfile
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from .ldraw import IDENTITY, Matrix, PlacedPart, Vec, mat_mul, mat_vec, normalise_name, parse_ref

TOLERANCE = 1.5  # LDraw units; a stud is 12 across
MAX_DEPTH = 12


@dataclass(frozen=True)
class Feature:
    kind: str  # "cyl", "clip", "fgr" or "gen"
    gender: str  # "M" or "F" (a clip is "F"; fingers carry their genderOfs)
    radius: float
    pos: Vec  # start of the segment
    axis: Vec  # unit direction the segment runs in
    length: float = 0.0
    group: str = ""


@dataclass
class ShadowFile:
    cyls: list[dict] = field(default_factory=list)
    clips: list[dict] = field(default_factory=list)
    fingers: list[dict] = field(default_factory=list)
    gens: list[dict] = field(default_factory=list)
    includes: list[tuple[str, Vec, Matrix]] = field(default_factory=list)
    clear: bool = False  # SNAP_CLEAR: ignore connectors of referenced primitives
    other: int = 0  # connector records of kinds this module does not understand


# -- shadow records -----------------------------------------------------------

_ATTR = re.compile(r"\[(\w+)=([^\]]*)\]")


def parse_shadow(text: str) -> ShadowFile:
    out = ShadowFile()
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("0 !LDCAD SNAP_"):
            continue
        kind = line.split()[2]
        attrs = {k.lower(): v.strip() for k, v in _ATTR.findall(line)}
        if kind == "SNAP_CLEAR":
            if not attrs.get("id"):
                out.clear = True
        elif kind == "SNAP_INCL" and attrs.get("ref"):
            out.includes.append((attrs["ref"], _vec(attrs.get("pos")), _mat(attrs.get("ori"))))
        elif kind == "SNAP_CYL":
            out.cyls.append(attrs)
        elif kind == "SNAP_CLP":
            out.clips.append(attrs)
        elif kind == "SNAP_FGR":
            out.fingers.append(attrs)
        elif kind == "SNAP_GEN":
            out.gens.append(attrs)
        else:
            out.other += 1
    return out


def _vec(text: str | None) -> Vec:
    if not text:
        return (0.0, 0.0, 0.0)
    nums = [float(t) for t in text.split()]
    return (nums[0], nums[1], nums[2]) if len(nums) >= 3 else (0.0, 0.0, 0.0)


def _mat(text: str | None) -> Matrix:
    if not text:
        return IDENTITY
    nums = [float(t) for t in text.split()]
    return tuple(nums) if len(nums) == 9 else IDENTITY  # type: ignore[return-value]


def _grid(text: str | None) -> list[tuple[float, float]]:
    """Offsets in (x, z) for a [grid=...] attribute; [(0, 0)] when absent.

    Forms seen: "8 4 20 20", "C 8 C 4 20 20", "C 8 4 20 20", "8 C 4 20 20".
    "C" before a count means that axis is centred on pos.
    """
    if not text:
        return [(0.0, 0.0)]
    tokens = text.split()
    counts, centred = [], []
    i = 0
    while i < len(tokens) and len(counts) < 2:
        if tokens[i].upper() == "C":
            centred.append(True)
            i += 1
            counts.append(int(float(tokens[i])))
        else:
            centred.append(False)
            counts.append(int(float(tokens[i])))
        i += 1
    spacing = [float(t) for t in tokens[i:i + 2]]
    if len(counts) < 2 or len(spacing) < 2:
        return [(0.0, 0.0)]
    nx, nz = counts
    sx, sz = spacing
    xs = [(k - (nx - 1) / 2) * sx if centred[0] else k * sx for k in range(nx)]
    zs = [(k - (nz - 1) / 2) * sz if centred[1] else k * sz for k in range(nz)]
    return [(x, z) for x in xs for z in zs]


def _unit(v: Vec) -> Vec:
    n = (v[0] ** 2 + v[1] ** 2 + v[2] ** 2) ** 0.5 or 1.0
    return (v[0] / n, v[1] / n, v[2] / n)


def _sections(secs: str) -> tuple[float | None, float]:
    """(radius, total length) of a [secs=...] list such as "R 6 16 R 8 4".

    Each section is a shape letter, a radius and a length. The radius reported
    is the one covering the most length, so a pin with 2 LDU chamfers of 6.25
    still reads as a radius-6 pin."""
    tokens = secs.split()
    by_radius: dict[float, float] = defaultdict(float)
    i = 0
    while i < len(tokens):
        try:
            float(tokens[i])
            i += 1  # a stray number where a shape letter was expected
            continue
        except ValueError:
            pass
        try:
            radius, length = float(tokens[i + 1]), float(tokens[i + 2])
        except (IndexError, ValueError):
            break
        by_radius[radius] += length
        i += 3
    if not by_radius:
        return None, 0.0
    radius = max(by_radius, key=lambda r: by_radius[r])
    return radius, sum(by_radius.values())


def _segment(attrs: dict, length: float) -> tuple[Vec, Vec]:
    """Start point and unit axis of a connector that runs along its own -Y
    (LDraw "up") from pos for `length`, or is centred on pos with
    [center=true]."""
    pos = _vec(attrs.get("pos"))
    ori = _mat(attrs.get("ori"))
    axis = _unit(mat_vec(ori, (0.0, -1.0, 0.0)))
    if attrs.get("center", "").lower() == "true":
        pos = tuple(pos[k] - axis[k] * length / 2 for k in range(3))  # type: ignore[assignment]
    return pos, axis


def cyl_features(attrs: dict) -> list[Feature]:
    gender = attrs.get("gender", "").upper()
    if gender not in ("M", "F"):
        return []
    radius, length = _sections(attrs.get("secs", ""))
    if radius is None:
        return []
    pos, axis = _segment(attrs, length)
    ori = _mat(attrs.get("ori"))
    out = []
    for gx, gz in _grid(attrs.get("grid")):
        off = mat_vec(ori, (gx, 0.0, gz))
        out.append(Feature("cyl", gender, radius,
                           (pos[0] + off[0], pos[1] + off[1], pos[2] + off[2]), axis, length))
    return out


def clip_features(attrs: dict) -> list[Feature]:
    try:
        radius = float(attrs.get("radius", "4"))
        length = float(attrs.get("length", "8"))
    except ValueError:
        return []
    pos, axis = _segment(attrs, length)
    return [Feature("clip", "F", radius, pos, axis, length, attrs.get("group", ""))]


def finger_features(attrs: dict) -> list[Feature]:
    """A hinge's row of fingers, centred on pos, running along -Y of ori.
    Gender is the genderOfs: which part owns the first finger."""
    gender = attrs.get("genderofs", "M").upper()
    try:
        widths = [float(t) for t in attrs.get("seq", "").split()]
        radius = float(attrs.get("radius", "4"))
    except ValueError:
        return []
    length = sum(widths) or 8.0
    centred = dict(attrs)
    centred.setdefault("center", "true")
    pos, axis = _segment(centred, length)
    return [Feature("fgr", gender, radius, pos, axis, length, attrs.get("group", ""))]


def gen_features(attrs: dict) -> list[Feature]:
    gender = attrs.get("gender", "").upper()
    if gender not in ("M", "F"):
        return []
    pos, axis = _segment(attrs, 0.0)
    return [Feature("gen", gender, 0.0, pos, axis, 0.0, attrs.get("group", ""))]


# -- libraries ---------------------------------------------------------------

def bundled_supplement() -> Path:
    """brickpipe's own records for primitives the shadow library lacks."""
    return Path(__file__).resolve().parent / "data" / "shadow-extra"


class ShadowLibrary:
    def __init__(self, root: str | Path, extras: list[Path] | None = None):
        """`extras` are folders with the same layout searched after `root`;
        the bundled supplement is always last."""
        self.root = Path(root)
        if not (self.root / "parts").is_dir():
            raise FileNotFoundError(f"Shadow library not found at {self.root} (expected a parts/ folder)")
        self.roots = [self.root, *(extras or []), bundled_supplement()]
        self._cache: dict[str, ShadowFile | None] = {}

    def get(self, name: str) -> ShadowFile | None:
        key = normalise_name(name)
        if key not in self._cache:
            self._cache[key] = None
            for root in self.roots:
                for folder in ("parts", "p"):
                    path = root / folder / key
                    if path.is_file():
                        self._cache[key] = parse_shadow(path.read_text("utf-8", "replace"))
                        break
                if self._cache[key] is not None:
                    break
        return self._cache[key]


class PartLibrary:
    """Reads official part files from an LDraw library folder or complete.zip."""

    def __init__(self, root: str | Path):
        self.root = Path(root)
        self._zip: zipfile.ZipFile | None = None
        self._names: dict[str, str] = {}
        self._cache: dict[str, str | None] = {}
        if self.root.is_file() and self.root.suffix.lower() == ".zip":
            self._zip = zipfile.ZipFile(self.root)
            for info in self._zip.infolist():
                low = info.filename.replace("\\", "/").lower()
                for folder in ("parts/", "p/"):
                    idx = low.find("/" + folder) if not low.startswith(folder) else -1
                    rel = low[idx + 1:] if idx >= 0 else (low if low.startswith(folder) else None)
                    if rel:
                        self._names.setdefault(rel, info.filename)
            if not self._names:
                raise FileNotFoundError(f"{self.root} holds no parts/ or p/ folder")
        else:
            base = self.root / "ldraw" if (self.root / "ldraw" / "parts").is_dir() else self.root
            if not (base / "parts").is_dir():
                raise FileNotFoundError(f"LDraw library not found at {self.root} (expected a parts/ folder)")
            self.root = base

    def read(self, name: str) -> str | None:
        key = normalise_name(name)
        if key in self._cache:
            return self._cache[key]
        text = None
        if self._zip:
            for folder in ("parts/", "p/"):
                real = self._names.get(folder + key)
                if real:
                    text = self._zip.read(real).decode("utf-8", "replace")
                    break
        else:
            for folder in ("parts", "p"):
                path = self.root / folder / key
                if path.is_file():
                    text = path.read_text("utf-8", "replace")
                    break
        self._cache[key] = text
        return text

    def close(self) -> None:
        if self._zip:
            self._zip.close()


# -- features per part -------------------------------------------------------

class Connectors:
    """Connector features of each part, in the part's own coordinates."""

    def __init__(self, parts: PartLibrary, shadow: ShadowLibrary):
        self.parts = parts
        self.shadow = shadow
        self._cache: dict[str, tuple[list[Feature], int] | None] = {}

    def features(self, part: str) -> tuple[list[Feature], int] | None:
        """(cylinder features, count of other connector kinds), or None when
        neither the part file nor any shadow record is known."""
        return self._collect(f"{part}.dat", 0)

    def _collect(self, name: str, depth: int) -> tuple[list[Feature], int] | None:
        key = normalise_name(name)
        if key in self._cache:
            return self._cache[key]
        self._cache[key] = ([], 0)  # guards against reference loops
        shadow = self.shadow.get(key)
        text = self.parts.read(key)
        if shadow is None and text is None:
            self._cache[key] = None
            return None

        feats: list[Feature] = []
        other = 0
        if shadow:
            for attrs in shadow.cyls:
                feats.extend(cyl_features(attrs))
            for attrs in shadow.clips:
                feats.extend(clip_features(attrs))
            for attrs in shadow.fingers:
                feats.extend(finger_features(attrs))
            for attrs in shadow.gens:
                feats.extend(gen_features(attrs))
            other += shadow.other
            for ref, pos, ori in shadow.includes:
                inc = self._collect(ref, depth + 1) if depth < MAX_DEPTH else None
                if inc:
                    feats.extend(_moved(inc[0], pos, ori))
                    other += inc[1]
        if text and not (shadow and shadow.clear) and depth < MAX_DEPTH:
            for line in text.splitlines():
                ref = parse_ref(line.strip())
                if ref is None:
                    continue
                sub = self._collect(ref.key, depth + 1)
                if sub:
                    feats.extend(_moved(sub[0], ref.pos, ref.matrix))
                    other += sub[1]
        self._cache[key] = (feats, other)
        return self._cache[key]


def _moved(feats: list[Feature], pos: Vec, mat: Matrix) -> list[Feature]:
    """Features carried through a reference. A scaled matrix stretches the
    length too: primitives such as wpinhole.dat are 1 LDU long and scaled
    along their axis by the part that uses them."""
    out = []
    for f in feats:
        p = mat_vec(mat, f.pos)
        a = mat_vec(mat, f.axis)
        stretch = (a[0] ** 2 + a[1] ** 2 + a[2] ** 2) ** 0.5 or 1.0
        out.append(Feature(f.kind, f.gender, f.radius,
                           (p[0] + pos[0], p[1] + pos[1], p[2] + pos[2]), _unit(a),
                           f.length * stretch, f.group))
    return out


# -- the check ---------------------------------------------------------------

@dataclass
class Group:
    pieces: list[int]  # indices into the piece list


@dataclass
class Report:
    groups: list[list[int]]  # largest first, indices into pieces
    unknown: list[int]  # pieces with no connector data at all
    unverified: list[int]  # loose, but carry connector kinds this check cannot match
    connections: int
    by_kind: dict[str, int] = field(default_factory=dict)  # connections per receiving kind
    press_fit_only: list[int] = field(default_factory=list)  # in the model only thanks to overlap
    partners: dict[int, list[tuple[int, str]]] = field(default_factory=dict)  # piece -> [(piece, kind)]
    overlaps: dict[int, list[int]] = field(default_factory=dict)  # piece -> pieces it press-fits

    def summary(self, pieces: list[PlacedPart] | None = None) -> dict:
        parts = sorted({pieces[i].part for i in self.press_fit_only}) if pieces else []
        return {"connections": self.connections, "by_kind": dict(self.by_kind),
                "press_fit_only": len(self.press_fit_only), "press_fit_parts": parts[:8],
                "groups": len(self.groups), "unknown": len(self.unknown)}


def _parallel(a: Vec, b: Vec) -> bool:
    dot = a[0] * b[0] + a[1] * b[1] + a[2] * b[2]
    la = (a[0] ** 2 + a[1] ** 2 + a[2] ** 2) ** 0.5 or 1.0
    lb = (b[0] ** 2 + b[1] ** 2 + b[2] ** 2) ** 0.5 or 1.0
    return abs(dot / (la * lb)) > 0.95


MIN_OVERLAP = 2.0  # LDU of shared length before two segments count as mated
GEN_TOLERANCE = 4.0  # generic connectors are blobs, not points


def _receives(f: Feature, g: Feature) -> bool:
    """Can male-ish `f` mate with female-ish `g`? Kinds and sizes only."""
    if f.kind == "cyl" and f.gender == "M":
        if g.kind == "cyl" and g.gender == "F":
            return abs(f.radius - g.radius) <= 0.01
        if g.kind == "clip":
            return abs(f.radius - g.radius) <= 0.5
        return False
    if f.kind == "fgr" and g.kind == "fgr":
        return f.gender != g.gender and f.group == g.group and abs(f.radius - g.radius) <= 0.5
    if f.kind == "gen" and f.gender == "M" and g.kind == "gen" and g.gender == "F":
        return f.group == g.group
    return False


def _mates(f: Feature, g: Feature) -> bool:
    """Geometry: same line, overlapping extents (or same point for blobs)."""
    d = tuple(g.pos[k] - f.pos[k] for k in range(3))
    if f.kind == "gen":
        return sum(c * c for c in d) ** 0.5 <= GEN_TOLERANCE
    if not _parallel(f.axis, g.axis):
        return False
    along = sum(d[k] * f.axis[k] for k in range(3))
    perp = tuple(d[k] - along * f.axis[k] for k in range(3))
    if sum(c * c for c in perp) ** 0.5 > TOLERANCE:
        return False
    # g's extent on f's axis: it starts at `along` and runs g.length one way or the other
    sign = 1.0 if sum(f.axis[k] * g.axis[k] for k in range(3)) >= 0 else -1.0
    g_lo, g_hi = sorted((along, along + sign * g.length))
    overlap = min(f.length, g_hi) - max(0.0, g_lo)
    needed = min(MIN_OVERLAP, f.length, g.length)
    return overlap >= needed - 1e-6


def _cells(f: Feature, cell: float):
    """Spatial-hash cells a segment passes through."""
    steps = max(1, int(f.length // cell) + 1)
    seen = set()
    for n in range(steps + 1):
        t = min(f.length, n * cell)
        p = tuple(f.pos[k] + f.axis[k] * t for k in range(3))
        key = tuple(round(c / cell) for c in p)
        if key not in seen:
            seen.add(key)
            yield key


def analyse(
    pieces: list[PlacedPart],
    connectors: Connectors,
    boxes: list[tuple[Vec, Vec] | None] | None = None,
) -> Report:
    """Group pieces by real connections. `boxes` (world bounding boxes, same
    order as pieces) enables the press-fit rule for overlapping pieces."""
    parent = list(range(len(pieces)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(a: int, b: int) -> None:
        parent[find(a)] = find(b)

    world: list[list[Feature]] = []
    unknown: list[int] = []
    has_other: set[int] = set()
    for i, piece in enumerate(pieces):
        got = connectors.features(piece.part)
        if got is None:
            unknown.append(i)
            world.append([])
            continue
        feats, other = got
        if other:
            has_other.add(i)
        world.append(_moved(feats, piece.pos, piece.matrix))

    # Spatial hash over the cells each receiving segment passes through; each
    # giving segment looks in the cells along its own length.
    cell = max(TOLERANCE * 2, GEN_TOLERANCE)
    receivers = lambda f: f.gender == "F" or f.kind == "clip" or (f.kind == "fgr" and f.gender == "F")  # noqa: E731
    index: dict[tuple, list[tuple[int, Feature]]] = defaultdict(list)
    for i, feats in enumerate(world):
        for f in feats:
            if receivers(f):
                for key in _cells(f, cell):
                    index[key].append((i, f))

    connections = 0
    by_kind: dict[str, int] = defaultdict(int)
    partners: dict[int, list[tuple[int, str]]] = defaultdict(list)
    overlaps: dict[int, list[int]] = defaultdict(list)
    for i, feats in enumerate(world):
        for f in feats:
            if receivers(f):
                continue
            tried: set[tuple[int, int]] = set()
            for base in _cells(f, cell):
                for dx in (-1, 0, 1):
                    for dy in (-1, 0, 1):
                        for dz in (-1, 0, 1):
                            for j, g in index.get((base[0] + dx, base[1] + dy, base[2] + dz), ()):
                                if j == i or (j, id(g)) in tried:
                                    continue
                                tried.add((j, id(g)))
                                if _receives(f, g) and _mates(f, g):
                                    if find(i) != find(j):
                                        union(i, j)
                                    connections += 1
                                    by_kind[g.kind] += 1
                                    partners[i].append((j, g.kind))
                                    partners[j].append((i, g.kind))

    before = {i: find(i) for i in range(len(pieces))}  # groups by connectors alone

    # Press fit: geometry that overlaps another piece's by more than a sliver.
    if boxes:
        known = [(i, b) for i, b in enumerate(boxes) if b is not None]
        known.sort(key=lambda kb: kb[1][0][0])
        for a in range(len(known)):
            ia, (lo_a, hi_a) = known[a]
            for b in range(a + 1, len(known)):
                ib, (lo_b, hi_b) = known[b]
                if lo_b[0] > hi_a[0] - 2:
                    break
                if all(min(hi_a[k], hi_b[k]) - max(lo_a[k], lo_b[k]) > 2 for k in range(3)):
                    union(ia, ib)
                    overlaps[ia].append(ib)
                    overlaps[ib].append(ia)

    groups: dict[int, list[int]] = defaultdict(list)
    for i in range(len(pieces)):
        groups[find(i)].append(i)
    ordered = sorted(groups.values(), key=len, reverse=True)
    main = set(ordered[0]) if ordered else set()
    unverified = [i for i in has_other if i not in main]
    # Pieces in the model only because their geometry overlaps something: the
    # connector-only grouping had them outside the main group's root.
    main_root = find(ordered[0][0]) if ordered else None
    sizes: dict[int, int] = defaultdict(int)
    for root in before.values():
        sizes[root] += 1
    biggest_before = max(sizes, key=lambda r: sizes[r]) if sizes else None
    press_fit_only = [i for i in main if before[i] != biggest_before] if main_root is not None else []
    return Report(ordered, unknown, unverified, connections, dict(by_kind), press_fit_only,
                  dict(partners), dict(overlaps))


def to_csv(report: Report, pieces: list[PlacedPart]) -> str:
    """One row per piece: where it is, which group it landed in, what it is
    connected to and what it merely overlaps. For working out why a piece
    was or was not counted as attached."""
    import csv
    import io

    group_of = {i: n for n, group in enumerate(report.groups) for i in group}
    press = set(report.press_fit_only)
    unknown = set(report.unknown)
    out = io.StringIO()
    w = csv.writer(out, lineterminator="\n")
    w.writerow(["index", "part", "colour", "submodel", "x", "y", "z", "group", "status",
                "connections", "connected_to", "overlaps"])
    for i, p in enumerate(pieces):
        status = ("no connector data" if i in unknown else "press fit only" if i in press
                  else "attached" if group_of.get(i) == 0 else "loose")
        links = report.partners.get(i, [])
        w.writerow([i, p.part, p.colour, " > ".join(p.path[1:]) or "main",
                    *(round(v, 1) for v in p.pos), group_of.get(i, ""), status, len(links),
                    " ".join(f"{j}:{pieces[j].part}({kind})" for j, kind in links),
                    " ".join(f"{j}:{pieces[j].part}" for j in report.overlaps.get(i, []))])
    return out.getvalue()


# -- finding the libraries ---------------------------------------------------

LDRAW_ZIP_URL = "https://library.ldraw.org/library/updates/complete.zip"


def managed_library_dir() -> Path:
    """Where brickpipe keeps a library it downloaded itself."""
    import os

    root = os.environ.get("LOCALAPPDATA") or os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(root) / "brickpipe" / "ldraw"


def download_library(
    target: Path | None = None,
    progress=None,
    url: str = LDRAW_ZIP_URL,
    opener=None,
) -> Path:
    """Fetch complete.zip (about 150 MB, free, from ldraw.org) into the managed
    folder and return the zip's path. `progress(done_bytes, total_bytes)` is
    called as it goes. The download is written to a temporary name and
    renamed only when it is complete, so a half-finished file is never used."""
    import urllib.request

    target = target or managed_library_dir()
    target.mkdir(parents=True, exist_ok=True)
    final = target / "complete.zip"
    partial = target / "complete.zip.part"
    request = urllib.request.Request(url, headers={"User-Agent": "brickpipe"})
    open_ = opener or (lambda req: urllib.request.urlopen(req, timeout=60))
    with open_(request) as response, open(partial, "wb") as out:
        total = int(response.headers.get("Content-Length") or 0)
        done = 0
        while True:
            chunk = response.read(1 << 18)
            if not chunk:
                break
            out.write(chunk)
            done += len(chunk)
            if progress:
                progress(done, total)
    if not zipfile.is_zipfile(partial):
        partial.unlink(missing_ok=True)
        raise OSError("The download was not a zip file; ldraw.org may have moved the library.")
    partial.replace(final)
    return final


def find_ldraw_library(hints: list[str | Path] = ()) -> Path | None:
    """The LDraw library on this machine: an env var, a hint, brickpipe's own
    download, or where LPub3D and the usual installers put it."""
    import os

    candidates: list[Path] = [Path(h) for h in hints if h]
    candidates.append(managed_library_dir() / "complete.zip")
    for var in ("LDRAW_DIR", "LDRAWDIR"):
        if os.environ.get(var):
            candidates.append(Path(os.environ[var]))
    local = os.environ.get("LOCALAPPDATA")
    if local:
        candidates.append(Path(local) / "LPub3D Software" / "LPub3D" / "libraries" / "complete.zip")
    if os.environ.get("LPUB3D"):
        candidates.append(Path(os.environ["LPUB3D"]).parent / "libraries" / "complete.zip")
    home = Path.home()
    for base in (home / "Desktop", home / "Downloads", home):
        candidates += sorted(base.glob("LPub3D*/libraries/complete.zip")) if base.is_dir() else []
    candidates += [
        home / "ldraw", home / "LDraw", Path("C:/LDraw"), Path("C:/Users/Public/Documents/LDraw"),
        Path("/usr/share/ldraw"), Path("/opt/ldraw/ldraw"), Path("/opt/ldraw"),
        Path.cwd().parent / "ldraw", Path.cwd().parent / "LDraw",
    ]
    for c in candidates:
        if c.is_file() and c.suffix.lower() == ".zip":
            return c
        if (c / "parts").is_dir() or (c / "ldraw" / "parts").is_dir():
            return c
    return None


def find_shadow_library(nova_db: str | Path | None) -> Path | None:
    if nova_db:
        candidate = Path(nova_db).resolve().parent / "offLibShadow"
        if (candidate / "parts").is_dir():
            return candidate
    for c in (Path.cwd().parent / "ldraw-nova" / "data" / "offLibShadow",):
        if (c / "parts").is_dir():
            return c
    return None
