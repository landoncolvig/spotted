"""Generate Spotted's app icon programmatically.

Outputs the full macOS iconset (uses iconutil to produce icon.icns) plus the
Tauri-required PNGs. Run from this directory:

    python3 _generate.py
"""
from __future__ import annotations

import math
import shutil
import subprocess
from pathlib import Path

try:
    from PIL import Image, ImageDraw, ImageFilter
except ImportError:
    raise SystemExit("Pillow required. Install: pip install pillow")


HERE = Path(__file__).resolve().parent
ICONSET = HERE / "spotted.iconset"

BG_INNER = (255, 178, 80, 255)   # #FFB250 — bright peach highlight
BG_OUTER = (232, 112, 20, 255)   # #E87014 — saturated vivid orange
FG = (255, 255, 255, 255)        # pure white


def squircle_mask(size: int) -> Image.Image:
    n = 5.0
    mask = Image.new("L", (size, size), 0)
    pixels = mask.load()
    cx = cy = (size - 1) / 2.0
    r = size / 2.0
    for y in range(size):
        for x in range(size):
            nx = (x - cx) / r
            ny = (y - cy) / r
            d = (abs(nx) ** n + abs(ny) ** n) ** (1.0 / n)
            if d <= 0.98:
                pixels[x, y] = 255
            elif d <= 1.0:
                pixels[x, y] = int(255 * (1.0 - (d - 0.98) / 0.02))
    return mask


def radial_gradient(size: int) -> Image.Image:
    """Small bright highlight upper-left, saturated orange dominates the rest."""
    img = Image.new("RGBA", (size, size), BG_OUTER)
    pixels = img.load()
    # Tight highlight in upper-left
    cx, cy = size * 0.33, size * 0.28
    # Larger max_d → faster transition to outer → orange dominates
    max_d = math.hypot(size * 1.1, size * 1.1)
    for y in range(size):
        for x in range(size):
            d = min(1.0, math.hypot(x - cx, y - cy) / max_d)
            # Ease-in (slow start, fast end) so the bright spot fades
            # quickly into the saturated orange body
            t = d
            r = int(BG_INNER[0] * (1 - t) + BG_OUTER[0] * t)
            g = int(BG_INNER[1] * (1 - t) + BG_OUTER[1] * t)
            b = int(BG_INNER[2] * (1 - t) + BG_OUTER[2] * t)
            pixels[x, y] = (r, g, b, 255)
    return img


def draw_brackets(draw: ImageDraw.ImageDraw, size: int) -> None:
    inset = size * 0.19
    arm = size * 0.20
    thickness = max(3, int(size * 0.055))  # thicker for presence
    color = FG
    tl = (inset, inset)
    draw.line([tl, (tl[0] + arm, tl[1])], fill=color, width=thickness)
    draw.line([tl, (tl[0], tl[1] + arm)], fill=color, width=thickness)
    tr = (size - inset, inset)
    draw.line([tr, (tr[0] - arm, tr[1])], fill=color, width=thickness)
    draw.line([tr, (tr[0], tr[1] + arm)], fill=color, width=thickness)
    bl = (inset, size - inset)
    draw.line([bl, (bl[0] + arm, bl[1])], fill=color, width=thickness)
    draw.line([bl, (bl[0], bl[1] - arm)], fill=color, width=thickness)
    br = (size - inset, size - inset)
    draw.line([br, (br[0] - arm, br[1])], fill=color, width=thickness)
    draw.line([br, (br[0], br[1] - arm)], fill=color, width=thickness)


def draw_center_dot(draw: ImageDraw.ImageDraw, size: int) -> None:
    r = size * 0.09  # slightly bigger
    cx = cy = size / 2.0
    draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=FG)


def render(size: int) -> Image.Image:
    s = size * 2
    bg = radial_gradient(s)
    canvas = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    mask = squircle_mask(s)
    canvas.paste(bg, (0, 0), mask)

    marks = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(marks)
    draw_brackets(d, s)
    draw_center_dot(d, s)
    canvas = Image.alpha_composite(canvas, marks)
    # No inner shadow — it muddied the foreground. The flat punchy look
    # reads brighter in dark mode anyway.

    return canvas.resize((size, size), Image.Resampling.LANCZOS)


def make_iconset() -> None:
    if ICONSET.exists():
        shutil.rmtree(ICONSET)
    ICONSET.mkdir(parents=True)
    sizes = [
        (16, "icon_16x16.png"),
        (32, "icon_16x16@2x.png"),
        (32, "icon_32x32.png"),
        (64, "icon_32x32@2x.png"),
        (128, "icon_128x128.png"),
        (256, "icon_128x128@2x.png"),
        (256, "icon_256x256.png"),
        (512, "icon_256x256@2x.png"),
        (512, "icon_512x512.png"),
        (1024, "icon_512x512@2x.png"),
    ]
    for size, name in sizes:
        img = render(size)
        img.save(ICONSET / name, "PNG", optimize=True)
        print(f"  {name} ({size}x{size})")


def make_icns() -> None:
    out = HERE / "icon.icns"
    subprocess.run(
        ["iconutil", "-c", "icns", str(ICONSET), "-o", str(out)],
        check=True,
    )
    print(f"  icon.icns  ({out.stat().st_size:,} bytes)")


def make_tauri_pngs() -> None:
    targets = {
        "32x32.png": 32,
        "128x128.png": 128,
        "128x128@2x.png": 256,
        "icon.png": 1024,
    }
    for name, size in targets.items():
        out = HERE / name
        render(size).save(out, "PNG", optimize=True)
        print(f"  {name} ({size}x{size})")


def main() -> None:
    print("Spotted icon — generating…")
    make_iconset()
    make_icns()
    make_tauri_pngs()
    print("Done.")


if __name__ == "__main__":
    main()
