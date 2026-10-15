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

A connection is a male and a female cylinder connector of the same radius at
the same point in space, on different pieces, with parallel axes. Pieces are
joined into groups by their connections; the biggest group is the model.

Only `SNAP_CYL` is matched (studs, pins, axles, bars in holes). Clips, hinges,
fingers and generic connectors (`SNAP_CLP`, `SNAP_FGR`, `SNAP_GEN`) are noted
but not matched, so a piece held only by one of those is reported as
unverified, not as loose. Pieces whose geometry overlaps another piece's
(a tyre on a rim) are treated as press-fitted.
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
    kind: str  # "cyl" or "other"
    gender: str  # "M" or "F"
    radius: float
    pos: Vec
    axis: Vec


@dataclass
class ShadowFile:
    cyls: list[dict] = field(default_factory=list)
    includes: list[tuple[str, Vec, Matrix]] = field(default_factory=list)
    clear: bool = False  # SNAP_CLEAR: ignore connectors of referenced primitives
    other: int = 0  # count of non-cylinder connector records


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


def cyl_features(attrs: dict) -> list[Feature]:
    gender = attrs.get("gender", "").upper()
    if gender not in ("M", "F"):
        return []
    secs = attrs.get("secs", "").split()
    radius = None
    for tok in secs:
        try:
            radius = float(tok)
            break
        except ValueError:
            continue
    if radius is None:
        return []
    pos = _vec(attrs.get("pos"))
    ori = _mat(attrs.get("ori"))
    axis = mat_vec(ori, (0.0, 1.0, 0.0))  # a cylinder runs along its own Y
    return [
        Feature("cyl", gender, radius,
                (pos[0] + mat_vec(ori, (gx, 0.0, gz))[0],
                 pos[1] + mat_vec(ori, (gx, 0.0, gz))[1],
                 pos[2] + mat_vec(ori, (gx, 0.0, gz))[2]),
                axis)
        for gx, gz in _grid(attrs.get("grid"))
    ]


# -- libraries ---------------------------------------------------------------

class ShadowLibrary:
    def __init__(self, root: str | Path):
        self.root = Path(root)
        if not (self.root / "parts").is_dir():
            raise FileNotFoundError(f"Shadow library not found at {self.root} (expected a parts/ folder)")
        self._cache: dict[str, ShadowFile | None] = {}

    def get(self, name: str) -> ShadowFile | None:
        key = normalise_name(name)
        if key not in self._cache:
            self._cache[key] = None
            for folder in ("parts", "p"):
                path = self.root / folder / key
                if path.is_file():
                    self._cache[key] = parse_shadow(path.read_text("utf-8", "replace"))
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
    out = []
    for f in feats:
        p = mat_vec(mat, f.pos)
        a = mat_vec(mat, f.axis)
        out.append(Feature(f.kind, f.gender, f.radius,
                           (p[0] + pos[0], p[1] + pos[1], p[2] + pos[2]), a))
    return out


# -- the check ---------------------------------------------------------------

@dataclass
class Group:
    pieces: list[int]  # indices into the piece list


@dataclass
class Report:
    groups: list[list[int]]  # largest first, indices into pieces
    unknown: list[int]  # pieces with no connector data at all
    unverified: list[int]  # loose by cylinders, but have clip/hinge/other connectors
    connections: int


def _parallel(a: Vec, b: Vec) -> bool:
    dot = a[0] * b[0] + a[1] * b[1] + a[2] * b[2]
    la = (a[0] ** 2 + a[1] ** 2 + a[2] ** 2) ** 0.5 or 1.0
    lb = (b[0] ** 2 + b[1] ** 2 + b[2] ** 2) ** 0.5 or 1.0
    return abs(dot / (la * lb)) > 0.95


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

    # Spatial hash on rounded position: a male and a female at the same point.
    cell = TOLERANCE * 2
    index: dict[tuple, list[tuple[int, Feature]]] = defaultdict(list)
    for i, feats in enumerate(world):
        for f in feats:
            if f.gender == "F":
                index[tuple(round(c / cell) for c in f.pos)].append((i, f))

    connections = 0
    for i, feats in enumerate(world):
        for f in feats:
            if f.gender != "M":
                continue
            base = tuple(round(c / cell) for c in f.pos)
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    for dz in (-1, 0, 1):
                        for j, g in index.get((base[0] + dx, base[1] + dy, base[2] + dz), ()):
                            if j == i or abs(g.radius - f.radius) > 0.01:
                                continue
                            d = sum((f.pos[k] - g.pos[k]) ** 2 for k in range(3)) ** 0.5
                            if d <= TOLERANCE and _parallel(f.axis, g.axis):
                                if find(i) != find(j):
                                    union(i, j)
                                connections += 1

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

    groups: dict[int, list[int]] = defaultdict(list)
    for i in range(len(pieces)):
        groups[find(i)].append(i)
    ordered = sorted(groups.values(), key=len, reverse=True)
    main = set(ordered[0]) if ordered else set()
    unverified = [i for i in has_other if i not in main]
    return Report(ordered, unknown, unverified, connections)


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
