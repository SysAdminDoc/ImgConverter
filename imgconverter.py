#!/usr/bin/env python3
"""
ImgConverter v3.1.0 - Universal image batch converter
Scans directories recursively and converts JPEG, PNG, HEIC, AVIF, WebP,
JPEG XL, RAW, TIFF, BMP, JPEG 2000, QOI, and ICO files to JPEG, PNG,
WebP, AVIF, TIFF, or JPEG XL. Auto-detects optimal format: PNG for
images with transparency, JPEG for photos. Preserves EXIF, ICC, and
XMP. CLI + GUI parity. See ROADMAP.md for in-flight work.
"""

import sys, os, subprocess, importlib, platform, ctypes, argparse, shutil, tempfile
from pathlib import Path


def _branding_icon_path() -> Path:
    candidates = []
    if getattr(sys, "frozen", False):
        exe_dir = Path(sys.executable).resolve().parent
        candidates.append(exe_dir / "icon.png")
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            candidates.append(Path(meipass) / "icon.png")
    current = Path(__file__).resolve()
    candidates.extend([current.parent / "icon.png", current.parent.parent / "icon.png", current.parent.parent.parent / "icon.png"])
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return Path("icon.png")


APP_VERSION = "3.1.0"

# Structured exit-code matrix — documented in README + man-page-style.
# CI / cron / Ansible scripts can branch on these without parsing log output.
EXIT_OK              = 0   # all files converted
EXIT_PARTIAL_FAILURE = 1   # some files failed, some succeeded
EXIT_INPUT_ERROR     = 2   # bad CLI args / missing input directory / unwritable output
EXIT_DEP_MISSING     = 3   # required Python module or optional codec missing
EXIT_DISK_FULL       = 4   # output medium ran out of space mid-run
EXIT_CANCELLED       = 5   # user pressed Ctrl-C / Cancel button
EXIT_TOTAL_FAILURE   = 6   # every file in batch failed

# Dependency floors — see requirements.txt / ROADMAP Appendix A6 for CVE rationale.
# Older versions of these expose users to known libheif / libjxl / Pillow RCEs.
DEP_FLOORS = {
    "PIL":          ("Pillow",             "12.2.0"),
    "pillow_heif":  ("pillow-heif",        "1.4.0"),
    "PyQt6":        ("PyQt6",              "6.8"),
    "rawpy":        ("rawpy",              "0.27.0"),    # optional
    "pillow_jxl":   ("pillow-jxl-plugin",  "1.3.6"),     # optional
    "qoi":          ("qoi",                "0.7"),       # optional
}
REQUIRED_DEPS = ("PIL", "pillow_heif", "PyQt6")
OPTIONAL_DEPS = ("rawpy", "pillow_jxl", "qoi")


def _install_deps(include_optional: bool = False) -> int:
    """Install required (and optionally optional) deps via pip. Returns exit code."""
    targets = REQUIRED_DEPS + (OPTIONAL_DEPS if include_optional else ())
    failed = []
    for module in targets:
        pkg, floor = DEP_FLOORS[module]
        spec = f"{pkg}>={floor}"
        print(f"[install-deps] {spec} …", flush=True)
        ok = False
        for cmd_extra in ([], ["--user"]):
            try:
                subprocess.check_call(
                    [sys.executable, "-m", "pip", "install", "--upgrade", spec] + cmd_extra
                )
                ok = True
                break
            except subprocess.CalledProcessError:
                continue
        if not ok:
            failed.append(spec)
            print(
                f"[install-deps] FAILED: {spec}\n"
                f"  If on a PEP 668 system (Debian/Ubuntu), use a virtualenv:\n"
                f"  python -m venv .venv && .venv/bin/pip install -r requirements.txt",
                file=sys.stderr,
            )
    if failed:
        print(f"[install-deps] {len(failed)} package(s) failed to install.", file=sys.stderr)
        return 3
    print("[install-deps] All targets installed.")
    return 0


def _check_required_deps_or_exit():
    """Verify required deps are importable; print actionable install hint if not."""
    missing = []
    for module in REQUIRED_DEPS:
        try:
            importlib.import_module(module)
        except ImportError:
            missing.append(DEP_FLOORS[module][0])
    if missing:
        print(
            f"[imgconverter] Missing required dependencies: {', '.join(missing)}\n"
            f"  Install all required + optional deps with:\n"
            f"      {sys.executable} -m imgconverter --install-deps\n"
            f"  Or manually:\n"
            f"      pip install -r requirements.txt",
            file=sys.stderr,
        )
        sys.exit(3)


def _warn_below_floor():
    """Warn (don't exit) when an installed dep is older than the documented floor."""
    try:
        from packaging.version import Version
    except ImportError:
        return  # packaging is bundled with pip; if missing, skip the check
    for module, (pkg, floor) in DEP_FLOORS.items():
        try:
            mod = importlib.import_module(module)
        except ImportError:
            continue
        installed = getattr(mod, "__version__", None)
        if not installed:
            continue
        try:
            if Version(installed) < Version(floor):
                print(
                    f"[imgconverter] WARNING: {pkg} {installed} is below the documented "
                    f"floor of {floor}. Older versions have known CVEs — see "
                    f"ROADMAP.md Appendix A6. Run: imgconverter --install-deps",
                    file=sys.stderr,
                )
        except Exception:
            pass


# Allow `--install-deps` to run before any heavy imports.
if "--install-deps" in sys.argv:
    sys.exit(_install_deps(include_optional=True))

_check_required_deps_or_exit()

import io
import time
import json
import threading
import traceback
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field

from PIL import Image, ImageCms, ImageOps
import pillow_heif
from pillow_heif import register_heif_opener

register_heif_opener()

# AVIF — Pillow 11.3+ ships native libaom/dav1d AVIF support. Prefer it
# over pillow_heif's AVIF path (which itself was deprecated in
# pillow_heif 1.0). Smaller binary footprint, no x265 GPL surface, and
# pillow_heif >=1.3 no longer offers an AVIF encoder at all.
HAS_AVIF = False
try:
    from PIL import AvifImagePlugin  # noqa: F401  registers .avif handler
    HAS_AVIF = "AVIF" in Image.SAVE
except ImportError:
    HAS_AVIF = False

# libheif memory cap — a hostile HEIC/AVIF can otherwise OOM the host via
# the SAO heap-overflow path (CVE-2025-29482, fixed in libheif 1.19.7+).
# 4 GB ceiling is generous for legitimate gigapixel scans, fatal for fuzz inputs.
HEIF_MAX_DECODE_BYTES = 4 * 1024 * 1024 * 1024
try:
    _opts = pillow_heif.options
    # API surface differs across pillow_heif versions; tolerate missing keys.
    for _attr, _val in (("DECODE_THREADS", max(1, os.cpu_count() or 1)),
                        ("ALLOW_INCORRECT_HEADERS", False)):
        if hasattr(_opts, _attr):
            setattr(_opts, _attr, _val)
    set_limits = getattr(pillow_heif, "set_security_limits", None)
    if callable(set_limits):
        set_limits(max_image_size_pixels=8000 * 8000)  # 64 MP guard; raise via env if needed
except Exception as _heif_sec_err:
    print(f"[WARN] HEIF security limits could not be set: {_heif_sec_err}", file=sys.stderr)

# Optional: JPEG XL plugin (registers into Pillow automatically on import)
HAS_JXL = False
try:
    import pillow_jxl  # noqa: F401
    HAS_JXL = True
except ImportError:
    pass

# Optional: RAW format support via rawpy (wraps libraw)
HAS_RAWPY = False
try:
    import rawpy
    HAS_RAWPY = True
except ImportError:
    pass

# Optional: QOI format support
HAS_QOI = False
try:
    import qoi as qoi_lib
    HAS_QOI = True
except ImportError:
    pass

# Optional: ExifTool for full metadata transport (MakerNotes, GPS sub-IFDs, IPTC,
# sidecar XMP, etc.) — Pillow's EXIF model drops these silently, which is the #1
# community complaint about HEIC->JPG converters. See ROADMAP Appendix A3.
EXIFTOOL_PATH = shutil.which("exiftool")
HAS_EXIFTOOL = EXIFTOOL_PATH is not None

# Optional pyvips backend — streaming pipeline that beats Pillow on >100 MP
# scans because it tile-processes instead of holding the full bitmap.
HAS_VIPS = False
try:
    import pyvips  # noqa: F401
    HAS_VIPS = True
except (ImportError, OSError):
    pyvips = None


def _vips_convert(src: Path, dst: Path, fmt: str, quality: int) -> tuple[bool, str]:
    """Fast-path conversion through libvips. Returns (ok, msg)."""
    if not HAS_VIPS:
        return False, "pyvips not installed"
    try:
        opts = {"access": "sequential"}
        image = pyvips.Image.new_from_file(str(src), **opts)
        save_args = {}
        if fmt == "jpeg":
            save_args["Q"] = quality
        elif fmt == "webp":
            save_args["Q"] = quality
        elif fmt == "avif":
            save_args["Q"] = quality
        elif fmt == "png":
            save_args["compression"] = 6
        elif fmt == "tiff":
            save_args["compression"] = "none"
        image.write_to_file(str(dst), **save_args)
        return True, "vips"
    except Exception as e:
        return False, str(e)


# Optional ffmpeg-quality-metrics / butteraugli for objective quality check.
HAS_FQM = shutil.which("ffmpeg-quality-metrics") is not None
BUTTERAUGLI_PATH = shutil.which("butteraugli")


def _verify_quality(src: Path, dst: Path) -> str | None:
    """Return a one-line quality summary string, or None if no tool is available."""
    if BUTTERAUGLI_PATH:
        try:
            pr = subprocess.run(
                [BUTTERAUGLI_PATH, str(src), str(dst)],
                capture_output=True, text=True, timeout=30,
            )
            if pr.returncode == 0:
                return f"butteraugli: {pr.stdout.strip().splitlines()[-1]}"
        except Exception:
            pass
    if HAS_FQM:
        try:
            pr = subprocess.run(
                ["ffmpeg-quality-metrics", str(src), str(dst), "-m", "psnr,ssim"],
                capture_output=True, text=True, timeout=60,
            )
            if pr.returncode == 0:
                # ffmpeg-quality-metrics outputs JSON
                data = json.loads(pr.stdout)
                psnr = data.get("global", {}).get("psnr", {}).get("psnr_avg")
                ssim = data.get("global", {}).get("ssim", {}).get("ssim_avg")
                if psnr is not None and ssim is not None:
                    return f"ffmpeg-quality-metrics: PSNR={psnr:.2f}dB SSIM={ssim:.4f}"
        except Exception:
            pass
    return None


# Plugin system — drop trusted .py files into ~/.imgconverter/plugins/ defining
# a top-level register(opts) callable. Decoder / Encoder hook signatures are
# documented in PLUGINS.md.
PLUGIN_TRUST_SCHEMA = 1
PLUGIN_TRUST_FILE = "trusted-plugins.json"


def _plugin_dir() -> Path:
    return Path.home() / ".imgconverter" / "plugins"


def _plugin_trust_path() -> Path:
    return _plugin_dir() / PLUGIN_TRUST_FILE


def _load_plugin_trust() -> dict[str, dict]:
    path = _plugin_trust_path()
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        print(f"[plugins] ignoring unreadable trust manifest: {e}", file=sys.stderr)
        return {}
    records = data.get("plugins", {}) if isinstance(data, dict) else {}
    if not isinstance(records, dict):
        return {}
    return {
        str(name): record
        for name, record in records.items()
        if isinstance(record, dict)
    }


def _write_plugin_trust(records: dict[str, dict]) -> None:
    path = _plugin_trust_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema": PLUGIN_TRUST_SCHEMA,
        "plugins": dict(sorted(records.items())),
    }
    import tempfile
    fd, tmp_name = tempfile.mkstemp(
        prefix=".trusted-plugins.",
        suffix=".tmp",
        dir=str(path.parent),
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fp:
            json.dump(payload, fp, indent=2, sort_keys=True)
            fp.write("\n")
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _resolve_plugin_ref(ref: str | Path) -> Path:
    plugin_dir = _plugin_dir()
    raw = Path(ref).expanduser()
    candidate = raw if raw.is_absolute() or raw.parent != Path(".") else plugin_dir / raw
    if candidate.suffix.lower() != ".py":
        raise ValueError("plugin must be a .py file")
    if candidate.name.startswith("_"):
        raise ValueError("helper plugins beginning with '_' are not executable entry points")
    if candidate.is_symlink():
        raise ValueError("symlinked plugin files are not supported")
    if not candidate.is_file():
        raise ValueError(f"plugin not found: {candidate}")
    try:
        if candidate.parent.resolve() != plugin_dir.resolve():
            raise ValueError(f"plugin must live in {plugin_dir}")
    except OSError as e:
        raise ValueError(f"plugin path cannot be resolved: {e}") from e
    return candidate


def _plugin_name_from_ref(ref: str | Path) -> str:
    name = Path(ref).name
    if Path(name).suffix == "":
        name += ".py"
    return name


def _plugin_trust_status(py: Path, records: dict[str, dict]) -> tuple[str, str]:
    if py.name.startswith("_"):
        return "skipped", "helper module"
    if py.suffix.lower() != ".py":
        return "skipped", "not a Python plugin"
    if py.is_symlink():
        return "blocked", "symlinked plugin files are not loaded"
    if not py.is_file():
        return "blocked", "not a regular file"
    try:
        digest = _file_sha256(py)
    except OSError as e:
        return "blocked", str(e)
    record = records.get(py.name)
    if not record:
        return "untrusted", f"run --trust-plugin {py.name} after auditing it"
    if record.get("sha256") != digest:
        return "changed", f"content hash changed; re-audit and run --trust-plugin {py.name}"
    return "trusted", digest


def _trust_plugin(ref: str | Path) -> tuple[bool, str]:
    try:
        py = _resolve_plugin_ref(ref)
        digest = _file_sha256(py)
        records = _load_plugin_trust()
        records[py.name] = {
            "sha256": digest,
            "trusted_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "path": str(py),
        }
        _write_plugin_trust(records)
        return True, f"[plugins] trusted {py.name} ({digest[:12]})"
    except (OSError, ValueError) as e:
        return False, f"[plugins] trust failed: {e}"


def _untrust_plugin(ref: str | Path) -> tuple[bool, str]:
    name = _plugin_name_from_ref(ref)
    records = _load_plugin_trust()
    if name not in records:
        return False, f"[plugins] no trusted entry for {name}"
    del records[name]
    _write_plugin_trust(records)
    return True, f"[plugins] removed trust for {name}"


def _list_plugins() -> int:
    plugin_dir = _plugin_dir()
    records = _load_plugin_trust()
    print(f"[plugins] directory: {plugin_dir}")
    if not plugin_dir.is_dir():
        print("[plugins] no plugin directory")
        return EXIT_OK
    seen = set()
    for py in sorted(plugin_dir.glob("*.py")):
        seen.add(py.name)
        status, detail = _plugin_trust_status(py, records)
        suffix = detail[:12] if status == "trusted" else detail
        print(f"[plugins] {py.name}: {status} ({suffix})")
    for name in sorted(set(records) - seen):
        print(f"[plugins] {name}: missing trusted entry")
    return EXIT_OK


def _load_plugins() -> list[str]:
    """Discover and import trusted user plugins. Returns loaded module names."""
    plugin_dir = _plugin_dir()
    if not plugin_dir.is_dir():
        return []
    trust_records = _load_plugin_trust()
    loaded = []
    for py in sorted(plugin_dir.glob("*.py")):
        status, detail = _plugin_trust_status(py, trust_records)
        if status != "trusted":
            print(f"[plugins] skipped {py.name}: {detail}", file=sys.stderr)
            continue
        try:
            mod_name = f"imgconverter_plugin_{py.stem}"
            spec = importlib.util.spec_from_file_location(mod_name, py)
            if spec and spec.loader:
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
                # If plugin has register() at module level, call it.
                if hasattr(mod, "register"):
                    mod.register({"app_version": APP_VERSION})
                loaded.append(py.stem)
        except Exception as e:
            print(f"[plugins] failed to load {py.name}: {e}", file=sys.stderr)
    return loaded

# Optional: jpegoptim / jpegtran for lossless JPEG recompression — bit-preserving
# size reduction without re-encoding pixels. Either is sufficient.
JPEGOPTIM_PATH = shutil.which("jpegoptim")
JPEGTRAN_PATH = shutil.which("jpegtran")
PNGQUANT_PATH = shutil.which("pngquant")
HAS_JPEG_RECOMPRESS = JPEGOPTIM_PATH is not None or JPEGTRAN_PATH is not None


def _recompress_jpeg_lossless(src: Path, dst: Path, strip_metadata: bool) -> tuple[bool, str]:
    """Copy src -> dst then run jpegoptim/jpegtran on dst. Pixel-preserving."""
    try:
        shutil.copy2(src, dst)
    except OSError as e:
        return False, f"copy failed: {e}"
    if JPEGOPTIM_PATH:
        cmd = [JPEGOPTIM_PATH, "--overwrite", "--quiet"]
        if strip_metadata:
            cmd.append("--strip-all")
        cmd.append(str(dst))
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
            if proc.returncode != 0:
                return False, (proc.stderr or "jpegoptim failed").strip()
            return True, "jpegoptim"
        except subprocess.TimeoutExpired:
            return False, "jpegoptim timed out"
    if JPEGTRAN_PATH:
        # jpegtran -copy all -optimize -progressive -outfile <tmp> <src>
        tmp = dst.with_suffix(dst.suffix + ".jpegtran.tmp")
        cmd = [JPEGTRAN_PATH, "-copy", "none" if strip_metadata else "all",
               "-optimize", "-outfile", str(tmp), str(dst)]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
            if proc.returncode != 0:
                tmp.unlink(missing_ok=True)
                return False, (proc.stderr or "jpegtran failed").strip()
            os.replace(str(tmp), str(dst))
            return True, "jpegtran"
        except subprocess.TimeoutExpired:
            tmp.unlink(missing_ok=True)
            return False, "jpegtran timed out"
    return False, "no jpegoptim or jpegtran on PATH"


def _run_exiftool_copy(src: Path, dst: Path) -> tuple[bool, str]:
    """Copy *all* metadata from src to dst using ExifTool. Returns (ok, message)."""
    if not HAS_EXIFTOOL:
        return False, "exiftool not installed"
    try:
        # -overwrite_original avoids _original sidecars; -P preserves dst mtime;
        # -tagsfromfile copies every tag including MakerNotes / sub-IFDs / IPTC;
        # -icc_profile is normally protected, so add it explicitly.
        proc = subprocess.run(
            [EXIFTOOL_PATH, "-overwrite_original", "-P",
             "-tagsfromfile", str(src),
             "-all:all", "-unsafe", "-icc_profile",
             str(dst)],
            capture_output=True, text=True, timeout=30,
        )
        if proc.returncode != 0:
            return False, (proc.stderr or proc.stdout or "exiftool failed").strip()
        return True, "metadata copied"
    except subprocess.TimeoutExpired:
        return False, "exiftool timed out"
    except Exception as e:
        return False, str(e)

_CLI_ONLY = any(a in sys.argv for a in ("--input", "-i", "--files", "--install-deps", "--version",
                                         "--list-presets", "--list-plugins", "--help", "-h"))
try:
    from PyQt6.QtCore import (
        Qt, QThread, pyqtSignal, QTimer, QSettings, QSize, QUrl,
    )
    from PyQt6.QtGui import (
        QFont, QColor, QPalette, QIcon, QPixmap, QPainter, QAction,
        QDragEnterEvent, QDropEvent,
    )
    from PyQt6.QtWidgets import (
        QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
        QLabel, QPushButton, QFileDialog, QComboBox, QSpinBox, QSlider,
        QProgressBar, QPlainTextEdit, QCheckBox, QGroupBox, QGridLayout,
        QFrame, QSplitter, QStatusBar, QMessageBox, QLineEdit, QStyle,
        QSystemTrayIcon, QMenu, QToolButton, QScrollArea, QSizePolicy,
    )
    HAS_PYQT6 = True
except ImportError:
    HAS_PYQT6 = False
    if not _CLI_ONLY:
        print("[ERROR] PyQt6 is required for GUI mode. Install it:\n"
              "  pip install PyQt6>=6.8\n"
              "Or use CLI mode: imgconverter --input ./photos --format jpeg",
              file=sys.stderr)
        sys.exit(EXIT_DEP_MISSING)

    class _Stub:
        pass

    def _signal_stub(*a, **kw):
        return None

    QThread = QMainWindow = QWidget = _Stub
    pyqtSignal = _signal_stub
    Qt = QSettings = QSize = QUrl = _Stub
    QFont = QColor = QPalette = QIcon = QPixmap = QPainter = QAction = _Stub
    QDragEnterEvent = QDropEvent = _Stub
    QApplication = QVBoxLayout = QHBoxLayout = _Stub
    QLabel = QPushButton = QFileDialog = QComboBox = QSpinBox = QSlider = _Stub
    QProgressBar = QPlainTextEdit = QCheckBox = QGroupBox = QGridLayout = _Stub
    QFrame = QSplitter = QStatusBar = QMessageBox = QLineEdit = QStyle = _Stub
    QSystemTrayIcon = QMenu = QToolButton = QScrollArea = QSizePolicy = _Stub
    QTimer = _Stub

# ── Catppuccin Mocha Palette ──────────────────────────────────────────────────
CAT = {
    "base":      "#1e1e2e", "mantle":   "#181825", "crust":    "#11111b",
    "surface0":  "#313244", "surface1": "#45475a", "surface2": "#585b70",
    "overlay0":  "#6c7086", "overlay1": "#7f849c", "overlay2": "#9399b2",
    "text":      "#cdd6f4", "subtext0": "#a6adc8", "subtext1": "#bac2de",
    "lavender":  "#b4befe", "blue":     "#89b4fa", "sapphire": "#74c7ec",
    "sky":       "#89dceb", "teal":     "#94e2d5", "green":    "#a6e3a1",
    "yellow":    "#f9e2af", "peach":    "#fab387", "maroon":   "#eba0ac",
    "red":       "#f38ba8", "mauve":    "#cba6f7", "pink":     "#f5c2e7",
    "flamingo":  "#f2cdcd", "rosewater":"#f5e0dc",
}

STAT_VALUE_STYLE = f"color: {CAT['green']}; font-size: 22px; font-weight: 700;"

STYLESHEET = f"""
QMainWindow {{
    background-color: {CAT['base']};
}}
QWidget {{
    background-color: {CAT['base']};
    color: {CAT['text']};
    font-family: 'Segoe UI', 'Inter', sans-serif;
    font-size: 13px;
}}
QLabel, QCheckBox {{
    background-color: transparent;
}}
QFrame#appHeader {{
    background-color: {CAT['mantle']};
    border: 1px solid {CAT['surface0']};
    border-radius: 8px;
}}
QLabel#appTitle {{
    color: {CAT['text']};
    font-size: 22px;
    font-weight: 800;
}}
QLabel#appVersion {{
    color: {CAT['overlay2']};
    font-size: 12px;
    font-weight: 600;
}}
QLabel#appSubtitle {{
    color: {CAT['subtext0']};
    font-size: 12px;
}}
QLabel#workflowState {{
    color: {CAT['lavender']};
    background-color: {CAT['surface0']};
    border: 1px solid {CAT['surface1']};
    border-radius: 6px;
    padding: 5px 10px;
    font-size: 12px;
    font-weight: 700;
}}
QLabel#fieldLabel {{
    color: {CAT['subtext1']};
    font-size: 12px;
    font-weight: 600;
}}
QGroupBox {{
    background-color: {CAT['mantle']};
    border: 1px solid {CAT['surface0']};
    border-radius: 8px;
    margin-top: 16px;
    padding: 18px 12px 12px 12px;
    font-weight: 700;
    font-size: 13px;
    color: {CAT['lavender']};
}}
QGroupBox::title {{
    subcontrol-origin: margin;
    subcontrol-position: top left;
    padding: 2px 10px 3px 10px;
    background-color: {CAT['mantle']};
    border-radius: 4px;
}}
QPushButton {{
    background-color: {CAT['surface0']};
    color: {CAT['text']};
    border: 1px solid {CAT['surface1']};
    border-radius: 6px;
    padding: 7px 16px;
    font-weight: 600;
    min-height: 22px;
}}
QPushButton:hover {{
    background-color: {CAT['surface1']};
    border-color: {CAT['lavender']};
}}
QPushButton:focus {{
    border: 1px solid {CAT['blue']};
}}
QPushButton:pressed {{
    background-color: {CAT['surface2']};
}}
QPushButton:disabled {{
    background-color: {CAT['crust']};
    color: {CAT['overlay0']};
    border-color: {CAT['surface0']};
}}
QPushButton#primaryBtn {{
    background-color: {CAT['blue']};
    color: {CAT['crust']};
    border: 1px solid {CAT['blue']};
    font-weight: 800;
    font-size: 14px;
    padding: 10px 24px;
}}
QPushButton#primaryBtn:hover {{
    background-color: {CAT['lavender']};
    border-color: {CAT['lavender']};
}}
QPushButton#primaryBtn:disabled {{
    background-color: {CAT['surface1']};
    color: {CAT['overlay0']};
    border-color: {CAT['surface1']};
}}
QPushButton#stopBtn {{
    background-color: {CAT['red']};
    color: {CAT['crust']};
    border: 1px solid {CAT['red']};
    font-weight: 800;
}}
QPushButton#stopBtn:hover {{
    background-color: {CAT['maroon']};
    border-color: {CAT['maroon']};
}}
QPushButton#stopBtn:pressed {{
    background-color: {CAT['surface2']};
}}
QPushButton#stopBtn:disabled {{
    background-color: {CAT['surface1']};
    color: {CAT['overlay0']};
    border: none;
}}
QPushButton#miniBtn {{
    font-size: 11px;
    padding: 3px 10px;
    min-height: 18px;
}}
QLineEdit {{
    background-color: {CAT['surface0']};
    color: {CAT['text']};
    border: 1px solid {CAT['surface1']};
    border-radius: 6px;
    padding: 7px 10px;
    selection-background-color: {CAT['blue']};
}}
QLineEdit:focus {{
    border: 1px solid {CAT['blue']};
}}
QComboBox {{
    background-color: {CAT['surface0']};
    color: {CAT['text']};
    border: 1px solid {CAT['surface1']};
    border-radius: 6px;
    padding: 7px 10px;
    min-width: 120px;
}}
QComboBox:hover {{
    border-color: {CAT['lavender']};
}}
QComboBox:focus {{
    border: 1px solid {CAT['blue']};
}}
QComboBox::drop-down {{
    border: none;
    width: 24px;
}}
QComboBox QAbstractItemView {{
    background-color: {CAT['surface0']};
    color: {CAT['text']};
    border: 1px solid {CAT['surface1']};
    selection-background-color: {CAT['surface1']};
    selection-color: {CAT['lavender']};
}}
QSpinBox {{
    background-color: {CAT['surface0']};
    color: {CAT['text']};
    border: 1px solid {CAT['surface1']};
    border-radius: 6px;
    padding: 5px 8px;
}}
QSpinBox:focus {{
    border: 1px solid {CAT['blue']};
}}
QSlider::groove:horizontal {{
    background: {CAT['surface0']};
    height: 6px;
    border-radius: 3px;
}}
QSlider::handle:horizontal {{
    background: {CAT['lavender']};
    width: 18px;
    height: 18px;
    margin: -6px 0;
    border-radius: 8px;
}}
QSlider::sub-page:horizontal {{
    background: {CAT['blue']};
    border-radius: 3px;
}}
QProgressBar {{
    background-color: {CAT['surface0']};
    border: 1px solid {CAT['surface1']};
    border-radius: 6px;
    text-align: center;
    color: {CAT['text']};
    font-weight: 600;
    min-height: 22px;
}}
QProgressBar::chunk {{
    background-color: {CAT['green']};
    border-radius: 5px;
}}
QPlainTextEdit {{
    background-color: {CAT['crust']};
    color: {CAT['subtext0']};
    border: 1px solid {CAT['surface0']};
    border-radius: 6px;
    padding: 8px;
    font-family: 'Cascadia Code', 'Consolas', monospace;
    font-size: 12px;
    selection-background-color: {CAT['surface1']};
}}
QCheckBox {{
    spacing: 8px;
    color: {CAT['text']};
}}
QCheckBox:hover {{
    color: {CAT['lavender']};
}}
QCheckBox::indicator {{
    width: 18px;
    height: 18px;
    border-radius: 4px;
    border: 2px solid {CAT['surface2']};
    background-color: {CAT['surface0']};
}}
QCheckBox::indicator:checked {{
    background-color: {CAT['blue']};
    border-color: {CAT['blue']};
}}
QCheckBox::indicator:disabled {{
    background-color: {CAT['crust']};
    border-color: {CAT['surface0']};
}}
QLabel#dimLabel {{
    color: {CAT['overlay2']};
    font-size: 12px;
}}
QLabel#statValue {{
    color: {CAT['green']};
    font-size: 22px;
    font-weight: 700;
}}
QLabel#statLabel {{
    color: {CAT['overlay2']};
    font-size: 11px;
    font-weight: 600;
}}
QFrame#actionBar, QFrame#statsFrame {{
    background-color: {CAT['mantle']};
    border: 1px solid {CAT['surface0']};
    border-radius: 8px;
}}
QFrame#statCard {{
    background-color: {CAT['base']};
    border: 1px solid {CAT['surface0']};
    border-radius: 8px;
}}
QStatusBar {{
    background-color: {CAT['mantle']};
    color: {CAT['subtext0']};
    border-top: 1px solid {CAT['surface0']};
    font-size: 12px;
}}
QFrame#separator {{
    background-color: {CAT['surface1']};
    max-height: 1px;
}}
QScrollBar:vertical {{
    background: {CAT['mantle']};
    width: 10px;
    margin: 0;
    border-radius: 5px;
}}
QScrollBar::handle:vertical {{
    background: {CAT['surface2']};
    min-height: 30px;
    border-radius: 5px;
}}
QScrollBar::handle:vertical:hover {{
    background: {CAT['overlay0']};
}}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
    height: 0;
    background: none;
}}
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{
    background: none;
}}
QScrollBar:horizontal {{
    background: {CAT['mantle']};
    height: 10px;
    margin: 0;
    border-radius: 5px;
}}
QScrollBar::handle:horizontal {{
    background: {CAT['surface2']};
    min-width: 30px;
    border-radius: 5px;
}}
QScrollBar::handle:horizontal:hover {{
    background: {CAT['overlay0']};
}}
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{
    width: 0;
    background: none;
}}
QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal {{
    background: none;
}}
QToolButton {{
    background-color: {CAT['surface0']};
    color: {CAT['text']};
    border: 1px solid {CAT['surface1']};
    border-radius: 6px;
    padding: 6px;
}}
QToolButton:hover {{
    background-color: {CAT['surface1']};
    border-color: {CAT['lavender']};
}}
QToolButton:focus {{
    border: 1px solid {CAT['blue']};
}}
QToolButton#advancedToggle {{
    background-color: {CAT['mantle']};
    border: 1px solid {CAT['surface0']};
    color: {CAT['subtext1']};
    font-weight: 700;
    padding: 9px 12px;
    text-align: left;
}}
QToolButton#advancedToggle:hover {{
    color: {CAT['text']};
    border-color: {CAT['surface2']};
}}
QToolButton::menu-indicator {{
    image: none;
}}
QMenu {{
    background-color: {CAT['surface0']};
    color: {CAT['text']};
    border: 1px solid {CAT['surface1']};
    border-radius: 6px;
    padding: 4px;
}}
QMenu::item {{
    padding: 6px 20px;
    border-radius: 4px;
}}
QMenu::item:selected {{
    background-color: {CAT['surface1']};
    color: {CAT['lavender']};
}}
"""


# ── Supported Input Formats ───────────────────────────────────────────────────

# Always available (pillow-heif plugin)
HEIC_EXTS = {".heic", ".heif", ".hif"}
AVIF_EXTS = {".avif"}

# Always available (Pillow built-in)
JPEG_EXTS  = {".jpg", ".jpeg", ".jpe", ".jfif"}
PNG_EXTS   = {".png"}
WEBP_EXTS  = {".webp"}
TIFF_EXTS  = {".tif", ".tiff"}
BMP_EXTS   = {".bmp"}
JP2_EXTS   = {".jp2", ".j2k", ".jpx"}
ICO_EXTS   = {".ico", ".cur"}

# Conditional on optional deps
JXL_EXTS = {".jxl"}
RAW_EXTS = {".cr2", ".cr3", ".nef", ".arw", ".dng", ".orf", ".rw2", ".raf"}
QOI_EXTS = {".qoi"}

# Family name -> (extension set, availability flag)
FORMAT_FAMILIES = {
    "JPEG":        (JPEG_EXTS, True),
    "PNG":         (PNG_EXTS, True),
    "HEIC/HEIF":   (HEIC_EXTS, True),
    "AVIF":        (AVIF_EXTS, True),
    "WebP":        (WEBP_EXTS, True),
    "TIFF":        (TIFF_EXTS, True),
    "BMP":         (BMP_EXTS, True),
    "JPEG 2000":   (JP2_EXTS, True),
    "ICO/CUR":     (ICO_EXTS, True),
    "JPEG XL":     (JXL_EXTS, HAS_JXL),
    "Camera RAW":  (RAW_EXTS, HAS_RAWPY),
    "QOI":         (QOI_EXTS, HAS_QOI),
}


def get_supported_extensions() -> set[str]:
    """Return all input extensions we can currently decode."""
    exts = JPEG_EXTS | PNG_EXTS | HEIC_EXTS | AVIF_EXTS | WEBP_EXTS | TIFF_EXTS | BMP_EXTS | JP2_EXTS | ICO_EXTS
    if HAS_JXL:
        exts |= JXL_EXTS
    if HAS_RAWPY:
        exts |= RAW_EXTS
    if HAS_QOI:
        exts |= QOI_EXTS
    return exts


def get_format_support_summary() -> str:
    """Human-readable list of supported format families."""
    families = ["JPEG", "PNG", "HEIC/HEIF", "AVIF", "WebP", "TIFF", "BMP", "JPEG 2000", "ICO/CUR"]
    if HAS_JXL:
        families.append("JPEG XL")
    if HAS_RAWPY:
        families.append("Camera RAW")
    if HAS_QOI:
        families.append("QOI")
    return ", ".join(families)


# ── Data ──────────────────────────────────────────────────────────────────────

@dataclass
class ConvertResult:
    src: Path
    dst: Path | None = None
    success: bool = False
    skipped: bool = False
    error: str = ""
    size_before: int = 0
    size_after: int = 0
    elapsed: float = 0.0
    src_deleted: bool = False
    warnings: list[str] = field(default_factory=list)

@dataclass
class ScanResult:
    files: list[Path] = field(default_factory=list)
    total_size: int = 0
    elapsed: float = 0.0


def _parse_size_spec(spec: str) -> int | None:
    """Parse a human-readable size like '500MB' or '2GB' to bytes."""
    if not spec:
        return None
    spec = spec.strip().upper()
    multipliers = {"B": 1, "KB": 1024, "MB": 1024**2, "GB": 1024**3, "TB": 1024**4}
    for suffix, mult in sorted(multipliers.items(), key=lambda x: -len(x[0])):
        if spec.endswith(suffix):
            try:
                return int(float(spec[:-len(suffix)].strip()) * mult)
            except ValueError:
                return None
    try:
        return int(spec)
    except ValueError:
        return None


# ── Conversion Engine ─────────────────────────────────────────────────────────

def _path_matches_exclude(rel: Path, exclude_patterns: list[str] | None = None) -> bool:
    """Return True when a relative path matches any CLI/GUI exclude glob."""
    import fnmatch
    s = rel.as_posix()
    for pat in exclude_patterns or []:
        norm = pat.replace("\\", "/")
        if fnmatch.fnmatchcase(s, norm) or fnmatch.fnmatchcase(rel.name, norm):
            return True
        if norm.endswith("/**"):
            prefix = norm[:-3].rstrip("/")
            if s == prefix or s.startswith(prefix + "/"):
                return True
        if rel.match(norm) or Path(s).match(norm):
            return True
    return False


def scan_directory(
    root: Path,
    recursive: bool = True,
    extensions: set[str] | None = None,
    on_progress=None,
    exclude_patterns: list[str] | None = None,
    max_file_size: int | None = None,
) -> ScanResult:
    """Find all supported image files in directory.

    Resilient to symlink loops via realpath-based visited-set bookkeeping.
    Optional ``exclude_patterns`` is a list of glob-style patterns (e.g.
    ``["*.thumb.*", "**/cache/**"]``) tested against the *relative* path.
    """
    t0 = time.perf_counter()
    supported = extensions or get_supported_extensions()
    result = ScanResult()
    current_dir = None
    dir_count = 0
    visited_dirs: set[str] = set()
    exclude_patterns = exclude_patterns or []

    def _walk(d: Path):
        # Resolve to catch loops via symlink-to-ancestor; skip on permission errors.
        try:
            real = str(d.resolve(strict=False))
        except OSError:
            return
        if real in visited_dirs:
            return
        visited_dirs.add(real)
        try:
            entries = list(d.iterdir())
        except (PermissionError, OSError):
            return
        for p in entries:
            try:
                if p.is_dir():
                    try:
                        rel_dir = p.relative_to(root)
                    except ValueError:
                        rel_dir = p
                    if _path_matches_exclude(rel_dir, exclude_patterns):
                        continue
                    if recursive:
                        _walk(p)
                    continue
                if not p.is_file():
                    continue
            except OSError:
                continue
            if p.suffix.lower() not in supported:
                continue
            try:
                rel = p.relative_to(root)
            except ValueError:
                rel = p
            if _path_matches_exclude(rel, exclude_patterns):
                continue
            try:
                st = p.stat()
            except OSError:
                continue
            if max_file_size is not None and st.st_size > max_file_size:
                continue
            result.files.append(p)
            result.total_size += st.st_size
            nonlocal current_dir, dir_count
            if p.parent != current_dir:
                if on_progress and current_dir is not None:
                    on_progress(len(result.files), result.total_size, str(current_dir), dir_count)
                current_dir = p.parent
                dir_count = 1
            else:
                dir_count += 1

    _walk(root)
    if on_progress and current_dir is not None:
        on_progress(len(result.files), result.total_size, str(current_dir), dir_count)
    result.files.sort()
    result.elapsed = time.perf_counter() - t0
    return result


_TEMPLATE_TOKENS = """
Supported tokens (case-sensitive, surrounded by curly braces):
  {stem}      source basename without extension
  {ext}       output extension (jpg / png / webp / avif / tiff / jxl)
  {fmt}       output format, lowercase ("jpeg", "png", ...)
  {src_dir}   leaf source directory name
  {rel_dir}   source path relative to scan root (preserves subdirs)
  {width}     output pixel width
  {height}    output pixel height
  {date}      file mtime as YYYY-MM-DD; override format via {date:%Y%m}
  {seq}       1-based sequence number; pad width via {seq:###} for zero-pad
"""


def _apply_output_template(
    template: str,
    src: Path,
    base_dir: Path | None,
    width: int,
    height: int,
    fmt: str,
    ext: str,
    seq: int,
) -> str:
    """Substitute roadmap-defined tokens in ``template``. Unknown tokens left intact."""
    import re
    from datetime import datetime

    rel_dir = ""
    if base_dir is not None:
        try:
            rel_dir = str(src.parent.relative_to(base_dir)).replace("\\", "/")
            if rel_dir == ".":
                rel_dir = ""
        except ValueError:
            rel_dir = ""

    src_dir = src.parent.name

    try:
        mtime = datetime.fromtimestamp(src.stat().st_mtime)
    except OSError:
        mtime = datetime.now()

    def repl(match: "re.Match[str]") -> str:
        key = match.group(1)
        spec = match.group(2)
        if key == "stem":
            return src.stem
        if key == "ext":
            return ext.lstrip(".")
        if key == "fmt":
            return fmt.lower()
        if key == "src_dir":
            return src_dir
        if key == "rel_dir":
            return rel_dir
        if key == "width":
            return str(width)
        if key == "height":
            return str(height)
        if key == "date":
            return mtime.strftime(spec) if spec else mtime.strftime("%Y-%m-%d")
        if key == "seq":
            if spec and set(spec) == {"#"}:
                return str(seq).zfill(len(spec))
            return str(seq)
        return match.group(0)  # leave unknown token untouched

    return re.sub(r"\{([a-z_]+)(?::([^}]*))?\}", repl, template)


_WATERMARK_POSITIONS = {
    "top-left":      lambda iw, ih, ww, wh, m: (m, m),
    "top":           lambda iw, ih, ww, wh, m: ((iw - ww) // 2, m),
    "top-right":     lambda iw, ih, ww, wh, m: (iw - ww - m, m),
    "left":          lambda iw, ih, ww, wh, m: (m, (ih - wh) // 2),
    "center":        lambda iw, ih, ww, wh, m: ((iw - ww) // 2, (ih - wh) // 2),
    "right":         lambda iw, ih, ww, wh, m: (iw - ww - m, (ih - wh) // 2),
    "bottom-left":   lambda iw, ih, ww, wh, m: (m, ih - wh - m),
    "bottom":        lambda iw, ih, ww, wh, m: ((iw - ww) // 2, ih - wh - m),
    "bottom-right":  lambda iw, ih, ww, wh, m: (iw - ww - m, ih - wh - m),
}


def _apply_watermark(img: "Image.Image", spec: str) -> "Image.Image":
    """spec format: 'TEXT|position|opacity' or 'image.png|position|opacity'.

    position one of: top-left top top-right left center right
                     bottom-left bottom bottom-right (default: bottom-right)
    opacity: float 0.0-1.0 (default 0.6)
    margin: hard-coded 16 px for now; configurable in a later pass.
    """
    parts = spec.split("|")
    payload = parts[0]
    position = parts[1] if len(parts) > 1 and parts[1] else "bottom-right"
    try:
        opacity = float(parts[2]) if len(parts) > 2 and parts[2] else 0.6
    except ValueError:
        opacity = 0.6
    opacity = max(0.0, min(1.0, opacity))
    margin = 16
    pos_fn = _WATERMARK_POSITIONS.get(position, _WATERMARK_POSITIONS["bottom-right"])

    base = img.convert("RGBA") if img.mode != "RGBA" else img.copy()
    iw, ih = base.size

    # Treat payload as a filesystem path when it exists; otherwise text.
    payload_path = Path(payload) if payload else None
    if payload_path and payload_path.is_file():
        from PIL import Image as _Image
        mark = _Image.open(payload_path).convert("RGBA")
    else:
        from PIL import ImageDraw, ImageFont
        font_size = max(20, min(iw, ih) // 24)
        try:
            font = ImageFont.truetype("DejaVuSans-Bold.ttf", font_size)
        except OSError:
            font = ImageFont.load_default()
        # Measure text once on a throwaway draw.
        probe_layer = _Image_module_alias().new("RGBA", (iw, ih), (0, 0, 0, 0))
        d = ImageDraw.Draw(probe_layer)
        try:
            bbox = d.textbbox((0, 0), payload, font=font)
            tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
        except Exception:
            tw, th = d.textlength(payload, font=font), font_size
        mark = _Image_module_alias().new("RGBA", (int(tw) + 8, int(th) + 8), (0, 0, 0, 0))
        d2 = ImageDraw.Draw(mark)
        d2.text((4, 4), payload, font=font, fill=(255, 255, 255, 255))

    if opacity < 1.0:
        alpha = mark.split()[-1].point(lambda a: int(a * opacity))
        mark.putalpha(alpha)

    ww, wh = mark.size
    pos = pos_fn(iw, ih, ww, wh, margin)
    base.alpha_composite(mark, dest=pos)
    return base


def _Image_module_alias():
    # Local import alias to avoid shadowing the top-level Image name in this helper.
    from PIL import Image as _I
    return _I


def _apply_canvas(img: "Image.Image", canvas_size: tuple[int, int],
                   bg_color: tuple[int, int, int, int] | str = (0, 0, 0, 0)) -> "Image.Image":
    """Center the image on a new canvas of ``canvas_size`` filled with ``bg_color``."""
    cw, ch = canvas_size
    base = Image.new("RGBA" if img.mode in ("RGBA", "LA", "PA") else "RGB",
                     (cw, ch), bg_color)
    iw, ih = img.size
    # Scale-to-fit
    scale = min(cw / iw, ch / ih)
    new_w = max(1, int(iw * scale))
    new_h = max(1, int(ih * scale))
    if (new_w, new_h) != (iw, ih):
        img = img.resize((new_w, new_h), Image.Resampling.LANCZOS)
    offset = ((cw - new_w) // 2, (ch - new_h) // 2)
    if img.mode in ("RGBA", "LA", "PA"):
        base.paste(img, offset, mask=img)
    else:
        base.paste(img, offset)
    return base


def _detect_hdr(img: "Image.Image", icc_profile: bytes | None = None) -> str | None:
    """Best-effort HDR / wide-gamut detection. Returns 'pq', 'hlg', 'wide', or None.

    Heuristic mix: PIL Image mode (I;16/F), bit depth, ICC profile colorspace
    description, and image.info hints from pillow_heif.
    """
    if img.mode in ("I;16", "I;16B", "I;16L", "F"):
        return "wide"
    info = img.info or {}
    color_space = info.get("color_space") or info.get("colorimetry") or ""
    if isinstance(color_space, bytes):
        color_space = color_space.decode("ascii", errors="replace")
    if "pq" in color_space.lower() or "smpte2084" in color_space.lower():
        return "pq"
    if "hlg" in color_space.lower() or "arib-std-b67" in color_space.lower():
        return "hlg"
    if icc_profile:
        try:
            from PIL import ImageCms
            prof = ImageCms.ImageCmsProfile(io.BytesIO(icc_profile))
            desc = (ImageCms.getProfileDescription(prof) or "").lower()
            if any(tag in desc for tag in ("display p3", "rec.2020", "rec2020", "bt.2020")):
                return "wide"
        except Exception:
            pass
    return None


def _tone_map_hdr(img: "Image.Image", curve: str) -> "Image.Image":
    """Apply a tone-mapping curve to bring HDR/wide-gamut input down to sRGB.

    curve : 'none' (no-op), 'reinhard', 'hable' (Uncharted 2), 'clip'.
    Works on RGB float arrays via numpy; returns an 8-bit RGB image.
    """
    if curve == "none":
        return img
    try:
        import numpy as np
    except ImportError:
        raise RuntimeError(
            "numpy is required for --tone-map. Install it: pip install numpy"
        )
    arr = np.asarray(img.convert("RGB"), dtype=np.float32) / 255.0
    if curve == "clip":
        out = np.clip(arr, 0.0, 1.0)
    elif curve == "reinhard":
        out = arr / (1.0 + arr)
        out = np.clip(out, 0.0, 1.0)
    elif curve == "hable":
        # Uncharted 2 filmic (John Hable, GDC 2010).
        A, B, C, D, E, F = 0.15, 0.50, 0.10, 0.20, 0.02, 0.30
        def fn(x):
            return ((x * (A * x + C * B) + D * E) / (x * (A * x + B) + D * F)) - E / F
        W = 11.2
        out = fn(arr) / fn(W)
        out = np.clip(out, 0.0, 1.0)
    else:
        out = np.clip(arr, 0.0, 1.0)
    return Image.fromarray((out * 255.0).round().astype(np.uint8), mode="RGB")


def _psnr(a: "Image.Image", b: "Image.Image") -> float:
    """Compute PSNR between two images. Returns +inf when identical."""
    try:
        import numpy as np
    except ImportError:
        raise RuntimeError(
            "numpy is required for --target-psnr. Install it: pip install numpy"
        )
    if a.size != b.size:
        return 0.0
    ma = np.asarray(a.convert("RGB"), dtype=np.float32)
    mb = np.asarray(b.convert("RGB"), dtype=np.float32)
    mse = float(np.mean((ma - mb) ** 2))
    if mse == 0:
        return float("inf")
    return 20.0 * float(np.log10(255.0)) - 10.0 * float(np.log10(mse))


def _binary_search_quality(
    img: "Image.Image",
    out_fmt: str,
    target: float,
    mode: str,
    base_kwargs: dict,
    qmin: int = 50,
    qmax: int = 95,
    max_iters: int = 8,
) -> tuple[int, int, float]:
    """Binary-search the quality knob to hit a target.

    mode == 'target-kb' -> target = output size in kilobytes
    mode == 'target-psnr' -> target = minimum PSNR (dB) vs source

    Returns (best_quality, best_size, best_metric).
    """
    import io as _io
    best_q = qmax
    best_size = -1
    best_metric = 0.0
    lo, hi = qmin, qmax
    # Strip unstable optimize/subsampling combos that can throw "broken data
    # stream" on high-entropy probe images; they re-apply in the final save.
    search_kwargs = {k: v for k, v in base_kwargs.items()
                     if k not in ("quality", "optimize")}
    for _ in range(max_iters):
        q = (lo + hi) // 2
        if q < qmin or q > qmax:
            break
        buf = _io.BytesIO()
        kwargs = dict(search_kwargs); kwargs["quality"] = q
        try:
            img.save(buf, out_fmt, **kwargs)
        except Exception:
            # Save failed (e.g. unsupported kwarg combo); fall back to qmax.
            break
        size_kb = buf.tell() / 1024.0
        if mode == "target-kb":
            best_q, best_size, best_metric = q, int(buf.tell()), size_kb
            if size_kb <= target:
                lo = q + 1   # try a higher quality
            else:
                hi = q - 1   # too big, lower
        else:  # target-psnr
            buf.seek(0)
            with Image.open(buf) as decoded:
                metric = _psnr(img, decoded)
            best_q, best_size, best_metric = q, int(buf.tell()), metric
            if metric < target:
                lo = q + 1
            else:
                hi = q - 1
        if lo > hi:
            break
    return best_q, best_size, best_metric


def has_transparency(img: Image.Image) -> bool:
    """Check if image has actual transparency data."""
    if img.mode in ("RGBA", "LA", "PA"):
        alpha = img.getchannel("A")
        extrema = alpha.getextrema()
        return extrema[0] < 255  # has non-opaque pixels
    return False


def count_frames(src: Path) -> int:
    """Return number of frames / sub-images in a source file.

    Multi-frame containers: animated WebP, animated AVIF / .avifs, animated GIF,
    APNG, multi-page TIFF, HEIC image sequences. RAW / QOI / JPEG / single-frame
    PNG return 1.
    """
    suffix = src.suffix.lower()
    if suffix in RAW_EXTS or suffix in QOI_EXTS:
        return 1
    try:
        with Image.open(str(src)) as img:
            return getattr(img, "n_frames", 1)
    except Exception:
        return 1


def _open_image(src: Path) -> tuple[Image.Image, dict]:
    """Open an image file, routing to the correct decoder.

    Returns (PIL Image, metadata_dict).
    metadata_dict contains 'exif', 'icc_profile', 'xmp' when available.
    """
    suffix = src.suffix.lower()
    meta = {}

    if suffix in RAW_EXTS and HAS_RAWPY:
        raw = rawpy.imread(str(src))
        rgb = raw.postprocess(use_camera_wb=True, output_bps=8)
        raw.close()
        img = Image.fromarray(rgb)
        # RAW files don't carry EXIF through rawpy — metadata not available
        return img, meta

    if suffix in QOI_EXTS and HAS_QOI:
        arr = qoi_lib.read(str(src))
        img = Image.fromarray(arr)
        return img, meta

    # Everything else goes through Pillow (+ plugins: pillow-heif, pillow-jxl)
    img = Image.open(str(src))

    # Extract metadata from Pillow's info dict
    if exif := img.info.get("exif"):
        meta["exif"] = exif
    if icc := img.info.get("icc_profile"):
        meta["icc_profile"] = icc
    if xmp := img.info.get("xmp"):
        meta["xmp"] = xmp

    if suffix in HEIC_EXTS and HAS_HEIF:
        try:
            heif_file = pillow_heif.open_heif(str(src))
            bd = getattr(heif_file, "bit_depth", None)
            if bd:
                meta["bit_depth"] = bd
        except Exception:
            pass

    return img, meta


def _estimated_raw_bytes(img: Image.Image) -> int:
    """Estimate uncompressed image payload size for TIFF container selection."""
    w, h = img.size
    bits_per_channel = 16 if img.mode in ("I;16", "I;16L", "I;16B") else 8
    return w * h * len(img.getbands()) * bits_per_channel // 8


def _requires_bigtiff(img: Image.Image) -> bool:
    return _estimated_raw_bytes(img) > 4 * 1024 * 1024 * 1024


def _convert_animated_or_sequence(
    src: Path,
    output_dir: Path,
    fmt: str,
    extract_frames: bool,
    base_dir: Path | None = None,
    preserve_structure: bool = False,
) -> ConvertResult:
    """Multi-frame / sequence handling.

    extract_frames=True : export every frame as {stem}.{seq:###}.{ext}
    extract_frames=False: try to preserve the animation in a multi-frame
                          output format (GIF/WebP/APNG/animated AVIF).
                          Falls back to first-frame-only when target can't
                          carry animation.
    """
    t0 = time.perf_counter()
    result = ConvertResult(src=src, size_before=src.stat().st_size)
    try:
        if preserve_structure and base_dir:
            try:
                rel = src.parent.relative_to(base_dir)
                dest_dir = output_dir / rel
            except ValueError:
                dest_dir = output_dir
        else:
            dest_dir = output_dir
        dest_dir.mkdir(parents=True, exist_ok=True)

        from PIL import ImageSequence
        ext_map = {"jpeg": ".jpg", "png": ".png", "webp": ".webp",
                   "avif": ".avif", "tiff": ".tiff", "jxl": ".jxl"}
        fmt_pil = {"jpeg": "JPEG", "png": "PNG", "webp": "WEBP",
                   "avif": "AVIF", "tiff": "TIFF", "jxl": "JXL"}.get(fmt, "JPEG")
        ext = ext_map.get(fmt, ".jpg")

        with Image.open(str(src)) as img:
            n = getattr(img, "n_frames", 1)
            if extract_frames or fmt_pil not in ("WEBP", "GIF", "PNG"):
                pad_width = max(3, len(str(n)))
                written = []
                for i, frame in enumerate(ImageSequence.Iterator(img), start=1):
                    seq_str = str(i).zfill(pad_width)
                    dst = dest_dir / f"{src.stem}.{seq_str}{ext}"
                    frame_save = frame.convert("RGB") if fmt_pil == "JPEG" else frame.copy()
                    save_kwargs = {}
                    if fmt_pil in ("JPEG", "WEBP", "AVIF", "JXL"):
                        save_kwargs["quality"] = 92
                    frame_save.save(str(dst), fmt_pil, **save_kwargs)
                    written.append(dst)
                result.dst = written[0] if written else None
                result.size_after = sum(p.stat().st_size for p in written) if written else 0
                result.success = bool(written)
                result.warnings.append(
                    f"multi-frame: exported {len(written)} frames as {pad_width}-digit sequence"
                )
            else:
                frames = [f.copy() for f in ImageSequence.Iterator(img)]
                dst = dest_dir / f"{src.stem}{ext}"
                save_kwargs = {"save_all": True,
                                "append_images": frames[1:] if len(frames) > 1 else [],
                                "duration": img.info.get("duration", 100),
                                "loop": img.info.get("loop", 0)}
                if fmt_pil == "WEBP":
                    save_kwargs["quality"] = 90
                frames[0].save(str(dst), fmt_pil, **save_kwargs)
                result.dst = dst
                result.size_after = dst.stat().st_size
                result.success = True
                result.warnings.append(f"multi-frame: animated {fmt_pil} with {len(frames)} frames")
    except Exception as e:
        result.error = f"multi-frame: {e}"
    result.elapsed = time.perf_counter() - t0
    return result


def _run_sidecar_hooks(
    src: Path, out_path: Path, meta: dict,
    result: "ConvertResult", emit_xmp_sidecar: bool, in_place: bool,
):
    """Post-save sidecar generation: XMP, Live Photo .MOV, depth, HDR gain-map."""
    if emit_xmp_sidecar and meta.get("xmp"):
        xmp_path = out_path.with_suffix(out_path.suffix + ".xmp")
        try:
            xmp_payload = meta["xmp"]
            if isinstance(xmp_payload, str):
                xmp_payload = xmp_payload.encode("utf-8")
            xmp_path.write_bytes(xmp_payload)
            result.warnings.append(f"xmp-sidecar: wrote {xmp_path.name}")
        except OSError as e:
            result.warnings.append(f"xmp-sidecar failed: {e}")

    if not in_place:
        mov_candidates = [
            src.with_suffix(".mov"), src.with_suffix(".MOV"),
            src.with_suffix(".heic.mov"),
        ]
        for mov in mov_candidates:
            if mov.exists():
                dst_mov = out_path.with_suffix(mov.suffix)
                try:
                    shutil.copy2(mov, dst_mov)
                    result.warnings.append(f"live-photo: copied {mov.name} -> {dst_mov.name}")
                except OSError as e:
                    result.warnings.append(f"live-photo: copy failed ({e})")
                break

    if src.suffix.lower() in HEIC_EXTS:
        depth_list = None
        try:
            heif_file = pillow_heif.read_heif(str(src))
            depth_list = getattr(heif_file, "depth_images", None)
        except Exception as e:
            depth_list = None
            result.warnings.append(f"depth: HEIC re-open failed ({e})")
        if depth_list:
            for i, depth in enumerate(depth_list):
                suffix = ".depth.png" if i == 0 else f".depth{i}.png"
                depth_path = out_path.with_suffix(suffix)
                try:
                    depth_img = Image.frombytes(
                        depth.mode, depth.size, bytes(depth.data),
                        "raw", depth.mode, depth.stride,
                    )
                    depth_img.save(depth_path, "PNG", optimize=True)
                    result.warnings.append(
                        f"depth: saved {depth_path.name} ({depth.size[0]}x{depth.size[1]})"
                    )
                except Exception as e:
                    result.warnings.append(f"depth: extract failed ({e})")

    if (HAS_EXIFTOOL and src.suffix.lower() in HEIC_EXTS and not in_place):
        try:
            probe = subprocess.run(
                [EXIFTOOL_PATH, "-j", "-GainMapHeadroom", "-HDRGainMap",
                 "-HDRImage", "-HasHDR", str(src)],
                capture_output=True, text=True, timeout=10,
            )
            if probe.returncode == 0 and probe.stdout:
                import json as _json
                meta_probe = _json.loads(probe.stdout)
                if meta_probe and any(
                    meta_probe[0].get(k) for k in
                    ("GainMapHeadroom", "HDRGainMap", "HasHDR", "HDRImage")
                ):
                    gainmap_sidecar = out_path.with_suffix(".gainmap.heic")
                    if not gainmap_sidecar.exists():
                        shutil.copy2(src, gainmap_sidecar)
                    result.warnings.append(
                        f"hdr-gainmap: ISO 21496-1 detected; archived "
                        f"original as {gainmap_sidecar.name} (libheif "
                        f"cannot yet transcode gain maps)"
                    )
        except Exception as e:
            result.warnings.append(f"hdr-gainmap: detection failed ({e})")


def convert_file(
    src: Path,
    output_dir: Path,
    fmt: str = "auto",
    jpeg_quality: int = 92,
    preserve_metadata: bool = True,
    preserve_structure: bool = False,
    base_dir: Path | None = None,
    in_place: bool = False,
    skip_existing: bool = False,
    resize_mode: str = "none",
    resize_value: int = 1920,
    prefix: str = "",
    suffix: str = "",
    lossless_webp: bool = False,
    progressive_jpeg: bool = False,
    chroma_subsampling: bool = False,
    convert_to_srgb: bool = False,
    tiff_compression: str = "none",
    png_compress_level: int = 6,
    use_exiftool: bool = True,
    name_template: str | None = None,
    seq: int = 1,
    only_if_smaller_pct: float | None = None,
    dpi: tuple[int, int] | None = None,
    icc_override: str | None = None,
    emit_xmp_sidecar: bool = False,
    recompress_lossless: bool = False,
    quality_mode: tuple[str, float] | None = None,  # ("target-kb", N) or ("target-psnr", DB)
    watermark: str | None = None,
    canvas: tuple[int, int] | None = None,
    canvas_bg: str = "transparent",
    tone_map: str = "none",
    avif_speed: int = 6,
    avif_codec: str = "auto",
    png_lossy: bool = False,
) -> ConvertResult:
    """Convert a single image file. Thread-safe.

    When ``use_exiftool`` is True and the ``exiftool`` binary is on PATH,
    runs an ExifTool tag-copy pass after the save so MakerNotes, GPS
    sub-IFDs, IPTC, and sidecar XMP make it across — Pillow drops these
    silently. Falls back to Pillow's EXIF / ICC / XMP keys when ExifTool
    is unavailable.
    """
    t0 = time.perf_counter()
    try:
        size_before = src.stat().st_size
    except OSError:
        size_before = 0
    result = ConvertResult(src=src, size_before=size_before)
    img = None
    out_path = None
    temp_path = None

    try:
        img, meta = _open_image(src)

        # Warn when RAW files have no metadata to preserve
        if src.suffix.lower() in RAW_EXTS and not meta:
            result.warnings.append("RAW file: no EXIF/ICC metadata available after demosaic")

        # EXIF auto-rotate — apply orientation and strip the tag
        rotated = ImageOps.exif_transpose(img)
        if rotated is not None:
            img.close()
            img = rotated
            # Refresh EXIF from the transposed image (orientation tag removed)
            if "exif" in img.info:
                meta["exif"] = img.info["exif"]

        # HDR tone mapping — detect PQ/HLG/wide-gamut source and apply curve
        # before subsequent sRGB / mode-flatten steps. Runs after EXIF-transpose
        # so orientation is correct.
        if tone_map and tone_map != "none":
            hdr_kind = _detect_hdr(img, meta.get("icc_profile"))
            if hdr_kind:
                img = _tone_map_hdr(img, tone_map)
                result.warnings.append(
                    f"hdr: detected {hdr_kind}; tone-mapped with {tone_map} curve"
                )

        # ICC profile override — embed a chosen profile (sRGB / Display P3 / Rec.2020)
        # regardless of source. Applied before --srgb so --srgb still wins if both
        # are requested (user explicitly asked for sRGB endpoint).
        if icc_override and not convert_to_srgb:
            try:
                profile_name = icc_override.lower()
                # Pillow's ImageCms.createProfile accepts: sRGB, LAB, XYZ
                if profile_name in ("srgb", "srgb-v4"):
                    dst_profile = ImageCms.createProfile("sRGB")
                else:
                    # For Display P3 / Rec.2020 / arbitrary path, the user
                    # supplies a path to an .icc file.
                    p = Path(icc_override)
                    if p.is_file():
                        dst_profile = ImageCms.ImageCmsProfile(str(p))
                    else:
                        raise ValueError(f"unknown profile preset or missing .icc file: {icc_override}")
                src_data = meta.get("icc_profile")
                if src_data:
                    src_profile = ImageCms.ImageCmsProfile(io.BytesIO(src_data))
                    img = ImageCms.profileToProfile(
                        img, src_profile, dst_profile, outputMode="RGB",
                    )
                # Replace the metadata ICC tag with the override.
                meta["icc_profile"] = ImageCms.ImageCmsProfile(dst_profile).tobytes()
                result.warnings.append(f"icc-override: embedded {icc_override}")
            except Exception as e:
                result.warnings.append(f"icc-override failed: {e}")

        # sRGB color space conversion
        if convert_to_srgb and "icc_profile" in meta:
            try:
                src_profile = ImageCms.ImageCmsProfile(io.BytesIO(meta["icc_profile"]))
                dst_profile = ImageCms.createProfile("sRGB")
                img = ImageCms.profileToProfile(img, src_profile, dst_profile, outputMode="RGB")
                # Update ICC profile in metadata to sRGB
                srgb_profile = ImageCms.ImageCmsProfile(dst_profile)
                meta["icc_profile"] = srgb_profile.tobytes()
            except Exception as e:
                result.warnings.append(f"sRGB conversion failed: {e}")

        # Resize if requested
        if resize_mode == "max_dim" and resize_value > 0:
            w, h = img.size
            if max(w, h) > resize_value:
                if w >= h:
                    new_w = resize_value
                    new_h = max(1, int(h * (resize_value / w)))
                else:
                    new_h = resize_value
                    new_w = max(1, int(w * (resize_value / h)))
                img = img.resize((new_w, new_h), Image.Resampling.LANCZOS)
            else:
                result.warnings.append(f"Already smaller than {resize_value}px ({w}x{h}), resize skipped")
        elif resize_mode == "scale" and resize_value != 100:
            w, h = img.size
            factor = resize_value / 100
            new_w = max(1, int(w * factor))
            new_h = max(1, int(h * factor))
            img = img.resize((new_w, new_h), Image.Resampling.LANCZOS)

        # Canvas resize — pad to a fixed canvas with a background fill.
        if canvas:
            bg_spec = canvas_bg or "transparent"
            if bg_spec == "transparent":
                bg = (0, 0, 0, 0)
            elif bg_spec.startswith("#"):
                # #RRGGBB or #RRGGBBAA hex
                h = bg_spec.lstrip("#")
                if len(h) == 6:
                    bg = tuple(int(h[i:i+2], 16) for i in (0, 2, 4)) + (255,)
                elif len(h) == 8:
                    bg = tuple(int(h[i:i+2], 16) for i in (0, 2, 4, 6))
                else:
                    bg = (0, 0, 0, 0)
            else:
                bg = bg_spec  # named color string — Pillow handles many
            img = _apply_canvas(img, canvas, bg)
            result.warnings.append(f"canvas: padded to {canvas[0]}x{canvas[1]} on {bg_spec}")

        # Watermark — text or PNG overlay; applied after resize/canvas.
        if watermark:
            try:
                img = _apply_watermark(img, watermark)
                result.warnings.append(f"watermark: applied ({watermark.split('|')[0][:40]})")
            except Exception as e:
                result.warnings.append(f"watermark failed: {e}")

        # Determine output format
        if fmt == "auto":
            out_fmt = "PNG" if has_transparency(img) else "JPEG"
        elif fmt == "jpeg":
            out_fmt = "JPEG"
        elif fmt == "png":
            out_fmt = "PNG"
        elif fmt == "webp":
            out_fmt = "WEBP"
        elif fmt == "tiff":
            out_fmt = "TIFF"
        elif fmt == "avif":
            if not HAS_AVIF:
                raise RuntimeError(
                    "AVIF output requires Pillow >=11.3 with native AVIF support "
                    "(pillow-heif >=1.0 deprecated its AVIF encoder)."
                )
            out_fmt = "AVIF"
        elif fmt == "jxl":
            if not HAS_JXL:
                raise RuntimeError("JPEG XL output requires pillow-jxl-plugin (pip install pillow-jxl-plugin)")
            out_fmt = "JXL"
        else:
            out_fmt = "JPEG"

        # Same-format guard — skip if input is already the output format
        # and no processing (resize, sRGB, strip metadata) is requested
        src_ext = src.suffix.lower()
        same_fmt = (
            (out_fmt == "JPEG" and src_ext in JPEG_EXTS)
            or (out_fmt == "PNG" and src_ext in PNG_EXTS)
            or (out_fmt == "WEBP" and src_ext in WEBP_EXTS)
            or (out_fmt == "AVIF" and src_ext in AVIF_EXTS)
            or (out_fmt == "TIFF" and src_ext in TIFF_EXTS)
            or (out_fmt == "JXL" and src_ext in JXL_EXTS)
        )
        no_processing = (
            resize_mode == "none"
            and not convert_to_srgb
            and preserve_metadata
            and not watermark
            and canvas is None
            and tone_map == "none"
            and dpi is None
            and icc_override is None
            and not name_template
        )
        if same_fmt and no_processing and not recompress_lossless:
            result.skipped = True
            result.warnings.append(f"Skipped: already {out_fmt} and no processing requested")
            result.elapsed = time.perf_counter() - t0
            return result

        # Lossless recompress fast-path: JPEG -> JPEG via jpegoptim / jpegtran,
        # pixel-bit-preserving. Bypasses the Pillow decode/encode chain entirely.
        if (recompress_lossless and out_fmt == "JPEG" and src_ext in JPEG_EXTS
                and HAS_JPEG_RECOMPRESS):
            ext = ".jpg"
            if in_place:
                dest_dir = src.parent
            elif preserve_structure and base_dir:
                rel = src.parent.relative_to(base_dir)
                dest_dir = output_dir / rel
            else:
                dest_dir = output_dir
            dest_dir.mkdir(parents=True, exist_ok=True)
            stem_short = prefix + src.stem + suffix
            out_path = dest_dir / (stem_short + ext)
            if skip_existing and out_path.exists():
                result.skipped = True
                result.dst = out_path
                result.size_after = out_path.stat().st_size
                result.elapsed = time.perf_counter() - t0
                return result
            ok, tool = _recompress_jpeg_lossless(src, out_path, not preserve_metadata)
            if ok:
                result.dst = out_path
                result.size_after = out_path.stat().st_size
                result.success = True
                result.warnings.append(f"recompress: pixel-lossless via {tool}")
                if in_place and result.success and out_path.resolve() != src.resolve():
                    src.unlink()
                    result.src_deleted = True
                result.elapsed = time.perf_counter() - t0
                return result
            else:
                result.warnings.append(
                    f"recompress: {tool}; falling back to standard re-encode"
                )

        ext_map = {"JPEG": ".jpg", "PNG": ".png", "WEBP": ".webp", "AVIF": ".avif", "TIFF": ".tiff", "JXL": ".jxl"}
        ext = ext_map.get(out_fmt, ".jpg")

        # Build output path — in-place writes next to the source file
        if in_place:
            dest_dir = src.parent
        elif preserve_structure and base_dir:
            rel = src.parent.relative_to(base_dir)
            dest_dir = output_dir / rel
        else:
            dest_dir = output_dir

        # Output filename — template language wins; prefix/suffix is the
        # backward-compat path when name_template is None.
        if name_template:
            applied = _apply_output_template(
                name_template, src, base_dir, img.size[0], img.size[1],
                fmt, ext, seq,
            )
            cand = Path(applied)
            if cand.is_absolute():
                result.warnings.append("template: absolute paths rejected for safety")
                cand = Path(cand.name)
            out_path = dest_dir / cand
            if not out_path.resolve().is_relative_to(dest_dir.resolve()):
                result.warnings.append("template: path escapes output dir, flattened")
                out_path = dest_dir / out_path.name
            # If user's template forgot the extension, append it.
            if out_path.suffix.lower() != ext.lower():
                out_path = out_path.with_suffix(ext)
            out_path.parent.mkdir(parents=True, exist_ok=True)
            stem = out_path.stem
        else:
            dest_dir.mkdir(parents=True, exist_ok=True)
            stem = prefix + src.stem + suffix
            out_path = dest_dir / (stem + ext)

        # Skip if output already exists
        if skip_existing and out_path.exists():
            result.skipped = True
            result.dst = out_path
            result.size_after = out_path.stat().st_size
            result.elapsed = time.perf_counter() - t0
            return result

        # Handle name collisions
        counter = 1
        while out_path.exists():
            out_path = dest_dir / f"{stem}_{counter}{ext}"
            counter += 1

        # Gather metadata
        save_kwargs = {}
        if preserve_metadata and meta:
            if "exif" in meta:
                save_kwargs["exif"] = meta["exif"]
            if "icc_profile" in meta:
                save_kwargs["icc_profile"] = meta["icc_profile"]
            if "xmp" in meta and out_fmt in ("JPEG", "WEBP", "TIFF", "AVIF", "JXL"):
                save_kwargs["xmp"] = meta["xmp"]

        # Format-specific options
        if out_fmt == "JPEG":
            # ICC-aware mode flattening — Pillow's blind .convert("RGB") on
            # CMYK / LAB / wide-gamut RGB loses the embedded profile and
            # produces the canonical iPhone "Display P3 → sRGB shift" bug
            # (Apple Community 254814534, ImageMagick #4391).
            if img.mode == "CMYK" and meta.get("icc_profile"):
                try:
                    src_p = ImageCms.ImageCmsProfile(io.BytesIO(meta["icc_profile"]))
                    dst_p = ImageCms.createProfile("sRGB")
                    img = ImageCms.profileToProfile(img, src_p, dst_p, outputMode="RGB")
                    meta["icc_profile"] = ImageCms.ImageCmsProfile(dst_p).tobytes()
                    save_kwargs["icc_profile"] = meta["icc_profile"]
                except Exception as e:
                    result.warnings.append(f"ICC-aware CMYK→RGB failed, falling back: {e}")
                    img = img.convert("RGB")
            elif img.mode == "CMYK":
                # CMYK without ICC — use Pillow's built-in conversion (lossy but unavoidable).
                img = img.convert("RGB")
                result.warnings.append("CMYK input had no ICC profile; used naive K-channel conversion")
            elif img.mode in ("RGBA", "LA", "PA", "P"):
                img = img.convert("RGB")
            save_kwargs["quality"] = jpeg_quality
            save_kwargs["subsampling"] = 2 if chroma_subsampling else 0
            save_kwargs["optimize"] = True
            if progressive_jpeg:
                save_kwargs["progressive"] = True
        elif out_fmt == "PNG":
            save_kwargs["optimize"] = True
            save_kwargs["compress_level"] = png_compress_level
        elif out_fmt == "WEBP":
            if lossless_webp:
                save_kwargs["lossless"] = True
            else:
                save_kwargs["quality"] = jpeg_quality
            save_kwargs["method"] = 4
        elif out_fmt == "TIFF":
            if tiff_compression == "lzw":
                save_kwargs["compression"] = "tiff_lzw"
            elif tiff_compression == "deflate":
                save_kwargs["compression"] = "tiff_deflate"
            if _requires_bigtiff(img):
                save_kwargs["big_tiff"] = True
                result.warnings.append("tiff: using BigTIFF for >4 GB estimated output")
        elif out_fmt == "AVIF":
            save_kwargs["quality"] = jpeg_quality
            save_kwargs["speed"] = avif_speed
            if avif_codec and avif_codec != "auto":
                save_kwargs["codec"] = avif_codec
            # Wide-gamut / 10-bit preservation when source is HEIC at >8 bpp.
            bit_depth = meta.get("bit_depth")
            if bit_depth and bit_depth > 8:
                save_kwargs["bits"] = bit_depth
                result.warnings.append(
                    f"avif: preserving {bit_depth}-bit depth from source HEIC"
                )
        elif out_fmt == "JXL":
            save_kwargs["quality"] = jpeg_quality
            save_kwargs["effort"] = 7
            # Lossless JPEG -> JXL transcoding. libjxl's signature feature:
            # bit-exact transcode with ~20 % size reduction, fully reversible
            # (Wikipedia JXL, HN 35212522). pillow_jxl defaults to lossless
            # JPEG reconstruction for JPEG sources; --lossless makes it
            # explicit and logs a warning so users see the win.
            if src.suffix.lower() in JPEG_EXTS:
                save_kwargs["lossless_jpeg"] = True
                result.warnings.append(
                    "jxl: lossless JPEG reconstruction (bit-exact, reversible)"
                )
            # Wide-gamut / 10-bit preservation for HEIC source.
            else:
                bit_depth = meta.get("bit_depth")
                if bit_depth and bit_depth > 8:
                    save_kwargs["bit_depth"] = bit_depth
                    result.warnings.append(
                        f"jxl: preserving {bit_depth}-bit depth from source HEIC"
                    )

        # Optional DPI tag — JPEG/TIFF/PNG support dpi; ignore for others.
        if dpi and out_fmt in ("JPEG", "PNG", "TIFF"):
            save_kwargs["dpi"] = dpi

        # --quality-mode binary search: tune the quality kwarg to hit a size
        # or PSNR target. Only meaningful for lossy formats with a quality knob.
        if quality_mode and out_fmt in ("JPEG", "WEBP", "AVIF", "JXL"):
            mode_name, target_val = quality_mode
            best_q, best_sz, best_metric = _binary_search_quality(
                img, out_fmt, target_val, mode_name, save_kwargs,
            )
            save_kwargs["quality"] = best_q
            result.warnings.append(
                f"quality-mode {mode_name}: q={best_q}, "
                f"size={best_sz}B, metric={best_metric:.2f}"
            )

        # Atomic write: use temp file for in-place mode
        if in_place:
            fd, tmp_str = tempfile.mkstemp(
                suffix=".imgconverter.tmp", dir=str(out_path.parent),
            )
            os.close(fd)
            temp_path = Path(tmp_str)
            img.save(str(temp_path), out_fmt, **save_kwargs)
        else:
            img.save(str(out_path), out_fmt, **save_kwargs)

        # pngquant lossy optimization — 50-80% size reduction via quantization.
        if png_lossy and out_fmt == "PNG" and PNGQUANT_PATH:
            target = temp_path if in_place else out_path
            try:
                proc = subprocess.run(
                    [PNGQUANT_PATH, "--force", "--output", str(target), "--quality=65-80",
                     "--skip-if-larger", str(target)],
                    capture_output=True, text=True, timeout=60,
                )
                if proc.returncode == 0:
                    result.warnings.append("png: pngquant lossy optimization applied")
                elif proc.returncode == 99:
                    result.warnings.append("png: pngquant skipped (output not smaller)")
            except Exception as e:
                result.warnings.append(f"pngquant failed: {e}")

        # Validate output file integrity. Image.verify() only checks the header;
        # pair it with a re-open + size-match so a truncated encode is detected.
        check_path = temp_path if in_place else out_path
        if not check_path.exists() or check_path.stat().st_size == 0:
            raise RuntimeError(f"Output file missing or empty: {check_path.name}")
        try:
            with Image.open(str(check_path)) as verify_img:
                verify_img.verify()
            with Image.open(str(check_path)) as decoded:
                dw, dh = decoded.size
                sw, sh = img.size
                if abs(dw - sw) > 1 or abs(dh - sh) > 1:
                    raise RuntimeError(
                        f"Output size {decoded.size} != source size {img.size}"
                    )
        except Exception as ve:
            raise RuntimeError(f"Output validation failed: {ve}")

        # Cross-decoder check via ffprobe when available — second opinion that
        # the file is parseable by something other than libpillow / libheif.
        # Free run for formats ffprobe natively supports (jpeg, png, webp,
        # avif, tiff). Failures don't fail the convert — just log a WARN.
        ffprobe = shutil.which("ffprobe")
        if ffprobe and out_fmt in ("JPEG", "PNG", "WEBP", "AVIF", "TIFF"):
            try:
                pr = subprocess.run(
                    [ffprobe, "-v", "error", "-select_streams", "v:0",
                     "-show_entries", "stream=width,height", "-of", "csv=p=0",
                     str(check_path)],
                    capture_output=True, text=True, timeout=10,
                )
                if pr.returncode == 0 and pr.stdout.strip():
                    dims = pr.stdout.strip().split(",")
                    if len(dims) >= 2:
                        ffw, ffh = int(dims[0]), int(dims[1])
                        if (ffw, ffh) != img.size:
                            result.warnings.append(
                                f"validator: ffprobe disagrees on dims "
                                f"({ffw}x{ffh} vs Pillow {img.size})"
                            )
            except Exception:
                pass

        # ExifTool tag-copy pass — recovers MakerNotes / GPS sub-IFDs / IPTC
        # / sidecar XMP that Pillow drops. Runs against the temp file so an
        # ExifTool failure can't corrupt the live output. Skipped when the
        # caller asked to strip metadata or explicitly opted out.
        if use_exiftool and HAS_EXIFTOOL and preserve_metadata:
            tagcopy_target = temp_path if in_place else out_path
            ok, msg = _run_exiftool_copy(src, tagcopy_target)
            if ok:
                result.warnings.append("metadata: exiftool tag-copy ok")
            else:
                result.warnings.append(
                    f"metadata: exiftool failed ({msg}); Pillow fallback applied"
                )

        # Atomic rename for in-place mode
        if in_place:
            os.replace(str(temp_path), str(out_path))
            temp_path = None  # Rename succeeded, no temp to clean

        result.dst = out_path
        result.size_after = out_path.stat().st_size
        result.success = True

        # Conditional re-encode: drop the output if it's not meaningfully
        # smaller than the source. Pattern from XnConvert / ImBatch.
        if only_if_smaller_pct is not None and only_if_smaller_pct > 0:
            threshold_ratio = (100.0 - only_if_smaller_pct) / 100.0
            if result.size_before > 0 and (result.size_after / result.size_before) > threshold_ratio:
                try:
                    out_path.unlink()
                except OSError:
                    pass
                result.dst = None
                result.success = False
                result.skipped = True
                result.warnings.append(
                    f"only-if-smaller: output {result.size_after}B was "
                    f">{threshold_ratio*100:.0f}% of source {result.size_before}B; "
                    f"discarded, keeping original"
                )
                # Don't run the rest of the post-save hooks if we discarded.
                result.elapsed = time.perf_counter() - t0
                return result

        # Optional quality verification — butteraugli / ffmpeg-quality-metrics.
        # Cheap shell-out; only runs when caller asked for it.
        # Note: convert_file doesn't currently expose verify_quality directly,
        # so the GUI/CLI pass it via the result.warnings post-write
        # block; see post-loop verify in _run_cli.

        _run_sidecar_hooks(src, out_path, meta, result, emit_xmp_sidecar, in_place)

        # In-place mode: delete the original after successful conversion
        if in_place and result.success:
            src.unlink()
            result.src_deleted = True

    except Exception as e:
        result.error = str(e)
        # Clean up temp file on failure
        if temp_path and temp_path.exists():
            try:
                temp_path.unlink()
            except OSError:
                pass
        # Clean up partial output on failure
        if out_path and out_path.exists() and not result.success:
            try:
                out_path.unlink()
            except OSError:
                pass
    finally:
        if img is not None:
            try:
                img.close()
            except Exception:
                pass

    result.elapsed = time.perf_counter() - t0
    return result


# ── Worker Threads ───────────────────────────────────────────────────────────

class ConvertWorker(QThread):
    progress = pyqtSignal(int, int)       # current, total
    current_file = pyqtSignal(str)        # filename currently processing
    file_done = pyqtSignal(object)        # ConvertResult
    finished_all = pyqtSignal(list)        # list[ConvertResult]
    log = pyqtSignal(str)

    def __init__(self, files, output_dir, fmt, quality, preserve_meta,
                 preserve_structure, base_dir, workers, in_place=False,
                 skip_existing=False, resize_mode="none", resize_value=1920,
                 prefix="", suffix="", lossless_webp=False,
                 progressive_jpeg=False, chroma_subsampling=False,
                 convert_to_srgb=False, tiff_compression="none",
                 png_compress_level=6, use_exiftool=True,
                 name_template=None, only_if_smaller_pct=None,
                 dpi=None, icc_override=None, emit_xmp_sidecar=False,
                 recompress_lossless=False, quality_mode=None,
                 watermark=None, canvas=None, canvas_bg="transparent",
                 tone_map="none", avif_speed=6, avif_codec="auto",
                 png_lossy=False, frames="first"):
        super().__init__()
        self.files = files
        self.output_dir = Path(output_dir)
        self.fmt = fmt
        self.quality = quality
        self.preserve_meta = preserve_meta
        self.preserve_structure = preserve_structure
        self.base_dir = base_dir
        self.workers = workers
        self.in_place = in_place
        self.skip_existing = skip_existing
        self.resize_mode = resize_mode
        self.resize_value = resize_value
        self.prefix = prefix
        self.suffix = suffix
        self.lossless_webp = lossless_webp
        self.progressive_jpeg = progressive_jpeg
        self.chroma_subsampling = chroma_subsampling
        self.convert_to_srgb = convert_to_srgb
        self.tiff_compression = tiff_compression
        self.png_compress_level = png_compress_level
        self.use_exiftool = use_exiftool
        self.name_template = name_template
        self.only_if_smaller_pct = only_if_smaller_pct
        self.dpi = dpi
        self.icc_override = icc_override
        self.emit_xmp_sidecar = emit_xmp_sidecar
        self.recompress_lossless = recompress_lossless
        self.quality_mode = quality_mode
        self.watermark = watermark
        self.canvas = canvas
        self.canvas_bg = canvas_bg
        self.tone_map = tone_map
        self.avif_speed = avif_speed
        self.avif_codec = avif_codec
        self.png_lossy = png_lossy
        self.frames = frames
        self._stop_event = threading.Event()

    def stop(self):
        self._stop_event.set()

    def run(self):
        results = []
        total = len(self.files)
        done = 0

        self.log.emit(f"Starting conversion of {total} files with {self.workers} workers...")

        # Multi-frame handling — extract or animate when source has >1 frame.
        if self.frames in ("all", "animate"):
            animated_files = [f for f in self.files if count_frames(f) > 1]
            if animated_files:
                self.log.emit(
                    f"[multi-frame] {len(animated_files)} sources have >1 frame; "
                    f"--frames={self.frames} active"
                )
                for f in animated_files:
                    r = _convert_animated_or_sequence(
                        f, self.output_dir, self.fmt,
                        extract_frames=(self.frames == "all"),
                        base_dir=self.base_dir,
                        preserve_structure=self.preserve_structure,
                    )
                    results.append(r)
                    done += 1
                    self.progress.emit(done, total)
                    self.current_file.emit(r.src.name)
                    self.file_done.emit(r)
                    if r.success:
                        self.log.emit(f"[OK*] {f.name}: {r.warnings[-1] if r.warnings else 'ok'}")
                    else:
                        self.log.emit(f"[FAIL*] {f.name}: {r.error}")
                self.files = [f for f in self.files if f not in animated_files]
                total = len(self.files) + done

        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            futures = {}
            for seq_i, f in enumerate(self.files, start=1):
                if self._stop_event.is_set():
                    break
                fut = pool.submit(
                    convert_file, f, self.output_dir, self.fmt,
                    self.quality, self.preserve_meta,
                    self.preserve_structure, self.base_dir,
                    self.in_place, self.skip_existing,
                    self.resize_mode, self.resize_value,
                    self.prefix, self.suffix,
                    self.lossless_webp, self.progressive_jpeg,
                    self.chroma_subsampling, self.convert_to_srgb,
                    self.tiff_compression, self.png_compress_level,
                    self.use_exiftool,
                    self.name_template,             # name_template
                    seq_i,                          # seq
                    self.only_if_smaller_pct,       # only_if_smaller_pct
                    self.dpi,                       # dpi
                    self.icc_override,              # icc_override
                    self.emit_xmp_sidecar,          # emit_xmp_sidecar
                    self.recompress_lossless,        # recompress_lossless
                    self.quality_mode,               # quality_mode
                    self.watermark,                  # watermark
                    self.canvas,                     # canvas
                    self.canvas_bg,                  # canvas_bg
                    self.tone_map,                   # tone_map
                    self.avif_speed,                 # avif_speed
                    self.avif_codec,                 # avif_codec
                    self.png_lossy,                  # png_lossy
                )
                futures[fut] = f

            for fut in as_completed(futures):
                if self._stop_event.is_set():
                    pool.shutdown(wait=False, cancel_futures=True)
                    self.log.emit("Conversion cancelled by user.")
                    break

                try:
                    result = fut.result()
                except Exception as exc:
                    f = futures[fut]
                    result = ConvertResult(src=f, size_before=0)
                    result.error = str(exc)
                results.append(result)
                done += 1
                self.progress.emit(done, total)
                self.current_file.emit(result.src.name)
                self.file_done.emit(result)

                if result.skipped:
                    self.log.emit(f"[SKIP] {result.src.name} — output already exists")
                elif result.success:
                    saved = result.size_before - result.size_after
                    pct = (saved / result.size_before * 100) if result.size_before else 0
                    deleted_tag = "  [source deleted]" if result.src_deleted else ""
                    self.log.emit(
                        f"[OK] {result.src.name} -> {result.dst.name}  "
                        f"({_fmt_size(result.size_before)} -> {_fmt_size(result.size_after)}, "
                        f"{pct:+.1f}%)  [{result.elapsed:.2f}s]{deleted_tag}"
                    )
                else:
                    self.log.emit(f"[FAIL] {result.src.name}: {result.error}")

                for warn in result.warnings:
                    self.log.emit(f"[WARN] {result.src.name}: {warn}")

        self.finished_all.emit(results)


class ScanWorker(QThread):
    finished = pyqtSignal(object)  # ScanResult
    log = pyqtSignal(str)
    scan_progress = pyqtSignal(int, int, str, int)  # total_count, total_bytes, dir_path, dir_file_count

    def __init__(self, directory, recursive, extensions=None):
        super().__init__()
        self.directory = Path(directory)
        self.recursive = recursive
        self.extensions = extensions

    def run(self):
        self.log.emit(f"Scanning {'recursively' if self.recursive else ''}: {self.directory}")
        result = scan_directory(
            self.directory, self.recursive, self.extensions,
            on_progress=lambda count, size, d, dc: self.scan_progress.emit(count, size, d, dc),
        )
        self.log.emit(
            f"Found {len(result.files)} supported files "
            f"({_fmt_size(result.total_size)}) in {result.elapsed:.2f}s"
        )
        self.finished.emit(result)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _fmt_size(b: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if b < 1024:
            return f"{b:.1f} {unit}"
        b /= 1024
    return f"{b:.1f} TB"


def _fmt_eta(seconds: float) -> str:
    """Format seconds as human-readable ETA string."""
    if seconds < 60:
        return f"{seconds:.0f}s"
    m, s = divmod(int(seconds), 60)
    if m < 60:
        return f"{m}m {s:02d}s"
    h, m = divmod(m, 60)
    return f"{h}h {m:02d}m"


def _open_path(path: str):
    """Open a file/folder in the native file manager (cross-platform)."""
    if platform.system() == "Windows":
        os.startfile(path)
    elif platform.system() == "Darwin":
        subprocess.Popen(["open", path])
    else:
        subprocess.Popen(["xdg-open", path])


def _create_app_icon() -> QIcon:
    """Create a simple app icon programmatically."""
    pm = QPixmap(64, 64)
    pm.fill(QColor(0, 0, 0, 0))
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setBrush(QColor(CAT["blue"]))
    p.setPen(Qt.PenStyle.NoPen)
    p.drawRoundedRect(2, 2, 60, 60, 14, 14)
    p.setPen(QColor(CAT["crust"]))
    f = QFont("Segoe UI", 30, QFont.Weight.Bold)
    p.setFont(f)
    p.drawText(pm.rect(), Qt.AlignmentFlag.AlignCenter, "I")
    p.end()
    return QIcon(pm)


# ── Conversion Presets ────────────────────────────────────────────────────────

USER_CACHE_DIR = Path.home() / ".cache" / "imgconverter"
USER_CONFIG_DIR = Path.home() / ".imgconverter"
USER_PRESET_DIR = USER_CONFIG_DIR / "presets"
USER_LOG_PATH = USER_CACHE_DIR / "imgconverter.log"

# QSettings shape version — bump when on-disk settings layout changes so the
# migration in _maybe_migrate_settings() runs once on startup.
SETTINGS_SCHEMA = 2


def _diag_log(message: str, level: str = "INFO"):
    """Append a timestamped line to ~/.cache/imgconverter/imgconverter.log.

    Best-effort: failures (disk full, permission denied) are swallowed so
    diagnostics never break the converter.
    """
    try:
        USER_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        # Rotate on size: 5 MB ceiling, keep one backup. Plenty for support cases.
        try:
            if USER_LOG_PATH.exists() and USER_LOG_PATH.stat().st_size > 5_000_000:
                rotated = USER_LOG_PATH.with_suffix(".log.1")
                if rotated.exists():
                    rotated.unlink()
                USER_LOG_PATH.rename(rotated)
        except OSError:
            pass
        from datetime import datetime
        with USER_LOG_PATH.open("a", encoding="utf-8", errors="replace") as fp:
            fp.write(f"[{datetime.now().isoformat(timespec='seconds')}] {level} {message}\n")
    except Exception:
        pass


def _check_for_update(current_version: str, timeout: float = 3.0) -> str | None:
    """Return the latest GitHub release tag if newer than ``current_version``, else None.

    Best-effort: any network/parse error returns None silently. Throttled
    by the caller via the .last_update_check QSettings key.
    """
    try:
        import urllib.request
        import urllib.error
        req = urllib.request.Request(
            "https://api.github.com/repos/SysAdminDoc/ImgConverter/releases/latest",
            headers={"Accept": "application/vnd.github+json",
                     "User-Agent": f"ImgConverter/{current_version}"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
        tag = (data.get("tag_name") or "").lstrip("v")
        if not tag:
            return None
        try:
            from packaging.version import Version
            if Version(tag) > Version(current_version):
                return tag
        except Exception:
            # Fallback: literal string mismatch counts as "newer".
            if tag and tag != current_version:
                return tag
        return None
    except Exception:
        return None




def _seed_user_preset_dir():
    """Dump built-in presets to disk on first launch so users can edit them."""
    try:
        USER_PRESET_DIR.mkdir(parents=True, exist_ok=True)
    except OSError:
        return
    for name, payload in PRESETS.items():
        slug = name.lower().replace(" ", "-").replace("/", "-")
        target = USER_PRESET_DIR / f"{slug}.json"
        if not target.exists():
            try:
                target.write_text(json.dumps({"name": name, **payload}, indent=2))
            except OSError:
                pass


def list_presets() -> dict[str, dict]:
    """Return a merged dict of built-in + user-supplied presets (user wins)."""
    merged = {k: dict(v) for k, v in PRESETS.items()}
    if USER_PRESET_DIR.is_dir():
        for path in sorted(USER_PRESET_DIR.glob("*.json")):
            try:
                data = json.loads(path.read_text())
            except (OSError, json.JSONDecodeError):
                continue
            display = data.get("name") or path.stem
            merged[display] = {k: v for k, v in data.items() if k != "name"}
    return merged


def load_preset(name: str) -> dict | None:
    """Resolve ``name`` against built-ins + user dir; return preset dict or None."""
    presets = list_presets()
    # Exact match first, then case-insensitive, then slug match.
    if name in presets:
        return presets[name]
    lc = name.lower()
    for k, v in presets.items():
        if k.lower() == lc:
            return v
    slug_target = name.lower().replace("-", " ")
    for k, v in presets.items():
        if k.lower() == slug_target:
            return v
    return None


PRESETS = {
    "Web Optimized": {
        "fmt": 1,              # JPEG
        "quality": 80,
        "progressive_jpeg": True,
        "chroma_subsampling": True,
        "convert_to_srgb": True,
        "resize_enabled": True,
        "resize_mode": 0,      # Max Dimension
        "resize_value": 1920,
    },
    "Archive Quality": {
        "fmt": 2,              # PNG
        "quality": 92,
        "png_compress_level": 6,
        "resize_enabled": False,
    },
    "Mobile Friendly": {
        "fmt": 3,              # WebP
        "quality": 75,
        "convert_to_srgb": True,
        "resize_enabled": True,
        "resize_mode": 0,      # Max Dimension
        "resize_value": 1080,
    },
    "Print / TIFF": {
        "fmt": 5,              # TIFF
        "tiff_compression": 1, # LZW
        "resize_enabled": False,
    },
}


# ── Disk Space Estimation ─────────────────────────────────────────────────────

SIZE_ESTIMATE_FACTORS = {"jpeg": 0.8, "auto": 0.8, "png": 1.2, "webp": 0.7, "avif": 0.5, "tiff": 1.5, "jxl": 0.45}


def _estimate_output_size(total_input_bytes: int, fmt: str) -> int:
    """Estimate total output size based on format and input size."""
    factor = SIZE_ESTIMATE_FACTORS.get(fmt, 1.0)
    return int(total_input_bytes * factor)


# ── Main Window ───────────────────────────────────────────────────────────────

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"ImgConverter v{APP_VERSION}")
        self.setMinimumSize(700, 520)
        self.resize(1040, 820)
        self.setAcceptDrops(True)

        self._icon = _create_app_icon()
        self.setWindowIcon(self._icon)

        self.settings = QSettings("ImgConverter", "ImgConverter")
        self._scan_result: ScanResult | None = None
        self._worker: ConvertWorker | None = None
        self._results: list[ConvertResult] = []
        self._convert_start_time: float = 0.0
        self._last_ok_dst: Path | None = None

        # System tray for completion notifications
        self._tray = QSystemTrayIcon(self._icon, self)

        self._build_ui()
        self._apply_dark_titlebar()
        self._restore_state()
        self._apply_accessibility_labels()
        self._log_startup()
        _diag_log(f"ImgConverter v{APP_VERSION} started (GUI mode)")
        # Optional update check — defer so we don't block startup paint.
        QTimer.singleShot(2000, self._maybe_check_for_update)

    def _apply_accessibility_labels(self):
        """Attach screen-reader-friendly accessible names + status tips.

        Qt screen-reader interop on Windows / macOS / Linux relies on
        QWidget.setAccessibleName / setAccessibleDescription. This pass
        labels every primary control on MainWindow so JAWS / NVDA /
        VoiceOver / Orca announce something meaningful instead of the
        default Qt class name.
        """
        labels = [
            ("workflow_state",     "Workflow status",          "Current batch workflow state"),
            ("src_edit",            "Source directory",         "Directory to scan for input images"),
            ("src_btn",             "Choose source folder",     "Open a folder picker for the source directory"),
            ("recent_btn",          "Recent source folders",    "Open recently used source directories"),
            ("dst_edit",            "Output directory",         "Where converted files go (blank = source/converted)"),
            ("dst_btn",             "Choose output folder",     "Open a folder picker for the output directory"),
            ("fmt_combo",           "Output format",            "Target image format (auto/jpeg/png/webp/avif/tiff/jxl)"),
            ("_preset_btn",         "Conversion presets",       "Apply a saved conversion preset"),
            ("quality_slider",      "Quality",                  "Encoder quality 50-100 for JPEG/WebP/AVIF/JXL"),
            ("workers_spin",        "Worker thread count",      "Number of parallel conversion threads"),
            ("recursive_chk",       "Recursive scan",           "Walk subdirectories under the source directory"),
            ("inplace_chk",         "Convert in place",         "Save output next to each source and delete the source after output validation"),
            ("skip_existing_chk",   "Skip existing outputs",    "Skip files whose output already exists"),
            ("meta_chk",            "Preserve metadata",        "Keep EXIF, ICC, and XMP through conversion"),
            ("progressive_jpeg_chk","Progressive JPEG",         "Encode JPEGs as progressive for web delivery"),
            ("lossless_webp_chk",   "Lossless WebP",            "Save WebP in lossless mode"),
            ("resize_chk",          "Enable resize",            "Resize images during conversion"),
            ("resize_combo",        "Resize mode",              "Max dimension in pixels, or percent scale"),
            ("resize_spin",         "Resize value",             "Numeric value for the chosen resize mode"),
            ("template_edit",       "Filename template",        "Output filename template with tokens like {stem} {width} {height}"),
            ("dpi_spin",            "DPI override",             "Set output DPI for JPEG/PNG/TIFF, 0 keeps original"),
            ("avif_speed_spin",     "AVIF speed",               "AVIF encoding speed 0-10, lower is slower but smaller"),
            ("frames_combo",        "Multi-frame mode",         "How to handle animated or multi-frame source images"),
            ("tone_map_combo",      "Tone mapping curve",       "HDR tone mapping for PQ/HLG/wide-gamut sources"),
            ("icc_edit",            "ICC profile override",     "Embed a specific ICC profile or leave blank to keep source"),
            ("watermark_edit",      "Watermark specification",  "Watermark text or image with position and opacity"),
            ("canvas_edit",         "Canvas size",              "Pad output to WxH canvas with background fill"),
            ("canvas_bg_edit",      "Canvas background",        "Canvas background color: transparent, hex, or named color"),
            ("xmp_sidecar_chk",     "Emit XMP sidecar",         "Write .xmp sidecar alongside output"),
            ("recompress_chk",      "Lossless JPEG recompress", "Pixel-lossless JPEG size reduction via jpegoptim/jpegtran"),
            ("only_if_smaller_chk", "Only if smaller",          "Discard output when not meaningfully smaller than input"),
            ("target_kb_spin",      "Target file size KB",      "Binary-search quality to hit a target output size in KB"),
            ("avif_codec_combo",    "AVIF codec",               "AVIF encoder: auto, aom, rav1e, or svt"),
            ("png_lossy_chk",       "Lossy PNG",                "Run pngquant for lossy PNG size reduction"),
            ("chroma_chk",          "Chroma subsampling",       "Use 4:2:0 chroma for smaller JPEG files"),
            ("srgb_chk",            "Convert to sRGB",          "Convert embedded ICC profiles to sRGB"),
            ("strip_meta_chk",      "Strip metadata",           "Remove all EXIF, ICC, and XMP from output"),
            ("structure_chk",       "Preserve folder structure", "Mirror source directory layout in output"),
            ("only_if_smaller_spin","Only-if-smaller threshold", "Percentage by which output must be smaller"),
            ("png_level_spin",      "PNG compression level",    "PNG compression 1 (fast) to 9 (smallest)"),
            ("tiff_comp_combo",     "TIFF compression",         "TIFF compression: None, LZW, or Deflate"),
            ("adv_toggle",          "Advanced output controls", "Show or hide advanced output controls"),
            ("scan_btn",            "Scan source",              "Scan the selected source for supported images"),
            ("convert_btn",         "Convert batch",            "Start converting the scanned batch"),
            ("stop_btn",            "Cancel conversion",        "Stop the current conversion batch"),
            ("paste_btn",           "Paste clipboard",          "Paste an image from clipboard as input"),
            ("auto_open_chk",       "Auto-open output",         "Automatically open the output folder when conversion finishes"),
            ("open_output_btn",     "Open output folder",       "Open the most recent output folder"),
            ("export_log_btn",      "Export log",               "Save the conversion log as a text file"),
            ("export_csv_btn",      "Export CSV",               "Export conversion results as a CSV report"),
            ("clear_log_btn",       "Clear log",                "Clear the activity log"),
        ]
        for attr, name, desc in labels:
            w = getattr(self, attr, None)
            if w is None:
                continue
            try:
                w.setAccessibleName(name)
                w.setAccessibleDescription(desc)
                if hasattr(w, "setStatusTip"):
                    w.setStatusTip(desc)
            except Exception:
                pass

    def _maybe_check_for_update(self):
        """Throttled GitHub release check — opt-in, off by default. 24-hour cooldown."""
        try:
            enabled_raw = self.settings.value("update_check_enabled", False)
            enabled = enabled_raw == "true" or enabled_raw is True
            if not enabled:
                return
            import time as _time
            last = float(self.settings.value("last_update_check", 0) or 0)
            if _time.time() - last < 86400:
                return
            self.settings.setValue("last_update_check", _time.time())
            latest = _check_for_update(APP_VERSION)
            if latest:
                self._log(f"[UPDATE] ImgConverter v{latest} is available "
                          f"(https://github.com/SysAdminDoc/ImgConverter/releases)")
                _diag_log(f"Update available: v{latest}")
        except Exception:
            pass

    def _log_startup(self):
        """Log supported formats, dependency versions, and optional dep status on launch."""
        self._log(f"ImgConverter v{APP_VERSION}")
        # Core dependency versions
        from PIL import __version__ as pil_ver
        from PyQt6.QtCore import PYQT_VERSION_STR
        heif_ver = getattr(pillow_heif, "__version__", "unknown")
        self._log(f"Pillow {pil_ver}, pillow-heif {heif_ver}, PyQt6 {PYQT_VERSION_STR}")
        # Optional dependency versions
        opt_vers = []
        if HAS_RAWPY:
            opt_vers.append(f"rawpy {getattr(rawpy, '__version__', '?')}")
        if HAS_JXL:
            opt_vers.append(f"pillow-jxl {getattr(pillow_jxl, '__version__', '?')}")
        if HAS_QOI:
            opt_vers.append(f"qoi {getattr(qoi_lib, '__version__', '?')}")
        if opt_vers:
            self._log(f"Optional: {', '.join(opt_vers)}")
        self._log(f"Supported input formats: {get_format_support_summary()}")
        missing = []
        if not HAS_JXL:
            missing.append("JPEG XL (pip install pillow-jxl-plugin)")
        if not HAS_RAWPY:
            missing.append("Camera RAW (pip install rawpy)")
        if not HAS_QOI:
            missing.append("QOI (pip install qoi)")
        if missing:
            self._log(f"Optional formats unavailable: {', '.join(missing)}")
        if HAS_EXIFTOOL:
            self._log(f"ExifTool: {EXIFTOOL_PATH}")
        else:
            self._log("[WARN] ExifTool not found — metadata limited to EXIF/ICC/XMP (MakerNotes, GPS sub-IFDs, IPTC will be lost)")
        exts = sorted(get_supported_extensions())
        self._log(f"Scanning for: {' '.join(exts)}")
        self._log("")

    def _update_title(self, state: str = "base", **kwargs):
        """Update window title bar with contextual info."""
        base = f"ImgConverter v{APP_VERSION}"
        if state == "scanned":
            count = kwargs.get("count", 0)
            self.setWindowTitle(f"{base} -- {count} files")
        elif state == "converting":
            current = kwargs.get("current", 0)
            total = kwargs.get("total", 0)
            self.setWindowTitle(f"{base} -- Converting {current}/{total}")
        elif state == "done":
            ok = kwargs.get("ok", 0)
            fail = kwargs.get("fail", 0)
            self.setWindowTitle(f"{base} -- Done ({ok} converted, {fail} failed)")
        else:
            self.setWindowTitle(base)

    def _apply_dark_titlebar(self):
        """Apply dark title bar on Windows 10 19041+ / Windows 11."""
        if platform.system() != "Windows":
            return
        try:
            hwnd = int(self.winId())
            value = ctypes.c_int(1)
            ctypes.windll.dwmapi.DwmSetWindowAttribute(
                hwnd, 20, ctypes.byref(value), ctypes.sizeof(value)
            )
        except Exception:
            pass

    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(18, 14, 18, 8)
        root.setSpacing(12)

        # ── Scroll area for controls ──
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll_widget = QWidget()
        scroll_layout = QVBoxLayout(scroll_widget)
        scroll_layout.setContentsMargins(0, 0, 42, 0)
        scroll_layout.setSpacing(12)
        scroll.setWidget(scroll_widget)

        # ── Header ──
        header = QFrame()
        header.setObjectName("appHeader")
        hdr = QHBoxLayout(header)
        hdr.setContentsMargins(14, 12, 14, 12)
        hdr.setSpacing(12)

        icon_label = QLabel()
        icon_label.setPixmap(self._icon.pixmap(QSize(32, 32)))
        icon_label.setFixedSize(34, 34)
        icon_label.setAccessibleName("ImgConverter app icon")
        hdr.addWidget(icon_label)

        title_block = QVBoxLayout()
        title_block.setContentsMargins(0, 0, 0, 0)
        title_block.setSpacing(2)
        title_row = QHBoxLayout()
        title_row.setContentsMargins(0, 0, 0, 0)
        title_row.setSpacing(8)
        title = QLabel("ImgConverter")
        title.setObjectName("appTitle")
        ver = QLabel(f"v{APP_VERSION}")
        ver.setObjectName("appVersion")
        self.workflow_state = QLabel("Ready")
        self.workflow_state.setObjectName("workflowState")
        self.workflow_state.setAccessibleName("Workflow status")
        self.workflow_state.setAccessibleDescription("Current batch workflow state")
        title_row.addWidget(title)
        title_row.addWidget(ver)
        title_row.addWidget(self.workflow_state)
        title_row.addStretch()
        desc = QLabel("Metadata-safe batch image conversion")
        desc.setObjectName("appSubtitle")
        desc.setWordWrap(True)
        title_block.addLayout(title_row)
        title_block.addWidget(desc)
        hdr.addLayout(title_block, 1)
        scroll_layout.addWidget(header)

        # ── Source / Output ──
        io_group = QGroupBox("Source and output")
        io_grid = QGridLayout(io_group)
        io_grid.setHorizontalSpacing(10)
        io_grid.setVerticalSpacing(10)
        io_grid.setColumnStretch(1, 1)

        source_label = QLabel("Source folder")
        source_label.setObjectName("fieldLabel")
        self.src_edit = QLineEdit()
        self.src_edit.setPlaceholderText("Drop files or choose a folder to scan")
        self.src_edit.setMinimumWidth(120)
        self.src_edit.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self.src_edit.textChanged.connect(lambda: self.src_edit.setStyleSheet(""))
        io_grid.addWidget(self.src_edit, 0, 1)
        self.src_btn = QPushButton("Choose")
        self.src_btn.setFixedWidth(84)
        self.src_btn.clicked.connect(self._browse_source)
        io_grid.addWidget(self.src_btn, 0, 2)

        self.recent_btn = QToolButton()
        self.recent_btn.setArrowType(Qt.ArrowType.DownArrow)
        self.recent_btn.setFixedWidth(30)
        self.recent_btn.setToolTip("Recent directories")
        self._recent_menu = QMenu(self)
        self.recent_btn.setMenu(self._recent_menu)
        self.recent_btn.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self._recent_menu.aboutToShow.connect(self._populate_recent_menu)
        source_label_row = QHBoxLayout()
        source_label_row.setContentsMargins(0, 0, 0, 0)
        source_label_row.setSpacing(6)
        source_label_row.addWidget(source_label)
        source_label_row.addWidget(self.recent_btn)
        source_label_row.addStretch()
        io_grid.addLayout(source_label_row, 0, 0)

        output_label = QLabel("Output folder")
        output_label.setObjectName("fieldLabel")
        io_grid.addWidget(output_label, 1, 0)
        self.dst_edit = QLineEdit()
        self.dst_edit.setPlaceholderText("Default: source/converted")
        self.dst_edit.setMinimumWidth(120)
        self.dst_edit.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self.dst_edit.textChanged.connect(lambda: self.dst_edit.setStyleSheet(""))
        io_grid.addWidget(self.dst_edit, 1, 1)
        self.dst_btn = QPushButton("Choose")
        self.dst_btn.setFixedWidth(84)
        self.dst_btn.clicked.connect(self._browse_output)
        io_grid.addWidget(self.dst_btn, 1, 2)

        self.recursive_chk = QCheckBox("Scan subdirectories")
        self.recursive_chk.setChecked(True)

        self.structure_chk = QCheckBox("Preserve folder structure in output")
        self.structure_chk.setChecked(True)

        io_options = QHBoxLayout()
        io_options.setContentsMargins(0, 0, 0, 0)
        io_options.setSpacing(18)
        io_options.addWidget(self.recursive_chk)
        io_options.addWidget(self.structure_chk)
        io_options.addStretch()
        io_grid.addLayout(io_options, 2, 1, 1, 2)

        self.inplace_chk = QCheckBox("Convert in place after verified output")
        self.inplace_chk.setChecked(False)
        self.inplace_chk.setStyleSheet(f"color: {CAT['peach']};")
        self.inplace_chk.setToolTip("Writes each converted file next to the source, then deletes the source only after output validation succeeds.")
        self.inplace_chk.toggled.connect(self._on_inplace_toggled)
        io_grid.addWidget(self.inplace_chk, 3, 1, 1, 2)

        scroll_layout.addWidget(io_group)

        # ── Input Format Filter ──
        filter_group = QGroupBox("Input formats")
        filter_layout = QGridLayout(filter_group)
        filter_layout.setHorizontalSpacing(8)
        filter_layout.setVerticalSpacing(7)

        self._format_filters: dict[str, QCheckBox] = {}
        col = 0
        row = 0
        for name, (exts, available) in FORMAT_FAMILIES.items():
            chk = QCheckBox(name)
            chk.setProperty("decoderAvailable", available)
            chk.setChecked(available)
            chk.setEnabled(available)
            if not available:
                chk.setToolTip(f"{name} decoder not installed")
                chk.setStyleSheet(f"color: {CAT['overlay0']};")
            self._format_filters[name] = chk
            filter_layout.addWidget(chk, row, col)
            col += 1
            if col > 4:
                col = 0
                row += 1

        for c in range(5):
            filter_layout.setColumnStretch(c, 1)

        scroll_layout.addWidget(filter_group)

        # ── Conversion Options ──
        opt_group = QGroupBox("Output settings")
        opt_grid = QGridLayout(opt_group)
        opt_grid.setHorizontalSpacing(10)
        opt_grid.setVerticalSpacing(10)
        opt_grid.setColumnStretch(0, 0)
        opt_grid.setColumnStretch(1, 1)
        opt_grid.setColumnStretch(2, 0)
        opt_grid.setColumnStretch(3, 1)

        format_label = QLabel("Format")
        format_label.setObjectName("fieldLabel")
        opt_grid.addWidget(format_label, 0, 0)
        self.fmt_combo = QComboBox()
        self.fmt_combo.setMinimumWidth(120)
        self.fmt_combo.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self.fmt_combo.addItems([
            "Auto (JPEG for photos, PNG for transparency)",
            "JPEG", "PNG", "WebP", "AVIF", "TIFF", "JPEG XL"
        ])
        self.fmt_combo.setItemData(0, "JPEG for photos, PNG when transparency exists", Qt.ItemDataRole.ToolTipRole)
        self.fmt_combo.setItemData(1, "Best for photographs, lossy compression", Qt.ItemDataRole.ToolTipRole)
        self.fmt_combo.setItemData(2, "Lossless, supports transparency", Qt.ItemDataRole.ToolTipRole)
        self.fmt_combo.setItemData(3, "Modern format, smaller files", Qt.ItemDataRole.ToolTipRole)
        self.fmt_combo.setItemData(4, "Next-gen AV1 codec, best compression ratio", Qt.ItemDataRole.ToolTipRole)
        self.fmt_combo.setItemData(5, "Lossless, professional workflows", Qt.ItemDataRole.ToolTipRole)
        if HAS_JXL:
            self.fmt_combo.setItemData(6, "Next-gen JPEG replacement, best quality-to-size ratio", Qt.ItemDataRole.ToolTipRole)
        else:
            model = self.fmt_combo.model()
            model.item(6).setEnabled(False)
            self.fmt_combo.setItemData(6, "Requires pillow-jxl-plugin (pip install pillow-jxl-plugin)", Qt.ItemDataRole.ToolTipRole)
        opt_grid.addWidget(self.fmt_combo, 0, 1, 1, 2)

        self._preset_btn = QToolButton()
        self._preset_btn.setText("Presets")
        self._preset_btn.setObjectName("menuButton")
        self._preset_btn.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self._preset_btn.setToolTip("Apply a conversion preset")
        self._preset_menu = QMenu(self)
        self._preset_menu.aboutToShow.connect(self._populate_preset_menu)
        self._preset_btn.setMenu(self._preset_menu)
        opt_grid.addWidget(self._preset_btn, 0, 3)

        self.quality_desc_label = QLabel("JPEG/WebP quality")
        self.quality_desc_label.setObjectName("fieldLabel")
        opt_grid.addWidget(self.quality_desc_label, 1, 0)
        self.quality_slider = QSlider(Qt.Orientation.Horizontal)
        self.quality_slider.setRange(50, 100)
        self.quality_slider.setValue(92)
        self.quality_slider.setTickPosition(QSlider.TickPosition.TicksBelow)
        self.quality_slider.setTickInterval(5)
        opt_grid.addWidget(self.quality_slider, 1, 1, 1, 2)
        self.quality_label = QLabel("92")
        self.quality_label.setStyleSheet(f"color: {CAT['blue']}; font-weight: 700; min-width: 30px;")
        self.quality_slider.valueChanged.connect(lambda v: self.quality_label.setText(str(v)))
        opt_grid.addWidget(self.quality_label, 1, 3)

        workers_label = QLabel("Workers")
        workers_label.setObjectName("fieldLabel")
        opt_grid.addWidget(workers_label, 2, 0)
        self.workers_spin = QSpinBox()
        self.workers_spin.setRange(1, 32)
        cpu_count = os.cpu_count() or 4
        self.workers_spin.setValue(min(cpu_count, 8))
        opt_grid.addWidget(self.workers_spin, 2, 1)

        self.meta_chk = QCheckBox("Preserve metadata")
        self.meta_chk.setChecked(True)
        self.meta_chk.setToolTip("Preserve EXIF, ICC color profiles, and XMP metadata")
        self.meta_chk.toggled.connect(lambda checked: self.strip_meta_chk.setChecked(False) if checked else None)
        opt_grid.addWidget(self.meta_chk, 2, 2)

        self.strip_meta_chk = QCheckBox("Strip metadata")
        self.strip_meta_chk.setChecked(False)
        self.strip_meta_chk.setToolTip("Remove all EXIF, ICC, and XMP metadata from output files")
        self.strip_meta_chk.toggled.connect(lambda checked: self.meta_chk.setChecked(False) if checked else None)
        opt_grid.addWidget(self.strip_meta_chk, 2, 3)

        self.skip_existing_chk = QCheckBox("Skip files that already have output")
        self.skip_existing_chk.setChecked(False)
        self.skip_existing_chk.setToolTip("Resume interrupted batches — skip files where output already exists")
        opt_grid.addWidget(self.skip_existing_chk, 3, 0, 1, 2)

        self.progressive_jpeg_chk = QCheckBox("Progressive JPEG")
        self.progressive_jpeg_chk.setChecked(False)
        self.progressive_jpeg_chk.setToolTip("Save JPEGs as progressive (loads top-to-bottom in browsers)")
        opt_grid.addWidget(self.progressive_jpeg_chk, 3, 2)

        self.lossless_webp_chk = QCheckBox("Lossless WebP")
        self.lossless_webp_chk.setChecked(False)
        self.lossless_webp_chk.setToolTip("Save WebP files in lossless mode (larger files, no quality loss)")
        opt_grid.addWidget(self.lossless_webp_chk, 3, 3)

        # ── Resize ──
        self.resize_chk = QCheckBox("Resize")
        self.resize_chk.setChecked(False)
        self.resize_chk.toggled.connect(self._on_resize_toggled)
        opt_grid.addWidget(self.resize_chk, 4, 0)

        self.resize_combo = QComboBox()
        self.resize_combo.setMinimumWidth(100)
        self.resize_combo.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self.resize_combo.addItems(["Max Dimension", "Scale"])
        self.resize_combo.setEnabled(False)
        self.resize_combo.currentIndexChanged.connect(self._on_resize_mode_changed)
        opt_grid.addWidget(self.resize_combo, 4, 1)

        self.resize_spin = QSpinBox()
        self.resize_spin.setRange(100, 10000)
        self.resize_spin.setValue(1920)
        self.resize_spin.setSuffix(" px")
        self.resize_spin.setEnabled(False)
        opt_grid.addWidget(self.resize_spin, 4, 2, 1, 2)

        # ── Filename Prefix / Suffix ──
        prefix_label = QLabel("Prefix")
        prefix_label.setObjectName("fieldLabel")
        opt_grid.addWidget(prefix_label, 5, 0)
        self.prefix_edit = QLineEdit()
        self.prefix_edit.setPlaceholderText("e.g. converted_")
        self.prefix_edit.setMinimumWidth(100)
        self.prefix_edit.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        opt_grid.addWidget(self.prefix_edit, 5, 1)

        suffix_label = QLabel("Suffix")
        suffix_label.setObjectName("fieldLabel")
        opt_grid.addWidget(suffix_label, 5, 2)
        self.suffix_edit = QLineEdit()
        self.suffix_edit.setPlaceholderText("e.g. _web")
        self.suffix_edit.setMinimumWidth(100)
        self.suffix_edit.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        opt_grid.addWidget(self.suffix_edit, 5, 3)

        # ── Chroma Subsampling + sRGB ──
        self.chroma_chk = QCheckBox("4:2:0 chroma (smaller JPEG files)")
        self.chroma_chk.setChecked(False)
        self.chroma_chk.setToolTip("Use 4:2:0 chroma subsampling instead of 4:4:4 for smaller JPEGs (slight color detail loss)")
        opt_grid.addWidget(self.chroma_chk, 6, 0, 1, 2)

        self.srgb_chk = QCheckBox("Convert colors to sRGB")
        self.srgb_chk.setChecked(False)
        self.srgb_chk.setToolTip("Convert embedded ICC profiles (e.g. Display P3) to sRGB for maximum compatibility")
        opt_grid.addWidget(self.srgb_chk, 6, 2, 1, 2)

        # ── TIFF Compression + PNG Compression Level ──
        self.tiff_comp_label = QLabel("TIFF compression")
        self.tiff_comp_label.setObjectName("fieldLabel")
        opt_grid.addWidget(self.tiff_comp_label, 7, 0)
        self.tiff_comp_combo = QComboBox()
        self.tiff_comp_combo.addItems(["None", "LZW", "Deflate"])
        opt_grid.addWidget(self.tiff_comp_combo, 7, 1)

        self.png_level_label = QLabel("PNG compression")
        self.png_level_label.setObjectName("fieldLabel")
        opt_grid.addWidget(self.png_level_label, 7, 2)
        self.png_level_spin = QSpinBox()
        self.png_level_spin.setRange(1, 9)
        self.png_level_spin.setValue(6)
        self.png_level_spin.setToolTip("PNG compression level (1=fastest, 9=smallest)")
        opt_grid.addWidget(self.png_level_spin, 7, 3)

        scroll_layout.addWidget(opt_group)

        # ── Advanced output controls ──
        self.adv_toggle = QToolButton()
        self.adv_toggle.setObjectName("advancedToggle")
        self.adv_toggle.setCheckable(True)
        self.adv_toggle.setChecked(False)
        self.adv_toggle.setArrowType(Qt.ArrowType.RightArrow)
        self.adv_toggle.setText("Show advanced output controls")
        self.adv_toggle.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.adv_toggle.toggled.connect(self._toggle_advanced)
        scroll_layout.addWidget(self.adv_toggle)

        self.adv_group = QGroupBox("Advanced output controls")
        self.adv_group.setVisible(False)
        adv_group = self.adv_group
        adv_grid = QGridLayout(adv_group)
        adv_grid.setHorizontalSpacing(10)
        adv_grid.setVerticalSpacing(10)
        adv_grid.setColumnStretch(0, 0)
        adv_grid.setColumnStretch(1, 1)
        adv_grid.setColumnStretch(2, 0)
        adv_grid.setColumnStretch(3, 1)

        template_label = QLabel("Name template")
        template_label.setObjectName("fieldLabel")
        adv_grid.addWidget(template_label, 0, 0)
        self.template_edit = QLineEdit()
        self.template_edit.setPlaceholderText("{stem}  (overrides prefix/suffix)")
        self.template_edit.setMinimumWidth(100)
        self.template_edit.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self.template_edit.setToolTip(
            "Output filename template. Tokens: {stem} {ext} {fmt} {src_dir} "
            "{rel_dir} {width} {height} {date[:FMT]} {seq[:###]}"
        )
        adv_grid.addWidget(self.template_edit, 0, 1)

        dpi_label = QLabel("DPI")
        dpi_label.setObjectName("fieldLabel")
        adv_grid.addWidget(dpi_label, 0, 2)
        self.dpi_spin = QSpinBox()
        self.dpi_spin.setRange(0, 2400)
        self.dpi_spin.setValue(0)
        self.dpi_spin.setSpecialValueText("(unchanged)")
        self.dpi_spin.setToolTip("Set output DPI tag for JPEG/PNG/TIFF (0 = keep original)")
        adv_grid.addWidget(self.dpi_spin, 0, 3)

        self.avif_speed_label = QLabel("AVIF speed")
        self.avif_speed_label.setObjectName("fieldLabel")
        adv_grid.addWidget(self.avif_speed_label, 1, 0)
        self.avif_speed_spin = QSpinBox()
        self.avif_speed_spin.setRange(0, 10)
        self.avif_speed_spin.setValue(6)
        self.avif_speed_spin.setToolTip(
            "AVIF encoding speed 0-10. Lower = smaller file, slower encode. "
            "0 = best compression, 10 = fastest."
        )
        adv_grid.addWidget(self.avif_speed_spin, 1, 1)

        self.avif_codec_label = QLabel("AVIF codec")
        self.avif_codec_label.setObjectName("fieldLabel")
        adv_grid.addWidget(self.avif_codec_label, 1, 2)
        self.avif_codec_combo = QComboBox()
        self.avif_codec_combo.addItems(["Auto", "aom", "rav1e", "svt"])
        self.avif_codec_combo.setToolTip(
            "AVIF encoder. Auto lets Pillow choose, svt is fastest, "
            "aom has best compression, rav1e is in between."
        )
        adv_grid.addWidget(self.avif_codec_combo, 1, 3)

        self.frames_label = QLabel("Multi-frame")
        self.frames_label.setObjectName("fieldLabel")
        adv_grid.addWidget(self.frames_label, 2, 0)
        self.frames_combo = QComboBox()
        self.frames_combo.addItems(["First Frame Only", "Extract All Frames", "Preserve Animation"])
        self.frames_combo.setToolTip(
            "How to handle multi-frame sources (animated WebP/GIF/APNG, multi-page TIFF, HEIC sequences)"
        )
        adv_grid.addWidget(self.frames_combo, 2, 1)

        self.tone_map_label = QLabel("Tone map")
        self.tone_map_label.setObjectName("fieldLabel")
        adv_grid.addWidget(self.tone_map_label, 2, 2)
        self.tone_map_combo = QComboBox()
        self.tone_map_combo.addItems(["None", "Reinhard", "Hable", "Clip"])
        self.tone_map_combo.setToolTip(
            "HDR tone-mapping curve for PQ/HLG/wide-gamut sources"
        )
        adv_grid.addWidget(self.tone_map_combo, 2, 3)

        icc_label = QLabel("ICC override")
        icc_label.setObjectName("fieldLabel")
        adv_grid.addWidget(icc_label, 3, 0)
        self.icc_edit = QLineEdit()
        self.icc_edit.setPlaceholderText("sRGB or path to .icc file")
        self.icc_edit.setMinimumWidth(100)
        self.icc_edit.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self.icc_edit.setToolTip(
            "Embed a specific ICC profile. Enter 'sRGB' for built-in or "
            "a path to an .icc/.icm file. Leave empty to keep source profile."
        )
        adv_grid.addWidget(self.icc_edit, 3, 1, 1, 3)

        watermark_label = QLabel("Watermark")
        watermark_label.setObjectName("fieldLabel")
        adv_grid.addWidget(watermark_label, 4, 0)
        self.watermark_edit = QLineEdit()
        self.watermark_edit.setPlaceholderText("text|position|opacity  or  logo.png|position|opacity")
        self.watermark_edit.setMinimumWidth(100)
        self.watermark_edit.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self.watermark_edit.setToolTip(
            "Watermark spec: 'TEXT|position|opacity' or 'image.png|position|opacity'. "
            "Positions: top-left, top, top-right, left, center, right, "
            "bottom-left, bottom, bottom-right. Opacity: 0.0-1.0."
        )
        adv_grid.addWidget(self.watermark_edit, 4, 1, 1, 3)

        canvas_label = QLabel("Canvas")
        canvas_label.setObjectName("fieldLabel")
        adv_grid.addWidget(canvas_label, 5, 0)
        self.canvas_edit = QLineEdit()
        self.canvas_edit.setPlaceholderText("WxH  (e.g. 1920x1080)")
        self.canvas_edit.setMinimumWidth(100)
        self.canvas_edit.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self.canvas_edit.setToolTip("Pad output to canvas size preserving aspect ratio")
        adv_grid.addWidget(self.canvas_edit, 5, 1)

        canvas_bg_label = QLabel("Canvas BG")
        canvas_bg_label.setObjectName("fieldLabel")
        adv_grid.addWidget(canvas_bg_label, 5, 2)
        self.canvas_bg_edit = QLineEdit()
        self.canvas_bg_edit.setText("transparent")
        self.canvas_bg_edit.setMinimumWidth(100)
        self.canvas_bg_edit.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self.canvas_bg_edit.setToolTip("Canvas background: 'transparent', '#RRGGBB', or named color")
        adv_grid.addWidget(self.canvas_bg_edit, 5, 3)

        self.xmp_sidecar_chk = QCheckBox("Emit XMP sidecar")
        self.xmp_sidecar_chk.setToolTip("Write .xmp sidecar alongside output (Adobe Bridge / darktable convention)")
        adv_grid.addWidget(self.xmp_sidecar_chk, 6, 0, 1, 2)

        self.recompress_chk = QCheckBox("Recompress JPEG (lossless)")
        self.recompress_chk.setToolTip(
            "For JPEG→JPEG: use jpegoptim/jpegtran for pixel-lossless size reduction "
            "instead of decode-re-encode. Requires jpegoptim or jpegtran on PATH."
        )
        adv_grid.addWidget(self.recompress_chk, 6, 2, 1, 2)

        self.only_if_smaller_chk = QCheckBox("Only if smaller by")
        self.only_if_smaller_chk.setToolTip("Discard output when it's not meaningfully smaller than input")
        adv_grid.addWidget(self.only_if_smaller_chk, 7, 0)
        self.only_if_smaller_spin = QSpinBox()
        self.only_if_smaller_spin.setRange(1, 99)
        self.only_if_smaller_spin.setValue(20)
        self.only_if_smaller_spin.setSuffix(" %")
        self.only_if_smaller_spin.setEnabled(False)
        self.only_if_smaller_chk.toggled.connect(self.only_if_smaller_spin.setEnabled)
        adv_grid.addWidget(self.only_if_smaller_spin, 7, 1)

        target_label = QLabel("Target KB")
        target_label.setObjectName("fieldLabel")
        adv_grid.addWidget(target_label, 7, 2)
        self.target_kb_spin = QSpinBox()
        self.target_kb_spin.setRange(0, 100000)
        self.target_kb_spin.setValue(0)
        self.target_kb_spin.setSpecialValueText("(disabled)")
        self.target_kb_spin.setToolTip("Binary-search quality to hit a target output size in KB (0 = disabled)")
        adv_grid.addWidget(self.target_kb_spin, 7, 3)

        self.png_lossy_chk = QCheckBox("Lossy PNG (pngquant)")
        self.png_lossy_chk.setToolTip(
            "Run pngquant for 50-80% PNG size reduction via lossy quantization. "
            "Requires pngquant on PATH."
        )
        adv_grid.addWidget(self.png_lossy_chk, 8, 0, 1, 2)

        scroll_layout.addWidget(adv_group)

        # Show/hide format-specific controls after every dependent widget exists.
        self.fmt_combo.currentIndexChanged.connect(self._on_format_changed)
        self._on_format_changed(self.fmt_combo.currentIndex())

        # ── Actions ──
        action_bar = QFrame()
        action_bar.setObjectName("actionBar")
        actions = QVBoxLayout(action_bar)
        actions.setContentsMargins(12, 10, 12, 10)
        actions.setSpacing(8)
        primary_actions = QHBoxLayout()
        primary_actions.setContentsMargins(0, 0, 0, 0)
        primary_actions.setSpacing(10)
        secondary_actions = QHBoxLayout()
        secondary_actions.setContentsMargins(0, 0, 0, 0)
        secondary_actions.setSpacing(10)

        self.scan_btn = QPushButton("Scan Source")
        self.scan_btn.setObjectName("primaryBtn")
        self.scan_btn.clicked.connect(self._scan)
        primary_actions.addWidget(self.scan_btn)

        self.convert_btn = QPushButton("Convert Batch")
        self.convert_btn.setObjectName("primaryBtn")
        self.convert_btn.setEnabled(False)
        self.convert_btn.clicked.connect(self._convert)
        primary_actions.addWidget(self.convert_btn)

        self.stop_btn = QPushButton("Cancel")
        self.stop_btn.setObjectName("stopBtn")
        self.stop_btn.setEnabled(False)
        self.stop_btn.clicked.connect(self._stop)
        primary_actions.addWidget(self.stop_btn)

        self.paste_btn = QPushButton("Paste Clipboard")
        self.paste_btn.setToolTip("Paste an image from the clipboard as a temporary PNG input")
        self.paste_btn.clicked.connect(self._paste_clipboard)
        primary_actions.addWidget(self.paste_btn)

        primary_actions.addStretch()

        self.auto_open_chk = QCheckBox("Auto-open output")
        self.auto_open_chk.setChecked(False)
        self.auto_open_chk.setToolTip("Automatically open the output folder when conversion finishes")
        secondary_actions.addStretch()
        secondary_actions.addWidget(self.auto_open_chk)

        self.open_output_btn = QPushButton("Open Output")
        self.open_output_btn.setEnabled(False)
        self.open_output_btn.clicked.connect(self._open_output)
        secondary_actions.addWidget(self.open_output_btn)

        actions.addLayout(primary_actions)
        actions.addLayout(secondary_actions)

        scroll_layout.removeWidget(filter_group)
        scroll_layout.insertWidget(2, action_bar)
        scroll_layout.insertWidget(4, filter_group)

        # ── Stats bar ──
        stats_frame = QFrame()
        stats_frame.setObjectName("statsFrame")
        stats_layout = QHBoxLayout(stats_frame)
        stats_layout.setContentsMargins(10, 10, 10, 10)
        stats_layout.setSpacing(8)

        self.stat_files = self._make_stat("0", "Files Found")
        self.stat_size = self._make_stat("0 B", "Total Size")
        self.stat_done = self._make_stat("0", "Converted")
        self.stat_skipped = self._make_stat("0", "Skipped")
        self.stat_failed = self._make_stat("0", "Failed")
        self.stat_saved = self._make_stat("0 B", "Space Saved")

        for w in [self.stat_files, self.stat_size, self.stat_done,
                  self.stat_skipped, self.stat_failed, self.stat_saved]:
            stats_layout.addWidget(w)

        scroll_layout.addWidget(stats_frame)

        # ── Progress ──
        self.progress_bar = QProgressBar()
        self.progress_bar.setTextVisible(True)
        self.progress_bar.setValue(0)
        scroll_layout.addWidget(self.progress_bar)

        # ── Log + controls ──
        log_header = QHBoxLayout()
        log_label = QLabel("Activity log")
        log_label.setObjectName("fieldLabel")
        log_header.addWidget(log_label)
        log_header.addStretch()

        self.export_log_btn = QPushButton("Export Log")
        self.export_log_btn.setObjectName("miniBtn")
        self.export_log_btn.setToolTip("Save the conversion log as a text file")
        self.export_log_btn.clicked.connect(self._export_log)
        log_header.addWidget(self.export_log_btn)

        self.export_csv_btn = QPushButton("Export CSV")
        self.export_csv_btn.setObjectName("miniBtn")
        self.export_csv_btn.setToolTip("Export conversion results as a CSV report")
        self.export_csv_btn.clicked.connect(self._export_csv)
        log_header.addWidget(self.export_csv_btn)

        self.clear_log_btn = QPushButton("Clear")
        self.clear_log_btn.setObjectName("miniBtn")
        self.clear_log_btn.setToolTip("Clear the log output")
        self.clear_log_btn.clicked.connect(self._clear_log)
        log_header.addWidget(self.clear_log_btn)

        log_container = QWidget()
        log_container_layout = QVBoxLayout(log_container)
        log_container_layout.setContentsMargins(0, 0, 0, 0)
        log_container_layout.setSpacing(4)
        log_container_layout.addLayout(log_header)

        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setPlaceholderText("Scan a source folder to see matching files, warnings, and conversion results.")
        self.log_view.setMaximumBlockCount(5000)
        self.log_view.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.log_view.customContextMenuRequested.connect(self._on_log_context_menu)
        log_container_layout.addWidget(self.log_view, 1)

        # ── Splitter: controls scroll area + log ──
        splitter = QSplitter(Qt.Orientation.Vertical)
        splitter.addWidget(scroll)
        splitter.addWidget(log_container)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([760, 160])
        root.addWidget(splitter, 1)

        # ── Status bar ──
        self.status_bar = QStatusBar()
        self.setStatusBar(self.status_bar)
        self._set_workflow_state("Ready", "Select a source folder to begin.")

    def _make_stat(self, value: str, label: str) -> QWidget:
        w = QFrame()
        w.setObjectName("statCard")
        w.setAccessibleName(label)
        w.setAccessibleDescription(f"{label}: {value}")
        lay = QVBoxLayout(w)
        lay.setContentsMargins(8, 7, 8, 7)
        lay.setSpacing(2)
        lay.setAlignment(Qt.AlignmentFlag.AlignCenter)
        val = QLabel(value)
        val.setObjectName("statValue")
        val.setAlignment(Qt.AlignmentFlag.AlignCenter)
        val.setMinimumWidth(88)
        lbl = QLabel(label)
        lbl.setObjectName("statLabel")
        lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(val)
        lay.addWidget(lbl)
        w._val = val
        return w

    def _toggle_advanced(self, checked: bool):
        self.adv_group.setVisible(checked)
        self.adv_toggle.setArrowType(Qt.ArrowType.DownArrow if checked else Qt.ArrowType.RightArrow)
        self.adv_toggle.setText(
            "Hide advanced output controls" if checked else "Show advanced output controls"
        )

    def _set_workflow_state(self, state: str, message: str | None = None):
        if hasattr(self, "workflow_state"):
            self.workflow_state.setText(state)
        if message and hasattr(self, "status_bar"):
            self.status_bar.showMessage(message)

    def _set_line_error(self, widget, message: str):
        widget.setStyleSheet(f"border: 1px solid {CAT['red']};")
        widget.setFocus()
        self._set_workflow_state("Needs input", message)

    def _set_conversion_busy(self, busy: bool):
        enabled = not busy
        for attr in [
            "src_edit", "src_btn", "recent_btn", "dst_edit", "dst_btn",
            "recursive_chk", "structure_chk", "inplace_chk",
            "fmt_combo", "_preset_btn", "quality_slider", "workers_spin",
            "meta_chk", "strip_meta_chk", "skip_existing_chk",
            "progressive_jpeg_chk", "lossless_webp_chk", "resize_chk",
            "resize_combo", "resize_spin", "prefix_edit", "suffix_edit",
            "chroma_chk", "srgb_chk", "tiff_comp_combo", "png_level_spin",
            "adv_toggle", "template_edit", "dpi_spin", "avif_speed_spin",
            "avif_codec_combo", "frames_combo", "tone_map_combo", "icc_edit",
            "watermark_edit", "canvas_edit", "canvas_bg_edit",
            "xmp_sidecar_chk", "recompress_chk", "only_if_smaller_chk",
            "only_if_smaller_spin", "target_kb_spin", "png_lossy_chk",
            "paste_btn", "auto_open_chk",
        ]:
            w = getattr(self, attr, None)
            if w is not None:
                w.setEnabled(enabled)
        for chk in getattr(self, "_format_filters", {}).values():
            if chk.property("decoderAvailable") is False:
                chk.setEnabled(False)
            else:
                chk.setEnabled(enabled)
        if self.inplace_chk.isChecked() and enabled:
            self.dst_edit.setEnabled(False)
            self.dst_btn.setEnabled(False)
            self.structure_chk.setEnabled(False)

    def _log(self, msg: str):
        self.log_view.appendPlainText(msg)

    # ── Drag & Drop ──
    def dragEnterEvent(self, event: QDragEnterEvent):
        if event.mimeData().hasUrls():
            supported = get_supported_extensions()
            for url in event.mimeData().urls():
                if url.isLocalFile():
                    p = Path(url.toLocalFile())
                    if p.is_dir() or (p.is_file() and p.suffix.lower() in supported):
                        event.acceptProposedAction()
                        self.src_edit.setStyleSheet(f"border: 2px solid {CAT['lavender']};")
                        return

    def dragMoveEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dragLeaveEvent(self, event):
        self.src_edit.setStyleSheet("")

    def dropEvent(self, event: QDropEvent):
        self.src_edit.setStyleSheet("")
        urls = event.mimeData().urls()

        # Check for directory drop first
        for url in urls:
            path = url.toLocalFile()
            if Path(path).is_dir():
                self.src_edit.setText(path)
                if not self.dst_edit.text() and not self.inplace_chk.isChecked():
                    self.dst_edit.setText(str(Path(path) / "converted"))
                self._add_recent_dir(path)
                self._log(f"Source set via drag & drop: {path}")
                event.acceptProposedAction()
                return

        # Handle individual file drops
        supported = get_supported_extensions()
        files = []
        for url in urls:
            p = Path(url.toLocalFile())
            if p.is_file() and p.suffix.lower() in supported:
                files.append(p)

        if files:
            files.sort()
            total_size = sum(f.stat().st_size for f in files)
            self._scan_result = ScanResult(files=files, total_size=total_size, elapsed=0)
            common_parent = str(Path(os.path.commonpath([str(f.parent) for f in files])))
            self.src_edit.setText(common_parent)
            if not self.dst_edit.text() and not self.inplace_chk.isChecked():
                self.dst_edit.setText(str(Path(common_parent) / "converted"))
            self.stat_files._val.setText(str(len(files)))
            self.stat_size._val.setText(_fmt_size(total_size))
            self.convert_btn.setEnabled(True)
            self._update_title("scanned", count=len(files))
            self._log(f"Added {len(files)} file{'s' if len(files) != 1 else ''} via drag & drop")
            event.acceptProposedAction()

    # ── Clipboard paste ──
    def _paste_clipboard(self):
        clipboard = QApplication.clipboard()
        img = clipboard.image()
        if img.isNull():
            self._log("[PASTE] No image found on clipboard.")
            self._set_workflow_state("No image", "Clipboard does not contain an image.")
            return
        tmp_dir = USER_CACHE_DIR / "clipboard"
        tmp_dir.mkdir(parents=True, exist_ok=True)
        ts = int(time.time() * 1000)
        tmp_path = tmp_dir / f"clipboard_{ts}.png"
        img.save(str(tmp_path), "PNG")
        files = [tmp_path]
        total_size = tmp_path.stat().st_size
        self._scan_result = ScanResult(files=files, total_size=total_size, elapsed=0)
        self.src_edit.setText(str(tmp_dir))
        if not self.dst_edit.text() and not self.inplace_chk.isChecked():
            self.dst_edit.setText(str(tmp_dir / "converted"))
        self.stat_files._val.setText("1")
        self.stat_size._val.setText(_fmt_size(total_size))
        self.convert_btn.setEnabled(True)
        self._update_title("scanned", count=1)
        self.progress_bar.setMaximum(100)
        self.progress_bar.setValue(0)
        self.progress_bar.setFormat("Ready to convert")
        self._set_workflow_state("Ready to convert", "Clipboard image is ready to convert.")
        self._log(f"[PASTE] Pasted clipboard image as {tmp_path.name} ({_fmt_size(total_size)})")

    # ── Log context menu ──
    def _on_log_context_menu(self, pos):
        menu = QMenu(self)
        copy_sel = menu.addAction("Copy Selection")
        copy_sel.setEnabled(self.log_view.textCursor().hasSelection())
        copy_sel.triggered.connect(self.log_view.copy)

        copy_all = menu.addAction("Copy All")
        copy_all.triggered.connect(lambda: QApplication.clipboard().setText(self.log_view.toPlainText()))

        menu.addSeparator()

        open_loc = menu.addAction("Open File Location")
        open_loc.setEnabled(self._last_ok_dst is not None)
        open_loc.triggered.connect(
            lambda: _open_path(str(self._last_ok_dst.parent)) if self._last_ok_dst else None
        )

        # Drag converted file to Explorer / Finder. Constructs the drag
        # object on click; user has to click + drag in a single gesture.
        # Pure Qt would require a more complex QDrag flow; the menu action
        # gives users a working alternative that doesn't require GUI tests.
        if self._last_ok_dst is not None:
            menu.addSeparator()
            copy_dst_path = menu.addAction("Copy Output Path")
            copy_dst_path.triggered.connect(
                lambda: QApplication.clipboard().setText(str(self._last_ok_dst))
            )
            reveal_dst = menu.addAction("Reveal Output in File Manager")
            reveal_dst.triggered.connect(
                lambda: _open_path(str(self._last_ok_dst.parent))
            )

        menu.exec(self.log_view.mapToGlobal(pos))

    # ── Presets ──
    def _populate_preset_menu(self):
        self._preset_menu.clear()
        for name in list_presets():
            self._preset_menu.addAction(name, lambda n=name: self._apply_preset(n))

    def _apply_preset(self, name: str):
        preset = list_presets().get(name)
        if not preset:
            return
        if "fmt" in preset:
            self.fmt_combo.setCurrentIndex(preset["fmt"])
        if "quality" in preset:
            self.quality_slider.setValue(preset["quality"])
        if "progressive_jpeg" in preset:
            self.progressive_jpeg_chk.setChecked(preset["progressive_jpeg"])
        else:
            self.progressive_jpeg_chk.setChecked(False)
        if "lossless_webp" in preset:
            self.lossless_webp_chk.setChecked(preset["lossless_webp"])
        else:
            self.lossless_webp_chk.setChecked(False)
        if "chroma_subsampling" in preset:
            self.chroma_chk.setChecked(preset["chroma_subsampling"])
        else:
            self.chroma_chk.setChecked(False)
        if "convert_to_srgb" in preset:
            self.srgb_chk.setChecked(preset["convert_to_srgb"])
        else:
            self.srgb_chk.setChecked(False)
        if "resize_enabled" in preset:
            self.resize_chk.setChecked(preset["resize_enabled"])
            if preset["resize_enabled"]:
                if "resize_mode" in preset:
                    self.resize_combo.setCurrentIndex(preset["resize_mode"])
                if "resize_value" in preset:
                    self.resize_spin.setValue(preset["resize_value"])
        if "tiff_compression" in preset:
            self.tiff_comp_combo.setCurrentIndex(preset["tiff_compression"])
        if "png_compress_level" in preset:
            self.png_level_spin.setValue(preset["png_compress_level"])
        self._log(f"Preset applied: {name}")

    # ── In-place toggle ──
    def _on_inplace_toggled(self, checked: bool):
        self.dst_edit.setEnabled(not checked)
        self.dst_btn.setEnabled(not checked)
        self.structure_chk.setEnabled(not checked)
        if checked:
            self.dst_edit.setPlaceholderText("In-place mode writes next to each source")
            self._set_workflow_state("In-place mode", "In-place mode will delete each source only after output validation succeeds.")
        else:
            self.dst_edit.setPlaceholderText("Default: source/converted")
            self._set_workflow_state("Ready", "Output folder mode restored.")

    # ── Browse ──
    def _browse_source(self):
        d = QFileDialog.getExistingDirectory(self, "Select Source Directory",
                                             self.src_edit.text() or str(Path.home()))
        if d:
            self.src_edit.setText(d)
            if not self.dst_edit.text():
                self.dst_edit.setText(str(Path(d) / "converted"))
            self._add_recent_dir(d)

    def _browse_output(self):
        d = QFileDialog.getExistingDirectory(self, "Select Output Directory",
                                             self.dst_edit.text() or str(Path.home()))
        if d:
            self.dst_edit.setText(d)

    # ── Format filter ──
    def _get_enabled_extensions(self) -> set[str]:
        """Build extension set from checked format filter checkboxes."""
        exts = set()
        for name, chk in self._format_filters.items():
            if chk.isChecked() and chk.isEnabled():
                family_exts, _ = FORMAT_FAMILIES[name]
                exts |= family_exts
        return exts

    # ── Recent directories ──
    def _add_recent_dir(self, path: str):
        """Add a directory to the recent list (max 10, deduplicated)."""
        recent = self._get_recent_dirs()
        if path in recent:
            recent.remove(path)
        recent.insert(0, path)
        self.settings.setValue("recent_dirs", json.dumps(recent[:10]))

    def _get_recent_dirs(self) -> list[str]:
        raw = self.settings.value("recent_dirs", "[]")
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return []

    def _populate_recent_menu(self):
        self._recent_menu.clear()
        recent = self._get_recent_dirs()
        if not recent:
            action = self._recent_menu.addAction("No recent directories")
            action.setEnabled(False)
        else:
            for path in recent:
                action = self._recent_menu.addAction(path)
                action.triggered.connect(lambda checked, p=path: self._set_source_dir(p))

    def _set_source_dir(self, path: str):
        self.src_edit.setText(path)
        if not self.dst_edit.text() and not self.inplace_chk.isChecked():
            self.dst_edit.setText(str(Path(path) / "converted"))
        self._log(f"Source set from recent: {path}")

    # ── Format-dependent controls ──
    def _on_format_changed(self, idx: int):
        """Show/hide format-specific controls based on selected output format."""
        # idx: 0=Auto, 1=JPEG, 2=PNG, 3=WebP, 4=AVIF, 5=TIFF, 6=JPEG XL
        is_auto = idx == 0
        is_jpeg = idx == 1
        is_png = idx == 2
        is_webp = idx == 3
        is_avif = idx == 4
        is_tiff = idx == 5
        is_jxl = idx == 6

        # Quality slider: JPEG, WebP, AVIF, JXL, Auto
        show_quality = is_auto or is_jpeg or is_webp or is_avif or is_jxl
        self.quality_desc_label.setVisible(show_quality)
        self.quality_slider.setVisible(show_quality)
        self.quality_label.setVisible(show_quality)

        # Quality label text
        if is_jpeg:
            self.quality_desc_label.setText("JPEG quality")
        elif is_webp:
            self.quality_desc_label.setText("WebP quality")
        elif is_avif:
            self.quality_desc_label.setText("AVIF quality")
        elif is_jxl:
            self.quality_desc_label.setText("JXL quality")
        else:
            self.quality_desc_label.setText("JPEG/WebP quality")

        # Chroma subsampling: JPEG, Auto
        self.chroma_chk.setVisible(is_auto or is_jpeg)

        # Progressive JPEG: JPEG, Auto
        self.progressive_jpeg_chk.setVisible(is_auto or is_jpeg)

        # Lossless WebP: WebP, Auto
        self.lossless_webp_chk.setVisible(is_webp)

        # TIFF compression: TIFF only
        self.tiff_comp_label.setVisible(is_tiff)
        self.tiff_comp_combo.setVisible(is_tiff)

        # PNG compression: PNG only
        self.png_level_label.setVisible(is_png)
        self.png_level_spin.setVisible(is_png)

        # AVIF speed + codec: AVIF only
        if hasattr(self, "avif_speed_label"):
            self.avif_speed_label.setVisible(is_avif)
            self.avif_speed_spin.setVisible(is_avif)
            self.avif_codec_label.setVisible(is_avif)
            self.avif_codec_combo.setVisible(is_avif)

    # ── Resize controls ──
    def _on_resize_toggled(self, checked: bool):
        self.resize_combo.setEnabled(checked)
        self.resize_spin.setEnabled(checked)

    def _on_resize_mode_changed(self, idx: int):
        if idx == 0:  # Max Dimension
            self.resize_spin.setRange(100, 10000)
            self.resize_spin.setSuffix(" px")
            if self.resize_spin.value() < 100:
                self.resize_spin.setValue(1920)
        else:  # Scale
            self.resize_spin.setRange(1, 500)
            self.resize_spin.setSuffix(" %")
            if self.resize_spin.value() > 500:
                self.resize_spin.setValue(50)

    # ── Log controls ──
    def _export_log(self):
        path, _ = QFileDialog.getSaveFileName(
            self, "Export Log", str(Path.home() / "imgconverter_log.txt"),
            "Text Files (*.txt);;All Files (*)"
        )
        if path:
            Path(path).write_text(self.log_view.toPlainText(), encoding="utf-8")
            self._log(f"Log exported to {path}")

    def _export_csv(self):
        """Export conversion results as a CSV report."""
        if not self._results:
            self._log("[ERROR] No conversion results to export.")
            self._set_workflow_state("No report", "Run a conversion before exporting a CSV report.")
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "Export CSV Report", str(Path.home() / "imgconverter_report.csv"),
            "CSV Files (*.csv);;All Files (*)"
        )
        if path:
            import csv
            with open(path, "w", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                writer.writerow(["Source", "Output", "Status", "Size Before", "Size After",
                                 "Delta", "Elapsed (s)", "Warnings"])
                for r in self._results:
                    status = "OK" if r.success else ("SKIP" if r.skipped else "FAIL")
                    delta = r.size_before - r.size_after if r.success else 0
                    writer.writerow([
                        str(r.src), str(r.dst or ""), status,
                        r.size_before, r.size_after, delta,
                        f"{r.elapsed:.3f}",
                        "; ".join(r.warnings) if r.warnings else "",
                    ])
            self._log(f"CSV report exported to {path}")

    def _clear_log(self):
        self.log_view.clear()
        self._update_title()

    # ── Scan ──
    def _scan(self):
        src = self.src_edit.text().strip()
        if not src or not Path(src).is_dir():
            self._log("[ERROR] Please select a valid source directory.")
            self._set_line_error(self.src_edit, "Select a valid source folder before scanning.")
            return

        self._update_title()
        self.scan_btn.setEnabled(False)
        self.convert_btn.setEnabled(False)
        self._set_workflow_state("Scanning", "Scanning source folder...")
        self._log(f"[SCAN] {src}")

        enabled_exts = self._get_enabled_extensions()
        if not enabled_exts:
            self._log("[ERROR] No input formats selected in the filter panel.")
            self.scan_btn.setEnabled(True)
            self._set_workflow_state("Needs input", "Select at least one available input format.")
            return

        self._scanner = ScanWorker(src, self.recursive_chk.isChecked(), enabled_exts)
        self._scanner.log.connect(self._log)
        self._scanner.scan_progress.connect(self._on_scan_progress)
        self._scanner.finished.connect(self._on_scan_done)
        self.progress_bar.setMaximum(0)
        self.progress_bar.setFormat("Scanning...")
        self._scanner.start()

    def _on_scan_progress(self, count: int, total_size: int, directory: str, dir_count: int):
        try:
            rel = str(Path(directory).relative_to(self.src_edit.text().strip()))
            if rel == ".":
                rel = Path(directory).name
        except ValueError:
            rel = directory
        self.stat_files._val.setText(str(count))
        self.stat_size._val.setText(_fmt_size(total_size))
        self._log(f"  {rel}/ — {dir_count} file{'s' if dir_count != 1 else ''}")
        self.status_bar.showMessage(f"Scanning... {count} files found ({_fmt_size(total_size)})")

    def _on_scan_done(self, result: ScanResult):
        self._scan_result = result
        self.scan_btn.setEnabled(True)
        self.progress_bar.setMaximum(100)
        self.progress_bar.setValue(0)
        self.progress_bar.setFormat("%p%")

        self.stat_files._val.setText(str(len(result.files)))
        self.stat_size._val.setText(_fmt_size(result.total_size))

        if result.files:
            self.convert_btn.setEnabled(True)
            self._update_title("scanned", count=len(result.files))
            # Count by format family
            ext_counts: dict[str, int] = {}
            for f in result.files:
                s = f.suffix.lower()
                if s in JPEG_EXTS:
                    ext_counts["JPEG"] = ext_counts.get("JPEG", 0) + 1
                elif s in PNG_EXTS:
                    ext_counts["PNG"] = ext_counts.get("PNG", 0) + 1
                elif s in HEIC_EXTS:
                    ext_counts["HEIC"] = ext_counts.get("HEIC", 0) + 1
                elif s in AVIF_EXTS:
                    ext_counts["AVIF"] = ext_counts.get("AVIF", 0) + 1
                elif s in WEBP_EXTS:
                    ext_counts["WebP"] = ext_counts.get("WebP", 0) + 1
                elif s in JXL_EXTS:
                    ext_counts["JXL"] = ext_counts.get("JXL", 0) + 1
                elif s in RAW_EXTS:
                    ext_counts["RAW"] = ext_counts.get("RAW", 0) + 1
                elif s in TIFF_EXTS:
                    ext_counts["TIFF"] = ext_counts.get("TIFF", 0) + 1
                elif s in BMP_EXTS:
                    ext_counts["BMP"] = ext_counts.get("BMP", 0) + 1
                elif s in JP2_EXTS:
                    ext_counts["JP2"] = ext_counts.get("JP2", 0) + 1
                elif s in QOI_EXTS:
                    ext_counts["QOI"] = ext_counts.get("QOI", 0) + 1
                elif s in ICO_EXTS:
                    ext_counts["ICO"] = ext_counts.get("ICO", 0) + 1
            breakdown = ", ".join(f"{v} {k}" for k, v in sorted(ext_counts.items(), key=lambda x: -x[1]))
            self._log(f"Breakdown: {breakdown}")
            self.progress_bar.setFormat("Ready to convert")
            self._set_workflow_state(
                "Ready to convert",
                f"Found {len(result.files)} files ({_fmt_size(result.total_size)}). Ready to convert."
            )
        else:
            self.convert_btn.setEnabled(False)
            self.progress_bar.setFormat("No files found")
            self._update_title()
            self._set_workflow_state("No files", "No supported image files found with the current filters.")
            self._log("[INFO] No supported image files found. Check the source folder and input format filters.")

    # ── Convert ──
    def _convert(self):
        if not self._scan_result or not self._scan_result.files:
            return

        in_place = self.inplace_chk.isChecked()

        if in_place:
            dst = self.src_edit.text().strip()
        else:
            dst = self.dst_edit.text().strip()
            if not dst:
                src = self.src_edit.text().strip()
                dst = str(Path(src) / "converted")
                self.dst_edit.setText(dst)
            Path(dst).mkdir(parents=True, exist_ok=True)

        # Source/output overlap guard
        src_resolved = Path(self.src_edit.text().strip()).resolve()
        dst_resolved = Path(dst).resolve()
        if not in_place and dst_resolved == src_resolved:
            self._log("[ERROR] Output directory is the same as source directory. Use a subfolder or enable in-place mode.")
            self.scan_btn.setEnabled(True)
            self.convert_btn.setEnabled(True)
            self._set_line_error(self.dst_edit, "Choose a different output folder or enable in-place mode.")
            return
        if not in_place and dst_resolved != src_resolved:
            try:
                if dst_resolved.is_relative_to(src_resolved):
                    self._log("[WARN] Output directory is inside the source directory. This is fine for most workflows.")
            except (ValueError, TypeError):
                pass

        fmt_map = {0: "auto", 1: "jpeg", 2: "png", 3: "webp", 4: "avif", 5: "tiff", 6: "jxl"}
        fmt = fmt_map.get(self.fmt_combo.currentIndex(), "auto")

        # Disk space pre-check
        try:
            estimated = _estimate_output_size(self._scan_result.total_size, fmt)
            disk = shutil.disk_usage(dst)
            if estimated > disk.free:
                self._log(
                    f"[ERROR] Not enough disk space. Estimated output: {_fmt_size(estimated)}, "
                    f"available: {_fmt_size(disk.free)}"
                )
                self.scan_btn.setEnabled(True)
                self.convert_btn.setEnabled(True)
                self._set_workflow_state("Blocked", "Not enough disk space for the estimated output.")
                return
            if estimated > disk.free * 0.8:
                self._log(
                    f"[WARN] Estimated output ({_fmt_size(estimated)}) exceeds 80% of "
                    f"available disk space ({_fmt_size(disk.free)})"
                )
        except (OSError, ValueError):
            pass  # Network drives or unmounted paths may not support disk_usage

        resize_mode = "none"
        if self.resize_chk.isChecked():
            resize_mode = "max_dim" if self.resize_combo.currentIndex() == 0 else "scale"

        self._results = []
        self._convert_start_time = time.perf_counter()
        self.progress_bar.setValue(0)
        self.progress_bar.setMaximum(len(self._scan_result.files))
        self.stat_done._val.setText("0")
        self.stat_skipped._val.setText("0")
        self.stat_failed._val.setText("0")
        self.stat_saved._val.setText("0 B")
        # Reset stat colors to default green for new batch
        self.stat_done._val.setStyleSheet(STAT_VALUE_STYLE)
        self.stat_skipped._val.setStyleSheet(STAT_VALUE_STYLE)
        self.stat_failed._val.setStyleSheet(STAT_VALUE_STYLE)
        self.stat_saved._val.setStyleSheet(STAT_VALUE_STYLE)

        self.scan_btn.setEnabled(False)
        self.convert_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self.open_output_btn.setEnabled(False)
        self._set_conversion_busy(True)
        self._set_workflow_state("Converting", "Converting batch...")

        if in_place:
            self._log("In-place mode: converted files saved next to originals, source files will be deleted")

        self._worker = ConvertWorker(
            files=self._scan_result.files,
            output_dir=Path(dst),
            fmt=fmt,
            quality=self.quality_slider.value(),
            preserve_meta=self.meta_chk.isChecked() and not self.strip_meta_chk.isChecked(),
            preserve_structure=self.structure_chk.isChecked(),
            base_dir=Path(self.src_edit.text().strip()),
            workers=self.workers_spin.value(),
            in_place=in_place,
            skip_existing=self.skip_existing_chk.isChecked(),
            resize_mode=resize_mode,
            resize_value=self.resize_spin.value(),
            prefix=self.prefix_edit.text(),
            suffix=self.suffix_edit.text(),
            lossless_webp=self.lossless_webp_chk.isChecked(),
            progressive_jpeg=self.progressive_jpeg_chk.isChecked(),
            chroma_subsampling=self.chroma_chk.isChecked(),
            convert_to_srgb=self.srgb_chk.isChecked(),
            tiff_compression=["none", "lzw", "deflate"][self.tiff_comp_combo.currentIndex()],
            png_compress_level=self.png_level_spin.value(),
            name_template=self.template_edit.text() or None,
            only_if_smaller_pct=float(self.only_if_smaller_spin.value()) if self.only_if_smaller_chk.isChecked() else None,
            dpi=(self.dpi_spin.value(), self.dpi_spin.value()) if self.dpi_spin.value() > 0 else None,
            icc_override=self.icc_edit.text() or None,
            emit_xmp_sidecar=self.xmp_sidecar_chk.isChecked(),
            recompress_lossless=self.recompress_chk.isChecked(),
            quality_mode=("target-kb", float(self.target_kb_spin.value())) if self.target_kb_spin.value() > 0 else None,
            watermark=self.watermark_edit.text() or None,
            canvas=_parse_canvas(self.canvas_edit.text()),
            canvas_bg=self.canvas_bg_edit.text() or "transparent",
            tone_map=["none", "reinhard", "hable", "clip"][self.tone_map_combo.currentIndex()],
            avif_speed=self.avif_speed_spin.value(),
            avif_codec=["auto", "aom", "rav1e", "svt"][self.avif_codec_combo.currentIndex()],
            png_lossy=self.png_lossy_chk.isChecked(),
            frames=["first", "all", "animate"][self.frames_combo.currentIndex()],
        )
        self._worker.log.connect(self._log)
        self._worker.progress.connect(self._on_progress)
        self._worker.current_file.connect(self._on_current_file)
        self._worker.file_done.connect(self._on_file_done)
        self._worker.finished_all.connect(self._on_convert_done)
        self._worker.start()

    def _on_progress(self, current, total):
        self.progress_bar.setValue(current)
        self._update_title("converting", current=current, total=total)
        elapsed = time.perf_counter() - self._convert_start_time
        if current > 0 and elapsed > 0 and current < total:
            speed = current / elapsed
            rate = elapsed / current
            remaining = (total - current) * rate
            self.status_bar.showMessage(
                f"Converting... {current}/{total} -- "
                f"{speed:.1f} files/sec -- "
                f"Elapsed: {_fmt_eta(elapsed)} -- "
                f"ETA: {_fmt_eta(remaining)}"
            )
        else:
            self.status_bar.showMessage(f"Converting... {current}/{total}")

    def _on_current_file(self, filename: str):
        display = filename if len(filename) <= 72 else f"{filename[:34]}...{filename[-34:]}"
        self.progress_bar.setFormat(f"%p% - {display}")

    def _on_file_done(self, result: ConvertResult):
        self._results.append(result)
        if result.success and result.dst:
            self._last_ok_dst = result.dst

        # Disk-full auto-stop: detect errno 28 / "No space left" and halt batch.
        if (not result.success and not result.skipped and result.error
                and ("errno 28" in result.error.lower()
                     or "no space left" in result.error.lower()
                     or "not enough space" in result.error.lower())):
            self._log(f"[ERROR] Disk full detected on {result.src.name} — stopping batch.")
            if self._worker:
                self._worker.stop()

        ok = sum(1 for r in self._results if r.success)
        skipped = sum(1 for r in self._results if r.skipped)
        fail = sum(1 for r in self._results if not r.success and not r.skipped)
        saved = sum(r.size_before - r.size_after for r in self._results if r.success)

        self.stat_done._val.setText(str(ok))
        self.stat_skipped._val.setText(str(skipped))
        if skipped:
            self.stat_skipped._val.setStyleSheet(f"color: {CAT['yellow']}; font-size: 22px; font-weight: 700;")
        self.stat_failed._val.setText(str(fail))
        if fail:
            self.stat_failed._val.setStyleSheet(f"color: {CAT['red']}; font-size: 22px; font-weight: 700;")
        self.stat_saved._val.setText(_fmt_size(abs(saved)))
        if saved >= 0:
            self.stat_saved._val.setStyleSheet(STAT_VALUE_STYLE)
        else:
            self.stat_saved._val.setStyleSheet(f"color: {CAT['peach']}; font-size: 22px; font-weight: 700;")

    def _play_completion_sound(self):
        """Play a platform-specific notification sound."""
        try:
            system = platform.system()
            if system == "Windows":
                import winsound
                winsound.MessageBeep(winsound.MB_ICONASTERISK)
            elif system == "Darwin":
                subprocess.Popen(["afplay", "/System/Library/Sounds/Glass.aiff"])
            else:
                subprocess.Popen(["paplay", "/usr/share/sounds/freedesktop/stereo/complete.oga"])
        except Exception:
            pass

    def _on_convert_done(self, results):
        self.scan_btn.setEnabled(True)
        self.convert_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        self.open_output_btn.setEnabled(True)
        self._set_conversion_busy(False)
        self.progress_bar.setFormat("%p%")

        ok = sum(1 for r in results if r.success)
        skipped = sum(1 for r in results if r.skipped)
        fail = sum(1 for r in results if not r.success and not r.skipped)
        deleted = sum(1 for r in results if r.src_deleted)
        total_time = sum(r.elapsed for r in results)
        wall_time = time.perf_counter() - self._convert_start_time
        saved = sum(r.size_before - r.size_after for r in results if r.success)

        parts = [f"{ok} converted"]
        if skipped:
            parts.append(f"{skipped} skipped")
        if fail:
            parts.append(f"{fail} failed")
        if deleted:
            parts.append(f"{deleted} sources deleted")
        summary = (
            f"Done! {', '.join(parts)}. "
            f"Space {'saved' if saved >= 0 else 'added'}: {_fmt_size(abs(saved))}. "
            f"Wall time: {_fmt_eta(wall_time)} ({total_time:.1f}s processing)"
        )
        self._log(f"\n{'='*60}")
        self._log(summary)
        self._log(f"{'='*60}")
        if fail and ok == 0:
            self._set_workflow_state("Failed", summary)
        elif fail:
            self._set_workflow_state("Review log", summary)
        else:
            self._set_workflow_state("Complete", summary)

        # Screen reader announcement (Qt 6.8+)
        try:
            from PyQt6.QtGui import QAccessibleAnnouncementEvent
            from PyQt6.QtWidgets import QAccessible
            evt = QAccessibleAnnouncementEvent(self, summary)
            QAccessible.updateAccessibility(evt)
        except (ImportError, AttributeError):
            pass

        # All-files-failed escalation — warn user prominently when nothing converted.
        total_attempted = ok + fail + skipped
        if ok == 0 and fail > 0 and total_attempted > 0:
            tray_icon = QSystemTrayIcon.MessageIcon.Critical
            QMessageBox.warning(
                self, "Conversion Failed",
                f"All {fail} file(s) failed to convert.\n\n"
                f"Check the log panel for error details.",
            )
        else:
            tray_icon = QSystemTrayIcon.MessageIcon.Information

        # System tray notification
        if QSystemTrayIcon.isSystemTrayAvailable():
            self._tray.show()
            self._tray.showMessage(
                "ImgConverter — Conversion Complete",
                f"{ok} converted, {fail} failed" + (f", {skipped} skipped" if skipped else ""),
                tray_icon,
                5000,
            )
            QTimer.singleShot(6000, self._tray.hide)

        self._update_title("done", ok=ok, fail=fail)
        self._play_completion_sound()
        if self.auto_open_chk.isChecked():
            self._open_output()
        self._save_state()

    def _stop(self):
        if self._worker:
            self._worker.stop()
            self.stop_btn.setEnabled(False)
            self._set_workflow_state("Stopping", "Stopping after the current file finishes...")

    def _open_output(self):
        if self.inplace_chk.isChecked():
            path = self.src_edit.text().strip()
        else:
            path = self.dst_edit.text().strip()
        if path and Path(path).exists():
            _open_path(path)

    # ── State persistence ──
    def _save_state(self):
        self.settings.setValue("src", self.src_edit.text())
        self.settings.setValue("dst", self.dst_edit.text())
        self.settings.setValue("fmt", self.fmt_combo.currentIndex())
        self.settings.setValue("quality", self.quality_slider.value())
        self.settings.setValue("workers", self.workers_spin.value())
        self.settings.setValue("recursive", self.recursive_chk.isChecked())
        self.settings.setValue("structure", self.structure_chk.isChecked())
        self.settings.setValue("metadata", self.meta_chk.isChecked())
        self.settings.setValue("inplace", self.inplace_chk.isChecked())
        self.settings.setValue("skip_existing", self.skip_existing_chk.isChecked())
        self.settings.setValue("progressive_jpeg", self.progressive_jpeg_chk.isChecked())
        self.settings.setValue("lossless_webp", self.lossless_webp_chk.isChecked())
        self.settings.setValue("resize_enabled", self.resize_chk.isChecked())
        self.settings.setValue("resize_mode", self.resize_combo.currentIndex())
        self.settings.setValue("resize_value", self.resize_spin.value())
        self.settings.setValue("prefix", self.prefix_edit.text())
        self.settings.setValue("suffix", self.suffix_edit.text())
        self.settings.setValue("chroma_subsampling", self.chroma_chk.isChecked())
        self.settings.setValue("convert_to_srgb", self.srgb_chk.isChecked())
        self.settings.setValue("tiff_compression", self.tiff_comp_combo.currentIndex())
        self.settings.setValue("png_compress_level", self.png_level_spin.value())
        self.settings.setValue("strip_metadata", self.strip_meta_chk.isChecked())
        self.settings.setValue("auto_open_output", self.auto_open_chk.isChecked())
        self.settings.setValue("template", self.template_edit.text())
        self.settings.setValue("dpi", self.dpi_spin.value())
        self.settings.setValue("avif_speed", self.avif_speed_spin.value())
        self.settings.setValue("avif_codec", self.avif_codec_combo.currentIndex())
        self.settings.setValue("frames_mode", self.frames_combo.currentIndex())
        self.settings.setValue("tone_map", self.tone_map_combo.currentIndex())
        self.settings.setValue("icc_override", self.icc_edit.text())
        self.settings.setValue("watermark", self.watermark_edit.text())
        self.settings.setValue("canvas", self.canvas_edit.text())
        self.settings.setValue("canvas_bg", self.canvas_bg_edit.text())
        self.settings.setValue("xmp_sidecar", self.xmp_sidecar_chk.isChecked())
        self.settings.setValue("recompress", self.recompress_chk.isChecked())
        self.settings.setValue("only_if_smaller_enabled", self.only_if_smaller_chk.isChecked())
        self.settings.setValue("only_if_smaller_pct", self.only_if_smaller_spin.value())
        self.settings.setValue("target_kb", self.target_kb_spin.value())
        self.settings.setValue("png_lossy", self.png_lossy_chk.isChecked())
        self.settings.setValue("adv_expanded", self.adv_toggle.isChecked())
        self.settings.setValue("geometry", self.saveGeometry())
        # Format filter states
        filter_state = {name: chk.isChecked() for name, chk in self._format_filters.items()}
        self.settings.setValue("format_filters", json.dumps(filter_state))

    def _maybe_migrate_settings(self):
        """Bump on-disk QSettings shape to current SETTINGS_SCHEMA."""
        try:
            stored = int(self.settings.value("settings_version", 0))
        except (TypeError, ValueError):
            stored = 0
        if stored == SETTINGS_SCHEMA:
            return

        # Migration v0/v1 -> v2: format index 6 used to be disabled in some
        # builds when pillow-jxl wasn't present; coerce out-of-range stored
        # values to 0 (auto) so _restore_state doesn't blow up.
        if stored < 2:
            try:
                fmt_v = self.settings.value("fmt")
                if fmt_v is not None:
                    idx = int(fmt_v)
                    if not (0 <= idx <= 6):
                        self.settings.setValue("fmt", 0)
            except (TypeError, ValueError):
                self.settings.setValue("fmt", 0)

        self.settings.setValue("settings_version", SETTINGS_SCHEMA)
        _diag_log(f"QSettings migrated from v{stored} to v{SETTINGS_SCHEMA}")

    @staticmethod
    def _safe_int(v, default=0):
        if v is None:
            return None
        try:
            return int(v)
        except (TypeError, ValueError):
            return default

    def _restore_state(self):
        self._maybe_migrate_settings()
        if v := self.settings.value("src"):
            self.src_edit.setText(v)
        if v := self.settings.value("dst"):
            self.dst_edit.setText(v)
        idx = self._safe_int(self.settings.value("fmt"))
        if idx is not None and 0 <= idx < self.fmt_combo.count():
            self.fmt_combo.setCurrentIndex(idx)
        if (n := self._safe_int(self.settings.value("quality"))) is not None:
            self.quality_slider.setValue(n)
        if (n := self._safe_int(self.settings.value("workers"))) is not None:
            self.workers_spin.setValue(n)
        if (v := self.settings.value("recursive")) is not None:
            self.recursive_chk.setChecked(v == "true" or v is True)
        if (v := self.settings.value("structure")) is not None:
            self.structure_chk.setChecked(v == "true" or v is True)
        if (v := self.settings.value("metadata")) is not None:
            self.meta_chk.setChecked(v == "true" or v is True)
        if (v := self.settings.value("inplace")) is not None:
            self.inplace_chk.setChecked(v == "true" or v is True)
        if (v := self.settings.value("skip_existing")) is not None:
            self.skip_existing_chk.setChecked(v == "true" or v is True)
        if (v := self.settings.value("progressive_jpeg")) is not None:
            self.progressive_jpeg_chk.setChecked(v == "true" or v is True)
        if (v := self.settings.value("lossless_webp")) is not None:
            self.lossless_webp_chk.setChecked(v == "true" or v is True)
        if (v := self.settings.value("resize_enabled")) is not None:
            self.resize_chk.setChecked(v == "true" or v is True)
        n = self._safe_int(self.settings.value("resize_mode"))
        if n is not None:
            self.resize_combo.blockSignals(True)
            self.resize_combo.setCurrentIndex(n)
            self.resize_combo.blockSignals(False)
            if n == 0:
                self.resize_spin.setRange(100, 10000)
                self.resize_spin.setSuffix(" px")
            else:
                self.resize_spin.setRange(1, 500)
                self.resize_spin.setSuffix(" %")
        if (n := self._safe_int(self.settings.value("resize_value"))) is not None:
            self.resize_spin.setValue(n)
        if (v := self.settings.value("prefix")) is not None:
            self.prefix_edit.setText(v)
        if (v := self.settings.value("suffix")) is not None:
            self.suffix_edit.setText(v)
        if (v := self.settings.value("chroma_subsampling")) is not None:
            self.chroma_chk.setChecked(v == "true" or v is True)
        if (v := self.settings.value("convert_to_srgb")) is not None:
            self.srgb_chk.setChecked(v == "true" or v is True)
        if (n := self._safe_int(self.settings.value("tiff_compression"))) is not None:
            self.tiff_comp_combo.setCurrentIndex(n)
        if (n := self._safe_int(self.settings.value("png_compress_level"))) is not None:
            self.png_level_spin.setValue(n)
        if (v := self.settings.value("strip_metadata")) is not None:
            self.strip_meta_chk.setChecked(v == "true" or v is True)
        if (v := self.settings.value("auto_open_output")) is not None:
            self.auto_open_chk.setChecked(v == "true" or v is True)
        if v := self.settings.value("template"):
            self.template_edit.setText(v)
        if (n := self._safe_int(self.settings.value("dpi"))) is not None:
            self.dpi_spin.setValue(n)
        if (n := self._safe_int(self.settings.value("avif_speed"))) is not None:
            self.avif_speed_spin.setValue(n)
        if (n := self._safe_int(self.settings.value("avif_codec"))) is not None:
            self.avif_codec_combo.setCurrentIndex(n)
        if (n := self._safe_int(self.settings.value("frames_mode"))) is not None:
            self.frames_combo.setCurrentIndex(n)
        if (n := self._safe_int(self.settings.value("tone_map"))) is not None:
            self.tone_map_combo.setCurrentIndex(n)
        if v := self.settings.value("icc_override"):
            self.icc_edit.setText(v)
        if v := self.settings.value("watermark"):
            self.watermark_edit.setText(v)
        if v := self.settings.value("canvas"):
            self.canvas_edit.setText(v)
        if v := self.settings.value("canvas_bg"):
            self.canvas_bg_edit.setText(v)
        if (v := self.settings.value("xmp_sidecar")) is not None:
            self.xmp_sidecar_chk.setChecked(v == "true" or v is True)
        if (v := self.settings.value("recompress")) is not None:
            self.recompress_chk.setChecked(v == "true" or v is True)
        if (v := self.settings.value("only_if_smaller_enabled")) is not None:
            self.only_if_smaller_chk.setChecked(v == "true" or v is True)
        if (n := self._safe_int(self.settings.value("only_if_smaller_pct"))) is not None:
            self.only_if_smaller_spin.setValue(n)
        if (n := self._safe_int(self.settings.value("target_kb"))) is not None:
            self.target_kb_spin.setValue(n)
        if (v := self.settings.value("png_lossy")) is not None:
            self.png_lossy_chk.setChecked(v == "true" or v is True)
        if (v := self.settings.value("adv_expanded")) is not None:
            expanded = v == "true" or v is True
            self.adv_toggle.setChecked(expanded)
            self._toggle_advanced(expanded)
        if v := self.settings.value("geometry"):
            self.restoreGeometry(v)
        # Restore format filter states
        if v := self.settings.value("format_filters"):
            try:
                saved = json.loads(v)
                for name, checked in saved.items():
                    if name in self._format_filters and self._format_filters[name].isEnabled():
                        self._format_filters[name].setChecked(checked)
            except (json.JSONDecodeError, TypeError):
                pass

    def closeEvent(self, event):
        self._save_state()
        if self._worker and self._worker.isRunning():
            self._worker.stop()
            self._worker.wait(3000)
        self._tray.hide()
        super().closeEvent(event)


# ── CLI Mode ──────────────────────────────────────────────────────────────────

def _build_parser() -> argparse.ArgumentParser:
    """Build argument parser for CLI mode."""
    p = argparse.ArgumentParser(
        prog="imgconverter",
        description=f"ImgConverter v{APP_VERSION} - High-performance image batch converter",
    )
    p.add_argument("--version", action="version", version=f"ImgConverter v{APP_VERSION}")
    p.add_argument("--install-deps", action="store_true",
                   help="Install/upgrade required + optional Python dependencies, then exit")
    p.add_argument("-i", "--input", type=str, help="Source file or directory to scan")
    p.add_argument("--files", nargs="+", metavar="PATH",
                   help="One or more selected image files/directories to convert (used by shell integration)")
    p.add_argument("-o", "--output", type=str, help="Output directory (default: <input>/converted)")
    p.add_argument("-f", "--format", type=str, default="auto",
                   choices=["auto", "jpeg", "png", "webp", "avif", "tiff", "jxl"],
                   help="Output format (default: auto)")
    p.add_argument("-q", "--quality", type=int, default=92, help="JPEG/WebP quality 50-100 (default: 92)")
    p.add_argument("-w", "--workers", type=int, default=min(os.cpu_count() or 4, 8),
                   help="Parallel worker count (default: min(cpu_count, 8))")
    p.add_argument("--in-place", action="store_true", help="Convert next to originals, delete source")
    p.add_argument("--recursive", action="store_true", default=True, dest="recursive",
                   help="Scan subdirectories (default)")
    p.add_argument("--no-recursive", action="store_false", dest="recursive",
                   help="Only scan top-level directory")
    p.add_argument("--dry-run", action="store_true", help="List files that would be converted, then exit")
    p.add_argument("--strip-metadata", action="store_true", help="Remove all metadata from output files")
    p.add_argument("--resize", type=str, default=None, metavar="MODE:VALUE",
                   help="Resize images, e.g. max_dim:1920 or scale:50")
    p.add_argument("--skip-existing", action="store_true",
                   help="Skip files where output already exists")
    p.add_argument("--progressive", action="store_true",
                   help="Save JPEGs as progressive")
    p.add_argument("--chroma-420", action="store_true",
                   help="Use 4:2:0 chroma subsampling for JPEG")
    p.add_argument("--lossless", action="store_true",
                   help="Save WebP in lossless mode")
    p.add_argument("--srgb", action="store_true",
                   help="Convert embedded ICC profiles to sRGB")
    p.add_argument("--prefix", type=str, default="",
                   help="Prepend text to output filenames")
    p.add_argument("--suffix", type=str, default="",
                   help="Append text to output filenames")
    p.add_argument("--tiff-compression", type=str, default="none",
                   choices=["none", "lzw", "deflate"],
                   help="TIFF compression method (default: none)")
    p.add_argument("--png-level", type=int, default=6,
                   help="PNG compression level 1-9 (default: 6)")
    p.add_argument("--png-lossy", action="store_true",
                   help="Run pngquant on PNG output for 50-80%% size reduction via "
                        "lossy quantization + dithering. Requires pngquant on PATH.")
    p.add_argument("--no-structure", action="store_true",
                   help="Flatten output (no subdirectory mirroring)")
    p.add_argument("--exclude", action="append", default=[], metavar="PATTERN",
                   help="Glob pattern to exclude (repeatable). Example: --exclude '*.thumb.*' --exclude 'cache/**'")
    p.add_argument("--no-exiftool", action="store_true",
                   help="Skip ExifTool tag-copy pass even when exiftool is on PATH (uses Pillow EXIF/ICC/XMP only)")
    p.add_argument("--template", type=str, default=None, metavar="STR",
                   help="Output filename template. Tokens: {stem} {ext} {fmt} "
                        "{src_dir} {rel_dir} {width} {height} {date[:FMT]} "
                        "{seq[:###]}. Example: --template '{rel_dir}/{stem}_{width}x{height}'. "
                        "Overrides --prefix/--suffix.")
    p.add_argument("--report", type=str, default=None, metavar="PATH",
                   help="Write structured per-file JSON report to PATH after conversion. "
                        "Top-level object: {summary: {...}, files: [...]}")
    p.add_argument("--preset", type=str, default=None, metavar="NAME",
                   help="Load preset from ~/.imgconverter/presets/NAME.json before applying other flags")
    p.add_argument("--list-presets", action="store_true",
                   help="List available presets (built-ins + ~/.imgconverter/presets/*.json) and exit")
    p.add_argument("--list-plugins", action="store_true",
                   help="List plugin files with trust status without loading them, then exit")
    p.add_argument("--trust-plugin", type=str, default=None, metavar="PATH",
                   help="Trust a plugin file in ~/.imgconverter/plugins/ by recording its SHA-256, then exit")
    p.add_argument("--untrust-plugin", type=str, default=None, metavar="NAME",
                   help="Remove a plugin from the local trust manifest, then exit")
    p.add_argument("--only-if-smaller", type=float, default=None, metavar="PCT",
                   help="Discard output and keep original when output is not at least PCT%% smaller "
                        "than source (e.g. --only-if-smaller 20 means keep only when output <= 80%% of input)")
    p.add_argument("--dpi", type=int, default=None, metavar="DPI",
                   help="Set output DPI tag for JPEG / PNG / TIFF (e.g. --dpi 300 for print)")
    p.add_argument("--icc", type=str, default=None, metavar="PROFILE",
                   help="Embed ICC profile in output. Built-in: 'sRGB'. Or path to .icc/.icm file. "
                        "Mutually exclusive with --srgb (--srgb wins).")
    p.add_argument("--xmp-sidecar", action="store_true",
                   help="Emit a .xmp sidecar alongside output (Adobe Bridge / darktable convention)")
    p.add_argument("--recompress", action="store_true",
                   help="For JPEG->JPEG: run jpegoptim / jpegtran for pixel-lossless size reduction "
                        "instead of decode-re-encode. Requires jpegoptim or jpegtran on PATH.")
    p.add_argument("--target-kb", type=float, default=None, metavar="N",
                   help="Binary-search quality to hit a target output size (kilobytes). "
                        "Up to 8 iterations; result.warnings logs the picked quality.")
    p.add_argument("--target-psnr", type=float, default=None, metavar="DB",
                   help="Binary-search quality to hit a minimum PSNR (dB) vs source. "
                        "40+ dB is excellent; 30 dB is visibly degraded.")
    p.add_argument("--watermark", type=str, default=None, metavar="SPEC",
                   help="Watermark text or path to PNG. Spec: 'TEXT|position|opacity' "
                        "(positions: top-left top top-right left center right "
                        "bottom-left bottom bottom-right; opacity 0.0-1.0). "
                        "Example: --watermark '\u00a9 2026|bottom-right|0.7' or "
                        "--watermark 'logo.png|top-left|0.4'")
    p.add_argument("--canvas", type=str, default=None, metavar="WxH",
                   help="Pad output to a canvas of WxH pixels with background fill, "
                        "preserving aspect ratio. Example: --canvas 1920x1080")
    p.add_argument("--canvas-bg", type=str, default="transparent", metavar="COLOR",
                   help="Canvas background: 'transparent' (default), '#RRGGBB', "
                        "'#RRGGBBAA', or named color")
    p.add_argument("--avif-speed", type=int, default=6, metavar="N",
                   help="AVIF encoding speed 0-10 (default: 6). Lower = smaller file, "
                        "slower encode. 0 = best compression, 10 = fastest.")
    p.add_argument("--avif-codec", type=str, default="auto",
                   choices=["auto", "aom", "rav1e", "svt"],
                   help="AVIF encoder codec. 'auto' (default) lets Pillow choose, "
                        "'svt' is fastest, 'aom' is smallest, 'rav1e' is in between.")
    p.add_argument("--max-file-size", type=str, default=None, metavar="SIZE",
                   help="Skip files larger than SIZE. Accepts: '500MB', '2GB', '100KB'. "
                        "Prevents OOM on multi-gigapixel images.")
    p.add_argument("--register-shell", action="store_true",
                   help="Install OS shell integration: Windows Explorer right-click menu "
                        "or Linux .desktop file. macOS prints Automator recipe. Then exit.")
    p.add_argument("--unregister-shell", action="store_true",
                   help="Remove the shell integration installed by --register-shell")
    p.add_argument("--use-cache", action="store_true",
                   help="Use ~/.cache/imgconverter/seen.sqlite to skip files whose (source-hash, "
                        "preset-hash) was successfully converted before. Use when re-running "
                        "the same batch.")
    p.add_argument("--clear-cache", action="store_true",
                   help="Delete ~/.cache/imgconverter/seen.sqlite and exit")
    p.add_argument("--resume", action="store_true",
                   help="Resume a previously interrupted batch from ~/.cache/imgconverter/queue.json")
    p.add_argument("--frames", type=str, default="first", choices=["first", "all", "animate"],
                   help="Multi-frame source handling: 'first' (default - first frame only), "
                        "'all' (export every frame as {stem}.NNN.{ext}), "
                        "'animate' (preserve as animated WebP/PNG/GIF; falls back to 'all' if "
                        "target format can't carry animation)")
    p.add_argument("--watch", action="store_true",
                   help="Watch --input directory and convert new files as they arrive. "
                        "Polling-based; --watch-interval controls cadence. Ctrl-C to stop.")
    p.add_argument("--watch-interval", type=float, default=2.0, metavar="SEC",
                   help="Watch-mode poll interval in seconds (default 2.0)")
    p.add_argument("--tone-map", type=str, default="none",
                   choices=["none", "reinhard", "hable", "clip"],
                   help="HDR tone-mapping curve when source is PQ/HLG/wide-gamut. "
                        "'none' (default - preserve), 'reinhard' (gentle), "
                        "'hable' (Uncharted 2 filmic), 'clip' (hard clamp).")
    p.add_argument("--use-processes", action="store_true",
                   help="Use a ProcessPoolExecutor instead of ThreadPoolExecutor. "
                        "Bypasses the GIL on Python interpreters that still have it; "
                        "no benefit on free-threaded builds. Cost: per-image fork overhead.")
    p.add_argument("--sidecar-history", action="store_true",
                   help="Write {output}.imgconverter.json next to each converted file with the "
                        "exact preset that produced it. Enables reproducible re-runs.")
    p.add_argument("--backend", type=str, default="pillow",
                   choices=["pillow", "vips"],
                   help="Encoder/decoder backend. 'pillow' (default) or 'vips' (requires "
                        "pyvips; tile-streams for huge images).")
    p.add_argument("--verify-quality", action="store_true",
                   help="After each conversion, run butteraugli (preferred) or "
                        "ffmpeg-quality-metrics if available, logging PSNR/SSIM "
                        "or butteraugli score in result.warnings.")
    return p


def _gil_status() -> str:
    """Return 'no-gil', 'gil', or 'unknown' based on the running interpreter."""
    is_gil = getattr(sys, "_is_gil_enabled", None)
    if callable(is_gil):
        return "no-gil" if not is_gil() else "gil"
    return "unknown"


def _log_dep_versions_cli():
    """Print dependency versions to stdout for CLI mode."""
    from PIL import __version__ as pil_ver
    heif_ver = getattr(pillow_heif, "__version__", "unknown")
    print(f"Pillow {pil_ver}, pillow-heif {heif_ver}  [python {sys.version_info[0]}.{sys.version_info[1]} {_gil_status()}]")
    opt_vers = []
    if HAS_RAWPY:
        opt_vers.append(f"rawpy {getattr(rawpy, '__version__', '?')}")
    if HAS_JXL:
        opt_vers.append(f"pillow-jxl {getattr(pillow_jxl, '__version__', '?')}")
    if HAS_QOI:
        opt_vers.append(f"qoi {getattr(qoi_lib, '__version__', '?')}")
    if HAS_EXIFTOOL:
        opt_vers.append(f"exiftool ({EXIFTOOL_PATH})")
    if opt_vers:
        print(f"Optional: {', '.join(opt_vers)}")


_PRESET_ARG_KEYS = (
    "format", "quality", "progressive", "chroma_420", "lossless", "srgb",
    "tiff_compression", "png_level", "prefix", "suffix", "template",
    "strip_metadata", "in_place", "skip_existing", "resize",
    "no_structure", "workers", "no_exiftool",
)


def _apply_preset_to_args(args, preset: dict):
    """Overlay a preset dict onto argparse Namespace; CLI args still win for non-defaults."""
    # Map preset GUI-shaped keys to CLI flag keys when needed.
    fmt_map = {0: "auto", 1: "jpeg", 2: "png", 3: "webp", 4: "avif", 5: "tiff", 6: "jxl"}
    for k, v in preset.items():
        if k == "fmt" and isinstance(v, int):
            args.format = fmt_map.get(v, args.format)
        elif k == "progressive_jpeg":
            args.progressive = v
        elif k == "chroma_subsampling":
            args.chroma_420 = v
        elif k == "lossless_webp":
            args.lossless = v
        elif k == "convert_to_srgb":
            args.srgb = v
        elif k == "png_compress_level":
            args.png_level = v
        elif k == "resize_enabled" and not v:
            args.resize = None
        elif k == "resize_mode":
            mode_map = {0: "max_dim", 1: "scale"}
            mode = mode_map.get(v, v)
            if args.resize is None and "resize_value" in preset:
                args.resize = f"{mode}:{preset['resize_value']}"
        elif k == "tiff_compression" and isinstance(v, int):
            args.tiff_compression = ("none", "lzw", "deflate")[v] if 0 <= v < 3 else "none"
        elif k in _PRESET_ARG_KEYS and hasattr(args, k):
            setattr(args, k, v)


HASH_CACHE_PATH = USER_CACHE_DIR / "seen.sqlite"


def _open_hash_cache():
    """Open (and lazily create) the conversion cache SQLite db. Returns None on failure."""
    try:
        import sqlite3
        USER_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(HASH_CACHE_PATH), timeout=2.0,
                                isolation_level=None, check_same_thread=False)
        conn.execute(
            "CREATE TABLE IF NOT EXISTS seen ("
            " src_hash TEXT, preset_hash TEXT, dst_hash TEXT, dst_size INTEGER, "
            " ts INTEGER, PRIMARY KEY (src_hash, preset_hash))"
        )
        return conn
    except Exception as e:
        _diag_log(f"hash cache open failed: {e}", level="WARNING")
        return None


def _file_sha256(path: Path) -> str:
    """SHA-256 of file contents, streamed."""
    import hashlib
    h = hashlib.sha256()
    with path.open("rb") as fp:
        for chunk in iter(lambda: fp.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _preset_hash(*parts) -> str:
    """Stable hash of the conversion preset (format, quality, resize, etc.)."""
    import hashlib
    payload = json.dumps([str(p) for p in parts], sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


QUEUE_STATE_PATH = USER_CACHE_DIR / "queue.json"


def _save_queue_state(input_dir: Path, output_dir: Path, args, pending: list[Path],
                       done: list[str], failed: list[str]):
    """Persist the in-flight queue so a Ctrl-C / power-cycle can resume."""
    try:
        USER_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        state = {
            "ts": int(time.time()),
            "version": APP_VERSION,
            "input": str(input_dir),
            "output": str(output_dir),
            "format": getattr(args, "format", None),
            "quality": getattr(args, "quality", None),
            "pending": [str(p) for p in pending],
            "done": done,
            "failed": failed,
        }
        QUEUE_STATE_PATH.write_text(json.dumps(state, indent=2))
    except Exception as e:
        _diag_log(f"queue save failed: {e}", level="WARNING")


def _load_queue_state() -> dict | None:
    if not QUEUE_STATE_PATH.is_file():
        return None
    try:
        return json.loads(QUEUE_STATE_PATH.read_text())
    except Exception as e:
        _diag_log(f"queue load failed: {e}", level="WARNING")
        return None


def _clear_queue_state():
    try:
        QUEUE_STATE_PATH.unlink(missing_ok=True)
    except Exception:
        pass


def _watch_directory(args, input_dir: Path, output_dir: Path,
                     resize_mode: str = "none", resize_value: int = 1920) -> int:
    """Polling-based watch mode. Avoids hard dep on watchdog.

    Every poll interval, rescan the directory; convert any file we haven't
    seen yet. Debounces partial-write races by requiring the file size to
    be stable for one full poll interval before processing.
    """
    interval = max(1.0, float(getattr(args, "watch_interval", 2.0)))
    print(f"[watch] watching {input_dir} every {interval:.1f}s — Ctrl-C to stop")
    seen_sizes: dict[Path, int] = {}
    converted: set[Path] = set()
    supported = get_supported_extensions()
    try:
        def _safe_walk(d: Path, visited: set[str]):
            try:
                real = str(d.resolve(strict=False))
            except OSError:
                return
            if real in visited:
                return
            visited.add(real)
            try:
                entries = list(d.iterdir())
            except (PermissionError, OSError):
                return
            for p in entries:
                try:
                    if p.is_dir():
                        if args.recursive:
                            yield from _safe_walk(p, visited)
                        continue
                    if p.is_file() and p.suffix.lower() in supported:
                        yield p
                except OSError:
                    continue

        while True:
            current = []
            try:
                visited: set[str] = set()
                for p in _safe_walk(input_dir, visited):
                    if p in converted:
                        continue
                    try:
                        size = p.stat().st_size
                    except OSError:
                        continue
                    if seen_sizes.get(p) == size:
                        current.append(p)
                    else:
                        seen_sizes[p] = size
            except OSError:
                pass

            for f in current:
                seq = len(converted) + 1
                try:
                    r = convert_file(
                        f, output_dir,
                        fmt=args.format,
                        jpeg_quality=args.quality,
                        preserve_metadata=not args.strip_metadata,
                        preserve_structure=not args.no_structure,
                        base_dir=input_dir,
                        in_place=args.in_place,
                        skip_existing=args.skip_existing,
                        resize_mode=resize_mode,
                        resize_value=resize_value,
                        prefix=args.prefix,
                        suffix=args.suffix,
                        lossless_webp=args.lossless,
                        progressive_jpeg=args.progressive,
                        chroma_subsampling=args.chroma_420,
                        convert_to_srgb=args.srgb,
                        tiff_compression=args.tiff_compression,
                        png_compress_level=args.png_level,
                        use_exiftool=not args.no_exiftool,
                        name_template=getattr(args, "template", None),
                        seq=seq,
                        only_if_smaller_pct=getattr(args, "only_if_smaller", None),
                        dpi=(args.dpi, args.dpi) if getattr(args, "dpi", None) else None,
                        icc_override=getattr(args, "icc", None),
                        emit_xmp_sidecar=getattr(args, "xmp_sidecar", False),
                        recompress_lossless=getattr(args, "recompress", False),
                        quality_mode=_build_quality_mode(args),
                        watermark=getattr(args, "watermark", None),
                        canvas=_parse_canvas(getattr(args, "canvas", None)),
                        canvas_bg=getattr(args, "canvas_bg", "transparent"),
                        tone_map=getattr(args, "tone_map", "none"),
                        avif_speed=getattr(args, "avif_speed", 6),
                        avif_codec=getattr(args, "avif_codec", "auto"),
                        png_lossy=getattr(args, "png_lossy", False),
                    )
                    if r.success:
                        print(f"[watch] OK  {f.name} -> {r.dst.name}")
                    elif r.skipped:
                        print(f"[watch] SKIP {f.name}")
                    else:
                        print(f"[watch] FAIL {f.name}: {r.error}")
                    converted.add(f)
                    seen_sizes.pop(f, None)
                except Exception as e:
                    print(f"[watch] error on {f.name}: {e}")

            time.sleep(interval)
    except KeyboardInterrupt:
        print(f"\n[watch] stopped. Total processed: {len(converted)}")
    return EXIT_OK


def _install_shell_integration(uninstall: bool = False) -> int:
    """Install / uninstall OS-level shell integration.

    Windows : adds 'Convert with ImgConverter' to the Explorer right-click menu
              for image files via HKCU\\Software\\Classes\\* registry keys.
    macOS   : prints the Automator Quick Action recipe (manual; safer than
              auto-installing into ~/Library/Services).
    Linux   : writes ~/.local/share/applications/imgconverter.desktop +
              ~/.local/share/file-manager/actions/imgconverter.desktop.
    """
    system = platform.system()
    exe = sys.executable
    script = str(Path(__file__).resolve())
    file_cmd_args = f'"{exe}" "{script}" --files %*'
    dir_cmd_args = f'"{exe}" "{script}" --input "%1"'

    if system == "Windows":
        try:
            import winreg
        except ImportError:
            print("[shell-integration] winreg unavailable.", file=sys.stderr)
            return EXIT_DEP_MISSING
        root = winreg.HKEY_CURRENT_USER
        file_base = r"Software\Classes\*\shell\ImgConverter"
        file_cmd_key = file_base + r"\command"
        dir_base = r"Software\Classes\Directory\shell\ImgConverter"
        dir_cmd_key = dir_base + r"\command"
        if uninstall:
            for cmd_key, base in ((file_cmd_key, file_base), (dir_cmd_key, dir_base)):
                try:
                    winreg.DeleteKey(root, cmd_key)
                except FileNotFoundError:
                    pass
                try:
                    winreg.DeleteKey(root, base)
                except FileNotFoundError:
                    pass
            print("[shell-integration] removed.")
            return EXIT_OK
        try:
            with winreg.CreateKeyEx(root, file_base, 0, winreg.KEY_SET_VALUE) as k:
                winreg.SetValueEx(k, "", 0, winreg.REG_SZ, "Convert with ImgConverter")
                winreg.SetValueEx(k, "Icon", 0, winreg.REG_SZ, exe)
                winreg.SetValueEx(k, "MultiSelectModel", 0, winreg.REG_SZ, "Player")
            with winreg.CreateKeyEx(root, file_cmd_key, 0, winreg.KEY_SET_VALUE) as k:
                winreg.SetValueEx(k, "", 0, winreg.REG_SZ, file_cmd_args)
            with winreg.CreateKeyEx(root, dir_base, 0, winreg.KEY_SET_VALUE) as k:
                winreg.SetValueEx(k, "", 0, winreg.REG_SZ, "Convert folder with ImgConverter")
                winreg.SetValueEx(k, "Icon", 0, winreg.REG_SZ, exe)
            with winreg.CreateKeyEx(root, dir_cmd_key, 0, winreg.KEY_SET_VALUE) as k:
                winreg.SetValueEx(k, "", 0, winreg.REG_SZ, dir_cmd_args)
            print(f"[shell-integration] installed: HKCU\\{file_base} and HKCU\\{dir_base}")
            return EXIT_OK
        except OSError as e:
            print(f"[shell-integration] failed: {e}", file=sys.stderr)
            return EXIT_INPUT_ERROR

    if system == "Linux":
        share = Path.home() / ".local" / "share"
        apps_dir = share / "applications"
        actions_dir = share / "file-manager" / "actions"
        if uninstall:
            for p in (apps_dir / "imgconverter.desktop",
                       actions_dir / "imgconverter.desktop"):
                try:
                    p.unlink()
                except FileNotFoundError:
                    pass
            print("[shell-integration] removed.")
            return EXIT_OK
        apps_dir.mkdir(parents=True, exist_ok=True)
        actions_dir.mkdir(parents=True, exist_ok=True)
        desktop_content = (
            "[Desktop Entry]\n"
            "Type=Application\n"
            "Name=Convert with ImgConverter\n"
            f"Exec={exe} {script} --files %F\n"
            "MimeType=image/heic;image/heif;image/avif;image/jpeg;image/png;image/webp;image/tiff;\n"
            "Terminal=false\n"
            "Categories=Graphics;\n"
        )
        (apps_dir / "imgconverter.desktop").write_text(desktop_content)
        (actions_dir / "imgconverter.desktop").write_text(desktop_content)
        print(f"[shell-integration] installed at {apps_dir / 'imgconverter.desktop'}")
        return EXIT_OK

    if system == "Darwin":
        print(
            "[shell-integration] macOS install:\n"
            "  1. Open Automator -> New -> Quick Action\n"
            "  2. Workflow receives: image files in Finder\n"
            "  3. Add 'Run Shell Script' action:\n"
            f"      {exe} {script} --files \"$@\"\n"
            "  4. Save as 'Convert with ImgConverter'\n"
            "  Auto-install into ~/Library/Services is intentionally skipped\n"
            "  because Automator workflows are signed bundles."
        )
        return EXIT_OK

    print(f"[shell-integration] no built-in installer for {system}.", file=sys.stderr)
    return EXIT_INPUT_ERROR


def _parse_canvas(spec: str | None) -> tuple[int, int] | None:
    """Parse 'WxH' into (W, H) ints; None on bad or non-positive input."""
    if not spec or not spec.strip():
        return None
    try:
        w, h = spec.lower().split("x")
        w, h = int(w), int(h)
        if w <= 0 or h <= 0:
            return None
        return (w, h)
    except (ValueError, AttributeError):
        return None


def _build_quality_mode(args) -> tuple[str, float] | None:
    """Translate --target-kb / --target-psnr into the (mode, target) tuple."""
    if getattr(args, "target_kb", None) is not None:
        return ("target-kb", float(args.target_kb))
    if getattr(args, "target_psnr", None) is not None:
        return ("target-psnr", float(args.target_psnr))
    return None


def _collect_cli_input_refs(args) -> tuple[Path, list[Path], list[Path]]:
    """Resolve CLI file/directory selections into a common base, dirs, and files."""
    refs: list[Path] = []
    if getattr(args, "input", None):
        refs.append(Path(args.input).expanduser().resolve())
    refs.extend(Path(p).expanduser().resolve() for p in (getattr(args, "files", None) or []))
    if not refs:
        print("[ERROR] Provide --input or --files.", file=sys.stderr)
        sys.exit(EXIT_INPUT_ERROR)

    dirs: list[Path] = []
    files: list[Path] = []
    missing: list[Path] = []
    for ref in refs:
        if ref.is_dir():
            dirs.append(ref)
        elif ref.is_file():
            files.append(ref)
        else:
            missing.append(ref)
    if missing:
        print(f"[ERROR] Path not found: {missing[0]}", file=sys.stderr)
        sys.exit(EXIT_INPUT_ERROR)

    common_inputs = dirs + [f.parent for f in files]
    try:
        base_dir = Path(os.path.commonpath([str(p) for p in common_inputs])).resolve()
    except (ValueError, OSError):
        base_dir = Path.cwd().resolve()
    return base_dir, dirs, files


def _scan_cli_inputs(
    args,
    base_dir: Path,
    input_dirs: list[Path],
    input_files: list[Path],
    max_bytes: int | None,
) -> ScanResult:
    """Scan selected CLI directories and files into a single ScanResult."""
    t0 = time.perf_counter()
    supported = get_supported_extensions()
    scan = ScanResult()
    seen: set[str] = set()
    exclude_patterns = getattr(args, "exclude", None) or []

    def _add_file(path: Path):
        if path.suffix.lower() not in supported:
            return
        try:
            rel = path.relative_to(base_dir)
        except ValueError:
            rel = Path(path.name)
        if _path_matches_exclude(rel, exclude_patterns):
            return
        try:
            st = path.stat()
        except OSError:
            return
        if max_bytes is not None and st.st_size > max_bytes:
            return
        try:
            key = str(path.resolve(strict=False))
        except OSError:
            key = str(path)
        if key in seen:
            return
        seen.add(key)
        scan.files.append(path)
        scan.total_size += st.st_size

    for directory in input_dirs:
        sub = scan_directory(
            directory,
            recursive=args.recursive,
            exclude_patterns=exclude_patterns,
            max_file_size=max_bytes,
        )
        for path in sub.files:
            _add_file(path)
    for path in input_files:
        _add_file(path)

    scan.files.sort()
    scan.elapsed = time.perf_counter() - t0
    return scan


def _run_cli(args):
    """Run headless CLI conversion."""
    from concurrent.futures import ThreadPoolExecutor, as_completed
    _diag_log(f"CLI invocation: format={args.format} input={args.input}")

    # Apply preset overlay (called before other arg-driven branches).
    if getattr(args, "preset", None):
        preset = load_preset(args.preset)
        if preset is None:
            available = ", ".join(list_presets().keys())
            print(f"[ERROR] Unknown preset: {args.preset!r}. Available: {available}",
                  file=sys.stderr)
            sys.exit(EXIT_INPUT_ERROR)
        _apply_preset_to_args(args, preset)
        print(f"[preset] applied: {args.preset}")

    input_dir, input_dirs, input_files = _collect_cli_input_refs(args)

    if args.in_place:
        output_dir = input_dir
    elif args.output:
        output_dir = Path(args.output).resolve()
    else:
        output_dir = input_dir / "converted"

    print(f"ImgConverter v{APP_VERSION} (CLI mode)")
    _log_dep_versions_cli()
    if getattr(args, "backend", "pillow") == "vips":
        if not HAS_VIPS:
            print("[ERROR] --backend vips requires pyvips. Run: pip install pyvips",
                  file=sys.stderr)
            sys.exit(EXIT_DEP_MISSING)
        print("[backend] WARNING: --backend vips is experimental. Metadata, resize,\n"
              "  watermark, canvas, tone-map, ICC override, and ExifTool passes are\n"
              "  not yet implemented — output will be quality-only conversion.",
              file=sys.stderr)
    if len(input_dirs) == 1 and not input_files:
        print(f"Input:  {input_dirs[0]}")
    else:
        print(f"Input:  {len(input_dirs) + len(input_files)} selected path(s)")
        print(f"Base:   {input_dir}")
    print(f"Output: {output_dir}")
    print(f"Format: {args.format}  Quality: {args.quality}  Workers: {args.workers}")
    flags = []
    if args.skip_existing: flags.append("skip-existing")
    if args.progressive: flags.append("progressive")
    if args.chroma_420: flags.append("chroma-4:2:0")
    if args.lossless: flags.append("lossless")
    if args.srgb: flags.append("sRGB")
    if args.no_structure: flags.append("flatten")
    if args.tiff_compression != "none": flags.append(f"tiff-{args.tiff_compression}")
    if args.png_level != 6: flags.append(f"png-level:{args.png_level}")
    if flags:
        print(f"Options: {', '.join(flags)}")
    if args.prefix:
        print(f"Prefix: '{args.prefix}'")
    if args.suffix:
        print(f"Suffix: '{args.suffix}'")

    # Validate JXL dependency
    if args.format == "jxl" and not HAS_JXL:
        print("[ERROR] JPEG XL output requires pillow-jxl-plugin (pip install pillow-jxl-plugin)",
              file=sys.stderr)
        sys.exit(EXIT_DEP_MISSING)

    # Validate AVIF dependency (Pillow 11.3+ native, was pillow-heif before v1.0)
    if args.format == "avif" and not HAS_AVIF:
        print("[ERROR] AVIF output requires Pillow >=11.3 with native AVIF support. "
              "Run: imgconverter --install-deps", file=sys.stderr)
        sys.exit(EXIT_DEP_MISSING)

    # Validate PNG compression level
    if args.png_level < 1 or args.png_level > 9:
        print(f"[ERROR] PNG compression level must be 1-9, got {args.png_level}",
              file=sys.stderr)
        sys.exit(EXIT_INPUT_ERROR)

    # Parse resize
    resize_mode = "none"
    resize_value = 1920
    if args.resize:
        try:
            parts = args.resize.split(":")
            if len(parts) == 2 and parts[0] == "max_dim":
                resize_mode = "max_dim"
                resize_value = int(parts[1])
            elif len(parts) == 2 and parts[0] == "scale":
                resize_mode = "scale"
                resize_value = int(parts[1])
            else:
                print(f"[ERROR] Invalid resize format: {args.resize} (use max_dim:VALUE or scale:VALUE)",
                      file=sys.stderr)
                sys.exit(EXIT_INPUT_ERROR)
        except ValueError:
            print(f"[ERROR] Invalid resize value: {args.resize}", file=sys.stderr)
            sys.exit(EXIT_INPUT_ERROR)

    # Watch mode short-circuits the scan/convert pipeline.
    if getattr(args, "watch", False):
        if len(input_dirs) != 1 or input_files:
            print("[ERROR] --watch requires exactly one input directory.", file=sys.stderr)
            sys.exit(EXIT_INPUT_ERROR)
        sys.exit(_watch_directory(args, input_dir, output_dir, resize_mode, resize_value))

    # Scan
    print(f"\nScanning{' recursively' if args.recursive else ''}...")
    max_bytes = _parse_size_spec(getattr(args, "max_file_size", None) or "")
    if max_bytes:
        print(f"[filter] skipping files larger than {_fmt_size(max_bytes)}")
    scan = _scan_cli_inputs(args, input_dir, input_dirs, input_files, max_bytes)
    print(f"Found {len(scan.files)} files ({_fmt_size(scan.total_size)}) in {scan.elapsed:.2f}s")

    # --resume: drop files that the previous run already converted.
    if getattr(args, "resume", False):
        state = _load_queue_state()
        if state and state.get("input") == str(input_dir):
            done_set = set(state.get("done", []))
            pre = len(scan.files)
            scan.files = [f for f in scan.files if str(f) not in done_set]
            scan.total_size = sum(f.stat().st_size for f in scan.files)
            print(f"[resume] {pre - len(scan.files)} files already done in previous run; "
                  f"continuing with {len(scan.files)}")
        else:
            print("[resume] no previous queue found (or different input dir); ignoring --resume")

    if not scan.files:
        print("No supported files found.")
        sys.exit(EXIT_OK)

    # Dry run — list and exit
    if args.dry_run:
        print(f"\n[DRY RUN] Would convert {len(scan.files)} files:")
        for f in sorted(scan.files):
            print(f"  {f}")
        sys.exit(EXIT_OK)

    # Disk space check
    if not args.in_place:
        output_dir.mkdir(parents=True, exist_ok=True)
    try:
        estimated = _estimate_output_size(scan.total_size, args.format)
        disk = shutil.disk_usage(str(output_dir))
        if estimated > disk.free:
            print(
                f"[ERROR] Not enough disk space. Estimated output: {_fmt_size(estimated)}, "
                f"available: {_fmt_size(disk.free)}",
                file=sys.stderr,
            )
            sys.exit(EXIT_DISK_FULL)
        if estimated > disk.free * 0.8:
            print(
                f"[WARN] Estimated output ({_fmt_size(estimated)}) exceeds 80% of "
                f"available disk space ({_fmt_size(disk.free)})"
            )
    except (OSError, ValueError):
        pass

    # Convert
    preserve_meta = not args.strip_metadata
    ok_count = 0
    fail_count = 0
    skip_count = 0
    total = len(scan.files)
    done_count = 0

    print(f"\nConverting {total} files with {args.workers} workers...\n")

    t0 = time.perf_counter()

    # Multi-frame handling — extract or animate when source has >1 frame.
    if getattr(args, "frames", "first") in ("all", "animate"):
        animated_files = [f for f in scan.files if count_frames(f) > 1]
        if animated_files:
            print(f"[multi-frame] {len(animated_files)} sources have >1 frame; "
                  f"--frames={args.frames} active")
            for f in animated_files:
                r = _convert_animated_or_sequence(
                    f, output_dir, args.format,
                    extract_frames=(args.frames == "all"),
                    base_dir=input_dir,
                    preserve_structure=not args.no_structure,
                )
                if r.success:
                    ok_count += 1
                    print(f"[OK*] {f.name}: {r.warnings[-1]}")
                else:
                    fail_count += 1
                    print(f"[FAIL*] {f.name}: {r.error}")
            scan.files = [f for f in scan.files if f not in animated_files]
            total = len(scan.files)

    all_results: list[ConvertResult] = []
    # Free-threaded Python: ThreadPoolExecutor scales linearly with cores.
    # Older Python: ProcessPoolExecutor bypasses the GIL at fork cost.
    Executor = ThreadPoolExecutor
    if getattr(args, "use_processes", False):
        from concurrent.futures import ProcessPoolExecutor as _PPE
        Executor = _PPE
        print(f"[pool] using process pool (workers={args.workers})")
    elif _gil_status() == "no-gil":
        print(f"[pool] free-threaded interpreter detected; thread-pool will scale linearly")

    cache_conn = _open_hash_cache() if getattr(args, "use_cache", False) else None
    cache_preset_key = _preset_hash(
        args.format, args.quality, args.in_place, args.no_structure,
        getattr(args, "template", None), resize_mode, resize_value,
        args.prefix, args.suffix, args.lossless, args.progressive,
        args.chroma_420, args.srgb, args.tiff_compression, args.png_level,
        getattr(args, "icc", None), getattr(args, "watermark", None),
        getattr(args, "canvas", None), getattr(args, "canvas_bg", "transparent"),
        getattr(args, "dpi", None), getattr(args, "recompress", False),
        getattr(args, "target_kb", None), getattr(args, "target_psnr", None),
    ) if cache_conn else None
    cache_skipped: list[Path] = []
    if cache_conn:
        pruned = []
        n_files = len(scan.files)
        for idx, f in enumerate(scan.files, 1):
            if idx % 50 == 0 or idx == n_files:
                print(f"\r[cache] checking {idx}/{n_files}...", end="", flush=True)
            try:
                src_h = _file_sha256(f)
                row = cache_conn.execute(
                    "SELECT dst_size FROM seen WHERE src_hash=? AND preset_hash=?",
                    (src_h, cache_preset_key),
                ).fetchone()
                if row:
                    cache_skipped.append(f)
                    continue
            except Exception:
                pass
            pruned.append(f)
        if n_files:
            print()
        if cache_skipped:
            print(f"[cache] skipping {len(cache_skipped)} files seen with this preset")
        scan.files = pruned
        total = len(scan.files)
    done_paths: list[str] = []
    failed_paths: list[str] = []

    with Executor(max_workers=args.workers) as pool:
        futures = {}
        for seq_i, f in enumerate(scan.files, start=1):
            fut = pool.submit(
                convert_file, f, output_dir, args.format, args.quality,
                preserve_meta, not args.no_structure, input_dir, args.in_place,
                args.skip_existing, resize_mode, resize_value,
                args.prefix, args.suffix, args.lossless, args.progressive,
                args.chroma_420, args.srgb, args.tiff_compression, args.png_level,
                not args.no_exiftool,           # use_exiftool
                getattr(args, "template", None), # name_template
                seq_i,                          # seq
                getattr(args, "only_if_smaller", None),  # only_if_smaller_pct
                (args.dpi, args.dpi) if getattr(args, "dpi", None) else None,  # dpi
                getattr(args, "icc", None),                  # icc_override
                getattr(args, "xmp_sidecar", False),         # emit_xmp_sidecar
                getattr(args, "recompress", False),          # recompress_lossless
                _build_quality_mode(args),                   # quality_mode
                getattr(args, "watermark", None),             # watermark
                _parse_canvas(getattr(args, "canvas", None)), # canvas
                getattr(args, "canvas_bg", "transparent"),    # canvas_bg
                getattr(args, "tone_map", "none"),             # tone_map
                getattr(args, "avif_speed", 6),                # avif_speed
                getattr(args, "avif_codec", "auto"),           # avif_codec
                getattr(args, "png_lossy", False),             # png_lossy
            )
            futures[fut] = f

        for fut in as_completed(futures):
            try:
                result = fut.result()
            except Exception as exc:
                f = futures[fut]
                result = ConvertResult(src=f, size_before=0)
                result.error = str(exc)
            all_results.append(result)
            done_count += 1
            if result.skipped:
                skip_count += 1
                print(f"[SKIP] ({done_count}/{total}) {result.src.name}")
            elif result.success:
                ok_count += 1
                saved = result.size_before - result.size_after
                pct = (saved / result.size_before * 100) if result.size_before else 0
                deleted_tag = "  [source deleted]" if result.src_deleted else ""
                print(
                    f"[OK] ({done_count}/{total}) {result.src.name} -> {result.dst.name}  "
                    f"({_fmt_size(result.size_before)} -> {_fmt_size(result.size_after)}, "
                    f"{pct:+.1f}%)  [{result.elapsed:.2f}s]{deleted_tag}"
                )
            else:
                fail_count += 1
                failed_paths.append(str(result.src))
                print(f"[FAIL] ({done_count}/{total}) {result.src.name}: {result.error}")

            if result.success and not result.skipped:
                done_paths.append(str(result.src))
                # Quality verification — butteraugli / ffmpeg-quality-metrics.
                if getattr(args, "verify_quality", False):
                    qline = _verify_quality(result.src, result.dst)
                    if qline:
                        print(f"[verify] {result.src.name}: {qline}")
                        result.warnings.append(f"verify: {qline}")
                # Sidecar JSON history — darktable pattern. Captures source
                # hash, the full conversion preset, and timestamp so the
                # output is reproducible from the metadata.
                if getattr(args, "sidecar_history", False) and result.dst:
                    try:
                        sidecar = result.dst.with_suffix(result.dst.suffix + ".imgconverter.json")
                        sidecar.write_text(json.dumps({
                            "version": APP_VERSION,
                            "timestamp": int(time.time()),
                            "src": str(result.src),
                            "src_hash": _file_sha256(result.src) if not result.src_deleted else "deleted",
                            "dst_hash": _file_sha256(result.dst) if result.dst and result.dst.exists() else "",
                            "preset": {
                                "format": args.format,
                                "quality": args.quality,
                                "workers": args.workers,
                                "resize": args.resize,
                                "prefix": args.prefix,
                                "suffix": args.suffix,
                                "template": getattr(args, "template", None),
                                "in_place": args.in_place,
                                "strip_metadata": args.strip_metadata,
                                "progressive": args.progressive,
                                "chroma_420": args.chroma_420,
                                "lossless": args.lossless,
                                "srgb": args.srgb,
                                "tiff_compression": args.tiff_compression,
                                "png_level": args.png_level,
                                "icc": getattr(args, "icc", None),
                                "watermark": getattr(args, "watermark", None),
                                "canvas": getattr(args, "canvas", None),
                                "tone_map": getattr(args, "tone_map", "none"),
                                "dpi": getattr(args, "dpi", None),
                                "target_kb": getattr(args, "target_kb", None),
                                "target_psnr": getattr(args, "target_psnr", None),
                            },
                            "result": {
                                "size_in": result.size_before,
                                "size_out": result.size_after,
                                "warnings": list(result.warnings),
                            },
                        }, indent=2, default=str))
                    except OSError:
                        pass
                # Persist into hash cache for future --use-cache runs.
                if cache_conn:
                    try:
                        cache_conn.execute(
                            "INSERT OR REPLACE INTO seen "
                            "(src_hash, preset_hash, dst_hash, dst_size, ts) "
                            "VALUES (?, ?, ?, ?, ?)",
                            (_file_sha256(result.src) if not result.src_deleted else "",
                             cache_preset_key,
                             _file_sha256(result.dst) if result.dst and result.dst.exists() else "",
                             result.size_after, int(time.time())),
                        )
                    except Exception as e:
                        _diag_log(f"cache persist failed for {result.src.name}: {e}", level="WARNING")

            # Persist queue state every 5 completions so a power cycle
            # doesn't lose more than a handful of converts.
            if done_count % 5 == 0:
                _save_queue_state(input_dir, output_dir, args,
                                   pending=[fp for fp in scan.files
                                            if str(fp) not in done_paths
                                            and str(fp) not in failed_paths],
                                   done=done_paths, failed=failed_paths)

            for warn in result.warnings:
                print(f"[WARN] {result.src.name}: {warn}")

    if cache_conn:
        try:
            cache_conn.close()
        except Exception:
            pass
    # Successful end-of-batch -> drop queue state so future --resume is a no-op.
    if fail_count == 0:
        _clear_queue_state()
    else:
        _save_queue_state(input_dir, output_dir, args, pending=[],
                           done=done_paths, failed=failed_paths)

    wall_time = time.perf_counter() - t0
    speed = ok_count / wall_time if wall_time > 0 else 0
    print(f"\nDone: {ok_count} converted, {fail_count} failed, {skip_count} skipped in {wall_time:.0f}s ({speed:.1f} files/sec)")

    # JSON report — structured per-file output for CI / Ansible / cron pipelines.
    if getattr(args, "report", None):
        report = {
            "summary": {
                "version": APP_VERSION,
                "input": str(input_dir),
                "output": str(output_dir),
                "format": args.format,
                "quality": args.quality,
                "workers": args.workers,
                "total": total,
                "ok": ok_count,
                "skipped": skip_count,
                "failed": fail_count,
                "elapsed_seconds": wall_time,
                "files_per_second": speed,
            },
            "files": [
                {
                    "src": str(r.src),
                    "dst": str(r.dst) if r.dst else None,
                    "ok": r.success,
                    "skipped": r.skipped,
                    "size_in": r.size_before,
                    "size_out": r.size_after,
                    "elapsed": r.elapsed,
                    "src_deleted": r.src_deleted,
                    "error": r.error or None,
                    "warnings": list(r.warnings),
                }
                for r in all_results
            ],
        }
        try:
            Path(args.report).write_text(json.dumps(report, indent=2, default=str))
            print(f"\n[report] wrote {args.report}")
        except OSError as e:
            print(f"[report] failed to write {args.report}: {e}", file=sys.stderr)

    # Structured exit-code matrix — see EXIT_CODES at module top.
    if fail_count == total and total > 0:
        sys.exit(EXIT_TOTAL_FAILURE)
    elif fail_count > 0:
        sys.exit(EXIT_PARTIAL_FAILURE)
    sys.exit(EXIT_OK)


# ── Entry Point ───────────────────────────────────────────────────────────────

def main():
    parser = _build_parser()
    args = parser.parse_args()

    # --install-deps short-circuit is also handled before imports at module top;
    # argparse path catches the case where the user types the flag after others.
    if getattr(args, "install_deps", False):
        sys.exit(_install_deps(include_optional=True))

    if getattr(args, "list_presets", False):
        _seed_user_preset_dir()
        for name, payload in list_presets().items():
            print(f"  {name}")
            for k, v in sorted(payload.items()):
                print(f"      {k}: {v}")
        sys.exit(EXIT_OK)

    if getattr(args, "list_plugins", False):
        sys.exit(_list_plugins())
    if getattr(args, "trust_plugin", None):
        ok, msg = _trust_plugin(args.trust_plugin)
        print(msg, file=sys.stderr if not ok else sys.stdout)
        sys.exit(EXIT_OK if ok else EXIT_INPUT_ERROR)
    if getattr(args, "untrust_plugin", None):
        ok, msg = _untrust_plugin(args.untrust_plugin)
        print(msg, file=sys.stderr if not ok else sys.stdout)
        sys.exit(EXIT_OK if ok else EXIT_INPUT_ERROR)

    if getattr(args, "register_shell", False):
        sys.exit(_install_shell_integration(uninstall=False))
    if getattr(args, "unregister_shell", False):
        sys.exit(_install_shell_integration(uninstall=True))

    if getattr(args, "clear_cache", False):
        try:
            HASH_CACHE_PATH.unlink(missing_ok=True)
            print(f"[cache] removed {HASH_CACHE_PATH}")
        except OSError as e:
            print(f"[cache] failed: {e}", file=sys.stderr)
            sys.exit(EXIT_INPUT_ERROR)
        sys.exit(EXIT_OK)

    _seed_user_preset_dir()
    _warn_below_floor()
    plugins = _load_plugins()
    if plugins:
        print(f"[plugins] loaded: {', '.join(plugins)}")

    if args.input or getattr(args, "files", None):
        _run_cli(args)
        return

    if not HAS_PYQT6:
        print("[ERROR] PyQt6 is required for GUI mode. Install it:\n"
              "  pip install PyQt6>=6.8\n"
              "Or use CLI mode: imgconverter --input ./photos --format jpeg",
              file=sys.stderr)
        sys.exit(EXIT_DEP_MISSING)

    app = QApplication(sys.argv)

    branding_icon = QIcon(str(_branding_icon_path()))

    app.setWindowIcon(branding_icon)
    app.setStyle("Fusion")
    app.setStyleSheet(STYLESHEET)

    # Dark palette fallback
    palette = QPalette()
    palette.setColor(QPalette.ColorRole.Window, QColor(CAT["base"]))
    palette.setColor(QPalette.ColorRole.WindowText, QColor(CAT["text"]))
    palette.setColor(QPalette.ColorRole.Base, QColor(CAT["crust"]))
    palette.setColor(QPalette.ColorRole.AlternateBase, QColor(CAT["surface0"]))
    palette.setColor(QPalette.ColorRole.Text, QColor(CAT["text"]))
    palette.setColor(QPalette.ColorRole.Button, QColor(CAT["surface0"]))
    palette.setColor(QPalette.ColorRole.ButtonText, QColor(CAT["text"]))
    palette.setColor(QPalette.ColorRole.Highlight, QColor(CAT["blue"]))
    palette.setColor(QPalette.ColorRole.HighlightedText, QColor(CAT["crust"]))
    app.setPalette(palette)

    app.setWindowIcon(_create_app_icon())
    window = MainWindow()
    window.setWindowIcon(branding_icon)
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    import multiprocessing
    multiprocessing.freeze_support()
    main()
