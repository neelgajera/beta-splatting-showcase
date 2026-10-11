"""Drop nearly transparent splats from a (beta-)splatting PLY to shrink it and speed up loading.

Usage: python tools/prune_ply.py <in.ply> <out.ply> [--min-opacity 0.02]
"""
import argparse

import numpy as np


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
                if s.split()[1] != "float":
                    raise ValueError(f"only float properties are supported: {s}")
                props.append(s.split()[2])
            elif s == "end_header":
                break
        data = np.fromfile(f, dtype="<f4", count=count * len(props)).reshape(count, len(props))
    return header, props, data


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("src")
    ap.add_argument("dst")
    ap.add_argument("--min-opacity", type=float, default=0.02)
    a = ap.parse_args()

    header, props, data = read_ply(a.src)
    opacity = 1.0 / (1.0 + np.exp(-data[:, props.index("opacity")]))
    keep = data[opacity >= a.min_opacity]
    header = [f"element vertex {len(keep)}\n" if h.startswith("element vertex") else h for h in header]
    with open(a.dst, "wb") as f:
        f.write("".join(header).encode("ascii"))
        keep.astype("<f4").tofile(f)
    print(f"{a.src}: {len(data)} -> {len(keep)} splats ({len(keep) / len(data):.0%}), min opacity {a.min_opacity}")


if __name__ == "__main__":
    main()
