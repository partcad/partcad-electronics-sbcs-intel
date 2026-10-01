"""Write partcad.yaml for the Intel SBC package from the build reports.

  gen_yaml.py REPORTS_DIR partcad.yaml [--release URL --sums SHA256SUMS]

With --release, every part is fetched from URL/<part>.step and pinned by the
sha256 listed for it in SHA256SUMS (the release assets CI publishes). Without
reports (an empty REPORTS_DIR), the package is written with no parts yet.
"""
import argparse
import json
import os

FAQ = "https://www.asus.com/support/faq/1052528/"

# name: (desc, vendor, sku, product url, SKUs covered, source file in the ASUS/Intel archive)
BOARDS = {
    "d33217ck": ("Intel NUC Board D33217CK (3rd gen Core i3-3217U)", "intel", "D33217CK",
                 FAQ,
                 "D33217CK", "D33217CK.STEP"),
    "d33217gke": ("Intel NUC Board D33217GKE / DCP847SKE (3rd gen Core i3-3217U / Celeron 847)", "intel", "D33217GKE",
                  FAQ,
                  "D33217GKE, DCP847SKE", "D33217GKE-DCP847SKE.STEP"),
    "d53427rke": ("Intel NUC Board D53427RKE (3rd gen Core i5-3427U)", "intel", "D53427RKE",
                  FAQ,
                  "D53427RKE", "D53427RKE.STEP"),
    "de3815tybe": ("Intel NUC Board DE3815TYBE (Atom E3815)", "intel", "DE3815TYBE",
                   FAQ,
                   "DE3815TYBE", "DE3815TYBE.STEP"),
    "nuc5myb": ("Intel NUC Board NUC5i3MYBE / NUC5i5MYBE (5th gen Core)", "intel", "NUC5i5MYBE",
                FAQ,
                "NUC5i3MYBE, NUC5i5MYBE", "NUC5i3MYBE-NUC5i5MYBE.STEP"),
    "nuc7i3dnb": ("Intel NUC Board NUC7i3DNBE (7th gen Core i3)", "intel", "NUC7i3DNBE",
                  FAQ,
                  "NUC7i3DNBE", "NUC7i3DN 01.stp"),
    "nuc7i5dnb": ("Intel NUC Board NUC7i5DNBE / NUC7i7DNBE (7th gen Core i5/i7)", "intel", "NUC7i5DNBE",
                  FAQ,
                  "NUC7i5DNBE, NUC7i7DNBE", "NUC7i5DN.STEP"),
    "nuc8cchb": ("Intel NUC Board NUC8CCHB (Chaco Canyon, 8th gen Core i5)", "intel", "NUC8CCHB",
                 FAQ,
                 "NUC8CCHB", "NUC_CH_Board_19WW36.STEP"),
    "nuc8pnb": ("Intel NUC Board NUC8v7PNB / NUC8v5PNB / NUC8i3PNB (Provo Canyon)", "intel", "NUC8v5PNB",
                FAQ,
                "NUC8v7PNB, NUC8v5PNB, NUC8i3PNB", "NUC8PNB_STEP.stp"),
    "nuc9qnb": ("Intel NUC 9 Compute Element NUC9i5QNB / i7 / i9 / V7 / VX (Quartz Canyon)", "intel", "NUC9i5QNB",
                FAQ,
                "NUC9VXQNB, NUC9V7QNB, NUC9i9QNB, NUC9i7QNB, NUC9i5QNB", "NUC_QN-GN_Full Module_19WW50.STEP"),
    "nuc11tnb": ("Intel NUC 11 Pro Board NUC11TNB (Tiger Canyon)", "intel", "NUC11TNBi5",
                 FAQ,
                 "NUC11TNBv7, NUC11TNBi7, NUC11TNBv5, NUC11TNBi5, NUC11TNBi3", "NUC11TNB.stp"),
    "nuc11tnb-dual-lan": ("Intel NUC 11 Pro Board NUC11TNB with the dual LAN expansion module", "intel", "NUC11TNBi5",
                          FAQ,
                          "NUC11TNBv7, NUC11TNBi7, NUC11TNBv5, NUC11TNBi5, NUC11TNBi3 (with expansion module)", "NUC11TNB_Dual_LAN.stp"),
    "nuc11tnbz": ("Intel NUC 11 Pro Board NUC11TNBi*0Z (no Thunderbolt ports)", "intel", "NUC11TNBi50Z",
                  FAQ,
                  "NUC11TNBi30Z, NUC11TNBi50Z, NUC11TNBi70Z", "NUC11TNBZ.STP"),
    "nuc12wsb": ("Intel NUC 12 Pro Board NUC12WSB (Wall Street Canyon)", "intel", "NUC12WSB",
                 "https://ark.intel.com/content/www/us/en/ark/products/121629/intel-nuc-12-pro-board-nuc12wsbv5.html",
                 "NUC12WSBv7, NUC12WSBi7, NUC12WSBv5, NUC12WSBi5, NUC12WSBi3", "NUC12WSB.STEP"),
    "nuc12wsb-dual-lan": ("Intel NUC 12 Pro Board NUC12WSB with the dual LAN expansion module", "intel", "NUC12WSBi5",
                          FAQ,
                          "NUC12WSBv7, NUC12WSBi7, NUC12WSBv5, NUC12WSBi5, NUC12WSBi3 (with expansion module)", "NUC12WSB_Dual_LAN.STEP"),
    "nuc12wsbz": ("Intel NUC 12 Pro Board NUC12WSBi*0Z (no Thunderbolt ports)", "intel", "NUC12WSBi50Z",
                  FAQ,
                  "NUC12WSBi70Z, NUC12WSBi50Z, NUC12WSBi30Z", "NUC12WSBZ.STEP"),
    "nuc13anb": ("Intel NUC 13 Pro Board NUC13ANB / NUC13L5B (Arena Canyon)", "intel", "NUC13ANBi5",
                 FAQ,
                 "NUC13ANBi7, NUC13ANBv5, NUC13ANBi5, NUC13ANBi3, NUC13L5Bv7, NUC13L5Bv5, NUC13L5Bi5, NUC13L5Bi3",
                 "NUC13ANB_NUC13LCB.STP"),
    "nuc14mnb": ("ASUS NUC 14 Essential Board NUC14MNB", "asus", "NUC14MNB",
                 FAQ,
                 "NUC14MNB*", "MN_MB_20240821.stp"),
    "nuc14rvb": ("ASUS NUC 14 Pro Board NUC14RVB (Revel Canyon), board only", "asus", "NUC14RVB",
                 FAQ,
                 "NUC14RVB*", "NUC14RVB_ board only.stp"),
    "nuc14rvb-thermal": ("ASUS NUC 14 Pro Board NUC14RVB (Revel Canyon) with its thermal module", "asus", "NUC14RVB",
                         FAQ,
                         "NUC14RVB*", "nuc14rvb__board_W Thermal Module.stp"),
    "nuc15crb": ("ASUS NUC 15 Pro Board NUC15CRB (Cyber Canyon)", "asus", "NUC15CRB",
                 FAQ,
                 "NUC15CRB*", "NUC15CRB_PCBA_STEP.stp"),
}


def fastener_size(d):
    """Largest metric screw whose ISO 273 fine clearance fits a hole of d mm."""
    for size, fine in ((4, 4.3), (3, 3.2), (2.5, 2.7), (2, 2.2)):
        if d >= fine - 0.05:
            return size
    return None


def usable(h, min_clear=3.0):
    return all(h["clear_" + k] is None or h["clear_" + k] >= min_clear for k in ("top", "bottom"))


def fmt(x):
    return ("%.3f" % x).rstrip("0").rstrip(".")


INTRO = """Intel NUC boards (the NUC business has been with ASUS since 2023) as
single-solid envelopes for assembly work: interference checks, clearance and
enclosure design. Each part is reduced from the vendor's PCBA model, which
describes the board as an assembly of hundreds of components (20-125 MB of
STEP per board), as published at %s

What each part is:

- **One solid.** The whole board, fused. Components that float in the vendor
  model (solder gaps) are tied to the board by thin rods.
- **The outer silhouette.** Every component becomes its box, carved by the
  openings visible along its own axes: port mouths with their keys and
  notches, the bores of threaded inserts, the steps of its outline. Openings
  narrower than about 0.7 mm or shallower than 0.6 mm (1 mm on the fan, heat
  sink and other large parts) are walled off, and so are enclosed voids, rows
  of heat sink fins and the gaps between neighbouring passives. Pins,
  contacts, fan blades and everything nobody can see from outside are gone.
- **The PCB exactly as drawn**, outline and every hole of 2.5 mm or more.
  PCB holes that a fastener can reach from both sides are declared as
  `//pub/std/metric/m` through-hole ports (top and bottom), sized by the
  largest metric screw whose ISO 273 fine clearance fits.

Placement: the PCB bottom is on Z=0 and the PCB outline is centred on X=Y=0;
+Z is the vendor's own up.

Not included: the NUC kits (chassis) and power bricks from the same page, and
NUC7i5BNB/NUC7i7BNB, NUC7i3BNB and D34010WYB/D54250WYB, whose STEP archives
are no longer downloadable.

The STEP files are not kept in this repository. `tools/` has the scripts that
produce them from the vendor archives (`tools/regenerate.sh`), GitHub Actions
runs them on every `v*` tag and publishes the results as release assets, and
each part below downloads its file from the release, pinned by its sha256."""


REPO = "partcad/partcad-electronics-sbcs-intel"

USAGE = """### Publishing updated STEP files

The STEP files are release assets, not repository files: `*.step` is ignored by
git, and `partcad.yaml` downloads every part from a release, pinned by its
sha256. To publish new ones:

1. **Change what is built.** The reduction is `tools/nuc_envelope.py` (with
   `meshunion.py`, `occ_util.py` and `minify_step.py`); the boards and the
   vendor archives they come from are listed in `tools/boards.tsv`. A new
   board also needs an entry in `BOARDS` in `tools/gen_yaml.py` (its
   description, vendor, SKU and URL).
2. **Try it locally** (optional). With the pinned packages installed
   (`pip install -r tools/requirements.txt`, Python 3.12):

   ```sh
   tools/regenerate.sh nuc12wsb          # one board; no names for all of them
   ```

   This writes `build/<part>.step` and `build/<part>.json` (the report: what
   was done, the read-back check, the PCB holes). Each report has to say
   `"readback": {"solids": 1, "free_shells": 0, ..., "valid": true}`.
3. **Commit and push to `main`, then push a new tag:**

   ```sh
   git tag -a v1.0.2 -m "STEP files v1.0.2"
   git push origin v1.0.2
   ```

   The `Release STEP files` workflow (`.github/workflows/release.yml`) builds
   every board in its own job and publishes `<part>.step`, `<part>.json` and
   `SHA256SUMS` as release `v1.0.2`. It can also be started by hand (Actions >
   Release STEP files > Run workflow, with the tag to create). If a board
   fails, nothing is published: fix it and release under a new tag.
4. **Point the package at the release:**

   ```sh
   gh release download v1.0.2 -R %(repo)s -D tools/reports \\
       -p '*.json' -p SHA256SUMS --clobber
   python3 tools/gen_yaml.py tools/reports partcad.yaml \\
       --release https://github.com/%(repo)s/releases/download/v1.0.2 \\
       --sums tools/reports/SHA256SUMS
   ```

   `gen_yaml.py` writes the whole `partcad.yaml`, this text included: edit
   `INTRO` and `USAGE` there, not here.
5. **Check and render**, then commit `partcad.yaml`, `tools/reports/`,
   `README.md` and the `*.svg` previews:

   ```sh
   pc test      # 'manufacturability: No suppliers found' is expected
   pc render    # downloads the release files, writes the SVGs and README.md
   ```

Earlier releases stay where they are, so a package pinned to an older tag of
this repository keeps downloading the files it was published with.""" % dict(repo=REPO)


def main(reports, out, names_order, release=None, sums=None):
    names_order = [n for n in names_order if os.path.exists(os.path.join(reports, n + ".json"))]
    hashes = {}
    if sums:
        for line in open(sums):
            digest, fname = line.split()
            hashes[fname.lstrip("*")] = digest
    L = []
    L.append('partcad: ">=0.8.0"')
    L.append("name: //pub/electronics/sbcs/intel")
    L.append("desc: Intel NUC boards (now made by ASUS)")
    L.append("url: %s" % FAQ)
    if "nuc12wsb" in names_order:
        L.append("cover:")
        L.append("  part: nuc12wsb")
    L.append("")
    L.append("dependencies:")
    L.append("  pub:")
    L.append("    onlyInRoot: True")
    L.append("    type: git")
    L.append("    url: https://github.com/partcad/partcad-index.git")
    L.append("")
    L.append("docs:")
    L.append("  intro: |")
    for line in (INTRO % FAQ).split("\n"):
        L.append(("    " + line) if line else "")
    L.append("  usage: |")
    for line in USAGE.split("\n"):
        L.append(("    " + line) if line else "")
    L.append("")
    L.append("# Every part is one solid: the board envelope, reduced from the vendor's")
    L.append("# PCBA model (see README.md). The PCB bottom is on Z=0 and the PCB outline is")
    L.append("# centred on X=Y=0; up is the vendor's own up.")
    if not names_order:
        L.append("# No parts yet: they are added once CI has published the STEP files as a")
        L.append("# release (tools/gen_yaml.py --release).")
        L.append("")
    else:
        L.append("parts:")
    for n in names_order:
        rep = json.load(open(os.path.join(reports, n + ".json")))
        desc, vendor, sku, url, skus, src = BOARDS[n]
        L.append("  %s:" % n)
        L.append("    type: step")
        if release:
            L.append("    fileFrom: url")
            L.append("    fileUrl: %s/%s.step" % (release.rstrip("/"), n))
            L.append("    fileHash: sha256:%s" % hashes[n + ".step"])
        L.append("    desc: %s" % desc)
        L.append("    vendor: %s" % vendor)
        L.append("    sku: %s" % sku)
        L.append("    url: %s" % url)
        L.append("    # 3D model: %s (%s)" % (FAQ, src))
        L.append("    # Covers: %s" % skus)
        t = rep["pcb_thickness"]
        groups = {}
        for i, h in enumerate(sorted(rep["holes"], key=lambda h: (h["y"], h["x"]))):
            if not usable(h):
                continue
            size = fastener_size(h["d"])
            if size is None:
                continue
            groups.setdefault(size, []).append(h)
        if groups:
            L.append("    implements:")
            L.append("      # PCB holes a fastener can reach from both sides, at the vendor's")
            L.append("      # diameter; the M size is the largest whose ISO 273 fine clearance fits.")
            k = 0
            for size, hs in sorted(groups.items()):
                L.append('      "//pub/std/metric/m:m-thru-depth;size=%s,depth=%s":' % (fmt(size), fmt(t)))
                for h in hs:
                    k += 1
                    tag = "hole%d" % k
                    L.append("        %s-top: [[%s, %s, %s], [1, 0, 0], 180]  # hole d%s mm" % (tag, fmt(h["x"]), fmt(h["y"]), fmt(t), fmt(h["d"])))
                    L.append("        %s-bottom: [[%s, %s, 0], [0, 0, 1], 0]" % (tag, fmt(h["x"]), fmt(h["y"])))
        L.append("")
    if "nuc12wsb" in names_order:
        L.append("  # The name this package used for the NUC 12 Pro board before it grew")
        L.append("  nuc12:")
        L.append("    type: alias")
        L.append("    source: :nuc12wsb")
        L.append("")
    L.append("render:")
    L.append("  svg:")
    L.append("  readme:")
    open(out, "w").write("\n".join(L) + "\n")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("reports")
    ap.add_argument("out")
    ap.add_argument("--release", help="base URL of the release assets")
    ap.add_argument("--sums", help="SHA256SUMS of the release assets")
    a = ap.parse_args()
    if bool(a.release) != bool(a.sums):
        ap.error("--release and --sums go together")
    main(a.reports, a.out, list(BOARDS), a.release, a.sums)
