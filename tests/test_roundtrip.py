"""Smoke-test: every supported output format produces a valid file with matching dimensions."""
import pytest

from imgconverter import convert_file, HAS_JXL


OUTPUT_FORMATS_BASE = ["jpeg", "png", "webp", "avif", "tiff"]
OUTPUT_FORMATS_OPT = ["jxl"] if HAS_JXL else []
EXT_FOR_FMT = {"jpeg": ".jpg", "png": ".png", "webp": ".webp",
               "avif": ".avif", "tiff": ".tiff", "jxl": ".jxl"}


@pytest.mark.parametrize("fmt", OUTPUT_FORMATS_BASE + OUTPUT_FORMATS_OPT)
def test_roundtrip_rgb_to_format(rgb_image, tmp_workdir, fmt):
    """RGB source -> every output format -> output exists, dims match, size > 0.

    Source is saved as BMP (a format we don't target) so the same-format
    no-op guard never fires regardless of the target.
    """
    src = tmp_workdir / "src.bmp"
    rgb_image.save(src)
    out_dir = tmp_workdir / "out"

    result = convert_file(src, out_dir, fmt=fmt, jpeg_quality=85)

    assert result.success, f"convert failed: {result.error}"
    assert result.dst is not None
    assert result.dst.exists()
    assert result.dst.suffix == EXT_FOR_FMT[fmt]
    assert result.dst.stat().st_size > 0

    # Re-open the saved file and confirm dimensions match the source.
    from PIL import Image
    with Image.open(result.dst) as out_img:
        assert out_img.size == rgb_image.size, (
            f"{fmt}: size mismatch — source {rgb_image.size}, output {out_img.size}"
        )


def test_rgba_to_jpeg_falls_back_to_png(rgba_image, tmp_workdir):
    """auto-mode RGBA input should pick PNG, not JPEG (transparency would be lost)."""
    src = tmp_workdir / "alpha.tiff"
    rgba_image.save(src)
    out_dir = tmp_workdir / "out"

    result = convert_file(src, out_dir, fmt="auto")
    assert result.success
    assert result.dst.suffix == ".png"


def test_heic_input_roundtrips_to_png(rgb_image, tmp_workdir):
    """The required HEIC decoder can feed the normal conversion pipeline."""
    import pillow_heif
    from PIL import Image

    src = tmp_workdir / "source.heic"
    pillow_heif.from_pillow(rgb_image).save(src)
    result = convert_file(src, tmp_workdir / "out", fmt="png")

    assert result.success, result.error
    assert result.dst is not None
    with Image.open(result.dst) as output:
        assert output.size == rgb_image.size


def test_raw_input_roundtrips_through_the_rawpy_decoder(tmp_workdir, monkeypatch):
    """The RAW branch is covered without requiring a binary camera fixture."""
    import numpy as np
    import imgconverter
    from PIL import Image

    source = tmp_workdir / "source.dng"
    source.write_bytes(b"synthetic raw payload")
    calls = {}

    class FakeRaw:
        def postprocess(self, **kwargs):
            calls["postprocess"] = kwargs
            return np.full((6, 8, 3), (40, 80, 120), dtype=np.uint8)

        def close(self):
            calls["closed"] = True

    class FakeRawPy:
        @staticmethod
        def imread(path):
            calls["path"] = path
            return FakeRaw()

    monkeypatch.setattr(imgconverter, "HAS_RAWPY", True)
    monkeypatch.setattr(imgconverter, "rawpy", FakeRawPy)
    result = convert_file(source, tmp_workdir / "out", fmt="png")

    assert result.success, result.error
    assert result.dst is not None
    assert calls["path"] == str(source)
    assert calls["postprocess"] == {"use_camera_wb": True, "output_bps": 8}
    assert calls["closed"] is True
    with Image.open(result.dst) as output:
        assert output.size == (8, 6)


def test_auto_rgb_selects_jpeg(rgb_image, tmp_workdir):
    """auto-mode RGB input (no alpha) should pick JPEG, not PNG."""
    src = tmp_workdir / "solid.bmp"
    rgb_image.save(src)
    out_dir = tmp_workdir / "out"

    result = convert_file(src, out_dir, fmt="auto")
    assert result.success
    assert result.dst.suffix == ".jpg"


def test_same_format_no_op_skip(rgb_image, tmp_workdir):
    """JPEG source -> JPEG target with no processing -> skipped, source untouched."""
    src = tmp_workdir / "src.jpg"
    rgb_image.save(src, "JPEG", quality=90)
    out_dir = tmp_workdir / "out"
    original_size = src.stat().st_size

    result = convert_file(src, out_dir, fmt="jpeg")
    assert result.skipped
    assert src.stat().st_size == original_size


def test_output_validation_catches_truncated_write(rgb_image, tmp_workdir, monkeypatch):
    """If the saved file has wrong dimensions, convert_file must surface the error."""
    src = tmp_workdir / "src.bmp"
    rgb_image.save(src)
    out_dir = tmp_workdir / "out"

    # Patch Image.save to write a different-size image (simulates truncation/corruption).
    from PIL import Image as PILImage
    real_save = PILImage.Image.save

    def bad_save(self, fp, *args, **kwargs):
        # write a 1x1 instead of original to force the dim-match check to fail
        return real_save(PILImage.new("RGB", (1, 1), (0, 0, 0)), fp, *args, **kwargs)

    monkeypatch.setattr(PILImage.Image, "save", bad_save)
    result = convert_file(src, out_dir, fmt="png")

    assert not result.success
    assert "size" in result.error.lower()


def test_zero_byte_file_fails_gracefully(tmp_workdir):
    """A zero-byte file should fail with a clear error, not crash."""
    src = tmp_workdir / "empty.jpg"
    src.write_bytes(b"")
    out_dir = tmp_workdir / "out"
    result = convert_file(src, out_dir, fmt="png")
    assert not result.success
    assert result.error


def test_1x1_pixel_converts_to_all_formats(tmp_workdir):
    """1x1 pixel images must convert without crashing."""
    from PIL import Image as PILImage
    src = tmp_workdir / "tiny.bmp"
    PILImage.new("RGB", (1, 1), (42, 42, 42)).save(src)
    for fmt in ["jpeg", "png", "webp"]:
        result = convert_file(src, tmp_workdir / f"out_{fmt}", fmt=fmt)
        assert result.success, f"1x1 -> {fmt} failed: {result.error}"
