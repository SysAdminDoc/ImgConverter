"""Regression test: EXIF orientation must be baked into pixels exactly once.

Background: HEIC stores orientation in two places (irot box AND EXIF tag).
Naive converters apply rotation twice — see ImageMagick #1232, sharp #4384,
Geeqie #923, Pillow #9294. ImgConverter uses ImageOps.exif_transpose() once and
must end up with output pixels at displayed-orientation + no remaining
EXIF Orientation tag.
"""
import pytest
from PIL import Image

from imgconverter import convert_file


def _build_tagged_jpeg(path, orientation_value: int):
    """Save a 200x100 RGB JPEG with EXIF Orientation tag set to ``orientation_value``."""
    # Pillow's Image.Exif is the simplest authoring API; load->set->save round-trip works.
    img = Image.new("RGB", (200, 100), (50, 200, 150))
    exif = img.getexif()
    exif[0x0112] = orientation_value  # Orientation
    img.save(path, "JPEG", exif=exif.tobytes(), quality=92)


def test_orientation_baked_once_no_double_rotation(tmp_workdir):
    """EXIF orientation=6 (rotate 90 CW) -> output dims swap once, tag cleared."""
    src = tmp_workdir / "rot.jpg"
    _build_tagged_jpeg(src, orientation_value=6)
    out_dir = tmp_workdir / "out"

    result = convert_file(src, out_dir, fmt="png")
    assert result.success

    with Image.open(result.dst) as out:
        out.load()
        # Orientation 6 = 90° CW. Source 200x100 -> after one rotation -> 100x200.
        # If we rotate twice we'd be back to 200x100.
        assert out.size == (100, 200), (
            f"orientation handling broken: expected (100,200), got {out.size}"
        )
        # PNG has no Orientation tag, so we can't check the tag on PNG.


def test_orientation_baked_once_jpeg_to_jpeg(tmp_workdir):
    """JPEG->JPEG with orientation tag: bake rotation, strip tag (or set to 1)."""
    src = tmp_workdir / "rot.jpg"
    _build_tagged_jpeg(src, orientation_value=8)  # 8 = 90° CCW
    out_dir = tmp_workdir / "out"

    # Add a non-no-op so the same-format-skip doesn't short-circuit
    result = convert_file(src, out_dir, fmt="jpeg", convert_to_srgb=True)
    assert result.success

    with Image.open(result.dst) as out:
        out.load()
        # 200x100 + 90° -> 100x200 (one rotation, not two)
        assert out.size == (100, 200)
        # EXIF Orientation tag should now be missing or set to 1 (no further rotation needed).
        out_exif = out.getexif()
        orient = out_exif.get(0x0112, 1)
        assert orient == 1, (
            f"EXIF Orientation should be cleared after baking rotation; got {orient}"
        )
