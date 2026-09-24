"""Browser adapter for BTX to GEM (With Names).py.

The original decode, placement, geometry healing, grid snapping, extrusion and
GEM writer functions below are copied unchanged. The original GEM parser is
also retained. Browser additions replace CLI paths/prompts/Matplotlib previews
with JSON; Plotly.js draws the same polygons and GEM face outlines.

Serve these four files together over HTTP (e.g. python -m http.server 8000).
Python runs locally in a Pyodide worker; there is no conversion server.
"""
import re, zlib, math, sys, csv, json, io, base64
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import List, Tuple, Dict, Optional
import numpy as np

LOCATIONS = {
    'Vernon, BC': '50.223 119.194 120.000  0.000',
    'Kelowna, BC': '49.960 119.380 120.000  0.000',
    'Vancouver, BC': '49.190 123.180 120.000  0.000',
    'Calgary, AB': '51.050 114.060 120.000  0.000',} 

CURRENT_LOCATION = 'Vancouver, BC' # choose your location here

Page_Scales = { # 1" = 10' → 1:120, 1" = 7' → 1:84 Add as needed so that RHS is number of inches in LHS 
    '1" = 10ft': 120.0,
    '1" = 8ft': 96.0,
    '1" = 7ft': 84.0,
    '3/16" = 1ft': 64.0,
    '1/8" = 1ft': 96.0,}

Page_Scale = Page_Scales['1" = 10ft']  # choose your page scale here

FLOOR_HEIGHT_FT = 9.0 # choose your floor height here (in feet) one for all floors 

# BB_GRID_SPACING = 0.125  # Ctrl+k → Grid and Snap settings in Bluebeam → Grid Spacing (inches)
# BB_GRID_SPACING = 0.125/2  # Ctrl+k → Grid and Snap settings in Bluebeam → Grid Spacing (inches)
# BB_GRID_SPACING = 0.04921875  # 0.125 cm → 0.0492 inch
BB_GRID_SPACING = 0.05905512  # 0.15 cm → 0.0492 inch

# Choose your output grid density here (THIS AFFECTS GEOMETRY HEALING AND SNAPPING) [TLDR: This says "snap drawing's points to a grid of X spacing"]
Grid_Densitys = { 
    '1"': 0.6,
    '2"': 1.2,
    'As drawn"': BB_GRID_SPACING * 72.0,  # convert inches to points
    '0.125cm': 0.125 / 2.54 * 72.0  # convert cm to points
}

GRID_SPACING = Grid_Densitys['As drawn"']  # choose your grid density here (in points)

# Keep your page scale & PT→m conversion logic
FT_TO_M = 0.3048
FLOOR_HEIGHT_M  = FLOOR_HEIGHT_FT * FT_TO_M


CURRENT_LOCATION = LOCATIONS[CURRENT_LOCATION]

# Your previous scaling approach:
PT_TO_M = 0.0254 * Page_Scale / 72.0

# Include parent polygons as rooms?
INCLUDE_PARENT_AS_ROOM = True

# Geometry healing (very tight defaults like before)
# HEAL_TOL_PT        = GRID_SPACING    # merge nearly-coincident vertices within one grid step
# PERP_SNAP_TOL_PT   = GRID_SPACING    # half-band width around the target axis for snapping
# ALONG_EXTEND_PT    = GRID_SPACING/2  # extend the snap band by ~half a grid step along the edge
# MIN_EDGE_LEN_PT    = GRID_SPACING    # ignore micro-edges below one grid step (likely noise)
# STRAIGHT_SLOPE_RATIO = 0.0 # we'll switch to an absolute test below

HEAL_TOL_PT        = GRID_SPACING/2    # merge nearly-coincident vertices within one grid step
PERP_SNAP_TOL_PT   = GRID_SPACING/2    # half-band width around the target axis for snapping
ALONG_EXTEND_PT    = GRID_SPACING/4  # extend the snap band by ~half a grid step along the edge
MIN_EDGE_LEN_PT    = GRID_SPACING/2    # ignore micro-edges below one grid step (likely noise)
STRAIGHT_SLOPE_RATIO = 0.0 # we'll switch to an absolute test below

# Shift into +X/+Y quadrant with minimal margin
OFFSET_MARGIN_PT = 0.0


def decode_raw(raw_text: str) -> str:
    raw_text = (raw_text or "").strip()
    if not raw_text:
        return ""
    data = bytes.fromhex(raw_text)
    return zlib.decompress(data).decode("utf-8", errors="ignore")

def extract_vertices(decoded: str) -> List[List[Tuple[float, float]]]:
    CLOSE_TOL = 1e-6
    polys: List[List[Tuple[float, float]]] = []
    for m in re.findall(r'Vertices\[(.*?)\]', decoded, flags=re.DOTALL):
        nums = [float(x) for x in m.strip().split()]
        if not nums or len(nums) % 2:
            continue
        pts = list(zip(nums[::2], nums[1::2]))
        if not (math.isclose(pts[0][0], pts[-1][0], abs_tol=CLOSE_TOL)
                and math.isclose(pts[0][1], pts[-1][1], abs_tol=CLOSE_TOL)):
            pts = pts + [pts[0]]
        polys.append(pts)
    return polys

def f2(elem: ET.Element, tag: str, default: float = 0.0) -> float:
    t = elem.findtext(tag)
    return float(t) if t is not None else default

def load_tool_polygons(tool: ET.Element):
    """
    Return (parent_polys, child_polys) in POINTS (BTX native).
    Child polygons are placed with (parentX - childX, parentY - childY).
    """
    parentX, parentY = f2(tool, "X"), f2(tool, "Y")

    parent_polys: List[List[Tuple[float, float]]] = []
    raw_p = tool.findtext("Raw")
    if raw_p:
        for poly in extract_vertices(decode_raw(raw_p)):
            parent_polys.append(poly)

    child_polys: List[List[Tuple[float, float]]] = []
    for ch in tool.findall("./Child"):
        raw_c = ch.findtext("Raw")
        if not raw_c:
            continue
        childX, childY = f2(ch, "X"), f2(ch, "Y")
        dx, dy = (parentX - childX), (parentY - childY)
        for poly in extract_vertices(decode_raw(raw_c)):
            placed = [(dx + x, dy + y) for (x, y) in poly]
            child_polys.append(placed)

    return parent_polys, child_polys

def _ensure_closed(poly):
    return poly if (len(poly) > 1 and poly[0] == poly[-1]) else (poly + [poly[0]])

def _changed_count(before: List[List[Tuple[float,float]]],
                   after:  List[List[Tuple[float,float]]]) -> int:
    """Count how many coordinates differ (strict float compare)."""
    cnt = 0
    for p0, p1 in zip(before, after):
        for (x0,y0), (x1,y1) in zip(p0, p1):
            if x0 != x1 or y0 != y1:
                cnt += 1
    return cnt

def straighten_axis_aligned(polys: List[List[Tuple[float, float]]]) -> List[List[Tuple[float, float]]]:
    polys = [[(float(x), float(y)) for (x, y) in _ensure_closed(poly)] for poly in polys]

    def snap_band_to_y(y_line, x_lo, x_hi):
        x_lo -= ALONG_EXTEND_PT; x_hi += ALONG_EXTEND_PT
        for p in polys:
            for i, (x, y) in enumerate(p):
                if x_lo <= x <= x_hi and abs(y - y_line) <= PERP_SNAP_TOL_PT:
                    p[i] = (x, y_line)

    def snap_band_to_x(x_line, y_lo, y_hi):
        y_lo -= ALONG_EXTEND_PT; y_hi += ALONG_EXTEND_PT
        for p in polys:
            for i, (x, y) in enumerate(p):
                if y_lo <= y <= y_hi and abs(x - x_line) <= PERP_SNAP_TOL_PT:
                    p[i] = (x_line, y)

    before = [p[:] for p in polys]

    for p in polys:
        n = len(p) - 1
        for i in range(n):
            (x0, y0), (x1, y1) = p[i], p[i+1]
            dx, dy = (x1 - x0), (y1 - y0)
            L = math.hypot(dx, dy)
            if L < MIN_EDGE_LEN_PT:
                continue

            # Nearly horizontal?
            # if abs(dy) <= STRAIGHT_SLOPE_RATIO * L:
            if abs(dy) <= PERP_SNAP_TOL_PT: 
                y_line = 0.5 * (y0 + y1)
                p[i]   = (x0, y_line)
                p[i+1] = (x1, y_line)
                x_lo, x_hi = (x0, x1) if x0 <= x1 else (x1, x0)
                snap_band_to_y(y_line, x_lo, x_hi)
                continue

            # Nearly vertical?
            # if abs(dx) <= STRAIGHT_SLOPE_RATIO * L:
            if abs(dx) <= PERP_SNAP_TOL_PT:
                x_line = 0.5 * (x0 + x1)
                p[i]   = (x_line, y0)
                p[i+1] = (x_line, y1)
                y_lo, y_hi = (y0, y1) if y0 <= y1 else (y1, y0)
                snap_band_to_x(x_line, y_lo, y_hi)

    after = [_ensure_closed(p) for p in polys]
    # changed = _changed_count(before, after)
    # if changed > 0:
    #     print(f'straighten_axis_aligned executed {changed} number of times')  # debug line only on real change
    return after

def snap_points(polys: List[List[Tuple[float, float]]], tol: float) -> List[List[Tuple[float, float]]]:
    if tol <= 0:
        return polys

    before = [p[:] for p in polys]

    def key(pt):
        return (int(round(pt[0] / tol)), int(round(pt[1] / tol)))

    representative: Dict[Tuple[int, int], Tuple[float, float]] = {}
    clusters: Dict[Tuple[int, int], List[Tuple[float, float]]] = {}

    for poly in polys:
        for (x, y) in poly:
            k = key((x, y))
            clusters.setdefault(k, []).append((x, y))

    for k, pts in clusters.items():
        sx = sum(p[0] for p in pts) / len(pts)
        sy = sum(p[1] for p in pts) / len(pts)
        representative[k] = (sx, sy)

    snapped = []
    for poly in polys:
        new_poly = []
        for (x, y) in poly:
            k = key((x, y))
            new_poly.append(representative[k])
        if new_poly[0] != new_poly[-1]:
            new_poly.append(new_poly[0])
        snapped.append(new_poly)

    changed = _changed_count(before, snapped)
    # if changed > 0:
    #     print(f'snap_points executed {changed} number of times')  # debug line only on real change
    return snapped

def offset_to_positive(polys, margin=0.0, eps=1e-9):
    # Always shift so the minimum becomes exactly 'margin' (no condition).
    minx = min(p[0] for poly in polys for p in poly)
    miny = min(p[1] for poly in polys for p in poly)
    dx = -minx + margin
    dy = -miny + margin

    out = []
    for poly in polys:
        new_poly = []
        for (x, y) in poly:
            X = x + dx
            Y = y + dy
            # squash tiny floating noise to exact zeros (or exact margin)
            if abs(X - margin) < eps: X = margin
            if abs(Y - margin) < eps: Y = margin
            new_poly.append((X, Y))
        if new_poly[0] != new_poly[-1]:
            new_poly.append(new_poly[0])
        out.append(new_poly)
    return out

def extrude_polygon_to_prism(pts2d_m: List[Tuple[float, float]], h_m: float):
    ring = pts2d_m[:-1] if len(pts2d_m) > 1 and pts2d_m[0] == pts2d_m[-1] else pts2d_m[:]
    n = len(ring)

    verts = []
    for (x, y) in ring:      # base (z=0)
        verts.append((x, y, 0.0))
    for (x, y) in ring:      # top (z=h)
        verts.append((x, y, h_m))

    faces = []
    faces.append({"indices": list(range(0, n)), "flag": 0})             # base
    faces.append({"indices": list(range(n, 2*n)), "flag": 0})           # top
    for i in range(n):                                                  # sides
        i_next = (i + 1) % n
        faces.append({"indices": [i, i_next, n + i_next, n + i], "flag": 0})

    return verts, faces

def extrude_with_zoffset(pts2d_m: List[Tuple[float, float]], h_m: float, z0_m: float):
    verts, faces = extrude_polygon_to_prism(pts2d_m, h_m)
    verts = [(x, y, z + z0_m) for (x, y, z) in verts]
    return verts, faces

def snap_to_grid(polys, step):
    changed = 0
    out = []
    for poly in polys:
        q = []
        for (x, y) in poly:
            nx = round(x / step) * step
            ny = round(y / step) * step
            if nx != x or ny != y:
                changed += 1
            q.append((nx, ny))
        if q[0] != q[-1]:
            q.append(q[0])
        out.append(q)
    # if changed > 0:
    #     print("snap_to_grid executed")
    return out

def read_room_csv(csv_path: Path) -> List[Tuple[str, int]]:
    """
    Accepts CSV with two columns: Name, Floor (case-insensitive).
    If headers are absent, treats first column as name, second as floor.
    Returns a list of (name, floor) in CSV order.
    """
    rows = []
    with csv_path.open("r", newline="", encoding="utf-8") as f:
        sniff = csv.Sniffer()
        sample = f.read(2048)
        f.seek(0)
        has_header = False
        try:
            has_header = sniff.has_header(sample)
        except Exception:
            pass
        reader = csv.reader(f)
        data = list(reader)

    if not data:
        return rows

    if has_header:
        header = [h.strip().lower() for h in data[0]]
        name_idx = None
        floor_idx = None
        # Flexible header detection
        for i, h in enumerate(header):
            if h in ("name", "room", "label"):
                name_idx = i
            if h in ("floor", "level", "storey", "story"):
                floor_idx = i
        if name_idx is None or floor_idx is None:
            # fallback: first two columns
            name_idx, floor_idx = 0, 1
        iter_rows = data[1:]
    else:
        name_idx, floor_idx = 0, 1
        iter_rows = data

    for r in iter_rows:
        if len(r) <= max(name_idx, floor_idx):
            continue
        name = r[name_idx].strip()
        try:
            floor = int(float(r[floor_idx]))
        except Exception:
            continue
        rows.append((name, floor))
    return rows

def write_gem(spaces: List[Dict], out_path: Path):
    rec = []
    rec += ["COM GEM data file exported by MODELIT", "ANT"]
    rec += ["SITE"]
    rec += [CURRENT_LOCATION]  # lat lon elev rot

    for s in spaces:
        rec += ["LAYER", "1",
                "COLOUR", "0",
                "CATEGORY", "1",
                "TYPE", "1",
                "SUBTYPE", "2001",
                "COLOURRGB", "16711680"]

        rec += [f"IES {s['name']}"]
        npts = len(s["verts"])
        nfaces = len(s["faces"])
        rec += [f"{npts} {nfaces}"]

        for (x, y, z) in s["verts"]:
            rec += [f"{x:.6f} {y:.6f} {z:.6f}"]

        for f in s["faces"]:
            inds = [i + 1 for i in f["indices"]]   # 1-based
            rec += [" ".join([str(len(inds))] + [str(k) for k in inds])]
            rec += [str(f.get("flag", 0))]         # no openings

    out_path.write_text("\n".join(rec), encoding="utf-8")

# Original GEM wireframe parser
def _next_nonempty(lines, i):
    n = len(lines)
    while i < n and lines[i].strip() == "":
        i += 1
    return i

def _parse_int_line(lines, i):
    i = _next_nonempty(lines, i)
    val = int(lines[i].strip().split()[0])
    return val, _next_nonempty(lines, i + 1)

def _parse_floats_line(lines, i):
    i = _next_nonempty(lines, i)
    vals = [float(x) for x in lines[i].strip().split()]
    return vals, _next_nonempty(lines, i + 1)

def _looks_like_face_line(lines, i, npts):
    i = _next_nonempty(lines, i)
    if i >= len(lines): 
        return False
    toks = lines[i].strip().split()
    if not toks or not re.fullmatch(r"[+-]?\d+", toks[0]):
        return False
    try:
        ints = [int(t) for t in toks]
    except Exception:
        return False
    m = ints[0]
    if m < 3 or len(ints) != m + 1:
        return False
    # Face vertex indices are 1..npts
    return all(1 <= ii <= npts for ii in ints[1:])

def _parse_face_line(lines, i):
    vals = [int(t) for t in lines[i].strip().split()]
    m = vals[0]
    idxs = vals[1:1+m]  # 1-based indices
    return idxs, _next_nonempty(lines, i + 1)

def _face_plane_axes(V, idxs):
    """Return (origin p0, unit u-axis, unit v-axis) for a face.
       Openings written in (u,v) are placed as: p = p0 + u*u_axis + v*v_axis
    """
    p0 = V[idxs[0]]
    # find two non-collinear edges
    for a_i in range(1, len(idxs) - 1):
        a = V[idxs[a_i]]   - p0
        b = V[idxs[a_i+1]] - p0
        n = np.cross(a, b)
        if np.linalg.norm(n) > 1e-9 and np.linalg.norm(a) > 1e-9:
            u = a / np.linalg.norm(a)
            n_hat = n / np.linalg.norm(n)
            v = np.cross(n_hat, u)
            v_norm = np.linalg.norm(v)
            if v_norm > 1e-9:
                v = v / v_norm
                return p0, u, v
    # fallback axes (should be rare)
    return p0, np.array([1.0, 0.0, 0.0]), np.array([0.0, 1.0, 0.0])

def parse_gem(path: Path):
    """Robust parser for ModelIT GEM wireframe with openings."""
    text = path.read_text(encoding="utf-8", errors="ignore")
    lines = text.splitlines()
    n = len(lines)
    i = 0

    # SITE (optional to display, but we store for completeness)
    while i < n and lines[i].strip() != "SITE":
        i += 1
    site = None
    if i < n and lines[i].strip() == "SITE":
        i = _next_nonempty(lines, i + 1)
        if i < n:
            parts = lines[i].strip().split()
            if len(parts) >= 4:
                lat, lon, elev, rot = map(float, parts[:4])
                site = {"latitude": lat, "longitude": lon, "elevation_m": elev, "rotation_deg": rot}
            i = _next_nonempty(lines, i + 1)

    spaces = []

    # Iterate blocks: LAYER ... IES <label> ... counts, vertices, faces, openings ...
    while i < n:
        # Find start of a space block
        while i < n and lines[i].strip() != "LAYER":
            i += 1
        if i >= n:
            break

        # Metadata (we only store layer & color for reference)
        layer_val, i = _parse_int_line(lines, i + 1)

        meta_tokens = ["COLOUR", "CATEGORY", "TYPE", "SUBTYPE", "COLOURRGB"]
        meta_vals = {}
        for tok in meta_tokens:
            i = _next_nonempty(lines, i)
            if i >= n or lines[i].strip() != tok:
                raise ValueError(f"Expected token '{tok}' near line {i+1}")
            val, i = _parse_int_line(lines, i + 1)
            meta_vals[tok] = val

        # Space label
        i = _next_nonempty(lines, i)
        if i >= n:
            break
        label = lines[i].strip()
        # Some exports might have blank/comments between; be resilient
        if not label.startswith("IES"):
            while i < n and not lines[i].startswith("IES"):
                i += 1
            if i >= n:
                break
            label = lines[i].strip()
        # Store a clean name
        space_name = label.split("IES", 1)[1].strip() if "IES" in label else label.strip()
        i = _next_nonempty(lines, i + 1)

        # Counts
        counts, i = _parse_floats_line(lines, i)
        npts, nfaces = int(counts[0]), int(counts[1])

        # Vertices (metres)
        verts = []
        for _ in range(npts):
            xyz, i = _parse_floats_line(lines, i)
            verts.append(xyz[:3])
        V = np.array(verts, dtype=float)

        # Define block end (start of next space or EOF)
        j = i
        while j < n and lines[j].strip() != "LAYER":
            j += 1
        block_end = j

        # Faces + openings (scan within [i, block_end))
        faces = []
        openings = []  # [{face_index, uv: (k,2) array}]
        face_count = 0
        cursor = i

        def try_consume_openings(cur, face_idx):
            """Consume one or more 'k [tag]' + k (u v) rows immediately following."""
            consumed_any = False
            while cur < block_end:
                probe = _next_nonempty(lines, cur)
                if probe >= block_end:
                    return probe, consumed_any
                line = lines[probe].strip()

                # Stop if next line is a face or a new block token
                if _looks_like_face_line(lines, probe, npts):
                    return probe, consumed_any
                if line in {"LAYER", "COLOUR", "CATEGORY", "TYPE", "SUBTYPE", "COLOURRGB"} or line.startswith("IES"):
                    return probe, consumed_any

                # Opening header: "k [tag]"
                toks = line.split()
                try:
                    k = int(toks[0])
                except Exception:
                    return probe, consumed_any

                # Validate next k lines are float pairs
                check = _next_nonempty(lines, probe + 1)
                ok = True
                for _ in range(k):
                    if check >= block_end:
                        ok = False
                        break
                    pair = lines[check].strip().split()
                    if len(pair) < 2:
                        ok = False
                        break
                    try:
                        float(pair[0]); float(pair[1])
                    except Exception:
                        ok = False
                        break
                    check = _next_nonempty(lines, check + 1)
                if not ok:
                    return probe, consumed_any

                # Consume header + k UV rows
                cur = _next_nonempty(lines, probe + 1)
                uv = []
                for _ in range(k):
                    vals, cur = _parse_floats_line(lines, cur)
                    uv.append(vals[:2])
                openings.append({"face_index": face_idx, "uv": np.array(uv, dtype=float)})
                consumed_any = True

            return cur, consumed_any

        while cursor < block_end and face_count < nfaces:
            # Find next face
            while cursor < block_end and not _looks_like_face_line(lines, cursor, npts):
                cursor += 1
            if cursor >= block_end:
                break

            idxs_1based, cursor = _parse_face_line(lines, cursor)
            # Flag
            flag, cursor = _parse_int_line(lines, cursor)
            faces.append({"indices": [k - 1 for k in idxs_1based], "flag": flag})

            # If flagged, read one or more subsequent opening polygons
            if flag == 1:
                cursor, _ = try_consume_openings(cursor, face_idx=len(faces) - 1)

            face_count += 1

        spaces.append({
            "name": space_name,
            "layer": layer_val,
            "meta": meta_vals,
            "vertices": V,
            "faces": faces,
            "openings": openings
        })

        # Move to next block
        i = block_end

    return {"site": site, "spaces": spaces}

# ---------- Browser adapter (I/O, validation, summaries only) ----------
def browser_options():
    return {
        'locations': LOCATIONS, 'scales': Page_Scales, 'grids': Grid_Densitys,
        'defaults': {'location': 'Vancouver, BC', 'scale': '1" = 10ft',
                     'grid': 'As drawn"', 'height_ft': 9.0},
        'bluebeam_grid_inches': BB_GRID_SPACING,
    }


def parse_room_table(text):
    """Explicit headers avoid Sniffer dropping the sample's tab-padded headers.
    Row order still drives the pairing; Room Number is a display identifier.
    Invalid rows are reported rather than silently shifting later room names.
    """
    data = [row for row in csv.reader(io.StringIO(text.lstrip('\ufeff')), strict=True)
            if any(cell.strip() for cell in row)]
    if not data:
        raise ValueError('The CSV is empty. Use Room Number, Name, Floor columns.')
    header = [c.strip().lower() for c in data[0]]
    names = ('name', 'room', 'label')
    floors = ('floor', 'level', 'storey', 'story')
    name_i = next((i for i, h in enumerate(header) if h in names), None)
    floor_i = next((i for i, h in enumerate(header) if h in floors), None)
    number_i = next((i for i, h in enumerate(header)
                     if h in ('room number', 'room #', 'number', '#')), None)
    has_header = name_i is not None or floor_i is not None
    if has_header:
        if name_i is None or floor_i is None:
            raise ValueError('CSV headers must include Name and Floor (or Level).')
        values = data[1:]
    else:
        values = data
        if len(data[0]) == 2:
            name_i, floor_i, number_i = 0, 1, None
        elif len(data[0]) >= 3:
            number_i, name_i, floor_i = 0, 1, 2
        else:
            raise ValueError('CSV needs Name and Floor columns.')
    rows = []
    for index, row in enumerate(values):
        line = index + (2 if has_header else 1)
        if len(row) <= max(name_i, floor_i, number_i or 0):
            raise ValueError(f'CSV row {line} is missing a column.')
        rows.append({'number': row[number_i].strip() if number_i is not None else str(index + 1),
                     'name': row[name_i].strip(), 'floor': row[floor_i].strip()})
    validate_rows(rows)
    if not rows:
        raise ValueError('The CSV has a header but no room rows.')
    return rows


def validate_rows(rows):
    if len(rows) > 10000:
        raise ValueError('The room table is limited to 10,000 rows.')
    specs = []
    for i, row in enumerate(rows, 1):
        name = str(row.get('name', '')).strip()
        if not name or any(ord(c) < 32 for c in name):
            raise ValueError(f'Row {i}: enter a room name without line breaks or control characters.')
        try:
            floor = float(row.get('floor', ''))
            if not math.isfinite(floor) or floor != int(floor) or not 1 <= floor <= 1000:
                raise ValueError()
        except (ValueError, TypeError):
            raise ValueError(f'Row {i}: floor must be a whole number from 1 to 1,000.') from None
        specs.append((name, int(floor)))
    return specs


def set_browser_config(config):
    global CURRENT_LOCATION, Page_Scale, GRID_SPACING, FLOOR_HEIGHT_FT, FLOOR_HEIGHT_M
    global PT_TO_M, HEAL_TOL_PT, PERP_SNAP_TOL_PT, ALONG_EXTEND_PT, MIN_EDGE_LEN_PT
    location = config.get('location', 'Vancouver, BC')
    scale = config.get('scale', '1" = 10ft')
    grid = config.get('grid', 'As drawn"')
    if location not in LOCATIONS:
        raise ValueError('Choose a location from the supplied script.')

    def positive_number(key, label):
        try:
            value = float(config.get(key, ''))
        except (ValueError, TypeError):
            raise ValueError(f'{label} must be a number greater than zero.') from None
        if not math.isfinite(value) or value <= 0:
            raise ValueError(f'{label} must be a finite number greater than zero.')
        return value

    if scale == 'custom':
        page_scale = positive_number('custom_scale', 'Custom page scale (1:N)')
    elif scale in Page_Scales:
        page_scale = Page_Scales[scale]
    else:
        raise ValueError('Choose a page scale or enter a custom 1:N ratio.')
    if grid == 'custom_inches':
        # Custom values are drawing/page spacing, as in the Bluebeam grid setting.
        grid_spacing = positive_number('custom_grid_inches', 'Custom grid spacing in inches') * 72.0
    elif grid == 'custom_cm':
        grid_spacing = positive_number('custom_grid_cm', 'Custom grid spacing in centimetres') / 2.54 * 72.0
    elif grid in Grid_Densitys:
        grid_spacing = Grid_Densitys[grid]
    else:
        raise ValueError('Choose a grid preset or enter custom inches or centimetres.')
    if not math.isfinite(grid_spacing) or grid_spacing <= 0 or not math.isfinite(0.0254 * page_scale):
        raise ValueError('The custom grid or scale is outside the supported numeric range.')
    height = float(config.get('height_ft', 9.0))
    if not math.isfinite(height) or height <= 0 or height > 1000:
        raise ValueError('Floor height must be greater than 0 and no more than 1,000 ft.')
    CURRENT_LOCATION = LOCATIONS[location]
    Page_Scale, GRID_SPACING = page_scale, grid_spacing
    FLOOR_HEIGHT_FT = height
    FLOOR_HEIGHT_M = height * FT_TO_M
    PT_TO_M = 0.0254 * Page_Scale / 72.0
    HEAL_TOL_PT = PERP_SNAP_TOL_PT = MIN_EDGE_LEN_PT = GRID_SPACING / 2
    ALONG_EXTEND_PT = GRID_SPACING / 4


def polygon_area(poly):
    return abs(sum(a[0]*b[1] - b[0]*a[1] for a, b in zip(poly[:-1], poly[1:]))) / 2


def analyze_browser(request):
    set_browser_config(request.get('config', {}))
    raw = base64.b64decode(request['btx'], validate=True)
    if len(raw) > 30 * 1024 * 1024:
        raise ValueError('Use a BTX smaller than 30 MB.')
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as error:
        raise ValueError(f'This BTX is not valid XML: {error}') from None
    all_parent, all_children = [], []
    for tool in root.findall('.//ToolChestItem'):
        parent, children = load_tool_polygons(tool)
        all_parent.extend(parent)
        all_children.extend(children)
    room_polys = (all_parent if INCLUDE_PARENT_AS_ROOM else []) + all_children
    if not room_polys:
        raise ValueError('No room polygons found. Export a Bluebeam tool set containing polygon markups.')
    for i, poly in enumerate(room_polys, 1):
        if len(poly) < 4 or not all(math.isfinite(v) for p in poly for v in p):
            raise ValueError(f'Polygon {i} has invalid or insufficient vertices.')
    # Same order and tolerances as the source main().
    room_polys = straighten_axis_aligned(room_polys)
    room_polys = snap_points(room_polys, tol=HEAL_TOL_PT)
    room_polys = snap_to_grid(room_polys, GRID_SPACING)
    room_polys = offset_to_positive(room_polys, margin=OFFSET_MARGIN_PT)
    for i, poly in enumerate(room_polys, 1):
        if len(set(poly[:-1])) < 3 or polygon_area(poly) <= 1e-12:
            raise ValueError(f'Polygon {i} collapses at this grid density. Choose a finer grid or check the drawing.')
    rows = request.get('rows', [])
    name_floor = validate_rows(rows)
    count = min(len(name_floor), len(room_polys))
    specs, polys = name_floor[:count], room_polys[:count]
    floor_counts, rooms, warnings = {}, [], []
    for i, ((name, floor), poly) in enumerate(zip(specs, polys)):
        floor_counts[floor] = floor_counts.get(floor, 0) + 1
        rooms.append({'index': i + 1, 'number': str(rows[i].get('number', i+1)),
                      'name': name, 'floor': floor, 'label': f'{name}-L{floor}',
                      'poly': poly, 'area_m2': polygon_area(poly) * PT_TO_M ** 2})
    if len(rows) != len(room_polys):
        warnings.append(f'{len(rows)} CSV rows / {len(room_polys)} BTX polygons. Only the first {count} paired rooms will be exported; {len(room_polys)-count} polygons and {len(rows)-count} CSV rows are excluded.')
    max_floor = max((f for _, f in specs), default=0)
    missing = [f for f in range(1, max_floor+1) if f not in floor_counts]
    if missing:
        warnings.append('Empty floors: ' + ', '.join(map(str, missing)) + '. Their vertical offsets are preserved.')
    diagonal = sum(1 for p in polys for a,b in zip(p[:-1],p[1:]) if a[0] != b[0] and a[1] != b[1])
    if diagonal:
        warnings.append(f'{diagonal} non-axis-aligned edges remain after healing (shown in red). Review these before export.')
    labels = [r['label'] for r in rooms]
    if len(labels) != len(set(labels)):
        warnings.append('Duplicate room labels exist on the same floor. Check the room names before export.')
    return {'rooms': rooms, 'polygons': room_polys, 'floor_counts': floor_counts,
            'total_found': len(room_polys), 'csv_count': len(rows), 'paired': count,
            'max_floor': max_floor, 'populated_floors': len(floor_counts),
            'height_ft': FLOOR_HEIGHT_FT, 'height_m': FLOOR_HEIGHT_M,
            'pt_to_m': PT_TO_M, 'page_scale': Page_Scale, 'grid_pt': GRID_SPACING, 'diagonal_edges': diagonal,
            'area_m2': sum(r['area_m2'] for r in rooms), 'warnings': warnings,
            'mismatch': len(rows) != len(room_polys)}


def preview_spaces(model):
    """Extrude the healed polygons immediately, without creating a GEM file.

    Unpaired polygons have no known floor. Show them at z=0 with explicit
    unassigned labels until a CSV supplies the names/floors. They remain
    excluded from GEM export. Paired geometry uses the original extrusion.
    """
    spaces = []
    for i, poly in enumerate(model['polygons']):
        room = model['rooms'][i] if i < model['paired'] else None
        floor = room['floor'] if room else None
        z0 = (floor - 1) * model['height_m'] if room else 0.0
        poly_m = [(x * model['pt_to_m'], y * model['pt_to_m']) for x, y in poly]
        vertices, faces = extrude_with_zoffset(poly_m, model['height_m'], z0)
        # Match the original writer's six-place coordinate formatting so the
        # live paired-room wireframe agrees with the later GEM export.
        vertices = [[float(f'{v:.6f}') for v in point] for point in vertices]
        spaces.append({'name': room['label'] if room else f'Polygon {i+1} (unassigned)',
                       'floor': floor, 'unassigned': room is None,
                       'vertices': vertices, 'faces': faces})
    return spaces


def export_browser(request):
    model = analyze_browser(request)
    if not model['paired']:
        raise ValueError('Add room names and floors before exporting.')
    if model['mismatch'] and not request.get('accept_mismatch'):
        raise ValueError('Confirm the row/polygon mismatch before exporting the paired rooms.')
    spaces_out = []
    for room in model['rooms']:
        poly_m = [(x * PT_TO_M, y * PT_TO_M) for x,y in room['poly']]
        verts, faces = extrude_with_zoffset(poly_m, FLOOR_HEIGHT_M, (room['floor']-1)*FLOOR_HEIGHT_M)
        spaces_out.append({'name': room['label'], 'verts': verts, 'faces': faces})
    # The original writer serializes to six decimal places. Parse that exact
    # export so the 3D view depicts the downloaded GEM, including its rounding.
    path = Path('_browser_export.gem')
    try:
        write_gem(spaces_out, path)
        gem = path.read_text(encoding='utf-8')
        parsed = parse_gem(path)
        serial_spaces = []
        for room, sp in zip(model['rooms'], parsed['spaces']):
            serial_spaces.append({'name': sp['name'], 'floor': room['floor'],
                                  'vertices': sp['vertices'].tolist(), 'faces': sp['faces']})
    finally:
        path.unlink(missing_ok=True)
    # Match the Windows line endings in the supplied GEM sample.
    return {'gem': gem.replace('\n', '\r\n'), 'spaces': serial_spaces, 'site': parsed['site'], 'summary': model}


def browser_dispatch(request_json):
    request = json.loads(request_json)
    action = request.get('action')
    if action == 'options':
        result = browser_options()
    elif action == 'csv':
        result = {'rows': parse_room_table(request['text'])}
    elif action == 'analyze':
        result = analyze_browser(request)
        result['preview_spaces'] = preview_spaces(result)
    elif action == 'export':
        result = export_browser(request)
    else:
        raise ValueError('Unknown browser action.')
    return json.dumps(result, allow_nan=False)
