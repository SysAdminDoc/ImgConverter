"""Tests for v3.0.0 features: CLI parsing, presets, watermark, canvas, tone
mapping, quality targeting, only-if-smaller, DPI, ICC, recompress, BigTIFF,
multi-frame, and scan exclude patterns."""
import json
import types

import pytest
from pathlib import Path
from PIL import Image

from imgconverter import (
    _build_parser,
    _build_quality_mode,
    _parse_canvas,
    convert_file,
    count_frames,
    list_presets,
    scan_directory,
    ConvertResult,
    PRESETS,
    HAS_JPEG_RECOMPRESS,
    HAS_JXL,
)


# ── 1. CLI arg parsing ───────────────────────────────────────────────────────


class TestCLIParsing:
    """Verify the argparse namespace produced by _build_parser."""

    def test_basic_flags(self):
        parser = _build_parser()
        args = parser.parse_args(["-i", "/photos", "-o", "/out", "-f", "webp", "-q", "80"])
        assert args.input == "/photos"
        assert args.output == "/out"
        assert args.format == "webp"
        assert args.quality == 80

    def test_advanced_flags(self):
        parser = _build_parser()
        args = parser.parse_args([
            "-i", "/src",
            "--progressive", "--chroma-420", "--srgb", "--lossless",
            "--strip-metadata", "--skip-existing",
        ])
        assert args.progressive is True
        assert args.chroma_420 is True
        assert args.srgb is True
        assert args.lossless is True
        assert args.strip_metadata is True
        assert args.skip_existing is True

    def test_v3_flags(self):
        parser = _build_parser()
        args = parser.parse_args([
            "-i", "/src",
            "--watermark", "Test|center|0.5",
            "--canvas", "800x600",
            "--canvas-bg", "#FF0000",
            "--tone-map", "reinhard",
            "--dpi", "300",
            "--icc", "sRGB",
            "--target-kb", "200",
            "--only-if-smaller", "25",
        ])
        assert args.watermark == "Test|center|0.5"
        assert args.canvas == "800x600"
        assert args.canvas_bg == "#FF0000"
        assert args.tone_map == "reinhard"
        assert args.dpi == 300
        assert args.icc == "sRGB"
        assert args.target_kb == 200.0
        assert args.only_if_smaller == 25.0

    def test_exclude_repeatable(self):
        parser = _build_parser()
        args = parser.parse_args([
            "-i", "/src",
            "--exclude", "*.thumb.*",
            "--exclude", "cache/**",
        ])
        assert args.exclude == ["*.thumb.*", "cache/**"]


# ── 2. Preset loading ────────────────────────────────────────────────────────


class TestPresets:
    """Verify list_presets returns built-in presets and can merge user presets."""

    def test_builtin_presets_present(self):
        presets = list_presets()
        for name in PRESETS:
            assert name in presets, f"Built-in preset {name!r} missing from list_presets()"

    def test_builtin_preset_keys(self):
        presets = list_presets()
        # Web Optimized is one of the hardcoded presets.
        wo = presets["Web Optimized"]
        assert wo["quality"] == 80
        assert wo["progressive_jpeg"] is True

    def test_user_preset_from_file(self, tmp_workdir, monkeypatch):
        """Drop a JSON file into a mocked USER_PRESET_DIR and verify merge."""
        import imgconverter
        fake_preset_dir = tmp_workdir / "presets"
        fake_preset_dir.mkdir()
        preset = {"name": "My Custom", "fmt": 2, "quality": 70}
        (fake_preset_dir / "my-custom.json").write_text(json.dumps(preset))

        monkeypatch.setattr(imgconverter, "USER_PRESET_DIR", fake_preset_dir)
        presets = list_presets()
        assert "My Custom" in presets
        assert presets["My Custom"]["quality"] == 70


# ── 3. Watermark ──────────────────────────────────────────────────────────────


class TestWatermark:

    def test_watermark_text_changes_pixels(self, rgb_image, tmp_workdir):
        """Converting with a watermark spec should produce output whose pixels
        differ from a plain conversion."""
        src = tmp_workdir / "src.bmp"
        rgb_image.save(src)

        out_plain = tmp_workdir / "plain"
        r_plain = convert_file(src, out_plain, fmt="png")
        assert r_plain.success

        out_wm = tmp_workdir / "wm"
        r_wm = convert_file(src, out_wm, fmt="png", watermark="SAMPLE|center|0.8")
        assert r_wm.success

        with Image.open(r_plain.dst) as a, Image.open(r_wm.dst) as b:
            assert list(a.convert("RGB").getdata()) != list(b.convert("RGB").getdata()), \
                "Watermark should alter pixels"

    def test_watermark_warning_emitted(self, rgb_image, tmp_workdir):
        src = tmp_workdir / "src.bmp"
        rgb_image.save(src)
        out = tmp_workdir / "out"
        result = convert_file(src, out, fmt="png", watermark="(C) Test|bottom-right|0.6")
        assert result.success
        assert any("watermark" in w for w in result.warnings)


# ── 4. Canvas resize ─────────────────────────────────────────────────────────


class TestCanvas:

    def test_parse_canvas_valid(self):
        assert _parse_canvas("1920x1080") == (1920, 1080)
        assert _parse_canvas("640X480") == (640, 480)

    def test_parse_canvas_invalid(self):
        assert _parse_canvas(None) is None
        assert _parse_canvas("") is None
        assert _parse_canvas("notaspec") is None
        assert _parse_canvas("12x") is None

    def test_canvas_changes_output_dims(self, rgb_image, tmp_workdir):
        """Canvas 400x400 on a 200x150 source should produce a 400x400 image."""
        src = tmp_workdir / "src.bmp"
        rgb_image.save(src)
        out = tmp_workdir / "out"
        result = convert_file(src, out, fmt="png", canvas=(400, 400))
        assert result.success
        with Image.open(result.dst) as img:
            assert img.size == (400, 400)
        assert any("canvas" in w for w in result.warnings)


# ── 5. Tone mapping ──────────────────────────────────────────────────────────


class TestToneMapping:

    def test_reinhard_on_synthetic_hdr(self, tmp_workdir):
        """A 16-bit (I;16) image should trigger HDR detect + reinhard tone map."""
        # Create a 16-bit grayscale image that _detect_hdr considers "wide".
        hdr = Image.new("I;16", (80, 60), 40000)
        src = tmp_workdir / "hdr.tiff"
        hdr.save(src)
        out = tmp_workdir / "out"
        result = convert_file(src, out, fmt="png", tone_map="reinhard")
        assert result.success
        assert any("tone-map" in w or "hdr" in w for w in result.warnings)

    def test_tonemap_no_op_on_normal_image(self, rgb_image, tmp_workdir):
        """An 8-bit RGB should not trigger HDR warnings."""
        src = tmp_workdir / "normal.bmp"
        rgb_image.save(src)
        out = tmp_workdir / "out"
        result = convert_file(src, out, fmt="png", tone_map="reinhard")
        assert result.success
        assert not any("hdr" in w for w in result.warnings)


# ── 6. Quality targeting ─────────────────────────────────────────────────────


class TestQualityTargeting:

    def test_build_quality_mode_target_kb(self):
        args = types.SimpleNamespace(target_kb=150, target_psnr=None)
        assert _build_quality_mode(args) == ("target-kb", 150.0)

    def test_build_quality_mode_none(self):
        args = types.SimpleNamespace(target_kb=None, target_psnr=None)
        assert _build_quality_mode(args) is None

    def test_target_kb_produces_near_target(self, rgb_image, tmp_workdir):
        """--target-kb should binary-search to land roughly near the target size."""
        src = tmp_workdir / "src.bmp"
        rgb_image.save(src)
        out = tmp_workdir / "out"
        target_kb = 5.0
        result = convert_file(
            src, out, fmt="jpeg",
            quality_mode=("target-kb", target_kb),
        )
        assert result.success
        actual_kb = result.size_after / 1024.0
        # Allow a generous tolerance — binary search does 8 iterations on a
        # tiny synthetic image so it won't nail the target exactly.
        assert actual_kb < target_kb * 3, (
            f"Output {actual_kb:.1f} KB is way above {target_kb} KB target"
        )
        assert any("quality-mode" in w for w in result.warnings)


# ── 7. Only-if-smaller ────────────────────────────────────────────────────────


class TestOnlyIfSmaller:

    def test_output_discarded_when_not_smaller(self, rgb_image, tmp_workdir):
        """Converting BMP -> PNG with only_if_smaller_pct=99 should keep the
        output only if it's at least 99 % smaller. For our tiny synthetic
        image the PNG is likely *larger* than the tiny threshold, so the
        output will be discarded (skipped=True)."""
        src = tmp_workdir / "src.bmp"
        rgb_image.save(src)
        # Make a very aggressive threshold — require 99 % reduction.
        out = tmp_workdir / "out"
        result = convert_file(src, out, fmt="png", only_if_smaller_pct=99)
        assert result.skipped, "Expected skipped=True when output isn't 99 % smaller"
        assert result.dst is None
        assert any("only-if-smaller" in w for w in result.warnings)

    def test_output_kept_when_smaller(self, rgb_image, tmp_workdir):
        """With a generous threshold of 0.1 %, any valid conversion should keep."""
        src = tmp_workdir / "src.bmp"
        rgb_image.save(src)
        out = tmp_workdir / "out"
        result = convert_file(src, out, fmt="jpeg", jpeg_quality=50, only_if_smaller_pct=0.1)
        assert result.success
        assert result.dst is not None
        assert result.dst.exists()


# ── 8. DPI override ──────────────────────────────────────────────────────────


class TestDPIOverride:

    def test_dpi_in_jpeg(self, rgb_image, tmp_workdir):
        src = tmp_workdir / "src.bmp"
        rgb_image.save(src)
        out = tmp_workdir / "out"
        result = convert_file(src, out, fmt="jpeg", dpi=(300, 300))
        assert result.success
        with Image.open(result.dst) as img:
            info_dpi = img.info.get("dpi")
            assert info_dpi is not None
            assert abs(info_dpi[0] - 300) < 1
            assert abs(info_dpi[1] - 300) < 1

    def test_dpi_in_tiff(self, rgb_image, tmp_workdir):
        src = tmp_workdir / "src.bmp"
        rgb_image.save(src)
        out = tmp_workdir / "out"
        result = convert_file(src, out, fmt="tiff", dpi=(600, 600))
        assert result.success
        with Image.open(result.dst) as img:
            info_dpi = img.info.get("dpi")
            assert info_dpi is not None
            assert abs(info_dpi[0] - 600) < 1


# ── 9. ICC override ──────────────────────────────────────────────────────────


class TestICCOverride:

    def test_srgb_icc_override_embeds_profile(self, rgb_image, tmp_workdir):
        """icc_override='sRGB' should embed an ICC profile in the output."""
        # First save a source with a known ICC profile so the conversion
        # path has something to transform from.
        from PIL import ImageCms
        srgb_profile = ImageCms.createProfile("sRGB")
        icc_bytes = ImageCms.ImageCmsProfile(srgb_profile).tobytes()
        src = tmp_workdir / "src.tiff"
        rgb_image.save(src, icc_profile=icc_bytes)

        out = tmp_workdir / "out"
        result = convert_file(src, out, fmt="png", icc_override="sRGB")
        assert result.success
        with Image.open(result.dst) as img:
            assert img.info.get("icc_profile"), "ICC profile should be embedded"
        assert any("icc-override" in w for w in result.warnings)

    def test_srgb_override_without_source_icc(self, rgb_image, tmp_workdir):
        """If source has no ICC, override may still succeed (just embed
        the target profile without colour-space transform)."""
        src = tmp_workdir / "src.bmp"
        rgb_image.save(src)
        out = tmp_workdir / "out"
        result = convert_file(src, out, fmt="png", icc_override="sRGB")
        # Should succeed regardless — either transform worked or gracefully warned.
        assert result.success


# ── 10. Recompress JPEG (lossless) ────────────────────────────────────────────


class TestRecompressJPEG:

    @pytest.mark.skipif(not HAS_JPEG_RECOMPRESS,
                        reason="jpegoptim / jpegtran not on PATH")
    def test_recompress_lossless_jpeg(self, rgb_image, tmp_workdir):
        """JPEG -> JPEG with recompress_lossless should use jpegoptim/jpegtran."""
        src = tmp_workdir / "src.jpg"
        rgb_image.save(src, "JPEG", quality=95)
        out = tmp_workdir / "out"
        result = convert_file(
            src, out, fmt="jpeg",
            recompress_lossless=True,
            convert_to_srgb=True,  # force non-no-op
        )
        assert result.success or result.skipped

    @pytest.mark.skipif(HAS_JPEG_RECOMPRESS,
                        reason="jpegoptim / jpegtran IS available")
    def test_recompress_skipped_when_no_tool(self, rgb_image, tmp_workdir):
        """Without jpegoptim/jpegtran, recompress path can't run; the file
        goes through the normal decode-reencode pipeline instead."""
        src = tmp_workdir / "src.jpg"
        rgb_image.save(src, "JPEG", quality=95)
        out = tmp_workdir / "out"
        result = convert_file(
            src, out, fmt="jpeg",
            recompress_lossless=True,
            convert_to_srgb=True,
        )
        # Falls through to normal conversion, which should still succeed.
        assert result.success


# ── 11. BigTIFF auto-detect ───────────────────────────────────────────────────


class TestBigTIFF:

    def test_bigtiff_warning_for_huge_estimate(self, tmp_workdir, monkeypatch):
        """When the raw-pixel estimate exceeds 4 GB, save_kwargs should get
        big_tiff=True and the warning should appear.

        We simulate this by creating a tiny image but monkeypatching its
        size property to return enormous dimensions."""
        from PIL import Image as PILImage

        # Create small image
        src = tmp_workdir / "big.bmp"
        img = PILImage.new("RGB", (10, 10), (100, 100, 100))
        img.save(src)

        out = tmp_workdir / "out"

        # Monkeypatch Image.size at the instance level after _open_image.
        # The BigTIFF check does:  w, h = img.size; bpp = ...; est_raw = w * h * bpp // 8
        # For RGB 8-bit: bpp=24, so est_raw = w * h * 3.
        # Need w*h*3 > 4 GB => w*h > ~1.43 billion.
        # Use 50000 x 30000 = 1.5 billion.
        real_open_image = __import__("imgconverter")._open_image

        def patched_open(path):
            img, meta = real_open_image(path)
            # Inject a fake .size via subclass to fool the BigTIFF check.
            class FakeImg(type(img)):
                @property
                def size(self):
                    return (50000, 30000)
            img.__class__ = FakeImg
            return img, meta

        monkeypatch.setattr("imgconverter._open_image", patched_open)

        # Also patch img.save to avoid writing a 4.5 GB file; just write a
        # valid small TIFF and verify the kwargs were propagated.
        saved_kwargs = {}
        original_save = PILImage.Image.save

        def spy_save(self, fp, format=None, **kwargs):
            saved_kwargs.update(kwargs)
            # Write a tiny valid file instead.
            tiny = PILImage.new("RGB", (10, 10), (0, 0, 0))
            return original_save(tiny, fp, format, **kwargs)

        monkeypatch.setattr(PILImage.Image, "save", spy_save)

        result = convert_file(src, out, fmt="tiff")
        # The spy should have received big_tiff=True.
        assert saved_kwargs.get("big_tiff") is True


# ── 12. Multi-frame handling ──────────────────────────────────────────────────


class TestMultiFrame:

    def test_count_frames_single(self, rgb_image, tmp_workdir):
        """A normal single-frame JPEG returns 1."""
        src = tmp_workdir / "single.jpg"
        rgb_image.save(src, "JPEG", quality=90)
        assert count_frames(src) == 1

    def test_count_frames_multipage_tiff(self, rgb_image, tmp_workdir):
        """A multi-page TIFF should report the correct frame count."""
        src = tmp_workdir / "multi.tiff"
        frame2 = Image.new("RGB", rgb_image.size, (255, 0, 0))
        rgb_image.save(src, save_all=True, append_images=[frame2])
        assert count_frames(src) == 2


# ── 13. Scan exclude patterns ────────────────────────────────────────────────


class TestScanExclude:

    def test_exclude_pattern_skips_matching_files(self, tmp_workdir):
        """Files matching exclude patterns must not appear in scan results."""
        scan_root = tmp_workdir / "photos"
        scan_root.mkdir()

        # Create files: one normal, one matching the exclude.
        keep = scan_root / "keep.jpg"
        skip = scan_root / "keep.thumb.jpg"
        keep.write_bytes(b"\xff\xd8\xff\xe0")  # minimal JPEG SOI
        skip.write_bytes(b"\xff\xd8\xff\xe0")

        result = scan_directory(scan_root, recursive=False,
                                exclude_patterns=["*.thumb.*"])
        names = {f.name for f in result.files}
        assert "keep.jpg" in names
        assert "keep.thumb.jpg" not in names

    def test_exclude_subdirectory_glob(self, tmp_workdir):
        """An exclude glob with ** should skip files deep in the tree."""
        scan_root = tmp_workdir / "photos"
        sub = scan_root / "cache" / "thumbs"
        sub.mkdir(parents=True)

        normal = scan_root / "photo.jpg"
        cached = sub / "thumb.jpg"
        normal.write_bytes(b"\xff\xd8\xff\xe0")
        cached.write_bytes(b"\xff\xd8\xff\xe0")

        result = scan_directory(scan_root, recursive=True,
                                exclude_patterns=["cache/**"])
        names = {f.name for f in result.files}
        assert "photo.jpg" in names
        assert "thumb.jpg" not in names
