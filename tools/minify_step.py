"""Lossless-in-practice STEP compaction.

* reals are rounded to 9 digits after the point (1e-9 mm): coarser rounding
  (1e-7) was seen to turn near-touching loops of a face self-intersecting
* identical *geometric* entities are merged (points, directions, vectors,
  placements, curves, surfaces) - topology is never merged, so separate solids
  stay separate
* entities are renumbered densely and written one per line without padding
"""
import re
import sys

GEOM = {
    "CARTESIAN_POINT", "DIRECTION", "VECTOR", "AXIS2_PLACEMENT_3D", "AXIS1_PLACEMENT",
    "AXIS2_PLACEMENT_2D", "LINE", "CIRCLE", "ELLIPSE", "PLANE", "CYLINDRICAL_SURFACE",
    "CONICAL_SURFACE", "SPHERICAL_SURFACE", "TOROIDAL_SURFACE", "B_SPLINE_CURVE_WITH_KNOTS",
    "B_SPLINE_SURFACE_WITH_KNOTS", "SURFACE_OF_LINEAR_EXTRUSION", "SURFACE_OF_REVOLUTION",
    "OFFSET_SURFACE",
}

ENT = re.compile(r"#(\d+)\s*=\s*(.*?);\s*$", re.S)
REF = re.compile(r"#(\d+)")
# a real: digits with a '.', optional exponent; not preceded by '#' or a letter
REAL = re.compile(r"(?<![#\w.])(-?\d+\.\d*(?:[eE][-+]?\d+)?)")


def split_statements(data):
    """Yield statements of the DATA section, honoring quoted strings."""
    out, q = [], False
    i = 0
    n = len(data)
    start = 0
    while i < n:
        c = data[i]
        if c == "'":
            q = not q
        elif c == ";" and not q:
            out.append(data[start:i + 1])
            start = i + 1
        i += 1
    tail = data[start:].strip()
    return out, tail


def fmt_real(m, decimals):
    s = m.group(1)
    x = float(s)
    if x == 0:
        return "0."
    r = round(x, decimals)
    if r == 0:
        return "0."
    t = ("%.*f" % (decimals, r)).rstrip("0")
    if t.startswith("-0.") and float(t) == 0:
        t = "0."
    return t


def compact_args(body, decimals):
    """Round reals and strip whitespace outside strings."""
    parts = re.split(r"('(?:[^']|'')*')", body)
    for i in range(0, len(parts), 2):
        p = re.sub(r"\s+", "", parts[i])
        p = REAL.sub(lambda m: fmt_real(m, decimals), p)
        parts[i] = p
    return "".join(parts)


def minify(src, dst, decimals=9):
    text = open(src, "rb").read().decode("latin1")
    h_end = text.index("DATA;") + len("DATA;")
    header = text[:h_end]
    d_end = text.rindex("ENDSEC;")
    data = text[h_end:d_end]
    footer = text[d_end:]
    stmts, _ = split_statements(data)
    ents = {}
    order = []
    for st in stmts:
        m = ENT.match(st.strip())
        if not m:
            continue
        eid = int(m.group(1))
        body = compact_args(m.group(2), decimals)
        ents[eid] = body
        order.append(eid)

    def etype(b):
        m = re.match(r"\(?\s*([A-Z_0-9]+)", b)
        return m.group(1) if m else ""

    # hash-cons geometry until nothing changes
    alias = {}

    def res(i):
        while i in alias:
            i = alias[i]
        return i

    changed = True
    while changed:
        changed = False
        seen = {}
        for eid in order:
            if eid in alias:
                continue
            b = ents[eid]
            if etype(b) not in GEOM:
                continue
            nb = REF.sub(lambda m: "#%d" % res(int(m.group(1))), b)
            ents[eid] = nb
            if nb in seen:
                alias[eid] = seen[nb]
                changed = True
            else:
                seen[nb] = eid
    keep = [e for e in order if e not in alias]
    newid = {e: i + 1 for i, e in enumerate(keep)}
    out = [header, "\n"]
    for e in keep:
        b = REF.sub(lambda m: "#%d" % newid[res(int(m.group(1)))], ents[e])
        out.append("#%d=%s;\n" % (newid[e], b))
    out.append(footer)
    open(dst, "wb").write("".join(out).encode("latin1"))
    return len(order), len(keep)


if __name__ == "__main__":
    a, b = minify(sys.argv[1], sys.argv[2], int(sys.argv[3]) if len(sys.argv) > 3 else 9)
    print("entities %d -> %d" % (a, b))
