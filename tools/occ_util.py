"""OCCT helpers shared by the NUC envelope tools: STEP reading, topology,
boxes, tessellation and exterior visibility by ray casting."""
import math
import time
import numpy as np


from OCP.TopExp import TopExp_Explorer
from OCP.TopAbs import (TopAbs_FACE, TopAbs_REVERSED)
from OCP.TopoDS import TopoDS, TopoDS_Iterator
from OCP.TopLoc import TopLoc_Location
from OCP.BRep import BRep_Tool, BRep_Builder
from OCP.BRepGProp import BRepGProp
from OCP.GProp import GProp_GProps
from OCP.Bnd import Bnd_Box, Bnd_OBB
from OCP.BRepBndLib import BRepBndLib
from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
from OCP.BRepMesh import BRepMesh_IncrementalMesh
from OCP.gp import gp_Pnt, gp_Dir, gp_Ax2


# ---------------------------------------------------------------- helpers
def explore(shape, typ, avoid=None):
    e = TopExp_Explorer(shape, typ, avoid) if avoid is not None else TopExp_Explorer(shape, typ)
    while e.More():
        yield e.Current()
        e.Next()


def nfaces(s):
    return sum(1 for _ in explore(s, TopAbs_FACE))


def volume(s):
    p = GProp_GProps()
    BRepGProp.VolumeProperties_s(s, p)
    return p.Mass()


def aabb(s):
    b = Bnd_Box()
    BRepBndLib.Add_s(s, b, False)
    return None if b.IsVoid() else b.Get()


def finite(bb, lim=1e4):
    return bb is not None and all(abs(v) < lim for v in bb)


def obb_of(s):
    o = Bnd_OBB()
    BRepBndLib.AddOBB_s(s, o, True, True, False)
    return o


def obb_dims(o):
    return 2 * o.XHSize(), 2 * o.YHSize(), 2 * o.ZHSize()


def box_from_obb(o):
    c = o.Center()
    xd, yd, zd = o.XDirection(), o.YDirection(), o.ZDirection()
    hx, hy, hz = o.XHSize(), o.YHSize(), o.ZHSize()
    corner = gp_Pnt(c.X() - xd.X() * hx - yd.X() * hy - zd.X() * hz,
                    c.Y() - xd.Y() * hx - yd.Y() * hy - zd.Y() * hz,
                    c.Z() - xd.Z() * hx - yd.Z() * hy - zd.Z() * hz)
    ax = gp_Ax2(corner, gp_Dir(zd.X(), zd.Y(), zd.Z()), gp_Dir(xd.X(), xd.Y(), xd.Z()))
    return BRepPrimAPI_MakeBox(ax, max(2 * hx, 1e-3), max(2 * hy, 1e-3), max(2 * hz, 1e-3)).Solid()


# ---------------------------------------------------------------- repair


# ---------------------------------------------------------------- mesh
def mesh_arrays(shape, defl=0.05):
    BRepMesh_IncrementalMesh(shape, defl, False, 0.35, True)
    V, F, off = [], [], 0
    for f in explore(shape, TopAbs_FACE):
        loc = TopLoc_Location()
        tri = BRep_Tool.Triangulation_s(TopoDS.Face_s(f), loc)
        if tri is None:
            continue
        tr = loc.Transformation()
        n = tri.NbNodes()
        pts = np.empty((n, 3))
        for i in range(1, n + 1):
            p = tri.Node(i).Transformed(tr)
            pts[i - 1] = (p.X(), p.Y(), p.Z())
        m = tri.NbTriangles()
        tris = np.empty((m, 3), dtype=np.int64)
        rev = f.Orientation() == TopAbs_REVERSED
        for i in range(1, m + 1):
            a, b, c = tri.Triangle(i).Get()
            tris[i - 1] = (a - 1, c - 1, b - 1) if rev else (a - 1, b - 1, c - 1)
        V.append(pts)
        F.append(tris + off)
        off += n
    if not V:
        return np.zeros((0, 3)), np.zeros((0, 3), dtype=np.int64)
    return np.vstack(V), np.vstack(F)


def fib_dirs(n):
    i = np.arange(n) + 0.5
    phi = np.arccos(1 - 2 * i / n)
    th = math.pi * (1 + 5 ** 0.5) * i
    return np.stack([np.cos(th) * np.sin(phi), np.sin(th) * np.sin(phi), np.cos(phi)], 1)


def visibility(meshes, ndirs=160, step=0.25, log=print):
    """meshes: list of (V,F). Returns per-body hit counts from exterior rays."""
    import trimesh
    Vs, Fs, owner, off = [], [], [], 0
    for bi, (V, F) in enumerate(meshes):
        if len(F) == 0:
            continue
        Vs.append(V); Fs.append(F + off); owner.append(np.full(len(F), bi)); off += len(V)
    V = np.vstack(Vs); F = np.vstack(Fs); owner = np.concatenate(owner)
    tm = trimesh.Trimesh(V, F, process=False)
    inter = trimesh.ray.ray_pyembree.RayMeshIntersector(tm)
    hits = np.zeros(len(meshes), dtype=np.int64)
    lo, hi = V.min(0), V.max(0)
    ctr, rad = (lo + hi) / 2, np.linalg.norm(hi - lo) / 2 + 1
    t = time.time()
    total = 0
    for d in fib_dirs(ndirs):
        a = np.array([1.0, 0, 0]) if abs(d[0]) < 0.9 else np.array([0, 1.0, 0])
        u = np.cross(d, a); u /= np.linalg.norm(u); v = np.cross(d, u)
        pu, pv = (V - ctr) @ u, (V - ctr) @ v
        gu = np.arange(pu.min() - step, pu.max() + step, step)
        gv = np.arange(pv.min() - step, pv.max() + step, step)
        # jitter the grid per direction so thin gaps are sampled differently
        jit = np.random.default_rng(len(gu)).uniform(0, step, 2)
        U, W = np.meshgrid(gu + jit[0], gv + jit[1])
        org = ctr - d * rad + np.outer(U.ravel(), u) + np.outer(W.ravel(), v)
        dirs = np.tile(d, (len(org), 1))
        tri = inter.intersects_first(org, dirs)
        tri = tri[tri >= 0]
        np.add.at(hits, owner[tri], 1)
        total += len(org)
    log("  visibility: %d rays in %.1fs" % (total, time.time() - t))
    return hits


# ---------------------------------------------------------------- worker


def read_brep(path):
    from OCP.BRepTools import BRepTools
    from OCP.TopoDS import TopoDS_Shape
    s = TopoDS_Shape()
    BRepTools.Read_s(s, path, BRep_Builder())
    return s


def iter_children(s):
    it = TopoDS_Iterator(s)
    while it.More():
        yield it.Value()
        it.Next()


# ---------------------------------------------------------------- main




# ---------------------------------------------------------------- STEP
from OCP.STEPCAFControl import STEPCAFControl_Reader
from OCP.TDocStd import TDocStd_Document
from OCP.TCollection import TCollection_ExtendedString
from OCP.XCAFDoc import XCAFDoc_DocumentTool
from OCP.TDF import TDF_LabelSequence
from OCP.TDataStd import TDataStd_Name
from OCP.IFSelect import IFSelect_RetDone
from OCP.Interface import Interface_Static


def label_name(lab):
    attr = TDataStd_Name()
    if lab.FindAttribute(TDataStd_Name.GetID_s(), attr):
        return attr.Get().ToExtString()
    return ""


def load_leaves(path, verbose=True):
    """Return (leaves, doc) where leaves = [(path_names, shape_with_location)]."""
    t = time.time()
    Interface_Static.SetCVal_s("xstep.cascade.unit", "MM")
    doc = TDocStd_Document(TCollection_ExtendedString("pc"))
    r = STEPCAFControl_Reader()
    r.SetNameMode(True)
    r.SetColorMode(False)
    r.SetLayerMode(False)
    if r.ReadFile(str(path)) != IFSelect_RetDone:
        raise RuntimeError("cannot read %s" % path)
    r.Transfer(doc)
    st = XCAFDoc_DocumentTool.ShapeTool_s(doc.Main())
    if verbose:
        print("read %s in %.1fs" % (path, time.time() - t), flush=True)
    leaves = []
    roots = TDF_LabelSequence()
    st.GetFreeShapes(roots)

    def walk(lab, loc, names):
        from OCP.XCAFDoc import XCAFDoc_ShapeTool
        name = label_name(lab)
        if XCAFDoc_ShapeTool.IsReference_s(lab):
            ref = lab.__class__()
            XCAFDoc_ShapeTool.GetReferredShape_s(lab, ref)
            l2 = loc.Multiplied(XCAFDoc_ShapeTool.GetLocation_s(lab))
            walk(ref, l2, names + [name])
            return
        if XCAFDoc_ShapeTool.IsAssembly_s(lab):
            comps = TDF_LabelSequence()
            XCAFDoc_ShapeTool.GetComponents_s(lab, comps)
            for i in range(1, comps.Length() + 1):
                walk(comps.Value(i), loc, names + [name])
            return
        shp = XCAFDoc_ShapeTool.GetShape_s(lab)
        if not shp.IsNull():
            leaves.append((names + [name], shp.Moved(loc)))

    for i in range(1, roots.Length() + 1):
        walk(roots.Value(i), TopLoc_Location(), [])
    return leaves, doc
