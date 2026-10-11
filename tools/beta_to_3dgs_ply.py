"""Convert a beta-splatting PLY to a standard 3DGS PLY (f_dc_*, opacity, scale_*, rot_*),
so viewers that only understand INRIA-style PLYs (e.g. Spark) can load it.

Beta kernel shape (beta) and spherical-beta lobes (sb_params_*) are dropped, so the render
is an approximation; this is mainly for load-time comparisons.

Usage: python tools/beta_to_3dgs_ply.py <in.ply> <out.ply>
"""
import sys

import numpy as np

KEEP = ["x", "y", "z", "nx", "ny", "nz"]
RENAME = {"sh0_0": "f_dc_0", "sh0_1": "f_dc_1", "sh0_2": "f_dc_2"}
TAIL = ["opacity", "scale_0", "scale_1", "scale_2", "rot_0", "rot_1", "rot_2", "rot_3"]


def main(src, dst):
    with open(src, "rb") as f:
        props, count = [], 0
        while True:
            line = f.readline().decode("ascii").strip()
            if line.startswith("element vertex"):
                count = int(line.split()[-1])
            elif line.startswith("property"):
                props.append(line.split()[2])
            elif line == "end_header":
                break
        data = np.fromfile(f, dtype="<f4", count=count * len(props)).reshape(count, len(props))

    out_names = KEEP + list(RENAME.values()) + TAIL
    src_names = KEEP + list(RENAME.keys()) + TAIL
    out = np.ascontiguousarray(data[:, [props.index(n) for n in src_names]])

    header = ["ply", "format binary_little_endian 1.0", f"element vertex {count}"]
    header += [f"property float {n}" for n in out_names] + ["end_header"]
    with open(dst, "wb") as f:
        f.write(("\n".join(header) + "\n").encode("ascii"))
        out.astype("<f4").tofile(f)
    print(f"{src} -> {dst}: {count} splats, {len(out_names)} props")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    main(sys.argv[1], sys.argv[2])
