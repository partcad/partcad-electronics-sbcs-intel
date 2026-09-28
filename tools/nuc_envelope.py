"""Reduce a vendor PCBA STEP model to one rough, port-preserving solid.

Every component (leaf of the vendor assembly) becomes its own axis-aligned box,
carved - in the component's own frame - by the openings visible along its six
axis directions: port mouths (with their keys and notches), bores of threaded
inserts, notches of its outline. Openings narrower than 2*r_open or shallower
than t_min (see SMALL / LARGE) are walled off, which drops contacts, pins,
slits and fin gaps. The PCB is kept exact, with every hole of 2.5 mm or more.
Rows of heat sink fins become one block, neighbouring passives one box.
Components nobody can see from outside are dropped; the rest are united into a
single solid (meshunion.py), placed with the PCB bottom on Z=0 and the PCB
outline centred on X=Y=0, and written as a compact STEP file (minify_step.py).

  python nuc_envelope.py vendor.step board.step report.json [--work DIR]

The report holds what was done, and the PCB holes a fastener can reach
(gen_yaml.py turns those into 'implements:' ports).
"""
import argparse, collections, json, math, os, subprocess, sys, tempfile, time
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import occ_util
from occ_util import load_leaves, explore, nfaces, volume, aabb, finite, mesh_arrays, read_brep, iter_children

import cv2
from shapely.geometry import Polygon
from shapely.ops import unary_union

from OCP.TopAbs import TopAbs_SOLID, TopAbs_SHELL, TopAbs_FACE
from OCP.TopoDS import TopoDS_Compound, TopoDS
from OCP.TopLoc import TopLoc_Location
from OCP.BRep import BRep_Builder
from OCP.BRepTools import BRepTools
from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox, BRepPrimAPI_MakePrism
from OCP.BRepBuilderAPI import BRepBuilderAPI_MakePolygon, BRepBuilderAPI_MakeFace, BRepBuilderAPI_Transform
from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut
from OCP.BRepCheck import BRepCheck_Analyzer
from OCP.TopTools import TopTools_ListOfShape, TopTools_IndexedMapOfShape
from OCP.gp import gp_Pnt, gp_Vec, gp_Trsf

PCTL = 10        # depth of an opening: this percentile of its pixels
# per size class: steps shallower than t_min are ignored, openings narrower
# than 2*r_open or smaller than a_min are walled off, outlines are simplified
# to simp (raised up to 3x for an outline with too many vertices)
SMALL = dict(t_min=0.6, r_open=0.35, a_min=2.0, simp=0.05, levels=2, per_view=3, max_vert=20)
LARGE = dict(t_min=1.0, r_open=1.0, a_min=6.0, simp=0.05, levels=1, per_view=10, max_vert=24)
LARGE_SIZE = 40.0


# ------------------------------------------------------------------ carving
def depth_map(inter, lo, hi, axis, sign, h):
    u, v = [a for a in range(3) if a != axis]
    gu = np.arange(lo[u] + h / 2, hi[u], h)
    gv = np.arange(lo[v] + h / 2, hi[v], h)
    U, W = np.meshgrid(gu, gv, indexing="ij")
    org = np.empty((U.size, 3))
    org[:, u] = U.ravel(); org[:, v] = W.ravel()
    face = hi[axis] if sign > 0 else lo[axis]
    org[:, axis] = face + sign * 1.0
    d = np.zeros(3); d[axis] = -sign
    ext = hi[axis] - lo[axis]
    D = np.full(len(org), ext)
    locs, ri, _ = inter.intersects_location(org, np.tile(d, (len(org), 1)), multiple_hits=False)
    if len(ri):
        D[ri] = np.minimum(np.abs(locs[:, axis] - face), ext)
    return D.reshape(U.shape), (u, v), face, ext


def mask_to_polys(mask, lo_u, lo_v, h, P):
    # replicate the border so that an opening reaching the edge of the box
    # runs past it instead of leaving a pixel-wide sliver of wall behind
    PAD = 3
    m = cv2.copyMakeBorder(np.ascontiguousarray(mask.T.astype(np.uint8)), PAD, PAD, PAD, PAD, cv2.BORDER_REPLICATE)
    lo_u -= PAD * h
    lo_v -= PAD * h
    cs, hier = cv2.findContours(m, cv2.RETR_CCOMP, cv2.CHAIN_APPROX_SIMPLE)
    if hier is None:
        return []
    hier = hier[0]
    xy = lambda c: np.stack([lo_u + (c[:, 0, 0] + 0.5) * h, lo_v + (c[:, 0, 1] + 0.5) * h], 1)
    polys = []
    for i, c in enumerate(cs):
        if hier[i][3] != -1 or len(c) < 3:
            continue
        holes, j = [], hier[i][2]
        while j != -1:
            if len(cs[j]) >= 3:
                holes.append(xy(cs[j]))
            j = hier[j][0]
        polys.append(Polygon(xy(c), holes).buffer(0))
    if not polys:
        return []
    # contours run through boundary pixel centres, i.e. h/2 inside the pixel
    # boundary: that is the conservative side already
    g0 = unary_union(polys)
    parts0 = [g0] if isinstance(g0, Polygon) else list(getattr(g0, "geoms", []))
    out = []
    for p0 in parts0:
        if not isinstance(p0, Polygon) or p0.area < P["a_min"]:
            continue
        for f in (1, 1.5, 2, 3):
            p = p0.simplify(P["simp"] * f, preserve_topology=True)
            nv = len(p.exterior.coords) + sum(len(i.coords) for i in p.interiors)
            if nv <= P["max_vert"]:
                break
        if isinstance(p, Polygon) and p.is_valid and p.area >= P["a_min"]:
            out.append(p)
    return out


def prism(poly, axis, sign, uv, face, d0, d1):
    u, v = uv

    def wire(coords, depth):
        mp = BRepBuilderAPI_MakePolygon()
        for x, y in coords:
            p = [0.0, 0.0, 0.0]; p[u] = x; p[v] = y; p[axis] = face - sign * depth
            mp.Add(gp_Pnt(*p))
        mp.Close()
        return mp.Wire()

    ext = np.asarray(poly.exterior.coords)[:-1]
    if len(ext) < 3:
        return None
    mf = BRepBuilderAPI_MakeFace(wire(ext, d0), True)
    for hole in poly.interiors:
        hc = np.asarray(hole.coords)[:-1]
        if len(hc) >= 3:
            mf.Add(wire(hc, d0))
    if not mf.IsDone():
        return None
    vec = [0.0, 0.0, 0.0]; vec[axis] = -sign * (d1 - d0)
    pr = BRepPrimAPI_MakePrism(mf.Face(), gp_Vec(*vec))
    return pr.Shape() if pr.IsDone() else None


def openings(D, ext, h, P):
    """Nested openings of one view: list of (mask, d0, d1)."""
    T_MIN, MAX_LEVELS = P["t_min"], P["levels"]
    k = max(1, int(round(P["r_open"] / h)))
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * k + 1, 2 * k + 1))
    amin_px = P["a_min"] / (h * h)
    out = []

    def rec(mask, base, level):
        m = cv2.morphologyEx(mask.astype(np.uint8), cv2.MORPH_OPEN, kernel)
        n, lab = cv2.connectedComponents(m, connectivity=8)
        for c in range(1, n):
            comp = lab == c
            if comp.sum() < amin_px:
                continue
            d = float(np.percentile(D[comp], PCTL))
            if d >= ext - h:           # see-through along this axis
                d = ext
            nb = base
            if d - base >= T_MIN:
                out.append((comp, base, d))
                nb = d
            if level + 1 < MAX_LEVELS and nb < ext:
                sub = comp & (D >= nb + T_MIN)
                if sub.any() and (nb > base or sub.sum() < comp.sum()):
                    rec(sub, nb, level + 1)

    rec(D >= T_MIN, 0.0, 0)
    # the most significant openings of this view only
    out.sort(key=lambda o: -o[0].sum() * (o[2] - o[1]))
    return out[:P["per_view"]]


def pcb_envelope(shape, stats):
    """Exact outline of a PCB with its holes of >= 2.5 mm, as one prism.

    Returns (solid, holes) with holes = [(x, y, z, nx, ny, nz, diameter)] for
    every circular hole kept, or (None, None) when the shape is not a board.
    """
    from OCP.BRepTools import BRepTools as BT
    from OCP.BRepAdaptor import BRepAdaptor_Surface, BRepAdaptor_Curve
    from OCP.GeomAbs import GeomAbs_Plane, GeomAbs_Circle
    from OCP.GProp import GProp_GProps
    from OCP.BRepGProp import BRepGProp
    from OCP.TopAbs import TopAbs_WIRE, TopAbs_EDGE
    best = None
    for f in explore(shape, TopAbs_FACE):
        ad = BRepAdaptor_Surface(TopoDS.Face_s(f))
        if ad.GetType() != GeomAbs_Plane:
            continue
        p = GProp_GProps(); BRepGProp.SurfaceProperties_s(f, p)
        if best is None or p.Mass() > best[0]:
            best = (p.Mass(), TopoDS.Face_s(f), ad.Plane())
    if best is None:
        return None, None
    _, F, pln = best
    n = pln.Axis().Direction()
    nv = np.array([n.X(), n.Y(), n.Z()])
    o = pln.Location(); ov = np.array([o.X(), o.Y(), o.Z()])
    V, _ = mesh_arrays(shape, 0.1)
    proj = (V - ov) @ nv
    lo, hi = proj.min(), proj.max()
    if hi - lo > 3.0:
        return None, None
    # extrude away from the face, through the board
    if abs(proj.max()) < 1e-3:   # face is the top
        depth = lo
    else:
        depth = hi
    outer = BT.OuterWire_s(F)
    mf = BRepBuilderAPI_MakeFace(pln, outer, True)
    holes = []
    for w in explore(F, TopAbs_WIRE):
        if w.IsSame(outer):
            continue
        wf = BRepBuilderAPI_MakeFace(pln, TopoDS.Wire_s(w), True)
        if not wf.IsDone():
            continue
        p = GProp_GProps(); BRepGProp.SurfaceProperties_s(wf.Face(), p)
        d_eq = 2 * math.sqrt(abs(p.Mass()) / math.pi)
        if d_eq < 2.5:
            stats["pcb_holes_filled"] += 1
            continue
        mf.Add(TopoDS.Wire_s(w))
        # a round hole: area close to that of the circle with its perimeter
        lp = GProp_GProps(); BRepGProp.LinearProperties_s(w, lp)
        per = lp.Mass()
        roundness = abs(p.Mass()) / (math.pi * (per / (2 * math.pi)) ** 2) if per > 0 else 0
        # the screw axis: centre of the longest circular arc of the outline
        # (corner holes are often slots or keyholes; their centroid is not it)
        arc = None
        for e in explore(w, TopAbs_EDGE):
            ca = BRepAdaptor_Curve(TopoDS.Edge_s(e))
            if ca.GetType() == GeomAbs_Circle:
                el = GProp_GProps(); BRepGProp.LinearProperties_s(e, el)
                if arc is None or el.Mass() > arc[0]:
                    arc = (el.Mass(), ca.Circle())
        if arc is not None and 1.2 <= arc[1].Radius() <= 2.5:
            c = arc[1].Location()
            holes.append((c.X(), c.Y(), c.Z(), nv[0], nv[1], nv[2], 2 * arc[1].Radius()))
        elif roundness > 0.9:
            c = p.CentreOfMass()
            holes.append((c.X(), c.Y(), c.Z(), nv[0], nv[1], nv[2], d_eq))
        stats["pcb_holes_kept"] += 1
    if not mf.IsDone():
        return None, None
    from OCP.ShapeFix import ShapeFix_Face
    ff = ShapeFix_Face(mf.Face()); ff.FixOrientation(); ff.Perform()
    pr = BRepPrimAPI_MakePrism(ff.Face(), gp_Vec(*(nv * depth)))
    if not pr.IsDone():
        return None, None
    sol = pr.Shape()
    if volume(sol) < 0:
        sol = sol.Reversed()
    if not BRepCheck_Analyzer(sol).IsValid():
        return None, None
    stats["pcb_exact"] += 1
    return sol, holes


def envelope(shape, stats):
    """Rough port-preserving solid of one component (in its own frame)."""
    import trimesh
    bb = aabb(shape)
    if not finite(bb):
        return None
    # the tessellation is the tight extent: the BRep box of a B-spline surface
    # is that of its control points and can be much larger
    ext0 = max(bb[3] - bb[0], bb[4] - bb[1], bb[5] - bb[2])
    V, F = mesh_arrays(shape, max(0.01, ext0 / 2000))
    if len(F) == 0:
        return None
    lo, hi = V.min(0), V.max(0)
    ext = hi - lo
    if ext.max() < 1e-3:
        return None
    ext_min = np.maximum(ext, 0.02)
    hi = lo + ext_min
    box = BRepPrimAPI_MakeBox(gp_Pnt(*lo), gp_Pnt(*hi)).Shape()
    # small or thin in two directions: nothing worth carving
    nf = nfaces(shape)
    P = LARGE if ext.max() > LARGE_SIZE else SMALL
    T_MIN = P["t_min"]
    if sorted(ext)[1] < 2 * T_MIN or ext.max() < 1.5:
        stats["plain_box"] += 1
        return box
    inter = trimesh.ray.ray_pyembree.RayMeshIntersector(trimesh.Trimesh(V, F, process=False))
    h = float(np.clip(ext.max() / 500, 0.02, 0.1))
    tools = []
    for axis in range(3):
        if ext[axis] < T_MIN:
            continue
        for sign in (1, -1):
            D, uv, face, e = depth_map(inter, lo, hi, axis, sign, h)
            for mask, d0, d1 in openings(D, e, h, P):
                for p in mask_to_polys(mask, lo[uv[0]], lo[uv[1]], h, P):
                    s = prism(p, axis, sign, uv, face,
                              -1.0 if d0 <= 0 else d0 - 1e-3,
                              e + 1.0 if d1 >= e else d1)
                    if s is not None:
                        tools.append(s)
    if not tools:
        stats["plain_box"] += 1
        return box
    cut = BRepAlgoAPI_Cut()
    a = TopTools_ListOfShape(); a.Append(box)
    t = TopTools_ListOfShape()
    for s in tools:
        t.Append(s)
    cut.SetArguments(a); cut.SetTools(t); cut.SetFuzzyValue(1e-5)
    cut.Build()
    if not cut.IsDone():
        stats["cut_failed"] += 1
        return box
    r = cut.Shape()
    sol = list(explore(r, TopAbs_SOLID))
    if not sol or volume(r) <= 1e-6:
        stats["cut_empty"] += 1
        return box
    if not BRepCheck_Analyzer(r).IsValid():
        from OCP.ShapeFix import ShapeFix_Shape
        fx = ShapeFix_Shape(r); fx.Perform(); r = fx.Shape()
        if not BRepCheck_Analyzer(r).IsValid() or volume(r) <= 1e-6:
            stats["invalid_to_box"] += 1
            return box
        stats["fixed"] += 1
    # everything stays planar (curved exact solids make the final fuse
    # crawl); a carving far more detailed than the part itself is not worth it
    if nfaces(r) > max(60, 2 * nf):
        stats["plain_box_costly"] += 1
        return box
    stats["carved"] += 1
    stats["openings"] += len(tools)
    return r


# ------------------------------------------------------------------ workers
def worker_main(workdir, ids):
    for i in ids:
        res = os.path.join(workdir, "e%05d.brep" % i)
        if os.path.exists(res):
            continue
        open(os.path.join(workdir, "cur%05d" % ids[0]), "w").write(str(i))
        s = read_brep(os.path.join(workdir, "p%05d.brep" % i))
        st = collections.Counter()
        env = None
        if os.path.exists(os.path.join(workdir, "pcb%05d" % i)):
            env, holes = pcb_envelope(s, st)
            if env is not None:
                json.dump(holes, open(os.path.join(workdir, "h%05d.json" % i), "w"))
        if env is None:
            env = envelope(s, st)
        comp = TopoDS_Compound(); B = BRep_Builder(); B.MakeCompound(comp)
        if env is not None:
            B.Add(comp, env)
        json.dump(dict(st), open(os.path.join(workdir, "s%05d.json" % i), "w"))
        BRepTools.Write_s(comp, res + ".tmp")
        os.replace(res + ".tmp", res)


def run_pool(workdir, n, log, nproc=int(os.environ.get("NPROC", "28")), timeout=180):
    failed = set()
    while True:
        todo = [i for i in range(n) if i not in failed and not os.path.exists(os.path.join(workdir, "e%05d.brep" % i))]
        if not todo:
            return failed
        chunks = [todo[j::nproc] for j in range(nproc) if todo[j::nproc]]
        procs = []
        for ch in chunks:
            cur = os.path.join(workdir, "cur%05d" % ch[0])
            if os.path.exists(cur):
                os.remove(cur)
            procs.append((ch, cur, subprocess.Popen(
                [sys.executable, os.path.abspath(__file__), "--worker", workdir, ",".join(map(str, ch))],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)))
        live = list(procs)
        while live:
            time.sleep(0.3)
            for it in list(live):
                ch, cur, p = it
                if p.poll() is not None:
                    live.remove(it)
                elif os.path.exists(cur) and time.time() - os.path.getmtime(cur) > timeout:
                    p.kill(); p.wait(); live.remove(it)
        for ch, cur, p in procs:
            if p.returncode != 0 and os.path.exists(cur):
                bad = int(open(cur).read())
                failed.add(bad)
                log("  component %d failed (exit %s)" % (bad, p.returncode))


def merge_tiny_boxes(world, gap=0.5, max_ext=6.0, min_fill=0.33):
    """Merge neighbouring tiny box envelopes (passives) into covering boxes.

    Only axis-aligned 6-face envelopes smaller than max_ext are considered;
    two groups merge when their boxes are within `gap` and the covering box
    stays at least `min_fill` full. Always adds material, never removes it.
    """
    idx, boxes = [], []
    for i, w in enumerate(world):
        if nfaces(w) != 6:
            continue
        b = np.array(aabb(w))
        e = b[3:] - b[:3]
        if e.max() > max_ext or abs(e.prod() - volume(w)) > 0.02 * e.prod():
            continue   # not small, or a box that is not axis-aligned
        idx.append(i); boxes.append(b)
    if len(boxes) < 2:
        return world, 0
    groups = [dict(ids=[i], box=b.copy(), vol=float(np.prod(b[3:] - b[:3]))) for i, b in zip(idx, boxes)]
    merged = True
    while merged:
        merged = False
        groups.sort(key=lambda g: g["box"][0])
        out = []
        while groups:
            g = groups.pop(0)
            k = 0
            while k < len(groups):
                h = groups[k]
                if h["box"][0] > g["box"][3] + gap:
                    break
                if all(h["box"][a] <= g["box"][a + 3] + gap and g["box"][a] <= h["box"][a + 3] + gap for a in range(3)):
                    nb = np.concatenate([np.minimum(g["box"][:3], h["box"][:3]), np.maximum(g["box"][3:], h["box"][3:])])
                    if (g["vol"] + h["vol"]) / np.prod(nb[3:] - nb[:3]) >= min_fill:
                        g = dict(ids=g["ids"] + h["ids"], box=nb, vol=g["vol"] + h["vol"])
                        groups.pop(k)
                        merged = True
                        continue
                k += 1
            out.append(g)
        groups = out
    drop, add = set(), []
    for g in groups:
        if len(g["ids"]) > 1:
            drop.update(g["ids"])
            add.append(BRepPrimAPI_MakeBox(gp_Pnt(*g["box"][:3]), gp_Pnt(*g["box"][3:])).Shape())
    return [w for i, w in enumerate(world) if i not in drop] + add, len(drop)


def wall_fin_stacks(world):
    plates = []
    for wi, w in enumerate(world):
        if nfaces(w) > 200:
            continue
        o = occ_util.obb_of(w)
        d = sorted(zip(occ_util.obb_dims(o), (o.XDirection(), o.YDirection(), o.ZDirection())), key=lambda t: t[0])
        if d[0][0] < 1.5 and d[1][0] > 4 * d[0][0]:
            nv = np.array([d[0][1].X(), d[0][1].Y(), d[0][1].Z()])
            if nv[np.argmax(abs(nv))] < 0:
                nv = -nv
            c = o.Center()
            plates.append((wi, tuple(np.round(nv, 2)), round(d[1][0]), round(d[2][0]), np.array([c.X(), c.Y(), c.Z()])))
    groups = collections.defaultdict(list)
    for p_ in plates:
        groups[p_[1:4]].append(p_)
    drop, blocks = set(), []
    for key, g in groups.items():
        if len(g) < 6:
            continue
        nv = np.array(key[0])
        rows = collections.defaultdict(list)
        for p_ in g:
            inplane = p_[4] - nv * (p_[4] @ nv)
            rows[tuple(np.round(inplane / 2.0))].append(p_)
        for r in rows.values():
            if len(r) < 6:
                continue
            r.sort(key=lambda p_: p_[4] @ nv)
            if np.diff([p_[4] @ nv for p_ in r]).max() > 3.0:
                continue
            comp = TopoDS_Compound(); B = BRep_Builder(); B.MakeCompound(comp)
            for p_ in r:
                B.Add(comp, world[p_[0]])
                drop.add(p_[0])
            blocks.append(occ_util.box_from_obb(occ_util.obb_of(comp)))
    return [w for i, w in enumerate(world) if i not in drop] + blocks, len(drop)


# ------------------------------------------------------------------ main
def _log(*a):
    import resource
    print(*a, "[peak %.1f GB]" % (resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1048576), flush=True)


def run(src, dst, report, work, log=_log):
    T0 = time.time()
    stats = collections.Counter()
    leaves, _ = load_leaves(src)
    faces_in = sum(nfaces(s) for _, s in leaves)
    # components: one per leaf prototype; a flat file (everything in one leaf)
    # is split into its solids and closed shells instead
    comps = []   # (shape in own frame, [locations])
    seen = TopTools_IndexedMapOfShape()
    index = {}
    for names, s in leaves:
        base = s.Located(TopLoc_Location())
        bb = aabb(base)
        nsol = sum(1 for _ in explore(base, TopAbs_SOLID)) + sum(1 for _ in explore(base, TopAbs_SHELL, TopAbs_SOLID))
        if nsol > 8 and (not finite(bb) or max(bb[3] - bb[0], bb[4] - bb[1], bb[5] - bb[2]) > 60):
            for sub in list(explore(base, TopAbs_SOLID)) + list(explore(base, TopAbs_SHELL, TopAbs_SOLID)):
                comps.append([sub, [s.Location()]])
            stats["split_leaves"] += 1
            continue
        k = seen.Add(base)
        if k not in index:
            index[k] = len(comps)
            comps.append([base, []])
        comps[index[k]][1].append(s.Location())
    log("  %d leaves -> %d components, %d faces in" % (len(leaves), len(comps), faces_in))

    wd = tempfile.mkdtemp(prefix="nucenv-", dir=work)
    for i, (s, _) in enumerate(comps):
        BRepTools.Write_s(s, os.path.join(wd, "p%05d.brep" % i))
    # the PCB: the component with the largest thin footprint
    best = None
    for i, (sh, _) in enumerate(comps):
        bb = aabb(sh)
        if not finite(bb):
            continue
        e = sorted([bb[3] - bb[0], bb[4] - bb[1], bb[5] - bb[2]])
        if e[0] < 3.0 and e[1] > 40 and (best is None or e[1] * e[2] > best[0]):
            best = (e[1] * e[2], i)
    if best is not None:
        open(os.path.join(wd, "pcb%05d" % best[1]), "w").close()
    t = time.time()
    failed = run_pool(wd, len(comps), log)
    stats["component_failures"] = len(failed)
    log("  envelopes in %.1fs" % (time.time() - t))
    world = []
    world_holes = []
    for i, (s, locs) in enumerate(comps):
        if i in failed:
            env = BRepPrimAPI_MakeBox(gp_Pnt(*aabb(s)[:3]), gp_Pnt(*aabb(s)[3:])).Shape() if finite(aabb(s)) else None
        else:
            stats.update(json.load(open(os.path.join(wd, "s%05d.json" % i))))
            ch = list(iter_children(read_brep(os.path.join(wd, "e%05d.brep" % i))))
            env = ch[0] if ch else None
        if env is None:
            stats["empty_components"] += 1
            continue
        hf = os.path.join(wd, "h%05d.json" % i)
        for loc in locs:
            w = env.Moved(loc)
            if finite(aabb(w)):
                world.append(w)
            if os.path.exists(hf):
                tr = loc.Transformation()
                for x, y, z, nx, ny, nz, dia in json.load(open(hf)):
                    p = gp_Pnt(x, y, z).Transformed(tr)
                    world_holes.append((p, dia))

    # dense passives: neighbouring tiny boxes -> covering boxes
    world, nm = merge_tiny_boxes(world)
    stats["tiny_merged"] = nm

    # fin stacks: rows of >= 6 parallel thin plates -> one block
    world, nf = wall_fin_stacks(world)
    stats["fins_walled"] = nf

    # drop what nobody can see from outside
    meshes = [mesh_arrays(w, 0.05) for w in world]
    hits = occ_util.visibility(meshes, ndirs=120, step=0.3, log=log)
    kept = [w for w, n in zip(world, hits) if n >= 3]
    stats["hidden_dropped"] = len(world) - len(kept)

    # frame from the PCB: the largest thin envelope
    best = None
    for w in kept:
        o = occ_util.obb_of(w)
        d = sorted(zip(occ_util.obb_dims(o), (o.XDirection(), o.YDirection(), o.ZDirection())), key=lambda q: q[0])
        if d[0][0] < 3.0 and (best is None or d[1][0] * d[2][0] > best[0]):
            best = (d[1][0] * d[2][0], w, d)
    _, pcb, d = best
    n = d[0][1]; z = np.array([n.X(), n.Y(), n.Z()])
    # keep the vendor's own sense of up: the PCB normal goes to +Z with the
    # sign of the vendor axis it is closest to
    if z[np.argmax(abs(z))] < 0:
        z = -z
    # X: the first vendor axis lying in the board plane (boards are often
    # square, so "the long side" would pick one arbitrarily)
    xl = np.eye(3)[[i for i in range(3) if i != int(np.argmax(abs(z)))][0]]
    x = xl - z * (xl @ z); x /= np.linalg.norm(x)
    y = np.cross(z, x)
    tr = gp_Trsf(); tr.SetValues(*x, 0, *y, 0, *z, 0)
    rb = aabb(BRepBuilderAPI_Transform(pcb, tr, True).Shape())
    t2 = gp_Trsf(); t2.SetTranslation(gp_Vec(-(rb[0] + rb[3]) / 2, -(rb[1] + rb[4]) / 2, -rb[2]))
    frame = t2.Multiplied(tr)

    comp = TopoDS_Compound(); B = BRep_Builder(); B.MakeCompound(comp)
    from OCP.ShapeFix import ShapeFix_Shape
    for w in kept:
        w = BRepBuilderAPI_Transform(w, frame, True).Shape()
        for so in explore(w, TopAbs_SOLID):
            if volume(so) < 0:
                so = so.Reversed()
            if not BRepCheck_Analyzer(so).IsValid():
                fx = ShapeFix_Shape(so); fx.Perform()
                good = [x for x in explore(fx.Shape(), TopAbs_SOLID) if volume(x) > 0 and BRepCheck_Analyzer(x).IsValid()]
                if good:
                    stats["inputs_fixed"] += 1
                    for x in good:
                        B.Add(comp, x)
                    continue
                bb = aabb(so)
                stats["inputs_boxed"] += 1
                so = BRepPrimAPI_MakeBox(gp_Pnt(*bb[:3]), gp_Pnt(*bb[3:])).Shape()
            B.Add(comp, so)
    BRepTools.Write_s(comp, os.path.join(wd, "all.brep"))
    # union: robust mesh booleans, rebuilt as one planar B-rep solid (OCCT's
    # general fuse drops inputs on assemblies of touching boxes)
    import meshunion
    t = time.time()
    inputs = [x for x in explore(comp, TopAbs_SOLID) if volume(x) > 1e-3]
    body, uinfo = meshunion.union_to_solid(inputs, mesh_arrays, aabb, log)
    for k, v in uinfo.items():
        stats["union_" + k] = v
    log("  union in %.1fs: %s" % (time.time() - t, dict(uinfo)))
    if body is None:
        raise RuntimeError("union failed: %s" % uinfo.get("brep"))
    solids = list(explore(body, TopAbs_SOLID))
    vols = [volume(x) for x in solids]
    from OCP.STEPControl import STEPControl_Writer, STEPControl_AsIs
    from OCP.Interface import Interface_Static
    Interface_Static.SetCVal_s("write.step.schema", "AP214IS")
    Interface_Static.SetCVal_s("write.step.unit", "MM")
    Interface_Static.SetIVal_s("write.surfacecurve.mode", 0)
    w = STEPControl_Writer()
    w.Transfer(body, STEPControl_AsIs)
    raw = dst + ".raw.step"
    w.Write(raw)
    import minify_step
    from OCP.STEPControl import STEPControl_Reader

    def read_back(path):
        rd = STEPControl_Reader()
        rd.ReadFile(path)
        rd.TransferRoots()
        back = rd.OneShape()
        bs = list(explore(back, TopAbs_SOLID))
        return dict(solids=len(bs), free_shells=sum(1 for _ in explore(back, TopAbs_SHELL, TopAbs_SOLID)),
                    volume=round(sum(volume(x) for x in bs), 1), valid=BRepCheck_Analyzer(back).IsValid())
    good = lambda r: r["solids"] == 1 and r["free_shells"] == 0 and r["volume"] > 0 and r["valid"]
    minify_step.minify(raw, dst)
    # what a reader of the file gets, which is what PartCAD will get
    stats["readback"] = read_back(dst)
    if not good(stats["readback"]):
        log("  compacted file reads back as %s; keeping the plain one" % stats["readback"])
        os.replace(raw, dst)
        stats["compacted"] = False
        stats["readback"] = read_back(dst)
    else:
        stats["compacted"] = True
        os.remove(raw)
    log("  read back: %s" % stats["readback"])
    bb = aabb(body)
    stats = dict(stats)
    # mounting holes: PCB holes in the final frame, and whether a fastener can
    # get to them from above / below the board
    import trimesh
    pcb_t = rb[5] - rb[2]
    V, F = mesh_arrays(body, 0.05)
    inter = trimesh.ray.ray_pyembree.RayMeshIntersector(trimesh.Trimesh(V, F, process=False))
    holes = []
    for p, dia in world_holes:
        q = p.Transformed(frame)
        if any(abs(h["x"] - q.X()) < 0.05 and abs(h["y"] - q.Y()) < 0.05 for h in holes):
            continue
        h = dict(x=round(q.X(), 3), y=round(q.Y(), 3), d=round(dia, 3))
        for key, z0, dz in (("top", pcb_t + 0.01, 1.0), ("bottom", -0.01, -1.0)):
            locs, _, _ = inter.intersects_location(np.array([[q.X(), q.Y(), z0]]), np.array([[0, 0, dz]]), multiple_hits=False)
            h["clear_" + key] = None if len(locs) == 0 else round(abs(locs[0][2] - z0), 2)
        holes.append(h)
    stats["pcb_thickness"] = round(pcb_t, 3)
    stats["holes"] = sorted(holes, key=lambda h: (h["y"], h["x"]))
    stats.update(dict(src_bytes=os.path.getsize(src), dst_bytes=os.path.getsize(dst), leaves=len(leaves),
                      components=len(comps), faces_in=faces_in, faces_out=nfaces(body), solids_out=len(solids),
                      volume=round(vols[0], 1) if vols else 0, valid=BRepCheck_Analyzer(body).IsValid(),
                      bbox=[round(v, 2) for v in bb], seconds=round(time.time() - T0, 1)))
    json.dump(stats, open(report, "w"), indent=1)
    log(json.dumps(stats))


if __name__ == "__main__":
    if sys.argv[1] == "--worker":
        worker_main(sys.argv[2], [int(x) for x in sys.argv[3].split(",")])
        sys.exit(0)
    ap = argparse.ArgumentParser()
    ap.add_argument("src"); ap.add_argument("dst"); ap.add_argument("report")
    ap.add_argument("--work", default="/tmp")
    a = ap.parse_args()
    run(a.src, a.dst, a.report, a.work)
