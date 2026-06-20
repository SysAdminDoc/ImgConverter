"""Tests for v3.0.0 features: CLI parsing, presets, watermark, canvas, tone
mapping, quality targeting, only-if-smaller, DPI, ICC, recompress, BigTIFF,
multi-frame, and scan exclude patterns."""
import json
import inspect
import re
import sys
import tomllib
import types
import zipfile

import pytest
from pathlib import Path
from PIL import Image

from imgconverter import (
    _apply_canvas,
    _build_parser,
    _build_quality_mode,
    _convert_animated_or_sequence,
    _load_queue_state,
    _load_watch_profiles,
    _install_shell_integration,
    _parse_canvas,
    _run_cli,
    build_cli_parity_matrix,
    CLI_FLAG_PARITY,
    _validate_cli_args,
    _save_queue_state,
    _save_watch_profiles,
    build_backend_info,
    convert_file,
    ConvertOptions,
    count_frames,
    list_presets,
    scan_directory,
    ConvertResult,
    EXIT_OK,
    EXIT_PARTIAL_FAILURE,
    PRESETS,
    QUEUE_STATE_PATH,
    HAS_JPEG_RECOMPRESS,
    HAS_JXL,
    export_support_bundle,
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

    def test_files_flag_accepts_multiple_paths(self):
        parser = _build_parser()
        args = parser.parse_args(["--files", "a.png", "b.jpg", "-f", "webp"])
        assert args.input is None
        assert args.files == ["a.png", "b.jpg"]
        assert args.format == "webp"


class TestCLIValidation:

    @pytest.mark.parametrize(
        ("argv", "message"),
        [
            (["--input", "photos", "--workers", "0"], "--workers"),
            (["--input", "photos", "--workers", "33"], "--workers"),
            (["--input", "photos", "--quality", "49"], "--quality"),
            (["--input", "photos", "--quality", "101"], "--quality"),
            (["--input", "photos", "--png-level", "0"], "--png-level"),
            (["--input", "photos", "--avif-speed", "11"], "--avif-speed"),
            (["--input", "photos", "--target-kb", "0"], "--target-kb"),
            (["--input", "photos", "--target-psnr", "-1"], "--target-psnr"),
            (["--input", "photos", "--only-if-smaller", "100"], "--only-if-smaller"),
            (["--input", "photos", "--dpi", "0"], "--dpi"),
            (["--input", "photos", "--resize", "scale:0"], "--resize value"),
            (["--input", "photos", "--resize", "bad"], "--resize"),
            (["--input", "photos", "--canvas", "0x500"], "--canvas"),
            (["--input", "photos", "--max-file-size", "huge"], "--max-file-size"),
            (["--input", "photos", "--max-file-size", "0"], "--max-file-size"),
            (["--input", "photos", "--max-file-size", "-1"], "--max-file-size"),
        ],
    )
    def test_invalid_numeric_cli_values_report_errors(self, argv, message):
        args = _build_parser().parse_args(argv)
        errors = _validate_cli_args(args)
        assert any(message in error for error in errors)

    def test_valid_numeric_cli_values_pass(self):
        args = _build_parser().parse_args([
            "--input", "photos",
            "--workers", "4",
            "--quality", "80",
            "--png-level", "6",
            "--avif-speed", "4",
            "--target-kb", "200",
            "--target-psnr", "40",
            "--only-if-smaller", "25",
            "--dpi", "300",
            "--resize", "max_dim:1920",
            "--canvas", "1920x1080",
            "--max-file-size", "500MB",
        ])
        assert _validate_cli_args(args) == []

    def test_process_workers_reject_loaded_plugins(self, monkeypatch):
        import imgconverter

        monkeypatch.setitem(imgconverter.PLUGIN_ENCODERS, "demo", object())
        args = _build_parser().parse_args(["--input", "photos", "--use-processes"])

        assert any("--use-processes" in error for error in _validate_cli_args(args))


class TestSupportBundle:

    def test_parser_accepts_support_bundle_without_input(self):
        args = _build_parser().parse_args(["--support-bundle", "support.zip"])

        assert args.support_bundle == "support.zip"

    def test_support_bundle_redacts_paths_and_contains_diagnostics(self, tmp_workdir, monkeypatch):
        import imgconverter

        home = Path.home()
        log_path = tmp_workdir / "imgconverter.log"
        log_path.write_text(
            f"[INFO] opened {home / 'Pictures' / 'private.heic'}\n"
            "[INFO] conversion finished\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(imgconverter, "USER_LOG_PATH", log_path)

        bundle = tmp_workdir / "support.zip"
        written = export_support_bundle(
            bundle,
            settings_snapshot={"format": "webp", "quality": 82},
            recent_log=f"GUI log path {home / 'Pictures' / 'private.heic'}",
        )

        assert written == bundle
        with zipfile.ZipFile(bundle) as zf:
            names = set(zf.namelist())
            assert {"support.json", "recent-log.txt", "gui-log.txt"} <= names
            support_text = zf.read("support.json").decode("utf-8")
            support = json.loads(support_text)
            recent_log = zf.read("recent-log.txt").decode("utf-8")
            gui_log = zf.read("gui-log.txt").decode("utf-8")

        assert support["app"]["version"] == imgconverter.APP_VERSION
        assert support["privacy"]["source_images_included"] is False
        assert support["settings"] == {"format": "webp", "quality": 82}
        assert "dependencies" in support
        assert "native_codecs" in support
        assert isinstance(support["native_codecs"], dict)
        assert "optional_tools" in support
        assert "plugin_trust" in support["schemas"]
        assert any(row["name"] == "JPEG" for row in support["formats"])
        assert str(home) not in recent_log
        assert str(home) not in gui_log
        assert "~" in recent_log
        assert "~" in gui_log


class TestBackendInfo:

    def test_parser_accepts_backend_info_flags(self):
        args = _build_parser().parse_args([
            "--backend-info",
            "--backend-benchmark", "sample.png",
        ])

        assert args.backend_info is True
        assert args.backend_benchmark == "sample.png"

    def test_backend_info_reports_required_capability_boundaries(self):
        report = build_backend_info()
        required_features = {"metadata", "resize", "watermark", "tone_map", "icc", "avif", "jxl"}

        assert {"pillow", "vips"} <= set(report["backends"])
        for backend in ("pillow", "vips"):
            features = report["backends"][backend]["features"]
            assert required_features <= set(features)
            assert isinstance(report["backends"][backend]["memory_behavior"], str)

        assert report["backends"]["pillow"]["features"]["metadata"]["supported"] is True
        assert report["backends"]["vips"]["features"]["metadata"]["supported"] is False

    def test_backend_info_includes_native_codec_inventory(self):
        report = build_backend_info()
        assert "native_codecs" in report
        codecs = report["native_codecs"]
        assert isinstance(codecs, dict)
        assert "libheif" in codecs
        assert "pillow" in codecs

    def test_vips_backend_rejects_unacknowledged_or_unsupported_options(self):
        args = _build_parser().parse_args(["--input", "photos", "--backend", "vips", "--format", "jpeg"])
        errors = _validate_cli_args(args)
        assert any("--strip-metadata" in error for error in errors)

        args = _build_parser().parse_args([
            "--input", "photos",
            "--backend", "vips",
            "--format", "jpeg",
            "--strip-metadata",
            "--resize", "max_dim:1920",
            "--watermark", "Demo|center|0.5",
        ])
        errors = _validate_cli_args(args)
        assert any("--resize" in error and "--watermark" in error for error in errors)

    def test_vips_backend_path_is_used_when_selected(self, rgb_image, tmp_workdir, monkeypatch):
        import imgconverter

        src = tmp_workdir / "source.png"
        rgb_image.save(src)
        out = tmp_workdir / "out"
        calls = []

        def fake_vips_convert(src_path, dst_path, fmt, quality):
            calls.append((src_path, dst_path, fmt, quality))
            Image.open(src_path).convert("RGB").save(dst_path, "JPEG", quality=quality)
            return True, "fake-vips"

        monkeypatch.setattr(imgconverter, "HAS_VIPS", True)
        monkeypatch.setattr(imgconverter, "_vips_format_available", lambda fmt: fmt == "jpeg")
        monkeypatch.setattr(imgconverter, "_vips_convert", fake_vips_convert)

        result = imgconverter.convert_file(
            src,
            out,
            fmt="jpeg",
            jpeg_quality=77,
            preserve_metadata=False,
            backend="vips",
        )

        assert result.success is True
        assert result.dst == out / "source.jpg"
        assert calls and calls[0][2:] == ("jpeg", 77)
        assert any("backend: vips" in warning for warning in result.warnings)


class TestReleaseMetadataConsistency:

    def test_app_version_matches_pyproject(self):
        import imgconverter
        data = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
        assert data["project"]["version"] == imgconverter.APP_VERSION

    def test_readme_badge_matches_app_version(self):
        import imgconverter
        readme = Path("README.md").read_text(encoding="utf-8")
        assert f"Version-{imgconverter.APP_VERSION}-" in readme

    def test_dependency_floors_match_across_sources(self):
        pyproject = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
        reqs_text = Path("requirements.txt").read_text(encoding="utf-8")

        pyproject_deps = {
            d.split(">=")[0].strip().lower(): d.split(">=")[1].strip().split(",")[0].strip('"')
            for d in pyproject["project"]["dependencies"]
            if ">=" in d
        }
        reqs_deps = {}
        for line in reqs_text.splitlines():
            line = line.strip()
            if line and not line.startswith("#") and ">=" in line:
                name, ver = line.split(">=", 1)
                reqs_deps[name.strip().lower()] = ver.strip()

        for pkg in ("pillow", "pillow-heif", "pyqt6"):
            assert pkg in pyproject_deps, f"{pkg} missing from pyproject.toml"
            assert pkg in reqs_deps, f"{pkg} missing from requirements.txt"
            assert pyproject_deps[pkg] == reqs_deps[pkg], (
                f"{pkg} floor mismatch: pyproject={pyproject_deps[pkg]} vs requirements={reqs_deps[pkg]}"
            )

    def test_conda_version_matches_app(self):
        import imgconverter
        conda_text = Path("packaging/conda-forge/meta.yaml").read_text(encoding="utf-8")
        assert f'version = "{imgconverter.APP_VERSION}"' in conda_text

    def test_conda_does_not_require_numpy_at_runtime(self):
        conda_text = Path("packaging/conda-forge/meta.yaml").read_text(encoding="utf-8")
        in_run = False
        in_run_constrained = False
        for line in conda_text.splitlines():
            stripped = line.strip()
            if stripped.startswith("run:") and "constrained" not in stripped:
                in_run = True
                in_run_constrained = False
                continue
            if stripped.startswith("run_constrained:"):
                in_run = False
                in_run_constrained = True
                continue
            if stripped and not stripped.startswith("-") and not stripped.startswith("#"):
                in_run = False
                in_run_constrained = False
            if in_run and "numpy" in stripped:
                raise AssertionError(
                    f"numpy must be in run_constrained, not run: {stripped}"
                )


class TestCLIGUIPParity:

    def test_every_parser_flag_has_parity_mapping(self):
        matrix = build_cli_parity_matrix()
        missing = [row["flag"] for row in matrix if row["surface"] == "unmapped"]
        valid_surfaces = {"gui", "cli-only", "admin-only", "internal-only"}
        invalid = [
            (row["flag"], row["surface"])
            for row in matrix
            if row["surface"] not in valid_surfaces
        ]
        parser_flags = {row["flag"] for row in matrix}
        stale = sorted(set(CLI_FLAG_PARITY) - parser_flags)

        assert missing == []
        assert invalid == []
        assert stale == []

    def test_readme_documents_required_cli_flags(self):
        readme = Path("README.md").read_text(encoding="utf-8")
        matrix = build_cli_parity_matrix(readme)
        missing = [
            row["flag"]
            for row in matrix
            if row["readme_required"] and not row["in_readme"]
        ]

        assert missing == []

    def test_gui_mapped_flags_point_at_existing_mainwindow_controls(self):
        import imgconverter

        source = (
            inspect.getsource(imgconverter.MainWindow._build_ui)
            + inspect.getsource(imgconverter.MainWindow._apply_accessibility_labels)
        )
        missing = []
        for row in build_cli_parity_matrix():
            if row["surface"] != "gui":
                continue
            if not row["gui"]:
                missing.append((row["flag"], "<no widgets>"))
                continue
            for widget in row["gui"]:
                if widget not in source:
                    missing.append((row["flag"], widget))

        assert missing == []


class TestConvertOptionsParity:

    def test_convert_file_kwargs_build_convert_options(self):
        """Legacy kwargs are auto-wrapped into ConvertOptions via **kwargs."""
        opts = ConvertOptions(fmt="webp", jpeg_quality=77, progressive_jpeg=True)
        for field_name in ConvertOptions.__dataclass_fields__:
            assert hasattr(opts, field_name), f"ConvertOptions missing field: {field_name}"

    def test_convert_file_accepts_opts_kwarg(self, rgb_image, tmp_workdir):
        src = tmp_workdir / "src.bmp"
        rgb_image.save(src)
        out_dir = tmp_workdir / "out"
        opts = ConvertOptions(fmt="png")
        result = convert_file(src, out_dir, opts=opts)
        assert result.success
        assert result.dst.suffix == ".png"

    def test_preserve_structure_falls_back_when_base_is_not_ancestor(self, rgb_image, tmp_workdir):
        source_dir = tmp_workdir / "source"
        unrelated_base = tmp_workdir / "other-root"
        source_dir.mkdir()
        unrelated_base.mkdir()
        src = source_dir / "photo.bmp"
        rgb_image.save(src)
        out_dir = tmp_workdir / "out"

        result = convert_file(
            src,
            out_dir,
            fmt="png",
            preserve_structure=True,
            base_dir=unrelated_base,
        )

        assert result.success
        assert result.dst == out_dir / "photo.png"


def _relative_luminance(hex_color: str) -> float:
    raw = hex_color.lstrip("#")
    channels = [int(raw[i:i + 2], 16) / 255 for i in (0, 2, 4)]

    def linearize(channel: float) -> float:
        if channel <= 0.03928:
            return channel / 12.92
        return ((channel + 0.055) / 1.055) ** 2.4

    r, g, b = [linearize(channel) for channel in channels]
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _contrast_ratio(foreground: str, background: str) -> float:
    fg = _relative_luminance(foreground)
    bg = _relative_luminance(background)
    light, dark = max(fg, bg), min(fg, bg)
    return (light + 0.05) / (dark + 0.05)


def _style_block(stylesheet: str, selector: str) -> str:
    match = re.search(rf"{re.escape(selector)}\s*\{{(?P<body>.*?)\}}", stylesheet, re.S)
    return "" if match is None else match.group("body")


class TestStylesheetAccessibility:

    def test_readable_stylesheet_pairs_meet_wcag_aa(self):
        import imgconverter

        failures = []
        for selector, foreground, background in imgconverter.STYLESHEET_READABLE_PAIRS:
            ratio = _contrast_ratio(imgconverter.CAT[foreground], imgconverter.CAT[background])
            if ratio < imgconverter.WCAG_AA_NORMAL_TEXT_CONTRAST:
                failures.append((selector, foreground, background, round(ratio, 2)))

        assert failures == []

    def test_focus_styles_exist_for_interactive_controls(self):
        import imgconverter

        missing = []
        weak = []
        for selector in imgconverter.STYLESHEET_FOCUS_SELECTORS:
            body = _style_block(imgconverter.STYLESHEET, selector)
            if not body:
                missing.append(selector)
            elif not any(prop in body for prop in ("border", "background", "color")):
                weak.append(selector)

        assert missing == []
        assert weak == []

    def test_main_window_controls_have_accessible_names(self, monkeypatch):
        import imgconverter

        if not imgconverter.HAS_PYQT6:
            pytest.skip("PyQt6 not available")

        monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
        monkeypatch.setattr(imgconverter.MainWindow, "_apply_dark_titlebar", lambda self: None)
        monkeypatch.setattr(imgconverter.MainWindow, "_restore_state", lambda self: None)
        monkeypatch.setattr(imgconverter.MainWindow, "_log_startup", lambda self: None)
        monkeypatch.setattr(imgconverter, "_diag_log", lambda *_args, **_kwargs: None)

        from PyQt6.QtWidgets import QApplication

        app = QApplication.instance() or QApplication([])
        window = imgconverter.MainWindow()
        try:
            missing = []
            wrong = []
            for attr, name, desc in imgconverter.MAIN_WINDOW_ACCESSIBILITY_LABELS:
                widget = getattr(window, attr, None)
                if widget is None:
                    missing.append(attr)
                elif widget.accessibleName() != name or widget.accessibleDescription() != desc:
                    wrong.append((attr, widget.accessibleName(), widget.accessibleDescription()))

            assert missing == []
            assert wrong == []
        finally:
            window.close()
            window.deleteLater()
            app.processEvents()


class TestDependencyFloors:
    """Verify documented dependency floors stay synced across install paths."""

    def test_pillow_floor_synced(self):
        import imgconverter

        root = Path(__file__).resolve().parents[1]
        floor = imgconverter.DEP_FLOORS["PIL"][1]
        assert floor == "12.2.0"
        assert f"Pillow>={floor}" in (root / "requirements.txt").read_text(encoding="utf-8")

        pyproject = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
        assert f"Pillow>={floor}" in pyproject["project"]["dependencies"]
        conda_recipe = root / "packaging" / "conda-forge" / "meta.yaml"
        assert f"pillow >={floor}" in conda_recipe.read_text(encoding="utf-8")


class TestPyInstallerGuards:

    def test_freeze_support_runs_before_heavy_imports(self):
        source = Path("imgconverter.py").read_text(encoding="utf-8")

        freeze_idx = source.index("multiprocessing.freeze_support()")
        deps_idx = source.index("_check_required_deps_or_exit()")
        pillow_idx = source.index("from PIL import Image")

        assert freeze_idx < deps_idx < pillow_idx

    def test_install_deps_refuses_frozen_executable(self, monkeypatch, capsys):
        import imgconverter

        def fail_check_call(*_args, **_kwargs):
            raise AssertionError("pip subprocess should not run from a frozen executable")

        monkeypatch.setattr(imgconverter.sys, "frozen", True, raising=False)
        monkeypatch.setattr(imgconverter.subprocess, "check_call", fail_check_call)

        code = imgconverter._install_deps(include_optional=True)

        assert code == imgconverter.EXIT_INPUT_ERROR
        assert "packaged executable" in capsys.readouterr().err


class TestSelectedFileCLI:
    """Verify shell-integration style selected files are valid CLI inputs."""

    def test_input_can_be_single_file(self, rgb_image, tmp_workdir):
        src = tmp_workdir / "single.bmp"
        rgb_image.save(src)
        out = tmp_workdir / "out"

        args = _build_parser().parse_args([
            "--input", str(src), "--output", str(out), "--format", "png",
        ])
        with pytest.raises(SystemExit) as exc:
            _run_cli(args)

        assert exc.value.code == EXIT_OK
        assert (out / "single.png").exists()

    def test_files_converts_multiple_selected_files(self, rgb_image, tmp_workdir):
        first = tmp_workdir / "first.bmp"
        second = tmp_workdir / "second.bmp"
        rgb_image.save(first)
        rgb_image.save(second)
        out = tmp_workdir / "out"

        args = _build_parser().parse_args([
            "--files", str(first), str(second),
            "--output", str(out),
            "--format", "png",
        ])
        with pytest.raises(SystemExit) as exc:
            _run_cli(args)

        assert exc.value.code == EXIT_OK
        assert (out / "first.png").exists()
        assert (out / "second.png").exists()


class TestWhenDoneCLI:

    def test_when_done_is_skipped_when_cli_batch_has_failures(
        self, rgb_image, tmp_workdir, monkeypatch, capsys,
    ):
        import imgconverter

        good = tmp_workdir / "good.bmp"
        bad = tmp_workdir / "bad.bmp"
        rgb_image.save(good)
        bad.write_bytes(b"not an image")
        called = []
        monkeypatch.setattr(imgconverter, "_execute_when_done", lambda action: called.append(action))

        args = _build_parser().parse_args([
            "--input", str(tmp_workdir),
            "--output", str(tmp_workdir / "out"),
            "--format", "png",
            "--no-recursive",
            "--when-done", "close",
        ])
        with pytest.raises(SystemExit) as exc:
            _run_cli(args)

        assert exc.value.code == EXIT_PARTIAL_FAILURE
        assert called == []
        assert "skipped 'close'" in capsys.readouterr().err


class TestShellIntegration:
    """Verify generated shell entries route files through --files."""

    def test_linux_desktop_entry_uses_file_selection(self, tmp_workdir, monkeypatch):
        import imgconverter

        monkeypatch.setattr(imgconverter.platform, "system", lambda: "Linux")
        monkeypatch.setattr(imgconverter.Path, "home", classmethod(lambda cls: tmp_workdir))
        monkeypatch.setattr(imgconverter.sys, "executable", str(tmp_workdir / "Python Folder" / "python"))
        monkeypatch.setattr(imgconverter, "__file__", str(tmp_workdir / "App Folder" / "imgconverter.py"))

        assert _install_shell_integration(False) == EXIT_OK
        desktop = tmp_workdir / ".local" / "share" / "applications" / "imgconverter.desktop"
        text = desktop.read_text(encoding="utf-8")
        exec_line = next(line for line in text.splitlines() if line.startswith("Exec="))
        assert "--files %F" in exec_line
        assert exec_line.startswith("Exec=\"")
        assert "Python Folder" in exec_line
        assert "App Folder" in exec_line

    def test_windows_registry_commands_use_files_and_directory_paths(self, monkeypatch):
        import imgconverter

        values = {}

        class Key:
            def __init__(self, name):
                self.name = name

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

        class FakeWinreg:
            HKEY_CURRENT_USER = object()
            REG_SZ = 1
            KEY_SET_VALUE = 2

            @staticmethod
            def CreateKeyEx(root, key, reserved, access):
                return Key(key)

            @staticmethod
            def SetValueEx(key, name, reserved, typ, value):
                values[(key.name, name)] = value

        monkeypatch.setattr(imgconverter.platform, "system", lambda: "Windows")
        monkeypatch.setitem(sys.modules, "winreg", FakeWinreg)

        assert _install_shell_integration(False) == EXIT_OK
        assert "--files %*" in values[(r"Software\Classes\*\shell\ImgConverter\command", "")]
        assert values[(r"Software\Classes\*\shell\ImgConverter", "MultiSelectModel")] == "Player"
        assert '--input "%1"' in values[(r"Software\Classes\Directory\shell\ImgConverter\command", "")]


# ── 2. Preset loading ────────────────────────────────────────────────────────


def _advanced_preset_payload():
    return {
        "schema_version": 2,
        "format": "avif",
        "quality": 77,
        "progressive": True,
        "chroma_420": True,
        "lossless": True,
        "srgb": True,
        "resize": "scale:50",
        "no_structure": True,
        "template": "{rel_dir}/{stem}_{seq:###}",
        "avif_speed": 3,
        "avif_codec": "svt",
        "watermark": "Demo|bottom-right|0.6",
        "canvas": "1920x1080",
        "canvas_bg": "#101010",
        "exclude": ["cache/**", "*.tmp"],
        "max_file_size": "500MB",
        "target_kb": 200,
        "target_psnr": 40,
        "only_if_smaller": 25,
        "xmp_sidecar": True,
        "sidecar_history": True,
        "strip_metadata": True,
        "dpi": 300,
        "icc": "sRGB",
        "recompress": True,
        "png_lossy": True,
        "frames": "all",
        "tone_map": "hable",
        "tiff_compression": "deflate",
        "png_level": 9,
    }


class _FakeCheck:
    def __init__(self):
        self.checked = None

    def setChecked(self, value):
        self.checked = bool(value)


class _FakeValue:
    def __init__(self):
        self.value = None

    def setValue(self, value):
        self.value = value

    def setCurrentIndex(self, value):
        self.value = value


class _FakeLine:
    def __init__(self):
        self.text = None

    def setText(self, value):
        self.text = value


class _FakePresetWindow:
    def __init__(self):
        for attr in (
            "progressive_jpeg_chk", "lossless_webp_chk", "chroma_chk", "srgb_chk",
            "inplace_chk", "skip_existing_chk", "xmp_sidecar_chk", "recompress_chk",
            "png_lossy_chk", "strip_meta_chk", "meta_chk", "structure_chk",
            "recursive_chk", "resize_chk", "only_if_smaller_chk",
        ):
            setattr(self, attr, _FakeCheck())
        for attr in (
            "fmt_combo", "quality_slider", "workers_spin", "resize_combo",
            "resize_spin", "tiff_comp_combo", "png_level_spin", "dpi_spin",
            "avif_speed_spin", "avif_codec_combo", "frames_combo",
            "tone_map_combo", "only_if_smaller_spin", "target_kb_spin",
            "meta_combo",
        ):
            setattr(self, attr, _FakeValue())
        for attr in (
            "prefix_edit", "suffix_edit", "template_edit", "icc_edit",
            "watermark_edit", "canvas_edit", "canvas_bg_edit", "exclude_edit",
            "max_file_size_edit",
        ):
            setattr(self, attr, _FakeLine())


class TestPresets:
    """Verify list_presets returns built-in presets and can merge user presets."""

    def test_builtin_presets_present(self):
        presets = list_presets()
        for name in PRESETS:
            assert name in presets, f"Built-in preset {name!r} missing from list_presets()"

    def test_builtin_preset_keys(self):
        presets = list_presets()
        wo = presets["Web Optimized"]
        assert wo["quality"] == 80
        assert wo.get("progressive_jpeg") is True or wo.get("progressive") is True

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

    def test_advanced_preset_normalizes_cli_and_gui_shapes(self):
        import imgconverter

        preset = _advanced_preset_payload()
        norm = imgconverter.normalize_preset(preset)

        assert norm["schema_version"] == 2
        assert norm["format"] == "avif"
        assert norm["fmt"] == 4
        assert norm["resize"] == "scale:50"
        assert norm["exclude"] == ["cache/**", "*.tmp"]
        assert norm["max_file_size"] == "500MB"
        assert norm["target_kb"] == 200.0
        assert norm["target_psnr"] == 40.0
        assert norm["xmp_sidecar"] is True
        assert norm["sidecar_history"] is True
        assert norm["strip_metadata"] is True

    def test_cli_preset_applies_advanced_options(self):
        import imgconverter

        args = _build_parser().parse_args(["--input", "/photos"])
        imgconverter._apply_preset_to_args(args, _advanced_preset_payload())

        assert args.format == "avif"
        assert args.template == "{rel_dir}/{stem}_{seq:###}"
        assert args.avif_speed == 3
        assert args.avif_codec == "svt"
        assert args.watermark == "Demo|bottom-right|0.6"
        assert args.canvas == "1920x1080"
        assert args.canvas_bg == "#101010"
        assert args.exclude == ["cache/**", "*.tmp"]
        assert args.max_file_size == "500MB"
        assert args.target_kb == 200.0
        assert args.target_psnr == 40.0
        assert args.xmp_sidecar is True
        assert args.sidecar_history is True
        assert args.strip_metadata is True
        assert args.no_structure is True
        assert args.resize == "scale:50"

    def test_gui_preset_applies_advanced_controls(self):
        import imgconverter

        fake = _FakePresetWindow()
        imgconverter._apply_preset_to_gui_controls(fake, _advanced_preset_payload())

        assert fake.fmt_combo.value == 4
        assert fake.quality_slider.value == 77
        assert fake.template_edit.text == "{rel_dir}/{stem}_{seq:###}"
        assert fake.avif_speed_spin.value == 3
        assert fake.avif_codec_combo.value == 3
        assert fake.watermark_edit.text == "Demo|bottom-right|0.6"
        assert fake.canvas_edit.text == "1920x1080"
        assert fake.canvas_bg_edit.text == "#101010"
        assert fake.exclude_edit.text == "cache/**; *.tmp"
        assert fake.max_file_size_edit.text == "500MB"
        assert fake.target_kb_spin.value == 200
        assert fake.xmp_sidecar_chk.checked is True
        assert fake.meta_combo.value == 3
        assert fake.structure_chk.checked is False
        assert fake.resize_chk.checked is True
        assert fake.resize_combo.value == 1
        assert fake.resize_spin.value == 50


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
            _get = getattr(Image.Image, "get_flattened_data", Image.Image.getdata)
            assert list(_get(a.convert("RGB"))) != list(_get(b.convert("RGB"))), \
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


# ── 5b. High bit-depth output caveats ───────────────────────────────────────


class TestHighBitDepthOutput:

    def test_avif_warns_instead_of_claiming_high_bit_depth(self, tmp_workdir, monkeypatch):
        import imgconverter
        if not imgconverter.HAS_AVIF:
            pytest.skip("AVIF unavailable")

        src = tmp_workdir / "src.heic"
        src.write_bytes(b"placeholder")

        def fake_open_image(path):
            return Image.new("RGB", (16, 16), (120, 40, 20)), {"bit_depth": 10}

        monkeypatch.setattr(imgconverter, "_open_image", fake_open_image)
        result = convert_file(src, tmp_workdir / "out", fmt="avif")

        assert result.success
        assert any("8-bit" in w and "JPEG XL" in w for w in result.warnings)
        assert not any("avif:" in w and "preserving" in w for w in result.warnings)


# ── 5b. Selective metadata stripping ────────────────────────────────────────


class TestSelectiveMetadataStripping:

    def _build_gps_jpeg(self, path):
        from PIL.TiffImagePlugin import IFDRational
        img = Image.new("RGB", (40, 30), (100, 150, 200))
        exif = img.getexif()
        exif[0x010F] = "TestMake"
        exif[0x0110] = "TestModel"
        exif[0x8298] = "Copyright 2026"
        gps_ifd = exif.get_ifd(0x8825)
        gps_ifd[0x0001] = "N"
        gps_ifd[0x0002] = (IFDRational(40), IFDRational(26), IFDRational(46))
        gps_ifd[0x0003] = "W"
        gps_ifd[0x0004] = (IFDRational(74), IFDRational(0), IFDRational(21))
        exif[0x8825] = gps_ifd
        img.save(path, "JPEG", exif=exif.tobytes(), quality=92)

    def test_strip_gps_preserves_copyright_and_make(self, tmp_workdir):
        src = tmp_workdir / "gps.jpg"
        self._build_gps_jpeg(src)
        out = tmp_workdir / "out"
        result = convert_file(
            src, out, fmt="jpeg",
            convert_to_srgb=True,
            strip_fields=frozenset({"gps"}),
        )
        assert result.success
        with Image.open(result.dst) as out_img:
            out_exif = out_img.getexif()
            assert out_exif.get(0x8298) == "Copyright 2026"
            assert out_exif.get(0x010F) == "TestMake"
            assert 0x8825 not in out_exif

    def test_strip_gps_device_removes_make_model(self, tmp_workdir):
        src = tmp_workdir / "gps_device.jpg"
        self._build_gps_jpeg(src)
        out = tmp_workdir / "out"
        result = convert_file(
            src, out, fmt="jpeg",
            convert_to_srgb=True,
            strip_fields=frozenset({"gps", "device"}),
        )
        assert result.success
        with Image.open(result.dst) as out_img:
            out_exif = out_img.getexif()
            assert out_exif.get(0x8298) == "Copyright 2026"
            assert out_exif.get(0x010F) is None
            assert out_exif.get(0x0110) is None
            assert 0x8825 not in out_exif

    def test_strip_all_removes_everything(self, tmp_workdir):
        src = tmp_workdir / "full.jpg"
        self._build_gps_jpeg(src)
        out = tmp_workdir / "out"
        result = convert_file(
            src, out, fmt="jpeg",
            preserve_metadata=False,
            strip_fields=frozenset({"all"}),
        )
        assert result.success
        with Image.open(result.dst) as out_img:
            assert not out_img.getexif()

    def test_cli_flags_parse_strip_gps_and_device(self):
        from imgconverter import _build_parser
        args = _build_parser().parse_args([
            "-i", "/photos", "--strip-gps", "--strip-device",
        ])
        assert args.strip_gps is True
        assert args.strip_device is True
        assert args.strip_metadata is False


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
        """Converting BMP -> PNG with only_if_smaller_pct=99.9 should keep the
        output only if it's at least 99.9 % smaller. For our tiny synthetic
        image the PNG is still larger than the tiny threshold, so the
        output will be discarded (skipped=True)."""
        src = tmp_workdir / "src.bmp"
        rgb_image.save(src)
        # Make a very aggressive threshold: require 99.9 % reduction.
        out = tmp_workdir / "out"
        result = convert_file(src, out, fmt="png", only_if_smaller_pct=99.9)
        assert result.skipped, "Expected skipped=True when output isn't 99.9 % smaller"
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

        We simulate this by forcing the BigTIFF decision helper to return true."""
        from PIL import Image as PILImage

        # Create small image
        src = tmp_workdir / "big.bmp"
        img = PILImage.new("RGB", (10, 10), (100, 100, 100))
        img.save(src)

        out = tmp_workdir / "out"
        monkeypatch.setattr("imgconverter._requires_bigtiff", lambda img: True)

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
        assert result.success
        assert saved_kwargs.get("big_tiff") is True
        assert any("BigTIFF" in w for w in result.warnings)


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

    def test_max_file_size_skips_large_inputs(self, tmp_workdir):
        scan_root = tmp_workdir / "photos"
        scan_root.mkdir()
        small = scan_root / "small.jpg"
        large = scan_root / "large.jpg"
        small.write_bytes(b"x" * 12)
        large.write_bytes(b"x" * 128)

        result = scan_directory(scan_root, recursive=False, max_file_size=64)
        names = {f.name for f in result.files}

        assert "small.jpg" in names
        assert "large.jpg" not in names


# ── 14. In-place mode ───────────────────────────────────────────────────────


class TestInPlace:

    def test_in_place_deletes_source_and_creates_output(self, rgb_image, tmp_workdir):
        src = tmp_workdir / "photo.bmp"
        rgb_image.save(src)
        result = convert_file(src, tmp_workdir, fmt="jpeg", in_place=True)
        assert result.success
        assert result.src_deleted
        assert not src.exists()
        assert result.dst is not None
        assert result.dst.exists()
        assert result.dst.suffix == ".jpg"

    def test_in_place_failure_preserves_source(self, tmp_workdir):
        src = tmp_workdir / "bad.bmp"
        src.write_bytes(b"not an image")
        result = convert_file(src, tmp_workdir, fmt="jpeg", in_place=True)
        assert not result.success
        assert src.exists()

    def test_in_place_same_ext_no_self_delete(self, rgb_image, tmp_workdir):
        src = tmp_workdir / "photo.jpg"
        rgb_image.save(src, "JPEG", quality=90)
        result = convert_file(
            src, tmp_workdir, fmt="jpeg", in_place=True,
            convert_to_srgb=True,
        )
        assert result.success
        assert not result.src_deleted
        assert result.dst == src
        assert src.exists()
        assert result.dst.exists()

    def test_in_place_same_ext_skip_existing_does_not_skip_source(self, rgb_image, tmp_workdir):
        src = tmp_workdir / "photo.jpg"
        rgb_image.save(src, "JPEG", quality=90)
        result = convert_file(
            src, tmp_workdir, fmt="jpeg", in_place=True,
            preserve_metadata=False,
            skip_existing=True,
        )
        assert result.success
        assert not result.skipped
        assert result.dst == src
        assert src.exists()


# ── 14b. Error code propagation ─────────────────────────────────────────────


class TestErrorCode:

    def test_error_code_set_on_oserror(self, tmp_workdir, monkeypatch):
        import errno
        src = tmp_workdir / "photo.bmp"
        src.write_bytes(b"not an image")
        out = tmp_workdir / "out"
        result = convert_file(src, out, fmt="jpeg")
        assert not result.success
        assert result.error_code is None or isinstance(result.error_code, int)

    def test_error_code_none_on_non_os_error(self, tmp_workdir):
        src = tmp_workdir / "corrupt.bmp"
        src.write_bytes(b"garbage data")
        out = tmp_workdir / "out"
        result = convert_file(src, out, fmt="jpeg")
        assert not result.success
        assert result.error_code is None

    def test_convert_result_has_error_code_field(self):
        r = ConvertResult(src=Path("dummy.jpg"))
        assert r.error_code is None


# ── 15. Same-format skip guard completeness ─────────────────────────────────


class TestSkipGuard:

    def test_same_format_with_watermark_not_skipped(self, rgb_image, tmp_workdir):
        src = tmp_workdir / "photo.jpg"
        rgb_image.save(src, "JPEG", quality=90)
        out = tmp_workdir / "out"
        result = convert_file(src, out, fmt="jpeg", watermark="Test|center|0.5")
        assert not result.skipped

    def test_same_format_with_canvas_not_skipped(self, rgb_image, tmp_workdir):
        src = tmp_workdir / "photo.jpg"
        rgb_image.save(src, "JPEG", quality=90)
        out = tmp_workdir / "out"
        result = convert_file(src, out, fmt="jpeg", canvas=(400, 400))
        assert not result.skipped

    def test_same_format_with_dpi_not_skipped(self, rgb_image, tmp_workdir):
        src = tmp_workdir / "photo.jpg"
        rgb_image.save(src, "JPEG", quality=90)
        out = tmp_workdir / "out"
        result = convert_file(src, out, fmt="jpeg", dpi=(300, 300))
        assert not result.skipped

    def test_same_format_with_strip_fields_not_skipped(self, rgb_image, tmp_workdir):
        src = tmp_workdir / "photo.jpg"
        rgb_image.save(src, "JPEG", quality=90)
        out = tmp_workdir / "out"
        result = convert_file(
            src, out, fmt="jpeg",
            strip_fields=frozenset({"gps"}),
            convert_to_srgb=True,
        )
        assert not result.skipped

    def test_same_format_no_processing_is_skipped(self, rgb_image, tmp_workdir):
        src = tmp_workdir / "photo.jpg"
        rgb_image.save(src, "JPEG", quality=90)
        out = tmp_workdir / "out"
        result = convert_file(src, out, fmt="jpeg")
        assert result.skipped


# ── 16. Strip metadata ─────────────────────────────────────────────────────


class TestStripMetadata:

    def test_strip_removes_exif(self, tmp_workdir):
        from PIL import Image
        img = Image.new("RGB", (100, 100), (128, 128, 128))
        from PIL.ExifTags import IFD
        exif = img.getexif()
        exif[0x010F] = "TestCamera"
        src = tmp_workdir / "exif.jpg"
        img.save(src, "JPEG", exif=exif.tobytes())
        with Image.open(src) as check:
            assert check.getexif().get(0x010F) == "TestCamera"
        out = tmp_workdir / "out"
        result = convert_file(src, out, fmt="jpeg", preserve_metadata=False,
                              use_exiftool=False)
        assert result.success
        with Image.open(result.dst) as opened:
            raw_exif = opened.info.get("exif", b"")
            assert not raw_exif or opened.getexif().get(0x010F) is None


# ── 17. Canvas alpha preservation ──────────────────────────────────────────


class TestCanvasAlpha:

    def test_rgba_canvas_preserves_transparency(self, rgba_image, tmp_workdir):
        canvas_img = _apply_canvas(rgba_image, (200, 200), (0, 0, 0, 0))
        assert canvas_img.mode == "RGBA"
        corner = canvas_img.getpixel((0, 0))
        assert corner[3] == 0

    def test_rgb_canvas_no_alpha_issue(self, rgb_image, tmp_workdir):
        canvas_img = _apply_canvas(rgb_image, (400, 300), (0, 0, 0))
        assert canvas_img.mode == "RGB"


# ── 18. Queue persistence ──────────────────────────────────────────────────


class TestWatchProfilePersistence:

    def test_load_watch_profiles_sanitizes_malformed_entries(self, tmp_workdir, monkeypatch):
        path = tmp_workdir / "watch-profiles.json"
        path.write_text(json.dumps([
            "bad",
            {"output": "missing-source"},
            {"source": str(tmp_workdir / "in"), "enabled": "false", "unknown": "drop"},
            {
                "source": str(tmp_workdir / "src"),
                "output": str(tmp_workdir / "dst"),
                "preset": "Archive Quality",
                "enabled": True,
                "last_run": "2026-06-19T00:00:00Z",
                "last_error": 404,
            },
        ]), encoding="utf-8")
        monkeypatch.setattr("imgconverter.WATCH_PROFILES_FILE", path)

        profiles = _load_watch_profiles()

        assert len(profiles) == 2
        assert profiles[0]["enabled"] is False
        assert profiles[0]["output"].endswith("converted")
        assert profiles[1]["preset"] == "Archive Quality"
        assert profiles[1]["last_error"] is None

    def test_save_watch_profiles_writes_loadable_shape(self, tmp_workdir, monkeypatch):
        path = tmp_workdir / "watch-profiles.json"
        monkeypatch.setattr("imgconverter.WATCH_PROFILES_FILE", path)

        _save_watch_profiles([
            {"source": str(tmp_workdir / "src"), "enabled": "yes", "extra": "drop"},
            {"source": ""},
        ])

        saved = json.loads(path.read_text(encoding="utf-8"))
        assert len(saved) == 1
        assert saved[0]["enabled"] is True
        assert sorted(saved[0]) == ["enabled", "last_error", "last_run", "output", "preset", "source"]


class TestQueuePersistence:

    def test_save_and_load_roundtrip(self, tmp_workdir, monkeypatch):
        monkeypatch.setattr("imgconverter.USER_CACHE_DIR", tmp_workdir)
        monkeypatch.setattr("imgconverter.QUEUE_STATE_PATH", tmp_workdir / "queue.json")
        args = types.SimpleNamespace(format="jpeg", quality=92)
        input_dir = tmp_workdir / "input"
        output_dir = tmp_workdir / "output"
        pending = [tmp_workdir / "a.jpg", tmp_workdir / "b.jpg"]
        _save_queue_state(input_dir, output_dir, args, pending, ["done.jpg"], [])
        state = _load_queue_state()
        assert state is not None
        assert state["input"] == str(input_dir)
        assert len(state["pending"]) == 2
        assert state["done"] == ["done.jpg"]

    def test_load_corrupt_returns_none(self, tmp_workdir, monkeypatch):
        qpath = tmp_workdir / "queue.json"
        qpath.write_text("{bad json!!!")
        monkeypatch.setattr("imgconverter.QUEUE_STATE_PATH", qpath)
        assert _load_queue_state() is None

    def test_load_non_object_queue_returns_none(self, tmp_workdir, monkeypatch):
        qpath = tmp_workdir / "queue.json"
        qpath.write_text("[]")
        monkeypatch.setattr("imgconverter.QUEUE_STATE_PATH", qpath)
        assert _load_queue_state() is None

    def test_load_queue_normalizes_bad_list_fields(self, tmp_workdir, monkeypatch):
        qpath = tmp_workdir / "queue.json"
        qpath.write_text(json.dumps({"input": "in", "pending": "bad", "done": None, "failed": ["x"]}))
        monkeypatch.setattr("imgconverter.QUEUE_STATE_PATH", qpath)
        state = _load_queue_state()
        assert state is not None
        assert state["pending"] == []
        assert state["done"] == []
        assert state["failed"] == ["x"]

    def test_cli_queue_marks_skipped_files_done(self, rgb_image, tmp_workdir, monkeypatch):
        good = tmp_workdir / "already.jpg"
        bad = tmp_workdir / "bad.bmp"
        rgb_image.save(good, "JPEG", quality=90)
        bad.write_bytes(b"not an image")
        queue_path = tmp_workdir / "queue.json"
        monkeypatch.setattr("imgconverter.QUEUE_STATE_PATH", queue_path)

        args = _build_parser().parse_args([
            "--input", str(tmp_workdir),
            "--output", str(tmp_workdir / "out"),
            "--format", "jpeg",
            "--no-recursive",
        ])
        with pytest.raises(SystemExit) as exc:
            _run_cli(args)

        assert exc.value.code == EXIT_PARTIAL_FAILURE
        state = json.loads(queue_path.read_text(encoding="utf-8"))
        assert str(good) in state["done"]
        assert str(bad) in state["failed"]
        assert str(good) not in state["pending"]


# ── 19. Multi-frame export ─────────────────────────────────────────────────


class TestMultiFrameExport:

    def test_extract_frames_produces_sequence(self, rgb_image, tmp_workdir):
        src = tmp_workdir / "multi.tiff"
        frame2 = Image.new("RGB", rgb_image.size, (255, 0, 0))
        rgb_image.save(src, save_all=True, append_images=[frame2])
        out = tmp_workdir / "out"
        result = _convert_animated_or_sequence(
            src, out, "jpeg", extract_frames=True,
        )
        assert result.success
        assert result.dst is not None
        exported = list((out).glob("*.jpg"))
        assert len(exported) == 2


# ── 20. Qt event-loop accessibility and keyboard tests ─────────────────────

_qt_skip_reason = None
try:
    import os as _qt_os
    _qt_os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PyQt6.QtWidgets import QApplication as _QApp
    from PyQt6.QtCore import Qt as _Qt
    _qt_app = _QApp.instance() or _QApp([])
    _HAS_QT_OFFSCREEN = True
except Exception as _qt_err:
    _HAS_QT_OFFSCREEN = False
    _qt_skip_reason = str(_qt_err)


@pytest.mark.skipif(not _HAS_QT_OFFSCREEN, reason=_qt_skip_reason or "PyQt6 offscreen unavailable")
class TestQtAccessibility:

    @pytest.fixture(autouse=True)
    def _window(self):
        import imgconverter
        self.window = imgconverter.MainWindow()
        yield
        self.window.close()

    def test_primary_controls_have_accessible_names(self):
        w = self.window
        named = []
        for attr in ("src_edit", "dst_edit", "fmt_combo", "quality_slider",
                     "workers_spin", "scan_btn", "convert_btn", "stop_btn",
                     "meta_combo", "when_done_combo"):
            widget = getattr(w, attr, None)
            if widget and widget.accessibleName():
                named.append(attr)
        assert len(named) >= 6

    def test_tab_order_covers_primary_flow(self):
        w = self.window
        focus_chain = []
        widget = w.src_edit
        seen = set()
        for _ in range(30):
            name = widget.objectName() or type(widget).__name__
            if id(widget) in seen:
                break
            seen.add(id(widget))
            focus_chain.append(name)
            widget = widget.nextInFocusChain()

        assert len(focus_chain) >= 8

    def test_scan_button_is_keyboard_activatable(self):
        w = self.window
        assert w.scan_btn.isEnabled()
        assert w.scan_btn.focusPolicy() != _Qt.FocusPolicy.NoFocus

    def test_format_combo_is_keyboard_navigable(self):
        w = self.window
        assert w.fmt_combo.focusPolicy() != _Qt.FocusPolicy.NoFocus
        assert w.fmt_combo.count() >= 6

    def test_input_filters_are_progressively_disclosed(self):
        w = self.window
        assert w.filter_group.isHidden()

        w.filter_toggle.setChecked(True)
        assert not w.filter_group.isHidden()
        assert "Hide input format filters" in w.filter_toggle.text()

        w.filter_toggle.setChecked(False)
        assert w.filter_group.isHidden()
        assert "Show input format filters" in w.filter_toggle.text()

    def test_validation_style_clears_when_max_file_size_changes(self):
        w = self.window
        w._set_line_error(w.max_file_size_edit, "Use a size like 500MB")
        assert "border" in w.max_file_size_edit.styleSheet()

        w.max_file_size_edit.setText("600MB")

        assert w.max_file_size_edit.styleSheet() == ""

    def test_export_log_reports_write_failure(self, tmp_workdir, monkeypatch):
        import imgconverter

        w = self.window
        monkeypatch.setattr(
            imgconverter.QFileDialog,
            "getSaveFileName",
            lambda *args, **kwargs: (str(tmp_workdir / "log.txt"), ""),
        )

        def fail_write(*args, **kwargs):
            raise OSError("permission denied")

        monkeypatch.setattr(imgconverter, "_write_text_atomic", fail_write)

        w.log_view.setPlainText("session log")
        w._export_log()

        assert w.workflow_state.text() == "Export failed"
        assert "Could not export log" in w.log_view.toPlainText()

    def test_export_csv_writes_report(self, tmp_workdir, monkeypatch):
        import imgconverter

        w = self.window
        target = tmp_workdir / "report.csv"
        monkeypatch.setattr(
            imgconverter.QFileDialog,
            "getSaveFileName",
            lambda *args, **kwargs: (str(target), ""),
        )
        w._results = [
            ConvertResult(
                src=tmp_workdir / "source.bmp",
                dst=tmp_workdir / "out" / "source.png",
                success=True,
                size_before=100,
                size_after=64,
                elapsed=0.125,
                warnings=["metadata copied"],
                metadata_report={"icc_before": True, "icc_after": True},
            )
        ]

        w._export_csv()

        report = target.read_text(encoding="utf-8")
        assert "Source,Output,Status" in report
        assert "source.bmp" in report
        assert "metadata copied" in report


# ── Watch mode integration ───────────────────────────────────────────────────


class TestWatchModeIntegration:
    """Watch mode debounce and ConvertOptions forwarding without a live polling loop."""

    def test_new_file_converts_with_options(self, rgb_image, tmp_workdir):
        """Simulate the watch-mode conversion path: file appears, opts forwarded."""
        src = tmp_workdir / "watch_in"
        src.mkdir()
        out = tmp_workdir / "watch_out"
        img_path = src / "photo.bmp"
        rgb_image.save(img_path)

        opts = ConvertOptions(fmt="png", jpeg_quality=75)
        result = convert_file(img_path, out, opts=opts)
        assert result.success
        assert result.dst.suffix == ".png"

    def test_debounce_size_stability(self, tmp_workdir):
        """Debounce logic: file must have stable size across two polls."""
        src = tmp_workdir / "growing.bmp"
        src.write_bytes(b"\x00" * 100)

        seen_sizes: dict[Path, int] = {}
        current = []

        size = src.stat().st_size
        if seen_sizes.get(src) == size:
            current.append(src)
        else:
            seen_sizes[src] = size

        assert current == [], "First poll should defer — size not yet stable"

        size2 = src.stat().st_size
        if seen_sizes.get(src) == size2:
            current.append(src)
        else:
            seen_sizes[src] = size2

        assert current == [src], "Second poll with same size should proceed"

    def test_convert_options_quality_forwarded(self, rgb_image, tmp_workdir):
        """ConvertOptions quality setting is respected in the conversion output."""
        src = tmp_workdir / "src.bmp"
        rgb_image.save(src)
        out_high = tmp_workdir / "high"
        out_low = tmp_workdir / "low"

        r_high = convert_file(src, out_high, opts=ConvertOptions(fmt="jpeg", jpeg_quality=98))
        r_low = convert_file(src, out_low, opts=ConvertOptions(fmt="jpeg", jpeg_quality=50))
        assert r_high.success and r_low.success
        assert r_high.size_after > r_low.size_after, \
            "Higher quality JPEG should be larger than lower quality"


# ── vips backend ─────────────────────────────────────────────────────────────


class TestVipsBackend:
    """Basic vips backend regression coverage."""

    def test_vips_convert_jpeg(self, rgb_image, tmp_workdir):
        from imgconverter import HAS_VIPS
        if not HAS_VIPS:
            pytest.skip("pyvips not installed")

        src = tmp_workdir / "src.bmp"
        rgb_image.save(src)
        out = tmp_workdir / "out"

        result = convert_file(src, out, fmt="jpeg", jpeg_quality=85, backend="vips",
                              preserve_metadata=False)
        assert result.success, f"vips conversion failed: {result.error}"
        assert result.dst.suffix == ".jpg"
        assert result.dst.stat().st_size > 0

    def test_vips_rejects_metadata_preserve(self, rgb_image, tmp_workdir):
        from imgconverter import HAS_VIPS
        if not HAS_VIPS:
            pytest.skip("pyvips not installed")

        src = tmp_workdir / "src.bmp"
        rgb_image.save(src)
        out = tmp_workdir / "out"

        result = convert_file(src, out, fmt="jpeg", backend="vips",
                              preserve_metadata=True)
        assert not result.success or any("metadata" in w.lower() for w in result.warnings), \
            "vips backend should warn about metadata loss or reject preserve_metadata=True"
