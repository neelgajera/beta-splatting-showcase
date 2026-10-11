"""Compute a viewer config (up vector, orbit center, camera ring, tour path) for a splat model.

With training cameras (best):
  python tools/compute_scene_config.py --ply model.ply --cameras cameras.json --out configs/x.json
Without cameras (manual pose; a virtual camera ring through --start around --look):
  python tools/compute_scene_config.py --ply model.ply --up 0,0,1 --start 0.9,3.3,0.3 --out configs/x.json
"""
import argparse
import json

import numpy as np


def read_ply_xyz_opacity(path):
    """Read x,y,z and opacity from a binary little-endian float32 PLY."""
    with open(path, "rb") as f:
        props = []
        count = 0
        while True:
            line = f.readline().decode("ascii").strip()
            if line.startswith("element vertex"):
                count = int(line.split()[-1])
            elif line.startswith("property"):
                dtype, name = line.split()[1:3]
                if dtype != "float":
                    raise ValueError(f"unsupported property type {dtype} for {name}")
                props.append(name)
            elif line == "end_header":
                break
        data = np.fromfile(f, dtype="<f4", count=count * len(props)).reshape(count, len(props))
    xyz = data[:, [props.index("x"), props.index("y"), props.index("z")]]
    opacity = 1.0 / (1.0 + np.exp(-data[:, props.index("opacity")]))
    return xyz, opacity


def nearest_point_to_rays(origins, dirs):
    """Least-squares point closest to all camera viewing rays (the look-at center)."""
    A = np.zeros((3, 3))
    b = np.zeros(3)
    for o, d in zip(origins, dirs):
        P = np.eye(3) - np.outer(d, d)
        A += P
        b += P @ o
    return np.linalg.lstsq(A, b, rcond=None)[0]


def smooth_camera_loop(pos, focus, up, n_keys=72, window_deg=40.0):
    """Closed, smoothed path through the training cameras, sampled at even azimuth steps.

    Radius and height are averaged over neighbouring cameras (Gaussian in azimuth) so the
    path follows the real capture oval without the hand-held jitter. Azimuth gaps (partial
    orbits) are not handled; callers check coverage first.
    """
    rel = pos - focus
    heights = rel @ up
    planar = rel - np.outer(heights, up)
    e1 = planar[0] / np.linalg.norm(planar[0])
    e2 = np.cross(up, e1)
    az = np.arctan2(planar @ e2, planar @ e1)
    radius = np.linalg.norm(planar, axis=1)

    # Keep the capture's direction of travel.
    direction = 1.0 if np.mean(np.sin(np.diff(np.unwrap(az)))) >= 0 else -1.0
    sigma = np.radians(window_deg) / 2
    keys = []
    for t in np.linspace(0, 2 * np.pi, n_keys, endpoint=False):
        a = az[0] + direction * t
        diff = np.angle(np.exp(1j * (az - a)))
        w = np.exp(-0.5 * (diff / sigma) ** 2)
        r = float((w * radius).sum() / w.sum())
        h = float((w * heights).sum() / w.sum())
        p = focus + up * h + r * (np.cos(a) * e1 + np.sin(a) * e2)
        keys.append(p.tolist())
    s = np.sort(az % (2 * np.pi))
    max_gap = float(np.degrees(np.diff(np.r_[s, s[0] + 2 * np.pi]).max()))
    return keys, max_gap


def virtual_ring(up, look, start, n=72):
    """Cameras on a circle through `start` around `look` (for models without cameras.json)."""
    up = np.asarray(up, float) / np.linalg.norm(up)
    look, start = np.asarray(look, float), np.asarray(start, float)
    rel = start - look
    h = rel @ up
    planar = rel - h * up
    r = np.linalg.norm(planar)
    e1 = planar / r
    e2 = np.cross(up, e1)
    pos = np.array([look + h * up + r * (np.cos(a) * e1 + np.sin(a) * e2)
                    for a in np.linspace(0, 2 * np.pi, n, endpoint=False)])
    forward = look - pos
    forward /= np.linalg.norm(forward, axis=1, keepdims=True)
    return pos, forward, up


def vec3(s):
    v = [float(x) for x in s.split(",")]
    if len(v) != 3:
        raise argparse.ArgumentTypeError("expected x,y,z")
    return v


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ply", required=True, help="model PLY")
    ap.add_argument("--cameras", help="training cameras.json (3DGS / beta-splatting output)")
    ap.add_argument("--up", type=vec3, help="manual mode: world up, e.g. 0,0,1")
    ap.add_argument("--start", type=vec3, help="manual mode: start camera position")
    ap.add_argument("--look", type=vec3, default=[0, 0, 0], help="manual mode: look-at point (orbit center)")
    ap.add_argument("--auto-center", action="store_true",
                    help="manual mode: re-estimate the subject center from splats (works for isolated objects)")
    ap.add_argument("--out", default="scene_config.json")
    a = ap.parse_args()

    if a.cameras:
        cams = json.load(open(a.cameras))
        pos = np.array([c["position"] for c in cams])
        rot = np.array([c["rotation"] for c in cams])  # camera-to-world rotation, OpenCV axes
        # Up: cameras' -Y axis averaged (OpenCV y points down).
        up = -rot[:, :, 1].mean(axis=0)
        up /= np.linalg.norm(up)
        forward = rot[:, :, 2]
        source = "cameras"
    elif a.up and a.start:
        pos, forward, up = virtual_ring(a.up, a.look, a.start)
        source = "manual"
    else:
        ap.error("pass --cameras, or --up and --start (manual mode)")

    cam_mid = pos.mean(axis=0)
    focus = nearest_point_to_rays(pos, forward)
    # If cameras look mostly in parallel (forward-facing capture) the ray solution is
    # unstable; fall back to a point in front of the cameras.
    spread = np.linalg.norm(pos - cam_mid, axis=1).max()
    mean_fwd = forward.mean(axis=0)
    orbit_capture = np.linalg.norm(mean_fwd) < 0.5
    if not orbit_capture or np.linalg.norm(focus - cam_mid) > 50 * max(spread, 1e-6):
        mean_fwd /= np.linalg.norm(mean_fwd)
        focus = cam_mid + mean_fwd * max(spread * 3, 1.0)

    # Torus around the camera path, as in 3d-wizard: ring radius in the plane
    # perpendicular to up, tube radius = ring / 4, center lifted to the camera height.
    rel = pos - focus
    heights = rel @ up
    planar = rel - np.outer(heights, up)
    ring_radius = float(np.linalg.norm(planar, axis=1).max())
    cam_dist = np.linalg.norm(rel, axis=1)

    # Splats: keep reasonably opaque ones and take robust bounds near the focus.
    xyz, opacity = read_ply_xyz_opacity(a.ply)
    solid = xyz[opacity > 0.5]
    d = np.linalg.norm(solid - focus, axis=1)
    content_radius = float(np.percentile(d, 50))

    # Subject (e.g. the car) center: bbox middle of opaque, above-ground splats inside the
    # camera ring. Cameras usually aim at roughly the right spot horizontally but sit above
    # the subject, so this mainly corrects the look-at height.
    rel_s = solid - focus
    h_s = rel_s @ up
    r_s = np.linalg.norm(rel_s - np.outer(h_s, up), axis=1)
    inner = r_s < 0.6 * float(np.linalg.norm(planar, axis=1).min())
    subject_center = focus
    subject_extent = None
    use_auto_center = source == "cameras" or a.auto_center
    if use_auto_center and inner.sum() > 1000:
        ground = np.percentile(h_s[inner], 2)
        body = rel_s[inner & (h_s > ground + 0.03 * ring_radius)]
        lo, hi = np.percentile(body, 5, axis=0), np.percentile(body, 95, axis=0)
        subject_center = focus + (lo + hi) / 2
        subject_extent = (hi - lo).tolist()

    # Start view: the first (training) camera, so the opening frame matches a real photo.
    start_pos = pos[0]
    start_look = start_pos + forward[0] * float(np.linalg.norm(focus - start_pos))

    path_keys, max_gap = smooth_camera_loop(pos, focus, up)

    config = {
        "camera_source": source,
        "num_cameras": len(pos),
        "num_splats": int(len(xyz)),
        "capture_type": "orbit" if orbit_capture else "forward_facing",
        "camera_up": up.tolist(),
        "camera_focus": focus.tolist(),
        "orbit_center": subject_center.tolist(),
        "subject_extent": subject_extent,
        "initial_camera_position": start_pos.tolist(),
        "initial_camera_look_at": start_look.tolist(),
        "torus_config": {
            "camera_center": (focus + up * float(heights.mean())).tolist(),
            "torus_inner_radius": ring_radius,
            "torus_ring_radius": ring_radius / 4,
        },
        "camera_distance": {"min": float(cam_dist.min()), "max": float(cam_dist.max()),
                            "mean": float(cam_dist.mean())},
        "camera_height_range": [float(heights.min()), float(heights.max())],
        "camera_path": {
            "closed": max_gap < 30.0,
            "max_azimuth_gap_deg": max_gap,
            "keyframes": path_keys,
        },
        "content_radius_p50": content_radius,
        "splat_bounds_p1_p99": {
            "min": np.percentile(solid, 1, axis=0).tolist(),
            "max": np.percentile(solid, 99, axis=0).tolist(),
        },
    }
    with open(a.out, "w") as f:
        json.dump(config, f, indent=2)
    print(f"wrote {a.out}: {config['num_splats']} splats, center {np.round(subject_center, 3).tolist()}, "
          f"ring r={ring_radius:.3f}, path closed={config['camera_path']['closed']}")


if __name__ == "__main__":
    main()
