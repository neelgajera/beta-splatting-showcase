"""Tame view-dependent color artifacts in a beta-splatting PLY.

Spherical-beta lobes (sb_params_*) can extrapolate badly at viewpoints the training cameras
never saw: a gray ground splat turns saturated blue when the tour camera passes slightly above
the capture path, because a lobe drives one color channel far below zero. This finds splats
whose color goes clearly more negative from the tour viewpoints than from any training camera,
and scales down only their lobe colors until it stays (almost) non-negative along the tour.
Bright highlights (channels above 1) are left alone. Positions, shapes, opacity and base color
are untouched.

Usage:
  python tools/fix_view_artifacts.py --ply in.ply --cameras cameras.json --config configs/x.json --out out.ply
"""
import argparse
import json

import numpy as np

SH_C0 = 0.28209479177387814
FACTORS = (1.0, 0.8, 0.6, 0.4, 0.2, 0.0)
CHUNK = 100_000


def read_ply(path):
    with open(path, "rb") as f:
        header, props, count = [], [], 0
        while True:
            line = f.readline().decode("ascii")
            header.append(line)
            s = line.strip()
            if s.startswith("element vertex"):
                count = int(s.split()[-1])
            elif s.startswith("property"):
                props.append(s.split()[2])
            elif s == "end_header":
                break
        data = np.fromfile(f, dtype="<f4", count=count * len(props)).reshape(count, len(props))
    return header, props, data


class Lobes:
    """Evaluates color = base + sum_l rgb_l * max(0, view . dir_l)^(4 e^beta_l), as the viewer shader does.

    sb_params are interleaved per lobe: [r0, r1, g0, g1, b0, b1, theta0, theta1, phi0, phi1, beta0, beta1].
    """

    def __init__(self, xyz, base, sb):
        self.xyz, self.base = xyz, base
        self.lobes = []
        for l in (0, 1):
            th, ph = sb[:, 6 + l], sb[:, 8 + l]
            direction = np.stack([np.sin(th) * np.cos(ph), np.sin(th) * np.sin(ph), np.cos(th)], 1)
            self.lobes.append((direction, sb[:, [0 + l, 2 + l, 4 + l]], 4 * np.exp(sb[:, 10 + l])))

    def worst_negative(self, eyes, scale=1.0):
        """Largest amount any color channel drops below 0 over the given eye positions."""
        worst = np.zeros(len(self.xyz), np.float32)
        for eye in eyes:
            view = self.xyz - eye
            view /= np.linalg.norm(view, axis=1, keepdims=True)
            c = self.base.copy()
            for direction, rgb, exponent in self.lobes:
                dp = (view * direction).sum(1)
                c += scale * rgb * np.where(dp > 0, np.abs(dp) ** exponent, 0)[:, None]
            np.maximum(worst, np.maximum(0, -c.min(1)), out=worst)
        return worst


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ply", required=True)
    ap.add_argument("--cameras", required=True, help="training cameras.json")
    ap.add_argument("--config", required=True, help="scene config with camera_path (compute_scene_config.py)")
    ap.add_argument("--out", required=True)
    ap.add_argument("--tolerance", type=float, default=0.05,
                    help="negative overshoot allowed along the tour, and margin over training views before acting")
    ap.add_argument("--min-opacity", type=float, default=0.2, help="ignore fainter splats")
    a = ap.parse_args()

    header, props, data = read_ply(a.ply)
    P = {p: i for i, p in enumerate(props)}
    xyz = data[:, [P["x"], P["y"], P["z"]]].astype(np.float64)
    base = np.clip(0.5 + SH_C0 * data[:, [P["sh0_0"], P["sh0_1"], P["sh0_2"]]], 0, 1)
    sb_cols = [P[f"sb_params_{k}"] for k in range(12)]
    opacity = 1 / (1 + np.exp(-data[:, P["opacity"]]))

    cfg = json.load(open(a.config))
    train_eyes = np.array([c["position"] for c in json.load(open(a.cameras))])
    up = np.array(cfg["camera_up"])
    center = np.array(cfg["orbit_center"])
    keys = np.array(cfg["camera_path"]["keyframes"])
    # Tour viewpoints, including the "lift" range the viewer offers.
    tour_eyes = [k + up * np.linalg.norm(k - center) * lift for k in keys for lift in (0.0, 0.08, 0.2, 0.4)]

    candidates = np.where(opacity > a.min_opacity)[0]
    lobe_scale = np.ones(len(data), np.float32)
    for i in range(0, len(candidates), CHUNK):
        idx = candidates[i:i + CHUNK]
        lobes = Lobes(xyz[idx], base[idx], data[idx][:, sb_cols].astype(np.float64))
        train = lobes.worst_negative(train_eyes)
        bad = lobes.worst_negative(tour_eyes) > np.maximum(train, 0) + a.tolerance
        if not bad.any():
            continue
        sub = Lobes(xyz[idx][bad], base[idx][bad], data[idx][bad][:, sb_cols].astype(np.float64))
        chosen = np.zeros(bad.sum(), np.float32)
        undecided = np.ones(bad.sum(), bool)
        for f in FACTORS[1:]:  # largest damping factor that brings the tour views back in line
            ok = sub.worst_negative(tour_eyes, f) <= a.tolerance
            pick = undecided & ok
            chosen[pick] = f
            undecided &= ~ok
        lobe_scale[idx[bad]] = chosen

    fixed = lobe_scale < 1
    rgb_cols = [P[f"sb_params_{k}"] for k in range(6)]  # r0 r1 g0 g1 b0 b1
    data[:, rgb_cols] *= lobe_scale[:, None]
    with open(a.out, "wb") as f:
        f.write("".join(header).encode("ascii"))
        data.astype("<f4").tofile(f)
    print(f"{a.ply}: damped view-dependent color on {fixed.sum()} of {len(data)} splats "
          f"({fixed.mean():.2%}); scale histogram { {f: int((lobe_scale == f).sum()) for f in FACTORS[1:]} }")


if __name__ == "__main__":
    main()
