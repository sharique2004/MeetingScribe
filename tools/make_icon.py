"""Generate MeetingScribe.icns — a microphone glyph on a rounded square.

Drawn procedurally with numpy signed-distance fields and written as PNG with
a minimal encoder (no Pillow), then converted to .icns with sips/iconutil.

Usage:  python tools/make_icon.py <output.icns>
"""

import struct
import subprocess
import sys
import tempfile
import zlib
from pathlib import Path

import numpy as np

SIZE = 1024


def write_png(path, rgba):
    """Minimal RGBA PNG writer."""
    h, w, _ = rgba.shape
    raw = b"".join(b"\x00" + rgba[y].tobytes() for y in range(h))

    def chunk(tag, data):
        body = tag + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    png = (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw, 9))
        + chunk(b"IEND", b"")
    )
    Path(path).write_bytes(png)


def rounded_rect_sdf(xx, yy, cx, cy, half_w, half_h, radius):
    dx = np.abs(xx - cx) - (half_w - radius)
    dy = np.abs(yy - cy) - (half_h - radius)
    outside = np.hypot(np.maximum(dx, 0), np.maximum(dy, 0))
    inside = np.minimum(np.maximum(dx, dy), 0)
    return outside + inside - radius


def coverage(sdf, soft=1.5):
    """SDF -> anti-aliased alpha in [0,1]."""
    return np.clip(0.5 - sdf / (2 * soft), 0.0, 1.0)


def over(base, color, alpha):
    """Composite a flat colour over base with per-pixel alpha (RGB and the
    alpha channel are both kept in 0-255)."""
    for c in range(3):
        base[..., c] = base[..., c] * (1 - alpha) + color[c] * alpha
    base[..., 3] = np.maximum(base[..., 3], alpha * 255)
    return base


def main(out_path):
    y, x = np.mgrid[0:SIZE, 0:SIZE].astype(np.float64)
    img = np.zeros((SIZE, SIZE, 4), dtype=np.float64)

    # --- rounded-square background, vertical indigo gradient ---------------
    # macOS icon grid: content square is ~82% of the canvas.
    pad = SIZE * 0.09
    bg = coverage(rounded_rect_sdf(x, y, SIZE / 2, SIZE / 2,
                                   SIZE / 2 - pad, SIZE / 2 - pad, SIZE * 0.185))
    t = np.clip((y - pad) / (SIZE - 2 * pad), 0, 1)
    grad = np.stack(
        [(38 + (13 - 38) * t), (52 + (17 - 52) * t), (84 + (23 - 84) * t)], axis=-1
    )  # #263454 -> #0d1117
    for c in range(3):
        img[..., c] = grad[..., c] * bg
    img[..., 3] = bg * 255

    # soft inner glow behind the mic
    glow_d = np.hypot(x - SIZE / 2, y - SIZE * 0.44) - SIZE * 0.05
    glow = np.clip(0.5 - glow_d / (SIZE * 0.46), 0, 1) ** 2 * 0.55 * bg
    over(img, (43, 80, 124), glow)

    blue = (88, 166, 255)   # app accent #58a6ff
    white = (236, 244, 252)

    # --- microphone capsule -------------------------------------------------
    capsule = coverage(rounded_rect_sdf(x, y, SIZE / 2, SIZE * 0.40,
                                        SIZE * 0.105, SIZE * 0.20, SIZE * 0.105))
    over(img, white, capsule * bg)
    # capsule grill lines
    for gy in (0.335, 0.40, 0.465):
        line = coverage(rounded_rect_sdf(x, y, SIZE / 2, SIZE * gy,
                                         SIZE * 0.062, SIZE * 0.008, SIZE * 0.008))
        over(img, (134, 178, 226), line * capsule)

    # --- holder arc: ring clipped below the capsule centre ------------------
    ring_d = np.abs(np.hypot(x - SIZE / 2, y - SIZE * 0.46) - SIZE * 0.185) - SIZE * 0.030
    ring = coverage(ring_d) * (y > SIZE * 0.46) * bg
    over(img, blue, ring)

    # --- stem and base -------------------------------------------------------
    stem = coverage(rounded_rect_sdf(x, y, SIZE / 2, SIZE * 0.70,
                                     SIZE * 0.024, SIZE * 0.055, SIZE * 0.024))
    over(img, blue, stem * bg)
    base = coverage(rounded_rect_sdf(x, y, SIZE / 2, SIZE * 0.775,
                                     SIZE * 0.105, SIZE * 0.024, SIZE * 0.024))
    over(img, blue, base * bg)

    rgba = np.clip(img, 0, 255).astype(np.uint8)

    with tempfile.TemporaryDirectory() as tmp:
        iconset = Path(tmp) / "MeetingScribe.iconset"
        iconset.mkdir()
        master = Path(tmp) / "master.png"
        write_png(master, rgba)
        for px in (16, 32, 64, 128, 256, 512, 1024):
            for scale, suffix in ((1, ""), (2, "@2x")):
                target = px * scale
                if target > 1024:
                    continue
                name = iconset / f"icon_{px}x{px}{suffix}.png"
                subprocess.run(
                    ["sips", "-z", str(target), str(target), str(master),
                     "--out", str(name)],
                    check=True, capture_output=True,
                )
        subprocess.run(["iconutil", "-c", "icns", str(iconset), "-o", out_path], check=True)
    print(f"wrote {out_path}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "MeetingScribe.icns")
