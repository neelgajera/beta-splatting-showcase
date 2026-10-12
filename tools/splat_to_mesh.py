"""Extract a closed, vertex-coloured proxy mesh from a Beta-splatting model, in the same upright frame as
tools/beta_to_3dgs.py, for shadows and reflections in the studio.

    python tools/splat_to_mesh.py --ply object.ply --cameras cameras.json --out car_proxy.glb

Splats make poor shadow casters and reflection sources: they cast shadows through an alpha-clipped pass, so thin
or semi-transparent regions let light through, and sparse areas leave gaps. A mesh doesn't have that problem:

  1. Render colour + expected depth of the splat model from every training camera, plus extra orbits at 45 and
     75 degrees elevation so the roof is covered.
  2. Back-project the opaque pixels to coloured points with normals facing their camera, and reconstruct the
     surface with screened Poisson (Open3D); trim low-density vertices, keep the largest component.
  3. Decimate (quadric), move into the studio frame (cameras' up = +Y, ground at y = 0) and save as GLB with
     vertex colours.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import numpy as np
import open3d as o3d
import torch
import trimesh

sys.path.insert(0, str(Path(__file__).resolve().parent))
from beta_to_3dgs import world_frame  # noqa: E402


def look_at(pos: np.ndarray, target: np.ndarray, up: np.ndarray) -> np.ndarray:
    """camera-to-world rotation (OpenCV: x right, y down, z forward)."""
    z = target - pos
    z /= np.linalg.norm(z)
    x = np.cross(z, up)
    x /= np.linalg.norm(x)
    return np.stack([x, np.cross(z, x), z], axis=1)


def views(cams: list, centre: np.ndarray, up: np.ndarray, extra_per_ring: int = 24) -> list:
    """(c2w rotation, position, fx, fy, w, h) for the training cameras plus higher orbits."""
    out = [(np.array(c["rotation"]), np.array(c["position"]), c["fx"], c["fy"], c["width"], c["height"]) for c in cams]
    radius = float(np.median([np.linalg.norm(np.array(c["position"]) - centre) for c in cams]))
    ref = cams[0]
    e1 = np.cross(up, [1.0, 0, 0]) if abs(up[0]) < 0.9 else np.cross(up, [0, 0, 1.0])
    e1 /= np.linalg.norm(e1)
    e2 = np.cross(up, e1)
    for elev in (45, 75):
        for k in range(extra_per_ring):
            a = 2 * np.pi * k / extra_per_ring
            el = np.deg2rad(elev)
            pos = centre + radius * (np.cos(el) * (np.cos(a) * e1 + np.sin(a) * e2) + np.sin(el) * up)
            out.append((look_at(pos, centre, up), pos, ref["fx"], ref["fy"], ref["width"], ref["height"]))
    return out


@torch.no_grad()
def render_rgbd(model, c2w, pos, fx, fy, w, h):
    from gsplat.rendering import rasterization

    R = torch.tensor(c2w.T, dtype=torch.float32, device="cuda")            # world -> camera
    t = -R @ torch.tensor(pos, dtype=torch.float32, device="cuda")
    view = torch.eye(4, device="cuda")
    view[:3, :3], view[:3, 3] = R, t
    K = torch.tensor([[fx, 0, w / 2], [0, fy, h / 2], [0, 0, 1]], dtype=torch.float32, device="cuda")
    out, alpha, _ = rasterization(
        means=model.get_xyz, quats=model.get_rotation, scales=model.get_scaling,
        opacities=model.get_opacity.squeeze(-1), betas=model.get_beta.squeeze(-1), colors=model.get_shs,
        viewmats=view[None], Ks=K[None], width=w, height=h, backgrounds=None,
        sh_degree=0, sb_number=model.sb_number, sb_params=model.get_sb_params, packed=False, render_mode="RGB+ED")
    rgb = (out[0, ..., :3].clamp(0, 1) * 255).byte().cpu().numpy()
    depth = out[0, ..., 3].cpu().numpy().astype(np.float32)
    depth[alpha[0, ..., 0].cpu().numpy() < 0.6] = 0.0                       # fuse opaque pixels only
    return rgb, depth, view.cpu().numpy()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ply", type=Path, required=True, help="Beta-splatting PLY of the object (cut out)")
    ap.add_argument("--cameras", type=Path, required=True, help="cameras.json from the training run")
    ap.add_argument("--out", type=Path, required=True, help="output .glb")
    ap.add_argument("--scale", type=float, default=0.5, help="render resolution relative to the training images")
    ap.add_argument("--voxel", type=float, default=0.008, help="TSDF voxel size in model units")
    ap.add_argument("--triangles", type=int, default=40000)
    ap.add_argument("--poisson-depth", type=int, default=9)
    ap.add_argument("--ground-percentile", type=float, default=1.0)
    args = ap.parse_args()

    sys.path.insert(0, os.environ.get("BETA_SPLATTING", "third_party/beta-splatting"))
    from scene.beta_model import BetaModel

    model = BetaModel(sh_degree=0, sb_number=2)
    model.load_ply(str(args.ply))
    xyz = model.get_xyz.detach().cpu().numpy()
    Rw, origin = world_frame(xyz.astype(np.float64), args.cameras, args.ground_percentile)
    up = Rw[1]
    centre = origin + up * 0.5 * float(np.percentile((xyz - origin) @ up, 95))
    cams = json.loads(args.cameras.read_text())

    # Back-project every opaque pixel to a coloured point, with its normal pointing at the camera that saw it,
    # then reconstruct a watertight surface with screened Poisson. (Open3D's TSDF integration produced empty
    # volumes on this build, while back-projection works, so Poisson is used.)
    cloud = o3d.geometry.PointCloud()
    vs = views(cams, centre, up)
    for c2w, pos, fx, fy, w, h in vs:
        w2, h2 = int(w * args.scale), int(h * args.scale)
        rgb, depth, extr = render_rgbd(model, c2w, pos, fx * args.scale, fy * args.scale, w2, h2)
        rgbd = o3d.geometry.RGBDImage.create_from_color_and_depth(
            o3d.geometry.Image(np.ascontiguousarray(rgb)), o3d.geometry.Image(np.ascontiguousarray(depth)),
            depth_scale=1.0, depth_trunc=1e3, convert_rgb_to_intensity=False)
        intr = o3d.camera.PinholeCameraIntrinsic(w2, h2, fx * args.scale, fy * args.scale, w2 / 2, h2 / 2)
        pc = o3d.geometry.PointCloud.create_from_rgbd_image(rgbd, intr, extr.astype(np.float64))
        pc = pc.voxel_down_sample(args.voxel)
        pc.estimate_normals(o3d.geometry.KDTreeSearchParamHybrid(radius=4 * args.voxel, max_nn=30))
        pc.orient_normals_towards_camera_location(np.asarray(pos, dtype=np.float64))
        cloud += pc
    cloud = cloud.voxel_down_sample(args.voxel)
    cloud, _ = cloud.remove_statistical_outlier(nb_neighbors=20, std_ratio=2.0)
    mesh, density = o3d.geometry.TriangleMesh.create_from_point_cloud_poisson(cloud, depth=args.poisson_depth)
    density = np.asarray(density)
    mesh.remove_vertices_by_mask(density < np.quantile(density, 0.03))   # Poisson's invented far surface
    clusters, counts, _ = mesh.cluster_connected_triangles()
    clusters, counts = np.asarray(clusters), np.asarray(counts)
    mesh.remove_triangles_by_mask(clusters != counts.argmax())
    mesh.remove_unreferenced_vertices()
    raw = len(mesh.triangles)
    mesh = mesh.simplify_quadric_decimation(args.triangles)
    mesh = mesh.filter_smooth_taubin(5)
    mesh.compute_vertex_normals()

    v = (np.asarray(mesh.vertices) - origin) @ Rw.T                     # studio frame
    v[:, 1] = np.maximum(v[:, 1], 0.0)
    colors = (np.clip(np.asarray(mesh.vertex_colors), 0, 1) * 255).astype(np.uint8)
    tm = trimesh.Trimesh(v, np.asarray(mesh.triangles), vertex_colors=np.c_[colors, np.full(len(colors), 255)],
                         process=False)
    tm.export(args.out)
    print(json.dumps({"views": len(vs), "raw_triangles": raw, "triangles": len(tm.faces),
                      "watertight": bool(tm.is_watertight), "bounds": tm.bounds.round(3).tolist(),
                      "kb": round(args.out.stat().st_size / 1024)}, indent=2))


if __name__ == "__main__":
    main()
