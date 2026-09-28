"""Robust union of planar solids via manifold3d, rebuilt as a planar B-rep.

OCCT's general fuse loses inputs or returns invalid shapes on assemblies of
hundreds of touching boxes. manifold3d's mesh booleans are guaranteed to
return a manifold, and since every envelope is planar (and the PCB's round
holes tessellate to well under 0.01 mm), nothing is lost by going through a
mesh: coplanar triangles are merged back into polygon faces, collinear edge
runs into single edges, and the faces share their edges exactly.
"""
import collections
import numpy as np
import manifold3d as mf
from scipy.spatial import cKDTree

from OCP.gp import gp_Pnt, gp_Pln, gp_Dir
from OCP.BRepBuilderAPI import (BRepBuilderAPI_MakeVertex, BRepBuilderAPI_MakeEdge,
                                BRepBuilderAPI_MakeWire, BRepBuilderAPI_MakeFace)
from OCP.BRep import BRep_Builder
from OCP.TopoDS import TopoDS_Shell, TopoDS_Solid, TopoDS
from OCP.ShapeFix import ShapeFix_Solid, ShapeFix_Shape
from shapely.geometry import Polygon, Point
from OCP.BRepCheck import BRepCheck_Analyzer


def solid_to_manifold(shape, mesh_fn, defl=0.005):
    V, F = mesh_fn(shape, defl)
    if len(F) == 0:
        return None
    # weld the per-face triangulations along shared edges
    uniq, inv = np.unique(np.round(V, 7), axis=0, return_inverse=True)
    F2 = inv.reshape(-1)[F]
    F2 = F2[(F2[:, 0] != F2[:, 1]) & (F2[:, 1] != F2[:, 2]) & (F2[:, 0] != F2[:, 2])]
    m = mf.Manifold(mf.Mesh64(vert_properties=np.asarray(uniq, dtype=np.float64),
                              tri_verts=np.asarray(F2, dtype=np.uint64)))
    if m.status() != mf.Error.NoError or m.is_empty() or m.volume() <= 0:
        return None
    if has_twins(m):
        # the shape touches itself along an edge or at a corner: fatten it a
        # little so that the pinch becomes a real (manifold) contact
        try:
            m2 = m.minkowski_sum(mf.Manifold.cube((0.01, 0.01, 0.01), True))
            if not has_twins(m2) and m2.volume() > 0:
                return m2
        except Exception:
            pass
        return None
    return m


def has_twins(m):
    V = np.asarray(m.to_mesh64().vert_properties)[:, :3]
    return len(np.unique(np.round(V, 6), axis=0)) < len(V)


def split_parts(m):
    """The positive (outward) connected parts of `m`, as manifolds, and how
    many parts there were in all. Inside-out parts are enclosed voids.

    Manifold.decompose() does the same, but its memory grows with the number
    of parts times the size of the whole mesh: a board with hundreds of loose
    components took ~95 GB. This labels triangles by connectivity instead.
    """
    mesh = m.to_mesh64()
    V = np.asarray(mesh.vert_properties)[:, :3]
    F = np.asarray(mesh.tri_verts, dtype=np.int64)
    if len(F) == 0:
        return [], 0
    from scipy.sparse import coo_matrix
    from scipy.sparse.csgraph import connected_components
    nv = len(V)
    rows = np.concatenate([F[:, 0], F[:, 1], F[:, 2]])
    cols = np.concatenate([F[:, 1], F[:, 2], F[:, 0]])
    g = coo_matrix((np.ones(len(rows), dtype=np.int8), (rows, cols)), shape=(nv, nv))
    n, lab = connected_components(g, directed=False)
    tlab = lab[F[:, 0]]
    order = np.argsort(tlab, kind="stable")
    bounds = np.searchsorted(tlab[order], np.arange(n + 1))
    parts = []
    for c in range(n):
        tris = F[order[bounds[c]:bounds[c + 1]]]
        if len(tris) == 0:
            continue
        used, inv = np.unique(tris, return_inverse=True)
        Vc = V[used]
        Fc = inv.reshape(-1, 3)
        a, b, cc = Vc[Fc[:, 0]], Vc[Fc[:, 1]], Vc[Fc[:, 2]]
        vol = np.einsum("ij,ij->i", a, np.cross(b, cc)).sum() / 6.0
        if vol <= 0:
            continue
        pm = mf.Manifold(mf.Mesh64(vert_properties=Vc, tri_verts=Fc.astype(np.uint64)))
        if pm.status() == mf.Error.NoError and not pm.is_empty():
            parts.append(pm)
    return parts, int(len(set(tlab.tolist())))


def surface_samples(V, F, spacing):
    """Vertices plus points spread over every triangle, about `spacing` apart."""
    a, b, c = V[F[:, 0]], V[F[:, 1]], V[F[:, 2]]
    area = 0.5 * np.linalg.norm(np.cross(b - a, c - a), axis=1)
    n = np.minimum(np.ceil(area / (spacing * spacing)).astype(np.int64), 20000)
    idx = np.repeat(np.arange(len(F)), n)
    rng = np.random.default_rng(0)
    u, v = rng.random(len(idx)), rng.random(len(idx))
    flip = u + v > 1
    u[flip], v[flip] = 1 - u[flip], 1 - v[flip]
    P = a[idx] + u[:, None] * (b[idx] - a[idx]) + v[:, None] * (c[idx] - a[idx])
    return np.vstack([V, P])


def box_manifold(bb):
    lo = np.array(bb[:3]); hi = np.array(bb[3:])
    return mf.Manifold.cube(tuple(hi - lo)).translate(tuple(lo))


def cylinder_between(p1, p2, r=0.6, overshoot=0.3):
    v = np.asarray(p2) - np.asarray(p1)
    L = np.linalg.norm(v)
    u = v / L if L > 1e-9 else np.array([0, 0, 1.0])
    h = L + 2 * overshoot
    c = mf.Manifold.cylinder(h, r, r, 4)   # a square rod: 4 faces, not 16
    # rotate +Z onto u
    z = np.array([0, 0, 1.0])
    ax = np.cross(z, u)
    s_, c_ = np.linalg.norm(ax), z @ u
    if s_ < 1e-12:
        R = np.eye(3) if c_ > 0 else np.diag([1.0, -1.0, -1.0])
    else:
        k = ax / s_
        K = np.array([[0, -k[2], k[1]], [k[2], 0, -k[0]], [-k[1], k[0], 0]])
        R = np.eye(3) + s_ * K + (1 - c_) * K @ K
    start = np.asarray(p1) - u * overshoot
    M = np.hstack([R, start[:, None]])
    return c.transform(M.tolist())


def grown(m, eps):
    if eps <= 0:
        return m
    """Scale a manifold about its box centre so its box grows by eps per side:
    parts that merely touch along an edge or at a corner then overlap, which
    keeps the union free of pinched (non-manifold) contacts."""
    lo, hi = np.array(m.bounding_box()[:3]), np.array(m.bounding_box()[3:])
    c = (lo + hi) / 2
    e = np.maximum(hi - lo, 1e-3)
    k = (e + 2 * eps) / e
    return m.translate(tuple(-c)).scale(tuple(k)).translate(tuple(c))


def outer_only(solid):
    """The solid bounded by its outer shell alone: enclosed voids are walled."""
    from OCP.BRepClass3d import BRepClass3d
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopAbs import TopAbs_SHELL, TopAbs_SOLID
    e = TopExp_Explorer(solid, TopAbs_SOLID)
    so = TopoDS.Solid_s(e.Current())
    n = 0
    e2 = TopExp_Explorer(so, TopAbs_SHELL)
    while e2.More():
        n += 1
        e2.Next()
    if n <= 1:
        return solid
    sh = BRepClass3d.OuterShell_s(so)
    B = BRep_Builder()
    s2 = TopoDS_Solid()
    B.MakeSolid(s2)
    B.Add(s2, sh)
    fx = ShapeFix_Solid(s2)
    fx.Perform()
    return fx.Solid()


def round_trip_problem(solid):
    """Why `solid` would not come back from a STEP file as one valid solid."""
    import os, tempfile
    from OCP.STEPControl import STEPControl_Writer, STEPControl_Reader, STEPControl_AsIs
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopAbs import TopAbs_SOLID
    from OCP.GProp import GProp_GProps
    from OCP.BRepGProp import BRepGProp
    fd, path = tempfile.mkstemp(suffix=".step")
    os.close(fd)
    try:
        w = STEPControl_Writer()
        w.Transfer(solid, STEPControl_AsIs)
        w.Write(path)
        r = STEPControl_Reader()
        r.ReadFile(path)
        r.TransferRoots()
        back = r.OneShape()
    finally:
        os.remove(path)
    sols = []
    e = TopExp_Explorer(back, TopAbs_SOLID)
    while e.More():
        sols.append(e.Current())
        e.Next()
    if len(sols) != 1:
        return "reads back as %d solids" % len(sols)
    gp = GProp_GProps(); BRepGProp.VolumeProperties_s(sols[0], gp)
    if gp.Mass() <= 0:
        return "reads back inside out"
    if not BRepCheck_Analyzer(back).IsValid():
        return "reads back invalid"
    return None


def union_to_solid(solids, mesh_fn, aabb_fn, log=print):
    """Union of OCC solids as one valid planar B-rep solid. Returns (solid, info)."""
    base = []
    info0 = collections.Counter()
    for so in solids:
        m = solid_to_manifold(so, mesh_fn)
        if m is None:
            info0["meshed_as_box"] += 1
            m = box_manifold(aabb_fn(so))
        base.append(m)
    last = None
    for eps in (0.0, 0.005, 0.02, 0.05):
        try:
            solid, info = _union(base, eps)
        except EdgeError as e:
            last = str(e)
            log("  union with grow %.3f: %s" % (eps, e))
            continue
        if solid is not None:
            solid = outer_only(solid)
            why = round_trip_problem(solid)
            if why is None:
                info.update(info0)
                info["grow"] = eps
                return solid, info
            info["brep"] = why
        last = info.get("brep")
        log("  union with grow %.3f rejected (%s)" % (eps, last))
    return None, {"brep": last}


def _mem(tag):
    import resource, sys, time
    print("    [union] %-28s peak %.1f GB  %s" % (tag, resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1048576,
                                                time.strftime("%H:%M:%S")), file=sys.stderr, flush=True)


def _union(base, eps):
    info = collections.Counter()
    ms = [grown(m, eps) for m in base]
    u = mf.Manifold.batch_boolean(ms, mf.OpType.Add)
    _mem("batch union")
    info["volume_union"] = round(u.volume(), 3)
    # enclosed voids come back as inside-out parts: dropping them fills the
    # void, which is exactly the inner detail that has to go
    def positive(m):
        ps, nall = split_parts(m)
        info["voids_filled"] = max(info.get("voids_filled", 0), nall - len(ps))
        return ps
    ps = positive(u)
    u = mf.Manifold.batch_boolean(ps, mf.OpType.Add) if len(ps) > 1 else ps[0]
    _mem("voids")
    # bridge separate parts to the largest, nearest first
    for it in range(20):
        parts = sorted(positive(u), key=lambda p: -p.volume())
        if len(parts) <= 1:
            break
        main = parts[0]
        mm = main.to_mesh64()
        # nearest points on the main body's surface, from a dense sample of it
        # (a large flat face has few vertices, so its vertices alone would pull
        # bridges far off; an exact closest-point query per piece costs memory
        # in proportion to the whole body, and there can be hundreds of pieces)
        tree = cKDTree(surface_samples(np.asarray(mm.vert_properties)[:, :3],
                                       np.asarray(mm.tri_verts, dtype=np.int64), 0.1))
        cyls = []
        for p in parts[1:]:
            pv = np.asarray(p.to_mesh64().vert_properties)[:, :3]
            dist, k = tree.query(pv)
            j = int(np.argmin(dist))
            ext = np.ptp(pv, axis=0)
            r = float(np.clip(0.3 * np.sort(ext)[1], 0.05, 0.3))
            cyls.append(cylinder_between(tree.data[k[j]], pv[j], r=r, overshoot=min(0.3, 2 * r)))
            info["bridges"] += 1
            info["bridged_volume"] += p.volume()
            info["bridge_max_len"] = max(info.get("bridge_max_len", 0), float(dist[j]))
        _mem("bridge rods %d" % len(cyls))
        u = mf.Manifold.batch_boolean([u] + cyls, mf.OpType.Add)
        _mem("bridged")
    parts = positive(u)
    info["parts"] = len(parts)
    u = mf.Manifold.batch_boolean(parts, mf.OpType.Add) if len(parts) > 1 else parts[0]
    if len(parts) > 1:
        # keep the main body; report what could not be joined
        parts = sorted(parts, key=lambda p: -p.volume())
        info["dropped_parts_volume"] = round(sum(p.volume() for p in parts[1:]), 3)
        u = parts[0]
    # pinches (the body touching itself along an edge or at a corner) show
    # up as distinct vertices at the same place: fill each with a tiny cube
    for _ in range(5):
        V = np.asarray(u.to_mesh64().vert_properties)[:, :3]
        key, cnt = np.unique(np.round(V, 6), axis=0, return_counts=True)
        twins = key[cnt > 1]
        if len(twins) == 0:
            break
        info["pinches_filled"] += len(twins)
        cubes = [mf.Manifold.cube((0.04, 0.04, 0.04), True).translate(tuple(p)) for p in twins]
        u = mf.Manifold.batch_boolean([u] + cubes, mf.OpType.Add)
    _mem("pinches")
    mesh = u.to_mesh64()
    V = np.asarray(mesh.vert_properties)[:, :3]
    F = np.asarray(mesh.tri_verts, dtype=np.int64)
    # collapse mesh edges shorter than OCCT can represent (not a positional
    # weld: coincident vertices of two sheets touching along an edge must
    # stay apart, or the edge becomes non-manifold)
    par = np.arange(len(V))

    def root(x):
        while par[x] != x:
            par[x] = par[par[x]]
            x = par[x]
        return x
    short = 0
    while True:
        E = np.vstack([F[:, [0, 1]], F[:, [1, 2]], F[:, [2, 0]]])
        L = np.linalg.norm(V[E[:, 0]] - V[E[:, 1]], axis=1)
        tiny = E[L < 1e-4]
        if len(tiny) == 0:
            break
        for i, j in tiny:
            ri, rj = root(i), root(j)
            if ri != rj:
                par[max(ri, rj)] = min(ri, rj)
                short += 1
        F = np.array([[root(x) for x in t] for t in F], dtype=np.int64)
        F = F[(F[:, 0] != F[:, 1]) & (F[:, 1] != F[:, 2]) & (F[:, 0] != F[:, 2])]
    info["short_edges_collapsed"] = short
    _mem("short edges")
    solid, msg = mesh_to_brep(V, F)
    _mem("brep")
    info["brep"] = msg
    info["volume_mesh"] = round(u.volume(), 3)
    return solid, info


class EdgeError(RuntimeError):
    pass


def _is_cap(V, t, rel):
    i, j, k = t
    A, Bv, C = V[i], V[j], V[k]
    cr = np.linalg.norm(np.cross(Bv - A, C - A))
    L = max(np.linalg.norm(Bv - A), np.linalg.norm(C - Bv), np.linalg.norm(A - C))
    return L > 0 and cr <= rel * L * L


def remove_caps(V, F, rel=1e-10, max_iter=50):
    """Remove zero-area 'cap' triangles (a vertex lying on the opposite edge).

    The neighbour across the long edge is split at the middle vertex, after
    which the cap covers nothing and is dropped. Orientation is kept, so the
    mesh stays watertight. Returns (F, all_removed).
    """
    F = [tuple(int(x) for x in t) for t in F]
    for _ in range(max_iter):
        edge_tri = {}
        for ti, (i, j, k) in enumerate(F):
            for u, v in ((i, j), (j, k), (k, i)):
                edge_tri[(u, v)] = ti
        caps = [ti for ti, t in enumerate(F) if _is_cap(V, t, rel)]
        if not caps:
            return np.array(F, dtype=np.int64), True
        touched, drop, add = set(), set(), []
        for ti in caps:
            if ti in touched:
                continue
            t = F[ti]
            best = None
            for r in range(3):
                a, b, c = t[r], t[(r + 1) % 3], t[(r + 2) % 3]
                lac = np.linalg.norm(V[c] - V[a])
                if best is None or lac > best[0]:
                    best = (lac, a, b, c)
            _, a, b, c = best
            if min(np.linalg.norm(V[b] - V[a]), np.linalg.norm(V[b] - V[c])) < 1e-6:
                continue   # a needle between coincident vertices, not a cap
            nb = edge_tri.get((a, c))
            if nb is None or nb in touched or nb == ti:
                continue
            p, q, r = F[nb]
            w = None
            for x, y, z in ((p, q, r), (q, r, p), (r, p, q)):
                if (x, y) == (a, c):
                    w = z
                    break
            if w is None:
                continue
            F[nb] = (a, b, w)
            add.append((b, c, w))
            drop.add(ti)
            touched.update((ti, nb))
        if not drop:
            break
        F = [t for k, t in enumerate(F) if k not in drop] + add
    return np.array(F, dtype=np.int64), not any(_is_cap(V, t, rel) for t in F)


def mesh_to_brep(V, F, plane_tol=1e-5, tri_groups=frozenset(), depth=0):
    """Watertight, outward triangle mesh -> planar B-rep solid (or None).

    Coplanar, edge-connected triangles become one polygon face; collinear
    runs of boundary edges become one edge; every edge is shared by exactly
    the two faces on either side of it. Face groups whose polygon face comes
    out invalid (a hole touching its outline at a vertex, say) are rebuilt
    from their triangles on a second pass.
    """
    V = np.asarray(V, dtype=np.float64)
    F = np.asarray(F, dtype=np.int64)
    if depth == 0:
        F, _ = remove_caps(V, F)
    a, b, c = V[F[:, 0]], V[F[:, 1]], V[F[:, 2]]
    n = np.cross(b - a, c - a)
    ln = np.linalg.norm(n, axis=1)
    # zero-area slivers are kept: dropping one would leave its neighbours'
    # edges unpaired. Each joins the face across its longest edge below.
    degen = ln <= 1e-10 * np.maximum(np.einsum("ij,ij->i", b - a, b - a), np.einsum("ij,ij->i", c - a, c - a))
    n = n / np.where(ln > 0, ln, 1.0)[:, None]
    d = np.einsum("ij,ij->i", n, a)
    edges = {}
    for t, (i, j, k) in enumerate(F):
        for u, v in ((i, j), (j, k), (k, i)):
            edges.setdefault((min(u, v), max(u, v)), []).append(t)
    parent = list(range(len(F)))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x
    for ts in edges.values():
        if len(ts) == 2:
            t1, t2 = ts
            if n[t1] @ n[t2] > 1 - 1e-9 and abs(d[t1] - d[t2]) < plane_tol:
                r1, r2 = find(t1), find(t2)
                if r1 != r2:
                    parent[r1] = r2
    for t in np.nonzero(degen)[0]:
        i, j, k = F[t]
        es = sorted(((i, j), (j, k), (k, i)), key=lambda e: -np.linalg.norm(V[e[0]] - V[e[1]]))
        for u, v in es:
            other = [x for x in edges[(min(u, v), max(u, v))] if x != t and not degen[x]]
            if other:
                r1, r2 = find(t), find(other[0])
                if r1 != r2:
                    parent[r1] = r2
                break
    groups = collections.defaultdict(list)
    for t in range(len(F)):
        groups[find(t)].append(t)
    # the normal of a group comes from one of its real triangles
    gnorm = {}
    for g, ts in groups.items():
        real = [t for t in ts if not degen[t]]
        gnorm[g] = n[real[0]] if real else None
    for g in [g for g, v in gnorm.items() if v is None]:
        return None, "a group of only degenerate triangles"

    # 1. boundary loops of every group (directed, outward convention)
    group_loops = {}
    for g, ts in groups.items():
        if g in tri_groups:
            continue
        inset = set()
        for t in ts:
            i, j, k = (int(x) for x in F[t])
            inset.update(((i, j), (j, k), (k, i)))
        es = [e for e in inset if (e[1], e[0]) not in inset]
        nxt = collections.defaultdict(list)
        for u, v in es:
            nxt[u].append(v)
        nn = gnorm[g]
        used, loops = set(), []
        for u0, v0 in es:
            if (u0, v0) in used:
                continue
            loop, u, v = [u0], u0, v0
            while True:
                used.add((u, v))
                loop.append(v)
                if v == u0:
                    break
                cands = [w for w in nxt[v] if (v, w) not in used]
                if not cands:
                    return None, "open loop"
                if len(cands) > 1:
                    din = V[v] - V[u]
                    cands.sort(key=lambda w: np.arctan2(np.dot(np.cross(din, V[w] - V[v]), nn), np.dot(din, V[w] - V[v])), reverse=True)
                u, v = v, cands[0]
            loops.append(loop[:-1])
        group_loops[g] = loops

    # 2. corners: a boundary vertex that is not a straight pass-through between
    #    exactly two collinear edges; consistent for every face that uses it
    inc = collections.defaultdict(set)
    for loops in group_loops.values():
        for lp in loops:
            for i in range(len(lp)):
                u, v = lp[i], lp[(i + 1) % len(lp)]
                inc[u].add((min(u, v), max(u, v)))
                inc[v].add((min(u, v), max(u, v)))
    for g in tri_groups:
        for t in groups[g]:
            for x in F[t]:
                inc[int(x)].add(("pinned",))

    def is_corner(v):
        es = inc[v]
        if len(es) != 2 or any(len(e) == 1 for e in es):
            return True
        (p, q), (r, s) = list(es)
        o1 = p if q == v else q
        o2 = r if s == v else s
        d1 = V[o1] - V[v]; d2 = V[o2] - V[v]
        cr = np.linalg.norm(np.cross(d1, d2))
        return not (cr < 1e-9 * np.linalg.norm(d1) * np.linalg.norm(d2) and d1 @ d2 < 0)
    corner = {v: is_corner(v) for v in inc}
    # a loop needs at least three corners; give it all its vertices otherwise
    # (and so, through `corner`, to its neighbours as well) until stable
    changed = True
    while changed:
        changed = False
        for loops in group_loops.values():
            for lp in loops:
                if sum(corner[x] for x in lp) < 3 and not all(corner[x] for x in lp):
                    for x in lp:
                        corner[x] = True
                    changed = True

    # 3. shared vertices and edges
    occ_v, occ_e = {}, {}

    def vtx(i):
        if i not in occ_v:
            occ_v[i] = BRepBuilderAPI_MakeVertex(gp_Pnt(*V[i])).Vertex()
        return occ_v[i]

    def oriented_edge(u, v):
        k = (min(u, v), max(u, v))
        if k not in occ_e:
            me = BRepBuilderAPI_MakeEdge(vtx(k[0]), vtx(k[1]))
            if not me.IsDone():
                raise EdgeError("edge %s %s -> %s, length %.3g" % (k, V[k[0]], V[k[1]], np.linalg.norm(V[k[0]] - V[k[1]])))
            occ_e[k] = me.Edge()
        e = occ_e[k]
        return TopoDS.Edge_s(e.Reversed()) if u > v else e

    B = BRep_Builder()
    shell = TopoDS_Shell()
    B.MakeShell(shell)
    bad_groups = set()
    for g, ts in groups.items():
        if g in tri_groups:
            for t in ts:
                if degen[t]:
                    return None, "degenerate triangle in a triangulated group"
                i, j, k = (int(x) for x in F[t])
                mw = BRepBuilderAPI_MakeWire()
                for u, v in ((i, j), (j, k), (k, i)):
                    mw.Add(oriented_edge(u, v))
                B.Add(shell, BRepBuilderAPI_MakeFace(gp_Pln(gp_Pnt(*V[i]), gp_Dir(*n[t])), mw.Wire(), True).Face())
            continue
        nn = gnorm[g]
        wires = []
        for lp in group_loops[g]:
            pts = [x for x in lp if corner[x]]
            mw = BRepBuilderAPI_MakeWire()
            for i in range(len(pts)):
                mw.Add(oriented_edge(pts[i], pts[(i + 1) % len(pts)]))
            if not mw.IsDone():
                bad_groups.add(g)
                break
            P = V[pts]
            area = 0.5 * np.dot(nn, np.sum(np.cross(P, np.roll(P, -1, axis=0)), axis=0))
            wires.append((area, mw.Wire(), P))
        if g in bad_groups or not wires:
            bad_groups.add(g)
            continue
        # loops that share a vertex, or come closer than the tolerances a STEP
        # reader recomputes, give a face that is valid here and a
        # self-intersecting one after a round trip: triangulate those instead
        allpts = [x for lp in group_loops[g] for x in lp if corner[x]]
        if len(set(allpts)) < len(allpts):
            bad_groups.add(g)
            continue
        if len(group_loops[g]) > 1:
            from scipy.spatial import cKDTree
            P = V[allpts]
            lab = np.concatenate([[k] * sum(1 for x in lp if corner[x]) for k, lp in enumerate(group_loops[g])])
            pairs = cKDTree(P).query_pairs(1e-4, output_type="ndarray")
            if len(pairs) and np.any(lab[pairs[:, 0]] != lab[pairs[:, 1]]):
                bad_groups.add(g)
                continue
        pln = gp_Pln(gp_Pnt(*V[group_loops[g][0][0]]), gp_Dir(*nn))
        ex = np.cross(nn, [1.0, 0, 0] if abs(nn[0]) < 0.9 else [0, 1.0, 0]); ex /= np.linalg.norm(ex)
        ey = np.cross(nn, ex)
        to2 = lambda P: np.stack([P @ ex, P @ ey], 1)
        outers = [w for w in wires if w[0] > 0]
        holes = [w for w in wires if w[0] <= 0]
        polys = [Polygon(to2(w[2])) for w in outers]
        assign = collections.defaultdict(list)
        for h in holes:
            q = Point(to2(h[2][:1])[0])
            best = None
            for k, pg in enumerate(polys):
                if pg.buffer(1e-6).contains(q) and (best is None or pg.area < polys[best].area):
                    best = k
            if best is None:
                bad_groups.add(g)
                break
            assign[best].append(h)
        if g in bad_groups:
            continue
        faces = []
        for k, (ar, w, P) in enumerate(outers):
            mfc = BRepBuilderAPI_MakeFace(pln, w, True)
            for h in assign[k]:
                mfc.Add(h[1])
            if not mfc.IsDone() or not BRepCheck_Analyzer(mfc.Face()).IsValid():
                bad_groups.add(g)
                break
            faces.append(mfc.Face())
        if g in bad_groups:
            continue
        for f in faces:
            B.Add(shell, f)
    if bad_groups:
        import sys as _s
        print("  mesh_to_brep: %d groups (%d triangles) rebuilt from triangles"
              % (len(bad_groups), sum(len(groups[g]) for g in bad_groups)), file=_s.stderr)
        if depth > 1:
            return None, "faces still invalid after triangulating them"
        return mesh_to_brep(V, F, plane_tol, frozenset(tri_groups) | bad_groups, depth + 1)
    solid = TopoDS_Solid()
    B.MakeSolid(solid)
    B.Add(solid, shell)
    fx = ShapeFix_Solid(solid)
    fx.Perform()
    fs = ShapeFix_Shape(fx.Solid())
    fs.Perform()
    r = fs.Shape()
    from OCP.GProp import GProp_GProps
    from OCP.BRepGProp import BRepGProp
    gp = GProp_GProps(); BRepGProp.VolumeProperties_s(r, gp)
    if gp.Mass() < 0:
        # rebuild around the reversed shell: a reversed *solid* does not
        # survive a STEP round trip, its shell does
        from OCP.TopExp import TopExp_Explorer
        from OCP.TopAbs import TopAbs_SHELL
        e = TopExp_Explorer(r, TopAbs_SHELL)
        sh = e.Current()
        s2 = TopoDS_Solid()
        B.MakeSolid(s2)
        B.Add(s2, sh.Reversed())
        r = s2
    return r, "ok"
