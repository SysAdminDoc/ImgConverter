#!/usr/bin/env python3
"""Build deterministic ImgConverter PNG and Windows ICO branding assets."""

from __future__ import annotations

import argparse
from pathlib import Path

from PIL import Image, ImageDraw


ROOT = Path(__file__).resolve().parents[1]
INDIGO = (108, 124, 255)
MIDNIGHT = (8, 16, 30)
MASTER_SIZE = 1024
ICO_SIZES = (16, 20, 24, 32, 40, 48, 64, 128, 256)


def _center_square(image: Image.Image) -> Image.Image:
    edge = min(image.size)
    left = (image.width - edge) // 2
    top = (image.height - edge) // 2
    return image.crop((left, top, left + edge, top + edge))


def _flatten_brand_colors(image: Image.Image) -> Image.Image:
    """Reduce a keyed ImageGen concept to the exact two-color brand palette."""
    source = image.convert("RGBA")
    flattened = Image.new("RGBA", source.size, (0, 0, 0, 0))
    output = []
    for red, green, blue, alpha in source.get_flattened_data():
        if alpha == 0:
            output.append((0, 0, 0, 0))
            continue
        luminance = 0.2126 * red + 0.7152 * green + 0.0722 * blue
        color = MIDNIGHT if luminance < 70 else INDIGO
        output.append((*color, alpha))
    flattened.putdata(output)
    return flattened


def build_assets(source_path: Path, png_path: Path, ico_path: Path) -> Image.Image:
    with Image.open(source_path) as source:
        square = _center_square(source)
        flattened = _flatten_brand_colors(square)
    master = flattened.resize(
        (MASTER_SIZE, MASTER_SIZE),
        Image.Resampling.LANCZOS,
        reducing_gap=3.0,
    )
    png_path.parent.mkdir(parents=True, exist_ok=True)
    ico_path.parent.mkdir(parents=True, exist_ok=True)
    master.save(png_path, format="PNG", optimize=True)
    master.save(
        ico_path,
        format="ICO",
        sizes=[(size, size) for size in ICO_SIZES],
        bitmap_format="png",
    )
    return master


def build_preview(master: Image.Image, preview_path: Path) -> None:
    """Render native and pixel-magnified samples on light and dark surfaces."""
    canvas = Image.new("RGB", (1200, 560), "#f3f6fb")
    draw = ImageDraw.Draw(canvas)
    draw.rectangle((0, 0, canvas.width, 280), fill="#08101e")
    sizes = (16, 24, 32, 48, 64, 128)
    for index, size in enumerate(sizes):
        x = 36 + index * 190
        native = master.resize((size, size), Image.Resampling.LANCZOS)
        canvas.paste(native, (x + (128 - size) // 2, 56), native)
        draw.text((x, 204), f"{size}px native", fill="#eaf0ff")
        if size <= 48:
            magnified = native.resize((size * 4, size * 4), Image.Resampling.NEAREST)
            canvas.paste(magnified, (x + (128 - size * 4) // 2, 320), magnified)
            draw.text((x, 510), f"{size}px raster at 4x", fill="#08101e")
        else:
            canvas.paste(native, (x + (128 - size) // 2, 352), native)
            draw.text((x, 510), f"{size}px native", fill="#08101e")
    preview_path.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(preview_path, format="PNG", optimize=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path, help="transparent ImageGen source PNG")
    parser.add_argument("--png", type=Path, default=ROOT / "icon.png")
    parser.add_argument("--ico", type=Path, default=ROOT / "icon.ico")
    parser.add_argument("--preview", type=Path)
    args = parser.parse_args()

    master = build_assets(
        args.source.expanduser().resolve(),
        args.png.expanduser().resolve(),
        args.ico.expanduser().resolve(),
    )
    if args.preview:
        build_preview(master, args.preview.expanduser().resolve())
    print(f"Wrote {args.png}")
    print(f"Wrote {args.ico}")
    if args.preview:
        print(f"Wrote {args.preview}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
