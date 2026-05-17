#!/usr/bin/env python3
"""
HEICShift v2.9.0 - Universal image batch converter
Scans directories recursively and converts JPEG, PNG, HEIC, AVIF, WebP,
JPEG XL, RAW, TIFF, BMP, JPEG 2000, QOI, and ICO files to JPEG, PNG,
WebP, AVIF, TIFF, or JPEG XL. Auto-detects optimal format: PNG for
images with transparency, JPEG for photos. Preserves EXIF, ICC, and
XMP. CLI + GUI parity. See ROADMAP.md for in-flight work.
"""

import sys, os, subprocess, importlib, platform, ctypes, argparse, shutil
from pathlib import Path


# codex-branding:start
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
# codex-branding:end


APP_VERSION = "2.9.0"

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
    "PIL":          ("Pillow",             "11.3.0"),
    "pillow_heif":  ("pillow-heif",        "1.3.0"),
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
        for cmd_extra in ([], ["--user"], ["--break-system-packages"]):
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
            print(f"[install-deps] FAILED: {spec}", file=sys.stderr)
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
            f"[heicshift] Missing required dependencies: {', '.join(missing)}\n"
            f"  Install all required + optional deps with:\n"
            f"      {sys.executable} -m heicshift --install-deps\n"
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
                    f"[heicshift] WARNING: {pkg} {installed} is below the documented "
                    f"floor of {floor}. Older versions have known CVEs — see "
                    f"ROADMAP.md Appendix A6. Run: heicshift --install-deps",
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
import traceback
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field

import numpy as np
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
except Exception:
    pass

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

# Optional: jpegoptim / jpegtran for lossless JPEG recompression — bit-preserving
# size reduction without re-encoding pixels. Either is sufficient.
JPEGOPTIM_PATH = shutil.which("jpegoptim")
JPEGTRAN_PATH = shutil.which("jpegtran")
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
    QSystemTrayIcon, QMenu, QToolButton, QScrollArea,
)

# ── Catppuccin Mocha Palette ──────────────────────────────────────────────────
CAT = {
    "base":      "#1e1e2e", "mantle":   "#181825", "crust":    "#11111b",
    "surface0":  "#313244", "surface1": "#45475a", "surface2": "#585b70",
    "overlay0":  "#6c7086", "overlay1": "#7f849c",
    "text":      "#cdd6f4", "subtext0": "#a6adc8", "subtext1": "#bac2de",
    "lavender":  "#b4befe", "blue":     "#89b4fa", "sapphire": "#74c7ec",
    "sky":       "#89dceb", "teal":     "#94e2d5", "green":    "#a6e3a1",
    "yellow":    "#f9e2af", "peach":    "#fab387", "maroon":   "#eba0ac",
    "red":       "#f38ba8", "mauve":    "#cba6f7", "pink":     "#f5c2e7",
    "flamingo":  "#f2cdcd", "rosewater":"#f5e0dc",
}

STYLESHEET = f"""
QMainWindow, QWidget {{
    background-color: {CAT['base']};
    color: {CAT['text']};
    font-family: 'Segoe UI', 'Inter', sans-serif;
    font-size: 13px;
}}
QGroupBox {{
    background-color: {CAT['mantle']};
    border: 1px solid {CAT['surface1']};
    border-radius: 8px;
    margin-top: 14px;
    padding: 16px 12px 12px 12px;
    font-weight: 600;
    font-size: 13px;
    color: {CAT['lavender']};
}}
QGroupBox::title {{
    subcontrol-origin: margin;
    subcontrol-position: top left;
    padding: 2px 10px;
    background-color: {CAT['mantle']};
    border-radius: 4px;
}}
QPushButton {{
    background-color: {CAT['surface0']};
    color: {CAT['text']};
    border: 1px solid {CAT['surface1']};
    border-radius: 6px;
    padding: 7px 18px;
    font-weight: 500;
    min-height: 20px;
}}
QPushButton:hover {{
    background-color: {CAT['surface1']};
    border-color: {CAT['lavender']};
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
    border: none;
    font-weight: 700;
    font-size: 14px;
    padding: 10px 28px;
}}
QPushButton#primaryBtn:hover {{
    background-color: {CAT['lavender']};
}}
QPushButton#primaryBtn:disabled {{
    background-color: {CAT['surface1']};
    color: {CAT['overlay0']};
}}
QPushButton#stopBtn {{
    background-color: {CAT['red']};
    color: {CAT['crust']};
    border: none;
    font-weight: 700;
}}
QPushButton#stopBtn:hover {{
    background-color: {CAT['maroon']};
}}
QLineEdit {{
    background-color: {CAT['surface0']};
    color: {CAT['text']};
    border: 1px solid {CAT['surface1']};
    border-radius: 6px;
    padding: 6px 10px;
    selection-background-color: {CAT['blue']};
}}
QLineEdit:focus {{
    border-color: {CAT['lavender']};
}}
QComboBox {{
    background-color: {CAT['surface0']};
    color: {CAT['text']};
    border: 1px solid {CAT['surface1']};
    border-radius: 6px;
    padding: 6px 10px;
    min-width: 120px;
}}
QComboBox:hover {{
    border-color: {CAT['lavender']};
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
    padding: 4px 8px;
}}
QSlider::groove:horizontal {{
    background: {CAT['surface0']};
    height: 6px;
    border-radius: 3px;
}}
QSlider::handle:horizontal {{
    background: {CAT['lavender']};
    width: 16px;
    height: 16px;
    margin: -5px 0;
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
QLabel#dimLabel {{
    color: {CAT['overlay1']};
    font-size: 12px;
}}
QLabel#statValue {{
    color: {CAT['green']};
    font-size: 22px;
    font-weight: 700;
}}
QLabel#statLabel {{
    color: {CAT['overlay1']};
    font-size: 11px;
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


# ── Conversion Engine ─────────────────────────────────────────────────────────

def scan_directory(
    root: Path,
    recursive: bool = True,
    extensions: set[str] | None = None,
    on_progress=None,
    exclude_patterns: list[str] | None = None,
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

    def _excluded(rel: Path) -> bool:
        s = rel.as_posix()
        for pat in exclude_patterns:
            if rel.match(pat) or Path(s).match(pat):
                return True
        return False

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
            if _excluded(rel):
                continue
            try:
                st = p.stat()
            except OSError:
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
    base.paste(img, ((cw - new_w) // 2, (ch - new_h) // 2))
    return base


def _psnr(a: "Image.Image", b: "Image.Image") -> float:
    """Compute PSNR between two images. Returns +inf when identical."""
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

    return img, meta


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
            frames = list(ImageSequence.Iterator(img))
            n = len(frames)
            if extract_frames or fmt_pil not in ("WEBP", "GIF", "PNG"):
                # Per-frame export
                pad_width = max(3, len(str(n)))
                written = []
                for i, frame in enumerate(frames, start=1):
                    seq = str(i).zfill(pad_width)
                    dst = dest_dir / f"{src.stem}.{seq}{ext}"
                    frame_save = frame.convert("RGB") if fmt_pil == "JPEG" else frame
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
                # Preserve as animated output (WebP/PNG/GIF only here).
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
                result.warnings.append(f"multi-frame: animated {fmt_pil} with {n} frames")
    except Exception as e:
        result.error = f"multi-frame: {e}"
    result.elapsed = time.perf_counter() - t0
    return result


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
) -> ConvertResult:
    """Convert a single image file. Thread-safe.

    When ``use_exiftool`` is True and the ``exiftool`` binary is on PATH,
    runs an ExifTool tag-copy pass after the save so MakerNotes, GPS
    sub-IFDs, IPTC, and sidecar XMP make it across — Pillow drops these
    silently. Falls back to Pillow's EXIF / ICC / XMP keys when ExifTool
    is unavailable.
    """
    t0 = time.perf_counter()
    result = ConvertResult(src=src, size_before=src.stat().st_size)
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
            img = rotated
            # Refresh EXIF from the transposed image (orientation tag removed)
            if "exif" in img.info:
                meta["exif"] = img.info["exif"]

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
                if in_place and result.success and out_path != src:
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
            # Template may include subdirectories; resolve relative to dest_dir.
            cand = Path(applied)
            if cand.is_absolute():
                out_path = cand
            else:
                out_path = dest_dir / cand
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
        elif out_fmt == "AVIF":
            save_kwargs["quality"] = jpeg_quality
            save_kwargs["speed"] = 6
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
            temp_path = out_path.parent / (out_path.name + ".heicshift.tmp")
            img.save(str(temp_path), out_fmt, **save_kwargs)
        else:
            img.save(str(out_path), out_fmt, **save_kwargs)

        # Validate output file integrity. Image.verify() only checks the header;
        # pair it with a re-open + size-match so a truncated encode is detected.
        check_path = temp_path if in_place else out_path
        if not check_path.exists() or check_path.stat().st_size == 0:
            raise RuntimeError(f"Output file missing or empty: {check_path.name}")
        try:
            with Image.open(str(check_path)) as verify_img:
                verify_img.verify()
            with Image.open(str(check_path)) as decoded:
                if decoded.size != img.size:
                    raise RuntimeError(
                        f"Output size {decoded.size} != source size {img.size}"
                    )
        except Exception as ve:
            raise RuntimeError(f"Output validation failed: {ve}")

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

        # XMP sidecar emit — for archival workflows that strip metadata from
        # the binary but want it preserved separately (Adobe Bridge / darktable
        # convention). Sidecar is named <output>.xmp.
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

        # ── Sidecar companions ────────────────────────────────────────────
        # Live Photo .MOV — iPhone HEIC stills are paired with a sibling
        # .MOV motion file. Carry it through alongside the converted still
        # so users keep the dynamic version.
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

        # Depth map / aux images — pillow_heif >= 0.20 exposes depth_images
        # via info; emit each as a 16-bit PNG sidecar next to the output.
        if src.suffix.lower() in HEIC_EXTS:
            depth_list = None
            try:
                # Re-open via pillow_heif's typed API to get the depth payload.
                heif_file = pillow_heif.read_heif(str(src))
                depth_list = getattr(heif_file, "depth_images", None)
            except Exception:
                depth_list = None
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

        # HDR gain-map detection (ISO 21496-1) — libheif lacks native support
        # so we can't TRANSCODE the gain map yet, but ExifTool can tell us
        # whether a Display P3 / Adaptive HDR map is present, and we copy
        # the source HEIC alongside as an archival fallback.
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
            except Exception:
                pass

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
                 png_compress_level=6, use_exiftool=True):
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
        self._stop = False

    def stop(self):
        self._stop = True

    def run(self):
        results = []
        total = len(self.files)
        done = 0

        self.log.emit(f"Starting conversion of {total} files with {self.workers} workers...")

        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            futures = {}
            for f in self.files:
                if self._stop:
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
                )
                futures[fut] = f

            for fut in as_completed(futures):
                if self._stop:
                    pool.shutdown(wait=False, cancel_futures=True)
                    self.log.emit("Conversion cancelled by user.")
                    break

                result = fut.result()
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
    p.drawText(pm.rect(), Qt.AlignmentFlag.AlignCenter, "H")
    p.end()
    return QIcon(pm)


# ── Conversion Presets ────────────────────────────────────────────────────────

USER_CACHE_DIR = Path.home() / ".cache" / "heicshift"
USER_CONFIG_DIR = Path.home() / ".heicshift"
USER_PRESET_DIR = USER_CONFIG_DIR / "presets"
USER_LOG_PATH = USER_CACHE_DIR / "heicshift.log"

# QSettings shape version — bump when on-disk settings layout changes so the
# migration in _maybe_migrate_settings() runs once on startup.
SETTINGS_SCHEMA = 2


def _diag_log(message: str, level: str = "INFO"):
    """Append a timestamped line to ~/.cache/heicshift/heicshift.log.

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
            "https://api.github.com/repos/SysAdminDoc/HEICShift/releases/latest",
            headers={"Accept": "application/vnd.github+json",
                     "User-Agent": f"HEICShift/{current_version}"},
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
        self.setWindowTitle(f"HEICShift v{APP_VERSION}")
        self.setMinimumSize(700, 520)
        self.resize(900, 800)
        self.setAcceptDrops(True)

        self._icon = _create_app_icon()
        self.setWindowIcon(self._icon)

        self.settings = QSettings("HEICShift", "HEICShift")
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
        _diag_log(f"HEICShift v{APP_VERSION} started (GUI mode)")
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
            ("src_edit",            "Source directory",         "Directory to scan for input images"),
            ("dst_edit",            "Output directory",         "Where converted files go (blank = source/converted)"),
            ("fmt_combo",           "Output format",            "Target image format (auto/jpeg/png/webp/avif/tiff/jxl)"),
            ("quality_slider",      "Quality",                  "Encoder quality 50-100 for JPEG/WebP/AVIF/JXL"),
            ("workers_spin",        "Worker thread count",      "Number of parallel conversion threads"),
            ("recursive_chk",       "Recursive scan",           "Walk subdirectories under the source directory"),
            ("inplace_chk",         "Convert in place",         "Save output next to each source and delete the source"),
            ("skip_existing_chk",   "Skip existing outputs",    "Skip files whose output already exists"),
            ("meta_chk",            "Preserve metadata",        "Keep EXIF, ICC, and XMP through conversion"),
            ("progressive_jpeg_chk","Progressive JPEG",         "Encode JPEGs as progressive for web delivery"),
            ("lossless_webp_chk",   "Lossless WebP",            "Save WebP in lossless mode"),
            ("resize_chk",          "Enable resize",            "Resize images during conversion"),
            ("resize_combo",        "Resize mode",              "Max dimension in pixels, or percent scale"),
            ("resize_spin",         "Resize value",             "Numeric value for the chosen resize mode"),
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
                self._log(f"[UPDATE] HEICShift v{latest} is available "
                          f"(https://github.com/SysAdminDoc/HEICShift/releases)")
                _diag_log(f"Update available: v{latest}")
        except Exception:
            pass

    def _log_startup(self):
        """Log supported formats, dependency versions, and optional dep status on launch."""
        self._log(f"HEICShift v{APP_VERSION}")
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
        exts = sorted(get_supported_extensions())
        self._log(f"Scanning for: {' '.join(exts)}")
        self._log("")

    def _update_title(self, state: str = "base", **kwargs):
        """Update window title bar with contextual info."""
        base = f"HEICShift v{APP_VERSION}"
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
        root.setContentsMargins(16, 12, 16, 8)
        root.setSpacing(10)

        # ── Scroll area for controls ──
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll_widget = QWidget()
        scroll_layout = QVBoxLayout(scroll_widget)
        scroll_layout.setContentsMargins(0, 0, 0, 0)
        scroll_layout.setSpacing(10)
        scroll.setWidget(scroll_widget)

        # ── Header ──
        hdr = QHBoxLayout()
        title = QLabel("HEICShift")
        title.setStyleSheet(f"font-size: 20px; font-weight: 800; color: {CAT['lavender']};")
        ver = QLabel(f"v{APP_VERSION}")
        ver.setStyleSheet(f"color: {CAT['overlay0']}; font-size: 12px; margin-left: 6px;")
        hdr.addWidget(title)
        hdr.addWidget(ver)
        hdr.addStretch()
        desc = QLabel("Universal image batch converter with metadata preservation")
        desc.setObjectName("dimLabel")
        hdr.addWidget(desc)
        scroll_layout.addLayout(hdr)

        # ── Source / Output ──
        io_group = QGroupBox("Directories")
        io_grid = QGridLayout(io_group)
        io_grid.setColumnStretch(1, 1)

        io_grid.addWidget(QLabel("Source:"), 0, 0)
        self.src_edit = QLineEdit()
        self.src_edit.setPlaceholderText("Select or drag & drop a directory containing image files...")
        io_grid.addWidget(self.src_edit, 0, 1)
        self.src_btn = QPushButton("Browse")
        self.src_btn.clicked.connect(self._browse_source)
        io_grid.addWidget(self.src_btn, 0, 2)

        self.recent_btn = QToolButton()
        self.recent_btn.setText("▾")
        self.recent_btn.setMinimumWidth(28)
        self.recent_btn.setToolTip("Recent directories")
        self._recent_menu = QMenu(self)
        self.recent_btn.setMenu(self._recent_menu)
        self.recent_btn.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self._recent_menu.aboutToShow.connect(self._populate_recent_menu)
        io_grid.addWidget(self.recent_btn, 0, 3)

        io_grid.addWidget(QLabel("Output:"), 1, 0)
        self.dst_edit = QLineEdit()
        self.dst_edit.setPlaceholderText("Converted files go here (default: source/converted)")
        io_grid.addWidget(self.dst_edit, 1, 1)
        self.dst_btn = QPushButton("Browse")
        self.dst_btn.clicked.connect(self._browse_output)
        io_grid.addWidget(self.dst_btn, 1, 2)

        self.recursive_chk = QCheckBox("Scan subdirectories")
        self.recursive_chk.setChecked(True)
        io_grid.addWidget(self.recursive_chk, 2, 1)

        self.structure_chk = QCheckBox("Preserve folder structure in output")
        self.structure_chk.setChecked(True)
        io_grid.addWidget(self.structure_chk, 2, 2)

        self.inplace_chk = QCheckBox("Convert in place (save next to original, delete source)")
        self.inplace_chk.setChecked(False)
        self.inplace_chk.setStyleSheet(f"color: {CAT['peach']};")
        self.inplace_chk.toggled.connect(self._on_inplace_toggled)
        io_grid.addWidget(self.inplace_chk, 3, 1, 1, 2)

        scroll_layout.addWidget(io_group)

        # ── Input Format Filter ──
        filter_group = QGroupBox("Input Format Filter")
        filter_layout = QGridLayout(filter_group)
        filter_layout.setSpacing(6)

        self._format_filters: dict[str, QCheckBox] = {}
        col = 0
        row = 0
        for name, (exts, available) in FORMAT_FAMILIES.items():
            chk = QCheckBox(name)
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
        opt_group = QGroupBox("Conversion Settings")
        opt_grid = QGridLayout(opt_group)
        opt_grid.setColumnStretch(0, 0)
        opt_grid.setColumnStretch(1, 1)
        opt_grid.setColumnStretch(2, 0)
        opt_grid.setColumnStretch(3, 1)

        opt_grid.addWidget(QLabel("Output Format:"), 0, 0)
        self.fmt_combo = QComboBox()
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
        self._preset_btn.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self._preset_btn.setToolTip("Apply a conversion preset")
        preset_menu = QMenu(self)
        for name in PRESETS:
            preset_menu.addAction(name, lambda n=name: self._apply_preset(n))
        self._preset_btn.setMenu(preset_menu)
        opt_grid.addWidget(self._preset_btn, 0, 3)

        self.quality_desc_label = QLabel("JPEG/WebP Quality:")
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

        opt_grid.addWidget(QLabel("Parallel Workers:"), 2, 0)
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
        opt_grid.addWidget(QLabel("Prefix:"), 5, 0)
        self.prefix_edit = QLineEdit()
        self.prefix_edit.setPlaceholderText("e.g. converted_")
        opt_grid.addWidget(self.prefix_edit, 5, 1)

        opt_grid.addWidget(QLabel("Suffix:"), 5, 2)
        self.suffix_edit = QLineEdit()
        self.suffix_edit.setPlaceholderText("e.g. _web")
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
        self.tiff_comp_label = QLabel("TIFF Compression:")
        opt_grid.addWidget(self.tiff_comp_label, 7, 0)
        self.tiff_comp_combo = QComboBox()
        self.tiff_comp_combo.addItems(["None", "LZW", "Deflate"])
        opt_grid.addWidget(self.tiff_comp_combo, 7, 1)

        self.png_level_label = QLabel("PNG Compression:")
        opt_grid.addWidget(self.png_level_label, 7, 2)
        self.png_level_spin = QSpinBox()
        self.png_level_spin.setRange(1, 9)
        self.png_level_spin.setValue(6)
        self.png_level_spin.setToolTip("PNG compression level (1=fastest, 9=smallest)")
        opt_grid.addWidget(self.png_level_spin, 7, 3)

        # Show/hide TIFF and PNG controls based on format selection
        self.fmt_combo.currentIndexChanged.connect(self._on_format_changed)
        self._on_format_changed(self.fmt_combo.currentIndex())

        scroll_layout.addWidget(opt_group)

        # ── Actions ──
        actions = QHBoxLayout()
        self.scan_btn = QPushButton("Scan Directory")
        self.scan_btn.setObjectName("primaryBtn")
        self.scan_btn.clicked.connect(self._scan)
        actions.addWidget(self.scan_btn)

        self.convert_btn = QPushButton("Convert All")
        self.convert_btn.setObjectName("primaryBtn")
        self.convert_btn.setEnabled(False)
        self.convert_btn.clicked.connect(self._convert)
        actions.addWidget(self.convert_btn)

        self.stop_btn = QPushButton("Stop")
        self.stop_btn.setObjectName("stopBtn")
        self.stop_btn.setEnabled(False)
        self.stop_btn.clicked.connect(self._stop)
        actions.addWidget(self.stop_btn)

        actions.addStretch()

        self.auto_open_chk = QCheckBox("Auto-open output folder")
        self.auto_open_chk.setChecked(False)
        self.auto_open_chk.setToolTip("Automatically open the output folder when conversion finishes")
        actions.addWidget(self.auto_open_chk)

        self.open_output_btn = QPushButton("Open Output Folder")
        self.open_output_btn.setEnabled(False)
        self.open_output_btn.clicked.connect(self._open_output)
        actions.addWidget(self.open_output_btn)

        scroll_layout.addLayout(actions)

        # ── Stats bar ──
        stats_frame = QFrame()
        stats_frame.setStyleSheet(
            f"background-color: {CAT['mantle']}; border-radius: 8px; padding: 6px;"
        )
        stats_layout = QHBoxLayout(stats_frame)
        stats_layout.setContentsMargins(16, 8, 16, 8)

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
        log_label = QLabel("Log")
        log_label.setStyleSheet(f"color: {CAT['overlay1']}; font-weight: 600; font-size: 12px;")
        log_header.addWidget(log_label)
        log_header.addStretch()

        self.export_log_btn = QPushButton("Export Log")
        self.export_log_btn.setStyleSheet("font-size: 11px; padding: 2px 10px;")
        self.export_log_btn.clicked.connect(self._export_log)
        log_header.addWidget(self.export_log_btn)

        self.export_csv_btn = QPushButton("Export CSV")
        self.export_csv_btn.setStyleSheet("font-size: 11px; padding: 2px 10px;")
        self.export_csv_btn.setToolTip("Export conversion results as a CSV report")
        self.export_csv_btn.clicked.connect(self._export_csv)
        log_header.addWidget(self.export_csv_btn)

        self.clear_log_btn = QPushButton("Clear")
        self.clear_log_btn.setStyleSheet("font-size: 11px; padding: 2px 10px;")
        self.clear_log_btn.clicked.connect(self._clear_log)
        log_header.addWidget(self.clear_log_btn)

        log_container = QWidget()
        log_container_layout = QVBoxLayout(log_container)
        log_container_layout.setContentsMargins(0, 0, 0, 0)
        log_container_layout.setSpacing(4)
        log_container_layout.addLayout(log_header)

        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
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
        root.addWidget(splitter, 1)

        # ── Status bar ──
        self.status_bar = QStatusBar()
        self.setStatusBar(self.status_bar)
        self.status_bar.showMessage("Ready")

    def _make_stat(self, value: str, label: str) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        lay.setAlignment(Qt.AlignmentFlag.AlignCenter)
        val = QLabel(value)
        val.setObjectName("statValue")
        val.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lbl = QLabel(label)
        lbl.setObjectName("statLabel")
        lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(val)
        lay.addWidget(lbl)
        w._val = val
        return w

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

        menu.exec(self.log_view.mapToGlobal(pos))

    # ── Presets ──
    def _apply_preset(self, name: str):
        preset = PRESETS.get(name)
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
            self.dst_edit.setPlaceholderText("(disabled — output goes next to each source file)")
        else:
            self.dst_edit.setPlaceholderText("Converted files go here (default: source/converted)")

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
            self.quality_desc_label.setText("JPEG Quality:")
        elif is_webp:
            self.quality_desc_label.setText("WebP Quality:")
        elif is_avif:
            self.quality_desc_label.setText("AVIF Quality:")
        elif is_jxl:
            self.quality_desc_label.setText("JXL Quality:")
        else:
            self.quality_desc_label.setText("JPEG/WebP Quality:")

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
            self, "Export Log", str(Path.home() / "heicshift_log.txt"),
            "Text Files (*.txt);;All Files (*)"
        )
        if path:
            Path(path).write_text(self.log_view.toPlainText(), encoding="utf-8")
            self._log(f"Log exported to {path}")

    def _export_csv(self):
        """Export conversion results as a CSV report."""
        if not self._results:
            self._log("[ERROR] No conversion results to export.")
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "Export CSV Report", str(Path.home() / "heicshift_report.csv"),
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
            return

        self._update_title()
        self.scan_btn.setEnabled(False)
        self.convert_btn.setEnabled(False)
        self.status_bar.showMessage("Scanning...")

        enabled_exts = self._get_enabled_extensions()
        if not enabled_exts:
            self._log("[ERROR] No input formats selected in the filter panel.")
            self.scan_btn.setEnabled(True)
            self.status_bar.showMessage("Ready")
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
            self.status_bar.showMessage(
                f"Found {len(result.files)} files ({_fmt_size(result.total_size)}). Ready to convert."
            )
        else:
            self.status_bar.showMessage("No supported image files found.")

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
        default_stat_style = f"color: {CAT['green']}; font-size: 22px; font-weight: 700;"
        self.stat_done._val.setStyleSheet(default_stat_style)
        self.stat_skipped._val.setStyleSheet(default_stat_style)
        self.stat_failed._val.setStyleSheet(default_stat_style)
        self.stat_saved._val.setStyleSheet(default_stat_style)

        self.scan_btn.setEnabled(False)
        self.convert_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self.open_output_btn.setEnabled(False)
        self.status_bar.showMessage("Converting...")

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
        self.progress_bar.setFormat(f"%p% — {filename}")

    def _on_file_done(self, result: ConvertResult):
        self._results.append(result)
        if result.success and result.dst:
            self._last_ok_dst = result.dst
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
            self.stat_saved._val.setStyleSheet(f"color: {CAT['green']}; font-size: 22px; font-weight: 700;")
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
        self.status_bar.showMessage(summary)

        # System tray notification
        if QSystemTrayIcon.isSystemTrayAvailable():
            self._tray.show()
            self._tray.showMessage(
                "HEICShift — Conversion Complete",
                f"{ok} converted, {fail} failed" + (f", {skipped} skipped" if skipped else ""),
                QSystemTrayIcon.MessageIcon.Information,
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
            self.status_bar.showMessage("Stopping...")

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

    def _restore_state(self):
        self._maybe_migrate_settings()
        if v := self.settings.value("src"):
            self.src_edit.setText(v)
        if v := self.settings.value("dst"):
            self.dst_edit.setText(v)
        if (v := self.settings.value("fmt")) is not None:
            idx = int(v)
            if 0 <= idx < self.fmt_combo.count():
                self.fmt_combo.setCurrentIndex(idx)
        if (v := self.settings.value("quality")) is not None:
            self.quality_slider.setValue(int(v))
        if (v := self.settings.value("workers")) is not None:
            self.workers_spin.setValue(int(v))
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
        if (v := self.settings.value("resize_mode")) is not None:
            self.resize_combo.blockSignals(True)
            self.resize_combo.setCurrentIndex(int(v))
            self.resize_combo.blockSignals(False)
            if int(v) == 0:
                self.resize_spin.setRange(100, 10000)
                self.resize_spin.setSuffix(" px")
            else:
                self.resize_spin.setRange(1, 500)
                self.resize_spin.setSuffix(" %")
        if (v := self.settings.value("resize_value")) is not None:
            self.resize_spin.setValue(int(v))
        if (v := self.settings.value("prefix")) is not None:
            self.prefix_edit.setText(v)
        if (v := self.settings.value("suffix")) is not None:
            self.suffix_edit.setText(v)
        if (v := self.settings.value("chroma_subsampling")) is not None:
            self.chroma_chk.setChecked(v == "true" or v is True)
        if (v := self.settings.value("convert_to_srgb")) is not None:
            self.srgb_chk.setChecked(v == "true" or v is True)
        if (v := self.settings.value("tiff_compression")) is not None:
            self.tiff_comp_combo.setCurrentIndex(int(v))
        if (v := self.settings.value("png_compress_level")) is not None:
            self.png_level_spin.setValue(int(v))
        if (v := self.settings.value("strip_metadata")) is not None:
            self.strip_meta_chk.setChecked(v == "true" or v is True)
        if (v := self.settings.value("auto_open_output")) is not None:
            self.auto_open_chk.setChecked(v == "true" or v is True)
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
        prog="heicshift",
        description=f"HEICShift v{APP_VERSION} - High-performance image batch converter",
    )
    p.add_argument("--version", action="version", version=f"HEICShift v{APP_VERSION}")
    p.add_argument("--install-deps", action="store_true",
                   help="Install/upgrade required + optional Python dependencies, then exit")
    p.add_argument("-i", "--input", type=str, help="Source directory to scan")
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
                   help="Load preset from ~/.heicshift/presets/NAME.json before applying other flags")
    p.add_argument("--list-presets", action="store_true",
                   help="List available presets (built-ins + ~/.heicshift/presets/*.json) and exit")
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
    p.add_argument("--register-shell", action="store_true",
                   help="Install OS shell integration: Windows Explorer right-click menu "
                        "or Linux .desktop file. macOS prints Automator recipe. Then exit.")
    p.add_argument("--unregister-shell", action="store_true",
                   help="Remove the shell integration installed by --register-shell")
    p.add_argument("--use-cache", action="store_true",
                   help="Use ~/.cache/heicshift/seen.sqlite to skip files whose (source-hash, "
                        "preset-hash) was successfully converted before. Use when re-running "
                        "the same batch.")
    p.add_argument("--clear-cache", action="store_true",
                   help="Delete ~/.cache/heicshift/seen.sqlite and exit")
    p.add_argument("--resume", action="store_true",
                   help="Resume a previously interrupted batch from ~/.cache/heicshift/queue.json")
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
    return p


def _log_dep_versions_cli():
    """Print dependency versions to stdout for CLI mode."""
    from PIL import __version__ as pil_ver
    heif_ver = getattr(pillow_heif, "__version__", "unknown")
    print(f"Pillow {pil_ver}, pillow-heif {heif_ver}")
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
    except Exception:
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
    except Exception:
        pass


def _load_queue_state() -> dict | None:
    if not QUEUE_STATE_PATH.is_file():
        return None
    try:
        return json.loads(QUEUE_STATE_PATH.read_text())
    except Exception:
        return None


def _clear_queue_state():
    try:
        QUEUE_STATE_PATH.unlink(missing_ok=True)
    except Exception:
        pass


def _watch_directory(args, input_dir: Path, output_dir: Path) -> int:
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
        from concurrent.futures import ThreadPoolExecutor
        pool = ThreadPoolExecutor(max_workers=args.workers)
        while True:
            current = []
            try:
                for p in input_dir.rglob("*") if args.recursive else input_dir.iterdir():
                    if not p.is_file() or p.suffix.lower() not in supported:
                        continue
                    if p in converted:
                        continue
                    try:
                        size = p.stat().st_size
                    except OSError:
                        continue
                    if seen_sizes.get(p) == size:
                        current.append(p)  # stable - ready to convert
                    else:
                        seen_sizes[p] = size  # still being written
            except OSError:
                pass

            for f in current:
                seq = len(converted) + 1
                try:
                    r = convert_file(
                        f, output_dir, args.format, args.quality,
                        not args.strip_metadata, not args.no_structure, input_dir,
                        args.in_place, args.skip_existing, "none", 1920,
                        args.prefix, args.suffix, args.lossless, args.progressive,
                        args.chroma_420, args.srgb, args.tiff_compression, args.png_level,
                        not args.no_exiftool, getattr(args, "template", None), seq,
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
        pool.shutdown(wait=False)
    return EXIT_OK


def _install_shell_integration(uninstall: bool = False) -> int:
    """Install / uninstall OS-level shell integration.

    Windows : adds 'Convert with HEICShift' to the Explorer right-click menu
              for image files via HKCU\\Software\\Classes\\* registry keys.
    macOS   : prints the Automator Quick Action recipe (manual; safer than
              auto-installing into ~/Library/Services).
    Linux   : writes ~/.local/share/applications/heicshift.desktop +
              ~/.local/share/file-manager/actions/heicshift.desktop.
    """
    system = platform.system()
    exe = sys.executable
    script = str(Path(__file__).resolve())
    cmd_args = f'"{exe}" "{script}" --input "%1"'

    if system == "Windows":
        try:
            import winreg
        except ImportError:
            print("[shell-integration] winreg unavailable.", file=sys.stderr)
            return EXIT_DEP_MISSING
        root = winreg.HKEY_CURRENT_USER
        base = r"Software\Classes\*\shell\HEICShift"
        cmd_key = base + r"\command"
        if uninstall:
            try:
                winreg.DeleteKey(root, cmd_key)
            except FileNotFoundError:
                pass
            try:
                winreg.DeleteKey(root, base)
                print("[shell-integration] removed.")
            except FileNotFoundError:
                pass
            return EXIT_OK
        try:
            with winreg.CreateKeyEx(root, base, 0, winreg.KEY_SET_VALUE) as k:
                winreg.SetValueEx(k, "", 0, winreg.REG_SZ, "Convert with HEICShift")
                winreg.SetValueEx(k, "Icon", 0, winreg.REG_SZ, exe)
            with winreg.CreateKeyEx(root, cmd_key, 0, winreg.KEY_SET_VALUE) as k:
                winreg.SetValueEx(k, "", 0, winreg.REG_SZ, cmd_args)
            print(f"[shell-integration] installed: HKCU\\{base}")
            return EXIT_OK
        except OSError as e:
            print(f"[shell-integration] failed: {e}", file=sys.stderr)
            return EXIT_INPUT_ERROR

    if system == "Linux":
        share = Path.home() / ".local" / "share"
        apps_dir = share / "applications"
        actions_dir = share / "file-manager" / "actions"
        if uninstall:
            for p in (apps_dir / "heicshift.desktop",
                       actions_dir / "heicshift.desktop"):
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
            "Name=Convert with HEICShift\n"
            f"Exec={exe} {script} --input %f\n"
            "MimeType=image/heic;image/heif;image/avif;image/jpeg;image/png;image/webp;image/tiff;\n"
            "Terminal=false\n"
            "Categories=Graphics;\n"
        )
        (apps_dir / "heicshift.desktop").write_text(desktop_content)
        (actions_dir / "heicshift.desktop").write_text(desktop_content)
        print(f"[shell-integration] installed at {apps_dir / 'heicshift.desktop'}")
        return EXIT_OK

    if system == "Darwin":
        print(
            "[shell-integration] macOS install:\n"
            "  1. Open Automator -> New -> Quick Action\n"
            "  2. Workflow receives: image files in Finder\n"
            "  3. Add 'Run Shell Script' action:\n"
            f"      {exe} {script} --input \"$@\"\n"
            "  4. Save as 'Convert with HEICShift'\n"
            "  Auto-install into ~/Library/Services is intentionally skipped\n"
            "  because Automator workflows are signed bundles."
        )
        return EXIT_OK

    print(f"[shell-integration] no built-in installer for {system}.", file=sys.stderr)
    return EXIT_INPUT_ERROR


def _parse_canvas(spec: str | None) -> tuple[int, int] | None:
    """Parse 'WxH' into (W, H) ints; None on bad input."""
    if not spec:
        return None
    try:
        w, h = spec.lower().split("x")
        return (int(w), int(h))
    except (ValueError, AttributeError):
        return None


def _build_quality_mode(args) -> tuple[str, float] | None:
    """Translate --target-kb / --target-psnr into the (mode, target) tuple."""
    if getattr(args, "target_kb", None) is not None:
        return ("target-kb", float(args.target_kb))
    if getattr(args, "target_psnr", None) is not None:
        return ("target-psnr", float(args.target_psnr))
    return None


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

    input_dir = Path(args.input).resolve()
    if not input_dir.is_dir():
        print(f"[ERROR] Not a directory: {input_dir}", file=sys.stderr)
        sys.exit(EXIT_INPUT_ERROR)

    if args.in_place:
        output_dir = input_dir
    elif args.output:
        output_dir = Path(args.output).resolve()
    else:
        output_dir = input_dir / "converted"

    print(f"HEICShift v{APP_VERSION} (CLI mode)")
    _log_dep_versions_cli()
    print(f"Input:  {input_dir}")
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
              "Run: heicshift --install-deps", file=sys.stderr)
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
        sys.exit(_watch_directory(args, input_dir, output_dir))

    # Scan
    print(f"\nScanning{'  recursively' if args.recursive else ''}...")
    scan = scan_directory(
        input_dir,
        recursive=args.recursive,
        exclude_patterns=getattr(args, "exclude", None) or [],
    )
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
        for f in scan.files:
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
        if cache_skipped:
            print(f"[cache] skipping {len(cache_skipped)} files seen with this preset")
        scan.files = pruned
        total = len(scan.files)
    done_paths: list[str] = []
    failed_paths: list[str] = []

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
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
            )
            futures[fut] = f

        for fut in as_completed(futures):
            result = fut.result()
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
                # Persist into hash cache for future --use-cache runs.
                if cache_conn:
                    try:
                        cache_conn.execute(
                            "INSERT OR REPLACE INTO seen "
                            "(src_hash, preset_hash, dst_hash, dst_size, ts) "
                            "VALUES (?, ?, ?, ?, ?)",
                            (_file_sha256(result.src), cache_preset_key,
                             _file_sha256(result.dst) if result.dst else "",
                             result.size_after, int(time.time())),
                        )
                    except Exception:
                        pass

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

    # CLI mode if --input is provided
    if args.input:
        _run_cli(args)
        return

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
    main()
