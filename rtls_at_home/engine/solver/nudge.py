"""Push a fixture's footprint out of the raster wall band (Nick 2026-09-26: "get all the furniture out of the walls").

The specs place furniture against the wall LINE (the plan's centre line); the raster paints the wall 5 cm either
side of it, so every piece against a wall overlapped the band by a cell or two and the 2-D plan drew it inside the
wall. Each axis-aligned edge of a rectilinear footprint whose strip of cells is mostly wall is moved inward past
the band, plus `margin` cells (real furniture stands at least an inch off the wall). Diagonal edges and sheets that
live entirely in the wall (mirrors, tile) are left alone.
"""
import numpy as np


def _inside(pts, x, y):
    """Even-odd point-in-polygon."""
    hit = False
    n = len(pts)
    for i in range(n):
        (xi, yi), (xj, yj) = pts[i], pts[(i + 1) % n]
        if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / (yj - yi) + xi:
            hit = not hit
    return hit


def _strip(wall, axis, coord, lo, hi):
    """Wall fraction of the one-cell strip at `coord` along `axis` ('x': a column, 'y': a row) between lo..hi."""
    h, w = wall.shape
    a, b = int(np.floor(lo)), int(np.ceil(hi))
    if b <= a:
        return 0.0
    if axis == "x":
        c = int(np.floor(coord))
        if not 0 <= c < w:
            return 0.0
        seg = wall[max(a, 0):min(b, h), c]
    else:
        r = int(np.floor(coord))
        if not 0 <= r < h:
            return 0.0
        seg = wall[r, max(a, 0):min(b, w)]
    return float(seg.mean()) if seg.size else 0.0


def nudge_off_walls(pts, wall, margin=1, limit=6, threshold=0.5, sheet=0.9):
    """pts: [(col, row), ...] in cell coordinates (floats), a rectilinear polygon; wall: bool (rows, cols).
    Returns (new_pts, moved) with moved {edge_index: cells shifted}."""
    pts = [(float(x), float(y)) for x, y in pts]
    n = len(pts)
    # a sheet on the wall face: nearly every cell of it is wall - not furniture to push around
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    cells = [(c, r) for r in range(int(np.floor(min(ys))), int(np.ceil(max(ys))))
             for c in range(int(np.floor(min(xs))), int(np.ceil(max(xs)))) if _inside(pts, c + 0.5, r + 0.5)]
    inw = [wall[r, c] for c, r in cells if 0 <= r < wall.shape[0] and 0 <= c < wall.shape[1]]
    if cells and inw and np.mean(inw) >= sheet:
        return pts, {}
    out = list(pts)
    moved = {}
    for i in range(n):
        (x0, y0), (x1, y1) = pts[i], pts[(i + 1) % n]
        if x0 == x1:                                       # a vertical edge: shifts along x
            axis, coord, lo, hi, mx, my = "x", x0, min(y0, y1), max(y0, y1), 1.0, 0.0
        elif y0 == y1:                                     # a horizontal edge: shifts along y
            axis, coord, lo, hi, mx, my = "y", y0, min(x0, x1), max(x0, x1), 0.0, 1.0
        else:
            continue                                       # diagonal: leave it
        midx, midy = (x0 + x1) / 2, (y0 + y1) / 2
        sgn = 1.0 if _inside(pts, midx + 0.5 * mx, midy + 0.5 * my) else -1.0      # inward normal
        k = 0
        while k < limit and _strip(wall, axis, coord + sgn * (k + 0.5), lo, hi) >= threshold:
            k += 1
        if k == 0 or k >= limit:                           # not in a band, or the edge runs ALONG the wall: leave it
            continue
        shift = k + margin
        extent = (max(xs) - min(xs)) if axis == "x" else (max(ys) - min(ys))
        if shift >= extent / 2:                            # would fold a thin piece (a TV on the wall): slide it whole
            out = [(px + sgn * shift * mx, py + sgn * shift * my) for px, py in out]
        else:
            for j in (i, (i + 1) % n):
                px, py = out[j]
                out[j] = (px + sgn * shift * mx, py + sgn * shift * my)
        moved[i] = shift
    return out, moved
