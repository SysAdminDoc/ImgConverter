"""Tests for sidecar companion outputs (Live Photo, depth map, HDR gain map)."""
import pytest

from imgconverter import convert_file


def test_live_photo_mov_paired_through_conversion(rgb_image, tmp_workdir):
    """A sibling .MOV next to source should be copied next to the converted still."""
    src = tmp_workdir / "IMG_0001.bmp"
    rgb_image.save(src)
    mov = tmp_workdir / "IMG_0001.mov"
    mov.write_bytes(b"\x00\x00\x00\x14ftypqt  ")  # minimal MOV header bytes

    out_dir = tmp_workdir / "out"
    result = convert_file(src, out_dir, fmt="png")
    assert result.success

    # The mov should land next to the converted png with the same stem.
    sidecar = result.dst.with_suffix(".mov")
    assert sidecar.exists(), (
        f"Live Photo sidecar not copied: {sidecar} missing. "
        f"Out dir contents: {list(out_dir.iterdir())}"
    )
    assert sidecar.read_bytes().startswith(b"\x00\x00\x00\x14ftyp")
    assert any("live-photo" in w for w in result.warnings), (
        f"expected 'live-photo' warning; got {result.warnings}"
    )


def test_no_mov_no_sidecar(rgb_image, tmp_workdir):
    """No sibling MOV -> no sidecar, no warning."""
    src = tmp_workdir / "plain.bmp"
    rgb_image.save(src)
    out_dir = tmp_workdir / "out"
    result = convert_file(src, out_dir, fmt="png")
    assert result.success
    assert not any("live-photo" in w for w in result.warnings)
