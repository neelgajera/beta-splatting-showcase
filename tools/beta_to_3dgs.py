"""Convert a Deformable Beta Splatting model to a standard 3DGS PLY that any splat engine can load
(PlayCanvas, SuperSplat, gsplat, ...), placed upright on the ground.

    python tools/beta_to_3dgs.py --ply object.ply --cameras cameras.json --out car_3dgs.ply

What changes, and how it is preserved:
  * Kernel. Beta splatting rasterizes each primitive as a 2D kernel (1 - r^2)^b over its projected scale
    ellipse. A Gaussian with the same total opacity over the footprint (integral pi / (b + 1) = 2 pi sigma^2)
    has sigma = scale / sqrt(2b + 2). Matching opacity mass rather than variance keeps coverage, so thin and
    semi-transparent regions don't turn see-through.
  * Colour. Beta splatting colours are a base colour plus K spherical-beta lobes
    rgb_k * max(0, d.mu_k)^(4 e^beta_k), which 3DGS can't store. Each primitive's colour is sampled over 256
    view directions and least-squares fitted with degree-3 spherical harmonics, which 3DGS and every viewer
    support. Broad highlights survive; very sharp lobes are softened (SH3 is band-limited).
  * Frame. Splat datasets come out of COLMAP y-down and arbitrarily rotated. The model is rotated so the
    cameras' mean up vector is +Y, the object's long axis is +Z, the ground (1st height percentile) is at
    y = 0 and the object is centred, so engines can use it with an identity transform. Primitives below the
    ground are removed. SH are fitted in the new frame, so no SH rotation is needed.

Needs PyTorch and beta-splatting (for its model loader); set BETA_SPLATTING to the repo path.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
from pathlib import Path

import numpy as np
import torch
from plyfile import PlyData, PlyElement

C0 = 0.28209479177387814
C1 = 0.4886025119029199
C2 = [1.0925484305920792, -1.0925484305920792, 0.31539156525252005, -1.0925484305920792, 0.5462742152960396]
C3 = [-0.5900435899266435, 2.890611442640554, -0.4570457994644658, 0.3731763325901154, -0.4570457994644658,
      1.445305721320277, -0.5900435899266435]


def sh_basis(d: torch.Tensor) -> torch.Tensor:
    """Real SH basis, degree 3, in the 3DGS convention. d: (K, 3) unit vectors -> (K, 16)."""
    x, y, z = d[:, 0], d[:, 1], d[:, 2]
    xx, yy, zz, xy, yz, xz = x * x, y * y, z * z, x * y, y * z, x * z
    return torch.stack([
        torch.full_like(x, C0),
        -C1 * y, C1 * z, -C1 * x,
        C2[0] * xy, C2[1] * yz, C2[2] * (2 * zz - xx - yy), C2[3] * xz, C2[4] * (xx - yy),
        C3[0] * y * (3 * xx - yy), C3[1] * xy * z, C3[2] * y * (4 * zz - xx - yy),
        C3[3] * z * (2 * zz - 3 * xx - 3 * yy), C3[4] * x * (4 * zz - xx - yy),
        C3[5] * z * (xx - yy), C3[6] * x * (xx - 3 * yy)], -1)


def fibonacci_sphere(n: int) -> torch.Tensor:
    i = torch.arange(n, dtype=torch.float64) + 0.5
    phi = torch.acos(1 - 2 * i / n)
    theta = math.pi * (1 + 5 ** 0.5) * i
    return torch.stack([torch.cos(theta) * torch.sin(phi), torch.sin(theta) * torch.sin(phi), torch.cos(phi)], -1).float()


def rotmat_to_quat(R: torch.Tensor) -> torch.Tensor:
    """(N,3,3) -> (N,4) wxyz, numerically safe branch per element."""
    m = R
    t = m[:, 0, 0] + m[:, 1, 1] + m[:, 2, 2]
    q = torch.empty(R.shape[0], 4, dtype=R.dtype, device=R.device)
    c0 = t > 0
    s = torch.sqrt(t[c0] + 1.0) * 2
    q[c0] = torch.stack([0.25 * s, (m[c0, 2, 1] - m[c0, 1, 2]) / s, (m[c0, 0, 2] - m[c0, 2, 0]) / s,
                         (m[c0, 1, 0] - m[c0, 0, 1]) / s], -1)
    rest = ~c0
    i = torch.argmax(torch.stack([m[:, 0, 0], m[:, 1, 1], m[:, 2, 2]], -1), -1)
    for k, (a, b, c) in enumerate([(0, 1, 2), (1, 2, 0), (2, 0, 1)]):
        sel = rest & (i == k)
        if sel.any():
            s = torch.sqrt(1.0 + m[sel, a, a] - m[sel, b, b] - m[sel, c, c]) * 2
            qq = torch.empty(int(sel.sum()), 4, dtype=R.dtype, device=R.device)
            qq[:, 0] = (m[sel, c, b] - m[sel, b, c]) / s
            qq[:, 1 + a] = 0.25 * s
            qq[:, 1 + b] = (m[sel, b, a] + m[sel, a, b]) / s
            qq[:, 1 + c] = (m[sel, c, a] + m[sel, a, c]) / s
            q[sel] = qq
    return q / q.norm(dim=-1, keepdim=True)


def world_frame(xyz: np.ndarray, cameras_json: Path, ground_percentile: float):
    """Rotation Rw (rows = new x, y, z axes in old coordinates) and origin, for an upright, centred object."""
    cams = json.loads(cameras_json.read_text())
    up = -np.mean([np.array(c["rotation"])[:, 1] for c in cams], axis=0)   # c2w rotation; OpenCV y is down
    up /= np.linalg.norm(up)
    h = xyz @ up
    ground = np.percentile(h, ground_percentile)
    centre = np.median(xyz, axis=0)
    flat = (xyz - centre) - np.outer((xyz - centre) @ up, up)
    long_axis = np.linalg.svd(flat[:: max(1, len(flat) // 20000)], full_matrices=False)[2][0]
    long_axis -= (long_axis @ up) * up
    long_axis /= np.linalg.norm(long_axis)
    x_axis = np.cross(up, long_axis)
    Rw = np.stack([x_axis, up, long_axis])           # new = Rw @ (old - origin)
    origin = centre - ((centre @ up) - ground) * up  # centre projected onto the ground plane
    return Rw, origin


@torch.no_grad()
def convert(ply: Path, cameras_json: Path, out: Path, ground_percentile: float = 1.0, n_dirs: int = 256,
            sb_number: int = 2) -> dict:
    sys.path.insert(0, os.environ.get("BETA_SPLATTING", "third_party/beta-splatting"))
    from scene.beta_model import BetaModel

    model = BetaModel(sh_degree=0, sb_number=sb_number)
    model.load_ply(str(ply))
    xyz = model.get_xyz.double()
    Rw_np, origin_np = world_frame(xyz.cpu().numpy(), cameras_json, ground_percentile)
    Rw = torch.tensor(Rw_np, device="cuda")
    origin = torch.tensor(origin_np, device="cuda")

    new_xyz = (xyz - origin) @ Rw.T
    keep = new_xyz[:, 1] > 0                          # drop primitives under the ground
    new_xyz = new_xyz[keep].float()

    beta = model.get_beta.squeeze(-1)[keep].double()                       # 4 e^b
    sigma = model.get_scaling[keep].double() / torch.sqrt(2 * beta + 2)[:, None]   # 2D opacity-mass match
    R_old = BetaModel_rotmats(model.get_rotation[keep].double())
    new_quat = rotmat_to_quat(Rw[None] @ R_old).float()

    # view-dependent colour: base + spherical beta lobes, sampled in the new frame, fitted with SH3
    dirs_new = fibonacci_sphere(n_dirs).cuda()                             # (K, 3)
    dirs_old = dirs_new @ Rw.float()                                       # old = Rw^T new
    Y = sh_basis(dirs_new)                                                 # (K, 16)
    pinv = torch.linalg.pinv(Y.double()).float()                           # (16, K)
    base = (C0 * model.get_shs[keep][:, 0, :]).float() + 0.5               # (N, 3)
    sb = model.get_sb_params[keep].float()                                 # (N, K_lobes, 6)
    coeffs = torch.empty(base.shape[0], 16, 3, device="cuda")
    for s in range(0, base.shape[0], 20000):
        e = slice(s, s + 20000)
        th, ph = sb[e, :, 3], sb[e, :, 4]
        mu = torch.stack([torch.sin(th) * torch.cos(ph), torch.sin(th) * torch.sin(ph), torch.cos(th)], -1)
        dot = torch.einsum("kd,nld->nkl", dirs_old, mu).clamp(min=0)       # (n, K, lobes)
        lobe = dot ** (4 * torch.exp(sb[e, None, :, 5]))
        col = base[e, None, :] + torch.einsum("nkl,nlc->nkc", lobe, sb[e, :, :3])   # (n, K, 3)
        coeffs[e] = torch.einsum("jk,nkc->njc", pinv, col.clamp(min=0) - 0.5)
    fit_rmse = None
    with torch.no_grad():   # fit quality on a subsample
        idx = torch.arange(0, base.shape[0], max(1, base.shape[0] // 5000), device="cuda")
        th, ph = sb[idx, :, 3], sb[idx, :, 4]
        mu = torch.stack([torch.sin(th) * torch.cos(ph), torch.sin(th) * torch.sin(ph), torch.cos(th)], -1)
        lobe = torch.einsum("kd,nld->nkl", dirs_old, mu).clamp(min=0) ** (4 * torch.exp(sb[idx, None, :, 5]))
        col = (base[idx, None, :] + torch.einsum("nkl,nlc->nkc", lobe, sb[idx, :, :3])).clamp(min=0)
        approx = torch.einsum("kj,njc->nkc", Y, coeffs[idx]) + 0.5
        fit_rmse = float(torch.sqrt(((approx - col) ** 2).mean()))

    n = new_xyz.shape[0]
    f_dc = coeffs[:, 0, :]
    f_rest = coeffs[:, 1:, :].transpose(1, 2).reshape(n, 45)               # channel-major, 3DGS layout
    attrs = {"x": new_xyz[:, 0], "y": new_xyz[:, 1], "z": new_xyz[:, 2],
             "nx": torch.zeros(n), "ny": torch.zeros(n), "nz": torch.zeros(n)}
    attrs.update({f"f_dc_{i}": f_dc[:, i] for i in range(3)})
    attrs.update({f"f_rest_{i}": f_rest[:, i] for i in range(45)})
    attrs["opacity"] = model._opacity[keep].squeeze(-1)
    attrs.update({f"scale_{i}": torch.log(sigma[:, i]).float() for i in range(3)})
    attrs.update({f"rot_{i}": new_quat[:, i] for i in range(4)})
    data = np.empty(n, dtype=[(k, "f4") for k in attrs])
    for k, v in attrs.items():
        data[k] = v.detach().float().cpu().numpy()
    PlyData([PlyElement.describe(data, "vertex")]).write(str(out))
    return {"input": int(keep.numel()), "output": n, "removed_below_ground": int((~keep).sum()),
            "sh_fit_rmse": round(fit_rmse, 4), "bounds_min": new_xyz.min(0).values.tolist(),
            "bounds_max": new_xyz.max(0).values.tolist(), "mb": round(out.stat().st_size / 1e6, 1)}


def BetaModel_rotmats(q: torch.Tensor) -> torch.Tensor:
    w, x, y, z = (q / q.norm(dim=-1, keepdim=True)).unbind(-1)
    return torch.stack([1 - 2 * (y * y + z * z), 2 * (x * y - w * z), 2 * (x * z + w * y),
                        2 * (x * y + w * z), 1 - 2 * (x * x + z * z), 2 * (y * z - w * x),
                        2 * (x * z - w * y), 2 * (y * z + w * x), 1 - 2 * (x * x + y * y)], -1).reshape(-1, 3, 3)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ply", type=Path, required=True, help="Beta-splatting PLY (e.g. a splat-segment cut-out)")
    ap.add_argument("--cameras", type=Path, required=True, help="cameras.json from the training run")
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--ground-percentile", type=float, default=1.0)
    args = ap.parse_args()
    print(json.dumps(convert(args.ply, args.cameras, args.out, args.ground_percentile), indent=2))


if __name__ == "__main__":
    main()
