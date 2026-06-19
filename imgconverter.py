#!/usr/bin/env python3
"""
ImgConverter v3.1.0 - Universal image batch converter
Scans directories recursively and converts JPEG, PNG, HEIC, AVIF, WebP,
JPEG XL, RAW, TIFF, BMP, JPEG 2000, QOI, and ICO files to JPEG, PNG,
WebP, AVIF, TIFF, or JPEG XL. Auto-detects optimal format: PNG for
images with transparency, JPEG for photos. Preserves EXIF, ICC, and
XMP. CLI + GUI parity. See ROADMAP.md for in-flight work.
"""

import multiprocessing
multiprocessing.freeze_support()

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


def _is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False) or hasattr(sys, "_MEIPASS"))


def _install_deps(include_optional: bool = False) -> int:
    """Install required (and optionally optional) deps via pip. Returns exit code."""
    if _is_frozen():
        print(
            "[install-deps] Refusing to run pip from a packaged executable.\n"
            "  Install dependencies from a Python source checkout instead:\n"
            "      python -m pip install -r requirements.txt",
            file=sys.stderr,
        )
        return EXIT_INPUT_ERROR
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
import zipfile
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
    if hasattr(_opts, "DECODE_THREADS"):
        _opts.DECODE_THREADS = max(1, os.cpu_count() or 1)
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

VIPS_OUTPUT_EXTS = {
    "jpeg": ".jpg",
    "png": ".png",
    "webp": ".webp",
    "avif": ".avif",
    "tiff": ".tiff",
    "jxl": ".jxl",
}


def _vips_format_available(fmt: str) -> bool:
    """Return whether the installed libvips build advertises a saver for fmt."""
    if not HAS_VIPS:
        return False
    suffix = VIPS_OUTPUT_EXTS.get(fmt)
    if not suffix:
        return False
    try:
        return bool(pyvips.foreign_find_save(f"imgconverter-probe{suffix}"))
    except Exception:
        return False


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
C2PATOOL_PATH = shutil.which("c2patool")


def _verify_c2pa(path: Path) -> dict[str, object] | None:
    """Verify C2PA manifest using c2patool. Returns structured result or None."""
    if not C2PATOOL_PATH:
        return None
    try:
        proc = subprocess.run(
            [C2PATOOL_PATH, str(path), "--detailed"],
            capture_output=True, text=True, timeout=15,
        )
        if proc.returncode == 0:
            try:
                manifest = json.loads(proc.stdout)
                claim_gen = None
                if isinstance(manifest, dict):
                    active = manifest.get("active_manifest")
                    if active and isinstance(manifest.get("manifests"), dict):
                        am = manifest["manifests"].get(active, {})
                        claim_gen = am.get("claim_generator")
                return {
                    "status": "verified",
                    "claim_generator": _redact_text(claim_gen) if claim_gen else None,
                    "manifest_count": len(manifest.get("manifests", {})) if isinstance(manifest, dict) else 0,
                }
            except (json.JSONDecodeError, KeyError):
                return {"status": "verified", "claim_generator": None, "manifest_count": 0}
        elif "no claim found" in (proc.stderr or proc.stdout or "").lower():
            return {"status": "no-manifest"}
        else:
            return {
                "status": "invalid",
                "error": _redact_text((proc.stderr or proc.stdout or "c2patool error").strip()[:200]),
            }
    except subprocess.TimeoutExpired:
        return {"status": "not-verified", "error": "c2patool timed out"}
    except Exception as e:
        return {"status": "not-verified", "error": _redact_text(str(e)[:200])}


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


def build_backend_info(benchmark_path: Path | None = None) -> dict[str, object]:
    """Return a structured capability report for conversion backends."""
    backends: dict[str, dict[str, object]] = {
        "pillow": {
            "available": True,
            "default": True,
            "status": "stable",
            "memory_behavior": "Whole-image decode/process/write; safest fidelity path.",
            "features": {
                "metadata": {"supported": True, "note": "EXIF/ICC/XMP via Pillow; fuller tag copy when ExifTool is available"},
                "resize": {"supported": True, "note": "Lanczos resize and scale modes"},
                "watermark": {"supported": True, "note": "Text or PNG overlay"},
                "canvas": {"supported": True, "note": "Padding with transparent, hex, or named color"},
                "tone_map": {"supported": True, "note": "numpy-backed HDR tone mapping"},
                "icc": {"supported": True, "note": "ICC passthrough, sRGB conversion, and override"},
                "avif": {"supported": HAS_AVIF, "note": "Pillow native AVIF encoder"},
                "jxl": {"supported": HAS_JXL, "note": "Requires pillow-jxl-plugin"},
            },
            "formats": {
                "jpeg": True,
                "png": True,
                "webp": True,
                "avif": HAS_AVIF,
                "tiff": True,
                "jxl": HAS_JXL,
            },
        },
        "vips": {
            "available": HAS_VIPS,
            "default": False,
            "status": "experimental",
            "memory_behavior": "Tile/stream oriented; intended for huge images when fidelity extras are not needed.",
            "features": {
                "metadata": {"supported": False, "note": "Current integration requires --strip-metadata to acknowledge loss"},
                "resize": {"supported": False, "note": "Not wired through ImgConverter's resize pipeline"},
                "watermark": {"supported": False, "note": "Not wired through ImgConverter's watermark pipeline"},
                "canvas": {"supported": False, "note": "Not wired through ImgConverter's canvas pipeline"},
                "tone_map": {"supported": False, "note": "Not wired through ImgConverter's HDR tone-map pipeline"},
                "icc": {"supported": False, "note": "No ICC override or sRGB conversion in the vips fast path"},
                "avif": {"supported": _vips_format_available("avif"), "note": "Depends on the installed libvips/libheif build"},
                "jxl": {"supported": _vips_format_available("jxl"), "note": "Depends on the installed libvips/libjxl build"},
            },
            "formats": {
                fmt: _vips_format_available(fmt)
                for fmt in VIPS_OUTPUT_EXTS
            },
        },
    }
    report: dict[str, object] = {
        "version": APP_VERSION,
        "backends": backends,
        "native_codecs": _native_codec_versions(),
        "benchmark": None,
    }
    if benchmark_path is not None:
        report["benchmark"] = _benchmark_backends(Path(benchmark_path))
    return report


def _benchmark_backends(src: Path) -> dict[str, object]:
    src = Path(src).expanduser().resolve()
    if not src.is_file():
        raise OSError(f"benchmark input is not a file: {src}")
    results: dict[str, object] = {}
    for backend in ("pillow", "vips"):
        if backend == "vips" and not HAS_VIPS:
            results[backend] = {"available": False, "status": "unavailable"}
            continue
        if backend == "vips" and not _vips_format_available("jpeg"):
            results[backend] = {"available": False, "status": "jpeg saver unavailable"}
            continue
        with tempfile.TemporaryDirectory(prefix=f"imgconverter-{backend}-bench-") as td:
            out_dir = Path(td)
            t0 = time.perf_counter()
            result = convert_file(
                src,
                out_dir,
                fmt="jpeg",
                jpeg_quality=90,
                preserve_metadata=False,
                backend=backend,
            )
            elapsed = time.perf_counter() - t0
            results[backend] = {
                "available": True,
                "status": "ok" if result.success else ("skipped" if result.skipped else "failed"),
                "elapsed_seconds": round(elapsed, 4),
                "output_bytes": result.size_after,
                "error": result.error,
                "warnings": result.warnings,
            }
    return {
        "input": _redact_text(str(src)),
        "format": "jpeg",
        "quality": 90,
        "backends": results,
    }


# Plugin system — drop trusted .py files into ~/.imgconverter/plugins/ defining
# a top-level register(opts) callable. Decoder / Encoder hook signatures are
# documented in PLUGINS.md.
PLUGIN_TRUST_SCHEMA = 1
PLUGIN_TRUST_FILE = "trusted-plugins.json"
PLUGIN_DECODERS: dict[str, object] = {}
PLUGIN_ENCODERS: dict[str, object] = {}
PLUGIN_STORAGE: dict[str, object] = {}
PLUGIN_CAPABILITIES: dict[str, dict[str, list[str]]] = {}


def _reset_plugin_registry():
    PLUGIN_DECODERS.clear()
    PLUGIN_ENCODERS.clear()
    PLUGIN_STORAGE.clear()
    PLUGIN_CAPABILITIES.clear()


def _as_plugin_list(value) -> list:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return list(value)
    return [value]


def _register_plugin_capabilities(plugin_name: str, payload) -> dict[str, list[str]]:
    """Validate and register Decoder/Encoder/Storage objects returned by register()."""
    summary = {"decoders": [], "encoders": [], "storage": []}
    if not payload:
        return summary
    if not isinstance(payload, dict):
        raise ValueError("register() must return a dict or None")

    for decoder in _as_plugin_list(payload.get("decoders")):
        extensions = getattr(decoder, "extensions", None)
        open_fn = getattr(decoder, "open", None)
        if not extensions or not callable(open_fn):
            raise ValueError("decoder must expose extensions and open(src)")
        for ext in extensions:
            ext = str(ext).strip().lower()
            if not ext:
                continue
            if not ext.startswith("."):
                ext = f".{ext}"
            PLUGIN_DECODERS[ext] = decoder
            summary["decoders"].append(ext)

    for encoder in _as_plugin_list(payload.get("encoders")):
        fmt = str(getattr(encoder, "fmt", "")).strip().lower()
        extension = str(getattr(encoder, "extension", "")).strip().lower()
        save_fn = getattr(encoder, "save", None)
        if not fmt or not extension or not callable(save_fn):
            raise ValueError("encoder must expose fmt, extension, and save(img, path, options)")
        if not extension.startswith("."):
            extension = f".{extension}"
        setattr(encoder, "extension", extension)
        PLUGIN_ENCODERS[fmt] = encoder
        summary["encoders"].append(fmt)

    for storage in _as_plugin_list(payload.get("storage")):
        scheme = str(getattr(storage, "scheme", "")).strip().lower().rstrip(":")
        write_fn = getattr(storage, "write", None)
        if not scheme or not callable(write_fn):
            raise ValueError("storage must expose scheme and write(src, dst_uri)")
        PLUGIN_STORAGE[scheme] = storage
        summary["storage"].append(scheme)

    PLUGIN_CAPABILITIES[plugin_name] = {
        key: sorted(set(values)) for key, values in summary.items() if values
    }
    return summary


def get_plugin_capability_summary() -> str:
    parts = []
    if PLUGIN_DECODERS:
        parts.append("Plugin decoders " + ", ".join(sorted(PLUGIN_DECODERS)))
    if PLUGIN_ENCODERS:
        parts.append("Plugin encoders " + ", ".join(sorted(PLUGIN_ENCODERS)))
    if PLUGIN_STORAGE:
        parts.append("Plugin storage " + ", ".join(f"{s}://" for s in sorted(PLUGIN_STORAGE)))
    return "; ".join(parts)


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


def get_plugin_trust_rows() -> list[dict]:
    """Return plugin trust inventory without importing or executing plugins."""
    plugin_dir = _plugin_dir()
    records = _load_plugin_trust()
    rows: list[dict] = []
    seen = set()
    if plugin_dir.is_dir():
        for py in sorted(plugin_dir.glob("*.py")):
            seen.add(py.name)
            status, detail = _plugin_trust_status(py, records)
            digest = ""
            if py.is_file() and not py.is_symlink():
                try:
                    digest = _file_sha256(py)
                except OSError:
                    digest = ""
            rows.append({
                "name": py.name,
                "path": str(py),
                "status": status,
                "hash_prefix": digest[:12],
                "reason": "trusted file matches manifest" if status == "trusted" else detail,
            })
    for name in sorted(set(records) - seen):
        rows.append({
            "name": name,
            "path": str(plugin_dir / name),
            "status": "missing",
            "hash_prefix": str(records[name].get("sha256", ""))[:12],
            "reason": "trusted manifest entry has no file on disk",
        })
    return rows


def _load_plugins() -> list[str]:
    """Discover and import trusted user plugins. Returns loaded module names."""
    _reset_plugin_registry()
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
                    capabilities = mod.register({"app_version": APP_VERSION})
                    registered = _register_plugin_capabilities(py.stem, capabilities)
                    details = []
                    if registered.get("decoders"):
                        details.append("decoders=" + ",".join(registered["decoders"]))
                    if registered.get("encoders"):
                        details.append("encoders=" + ",".join(registered["encoders"]))
                    if registered.get("storage"):
                        details.append("storage=" + ",".join(registered["storage"]))
                    if details:
                        print(f"[plugins] {py.name}: registered {'; '.join(details)}")
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


STRIP_PRESETS = {
    "none":       frozenset(),
    "all":        frozenset({"all"}),
    "gps":        frozenset({"gps"}),
    "gps+device": frozenset({"gps", "device"}),
}

GPS_EXIF_TAGS = {
    0x0001, 0x0002, 0x0003, 0x0004, 0x0005, 0x0006, 0x0007,
    0x0008, 0x0009, 0x000A, 0x000B, 0x000C, 0x000D, 0x000E,
    0x000F, 0x0010, 0x0011, 0x0012, 0x001B, 0x001C, 0x001D, 0x001E,
}
DEVICE_EXIF_TAGS = {
    0x010F,  # Make
    0x0110,  # Model
    0x0131,  # Software
    0xA431,  # BodySerialNumber
    0xA432,  # LensSpecification
    0xA433,  # LensMake
    0xA434,  # LensModel
    0xA435,  # LensSerialNumber
    0xC614,  # UniqueCameraModel (DNG)
}


def _strip_exif_fields(exif_bytes: bytes, groups: frozenset[str]) -> bytes:
    """Remove selected tag groups from EXIF data. Returns modified EXIF bytes."""
    if not exif_bytes or not groups or "all" in groups:
        return exif_bytes
    try:
        from PIL.Image import Exif
        exif = Exif()
        exif.load(exif_bytes)

        if "gps" in groups:
            gps_ifd = exif.get_ifd(0x8825)
            if gps_ifd:
                for tag in list(gps_ifd):
                    del gps_ifd[tag]
            if 0x8825 in exif:
                del exif[0x8825]

        if "device" in groups:
            for tag in DEVICE_EXIF_TAGS:
                if tag in exif:
                    del exif[tag]

        return exif.tobytes()
    except Exception:
        return exif_bytes


def _run_exiftool_copy(src: Path, dst: Path,
                       strip_groups: frozenset[str] | None = None) -> tuple[bool, str]:
    """Copy metadata from src to dst using ExifTool, optionally excluding groups."""
    if not HAS_EXIFTOOL:
        return False, "exiftool not installed"
    try:
        cmd = [EXIFTOOL_PATH, "-overwrite_original", "-P",
               "-tagsfromfile", str(src)]

        groups = strip_groups or frozenset()
        if "all" in groups:
            return False, "strip-all: skipping exiftool copy"

        cmd.extend(["-all:all", "-unsafe", "-icc_profile"])

        if "gps" in groups:
            cmd.append("-gps:all=")
            cmd.append("-GPSLatitude=")
            cmd.append("-GPSLongitude=")
            cmd.append("-GPSAltitude=")
            cmd.append("-GPSPosition=")
        if "device" in groups:
            cmd.append("-Make=")
            cmd.append("-Model=")
            cmd.append("-Software=")
            cmd.append("-SerialNumber=")
            cmd.append("-BodySerialNumber=")
            cmd.append("-LensSerialNumber=")
            cmd.append("-LensMake=")
            cmd.append("-LensModel=")
            cmd.append("-InternalSerialNumber=")

        cmd.append(str(dst))
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if proc.returncode != 0:
            return False, (proc.stderr or proc.stdout or "exiftool failed").strip()
        suffix = ""
        if groups:
            suffix = f" (stripped: {', '.join(sorted(groups))})"
        return True, f"metadata copied{suffix}"
    except subprocess.TimeoutExpired:
        return False, "exiftool timed out"
    except Exception as e:
        return False, str(e)

_CLI_ONLY = any(a in sys.argv for a in ("--input", "-i", "--files", "--support-bundle",
                                         "--backend-info", "--backend-benchmark",
                                         "--install-deps", "--version", "--list-presets",
                                         "--list-plugins", "--help", "-h"))
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
        QDialog, QTableWidget, QTableWidgetItem,
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
    QDialog = QTableWidget = QTableWidgetItem = _Stub
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
    color: {CAT['overlay2']};
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
    color: {CAT['subtext1']};
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
    color: {CAT['subtext1']};
    border: 1px solid {CAT['surface1']};
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
QLineEdit:disabled, QComboBox:disabled, QSpinBox:disabled {{
    background-color: {CAT['crust']};
    color: {CAT['overlay2']};
    border-color: {CAT['surface0']};
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
QSlider::handle:horizontal:focus {{
    background: {CAT['blue']};
    border: 1px solid {CAT['text']};
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
QPlainTextEdit:focus {{
    border: 1px solid {CAT['blue']};
}}
QCheckBox {{
    spacing: 8px;
    color: {CAT['text']};
}}
QCheckBox:hover {{
    color: {CAT['lavender']};
}}
QCheckBox:disabled {{
    color: {CAT['subtext1']};
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
QCheckBox::indicator:focus {{
    border: 2px solid {CAT['blue']};
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
QToolButton:disabled {{
    background-color: {CAT['crust']};
    color: {CAT['overlay2']};
    border-color: {CAT['surface0']};
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

WCAG_AA_NORMAL_TEXT_CONTRAST = 4.5
STYLESHEET_READABLE_PAIRS = (
    ("QWidget", "text", "base"),
    ("QLabel#appTitle", "text", "mantle"),
    ("QLabel#appVersion", "overlay2", "mantle"),
    ("QLabel#appSubtitle", "subtext0", "mantle"),
    ("QLabel#workflowState", "lavender", "surface0"),
    ("QLabel#fieldLabel", "subtext1", "base"),
    ("QGroupBox", "lavender", "mantle"),
    ("QPushButton", "text", "surface0"),
    ("QPushButton:disabled", "overlay2", "crust"),
    ("QPushButton#primaryBtn", "crust", "blue"),
    ("QPushButton#primaryBtn:disabled", "subtext1", "surface1"),
    ("QPushButton#stopBtn", "crust", "red"),
    ("QPushButton#stopBtn:disabled", "subtext1", "surface1"),
    ("QLineEdit", "text", "surface0"),
    ("QComboBox", "text", "surface0"),
    ("QSpinBox", "text", "surface0"),
    ("QLineEdit/QComboBox/QSpinBox:disabled", "overlay2", "crust"),
    ("QProgressBar", "text", "surface0"),
    ("QPlainTextEdit", "subtext0", "crust"),
    ("QCheckBox", "text", "base"),
    ("QCheckBox:disabled", "subtext1", "base"),
    ("QLabel#dimLabel", "overlay2", "base"),
    ("QLabel#statValue", "green", "base"),
    ("QLabel#statLabel", "overlay2", "base"),
    ("QStatusBar", "subtext0", "mantle"),
    ("QToolButton", "text", "surface0"),
    ("QToolButton:disabled", "overlay2", "crust"),
    ("QToolButton#advancedToggle", "subtext1", "mantle"),
    ("QMenu", "text", "surface0"),
    ("QMenu::item:selected", "lavender", "surface1"),
)
STYLESHEET_FOCUS_SELECTORS = (
    "QPushButton:focus",
    "QLineEdit:focus",
    "QComboBox:focus",
    "QSpinBox:focus",
    "QSlider::handle:horizontal:focus",
    "QPlainTextEdit:focus",
    "QCheckBox::indicator:focus",
    "QToolButton:focus",
)


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


def get_format_families() -> dict[str, tuple[set[str], bool]]:
    families = {name: (set(exts), available) for name, (exts, available) in FORMAT_FAMILIES.items()}
    if PLUGIN_DECODERS:
        families["Plugins"] = (set(PLUGIN_DECODERS), True)
    return families


def get_supported_extensions() -> set[str]:
    """Return all input extensions we can currently decode."""
    exts = JPEG_EXTS | PNG_EXTS | HEIC_EXTS | AVIF_EXTS | WEBP_EXTS | TIFF_EXTS | BMP_EXTS | JP2_EXTS | ICO_EXTS
    if HAS_JXL:
        exts |= JXL_EXTS
    if HAS_RAWPY:
        exts |= RAW_EXTS
    if HAS_QOI:
        exts |= QOI_EXTS
    exts |= set(PLUGIN_DECODERS)
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
    if plugin_summary := get_plugin_capability_summary():
        families.append(plugin_summary)
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
    metadata_report: dict = field(default_factory=dict)

@dataclass
class ScanResult:
    files: list[Path] = field(default_factory=list)
    total_size: int = 0
    elapsed: float = 0.0


@dataclass
class ConvertOptions:
    """Validated conversion-options boundary.

    Every execution surface (GUI, CLI, watch, presets) builds one of these.
    ``convert_file()`` accepts it directly. Adding a field here without
    wiring it through every surface will break the parity test.
    """
    fmt: str = "auto"
    jpeg_quality: int = 92
    preserve_metadata: bool = True
    preserve_structure: bool = False
    base_dir: Path | None = None
    in_place: bool = False
    skip_existing: bool = False
    resize_mode: str = "none"
    resize_value: int = 1920
    prefix: str = ""
    suffix: str = ""
    lossless_webp: bool = False
    progressive_jpeg: bool = False
    chroma_subsampling: bool = False
    convert_to_srgb: bool = False
    tiff_compression: str = "none"
    png_compress_level: int = 6
    use_exiftool: bool = True
    name_template: str | None = None
    only_if_smaller_pct: float | None = None
    dpi: tuple[int, int] | None = None
    icc_override: str | None = None
    emit_xmp_sidecar: bool = False
    recompress_lossless: bool = False
    quality_mode: tuple[str, float] | None = None
    watermark: str | None = None
    canvas: tuple[int, int] | None = None
    canvas_bg: str = "transparent"
    tone_map: str = "none"
    avif_speed: int = 6
    avif_codec: str = "auto"
    png_lossy: bool = False
    backend: str = "pillow"
    strip_fields: frozenset[str] = field(default_factory=frozenset)


def _same_resolved_path(left: Path, right: Path) -> bool:
    """Best-effort equality for paths that may not exist yet."""
    try:
        return left.resolve(strict=False) == right.resolve(strict=False)
    except OSError:
        return left.absolute() == right.absolute()


def _parse_size_spec(spec: str) -> int | None:
    """Parse a human-readable size like '500MB' or '2GB' to bytes."""
    if not spec:
        return None
    spec = spec.strip().upper()
    multipliers = {"B": 1, "KB": 1024, "MB": 1024**2, "GB": 1024**3, "TB": 1024**4}
    for suffix, mult in sorted(multipliers.items(), key=lambda x: -len(x[0])):
        if spec.endswith(suffix):
            try:
                value = int(float(spec[:-len(suffix)].strip()) * mult)
                return value if value > 0 else None
            except ValueError:
                return None
    try:
        value = int(spec)
        return value if value > 0 else None
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


METADATA_KINDS = ("exif", "icc", "xmp", "iptc", "makernotes", "c2pa")


def _file_contains_marker(path: Path | None, marker: bytes, limit: int = 2 * 1024 * 1024) -> bool:
    if path is None:
        return False
    try:
        with path.open("rb") as fp:
            return marker.lower() in fp.read(limit).lower()
    except OSError:
        return False


def _metadata_presence_from_image(
    img: Image.Image | None,
    meta: dict | None = None,
    path: Path | None = None,
) -> dict[str, bool]:
    meta = meta or {}
    info = getattr(img, "info", {}) if img is not None else {}
    presence = {kind: False for kind in METADATA_KINDS}
    exif_obj = None
    try:
        exif_obj = img.getexif() if img is not None else None
    except Exception:
        exif_obj = None
    presence["exif"] = bool(meta.get("exif") or info.get("exif") or exif_obj)
    presence["icc"] = bool(meta.get("icc_profile") or info.get("icc_profile"))
    presence["xmp"] = bool(
        meta.get("xmp") or info.get("xmp") or info.get("XML:com.adobe.xmp")
    )
    presence["iptc"] = bool(meta.get("iptc") or info.get("iptc") or info.get("photoshop"))
    maker = meta.get("makernotes")
    if not maker and exif_obj:
        try:
            maker = exif_obj.get(0x927C)  # MakerNote
        except Exception:
            maker = None
    presence["makernotes"] = bool(maker)
    presence["c2pa"] = bool(meta.get("c2pa")) or _file_contains_marker(path, b"c2pa")
    return presence


def _metadata_presence_from_path(path: Path | None) -> dict[str, bool]:
    if path is None or not path.exists():
        return {kind: False for kind in METADATA_KINDS}
    try:
        with Image.open(str(path)) as img:
            return _metadata_presence_from_image(img, path=path)
    except Exception:
        return {
            **{kind: False for kind in METADATA_KINDS},
            "c2pa": _file_contains_marker(path, b"c2pa"),
        }


def _finalize_metadata_report(result: ConvertResult, after: dict[str, bool],
                              preserve_metadata: bool, src: Path | None = None,
                              dst: Path | None = None):
    before = result.metadata_report.get("before") or {kind: False for kind in METADATA_KINDS}
    dropped = [kind for kind in METADATA_KINDS if before.get(kind) and not after.get(kind)]

    c2pa_result = None
    if before.get("c2pa") and C2PATOOL_PATH and src:
        c2pa_result = _verify_c2pa(src)

    result.metadata_report = {
        "before": before,
        "after": after,
        "dropped": dropped,
        "preserve_requested": bool(preserve_metadata),
    }
    if c2pa_result:
        result.metadata_report["c2pa_verification"] = c2pa_result
    if preserve_metadata and dropped:
        result.warnings.append("metadata dropped: " + ", ".join(dropped))


def _open_image(src: Path) -> tuple[Image.Image, dict]:
    """Open an image file, routing to the correct decoder.

    Returns (PIL Image, metadata_dict).
    metadata_dict contains 'exif', 'icc_profile', 'xmp' when available.
    """
    suffix = src.suffix.lower()
    meta = {}

    if suffix in PLUGIN_DECODERS:
        opened = PLUGIN_DECODERS[suffix].open(src)
        if isinstance(opened, tuple):
            img, plugin_meta = opened
            return img, dict(plugin_meta or {})
        return opened, meta

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
    if iptc := img.info.get("iptc") or img.info.get("photoshop"):
        meta["iptc"] = iptc
    try:
        maker = img.getexif().get(0x927C)
        if maker:
            meta["makernotes"] = maker
    except Exception:
        pass
    if _file_contains_marker(src, b"c2pa"):
        meta["c2pa"] = True

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


def _convert_file_vips(
    src: Path,
    output_dir: Path,
    fmt: str,
    jpeg_quality: int,
    preserve_structure: bool,
    base_dir: Path | None,
    in_place: bool,
    skip_existing: bool,
    prefix: str,
    suffix: str,
) -> ConvertResult:
    """Quality-only libvips fast path. Feature validation happens before use."""
    t0 = time.perf_counter()
    try:
        size_before = src.stat().st_size
    except OSError:
        size_before = 0
    result = ConvertResult(src=src, size_before=size_before)
    temp_path: Path | None = None
    try:
        fmt_key = str(fmt).lower()
        ext = VIPS_OUTPUT_EXTS.get(fmt_key)
        if not ext:
            raise RuntimeError(f"vips backend requires an explicit format, got {fmt}")
        if not _vips_format_available(fmt_key):
            raise RuntimeError(f"vips backend cannot save {fmt_key} with this libvips build")

        if in_place:
            dest_dir = src.parent
        elif preserve_structure and base_dir:
            try:
                rel = src.parent.relative_to(base_dir)
            except ValueError:
                rel = Path()
            dest_dir = output_dir / rel
        else:
            dest_dir = output_dir
        dest_dir.mkdir(parents=True, exist_ok=True)
        stem = prefix + src.stem + suffix
        out_path = dest_dir / (stem + ext)

        same_output_as_source = in_place and _same_resolved_path(out_path, src)
        if skip_existing and out_path.exists() and not same_output_as_source:
            result.skipped = True
            result.dst = out_path
            result.size_after = out_path.stat().st_size
            result.elapsed = time.perf_counter() - t0
            return result

        counter = 1
        while out_path.exists() and not same_output_as_source:
            out_path = dest_dir / f"{stem}_{counter}{ext}"
            counter += 1

        write_path = out_path
        if in_place:
            fd, tmp_str = tempfile.mkstemp(
                suffix=f".imgconverter.tmp{ext}", dir=str(out_path.parent),
            )
            os.close(fd)
            temp_path = Path(tmp_str)
            write_path = temp_path

        ok, msg = _vips_convert(src, write_path, fmt_key, jpeg_quality)
        if not ok:
            raise RuntimeError(msg)
        if not write_path.exists() or write_path.stat().st_size == 0:
            raise RuntimeError(f"Output file missing or empty: {write_path.name}")
        try:
            with Image.open(str(write_path)) as verify_img:
                verify_img.verify()
            with Image.open(str(write_path)) as verify_img:
                verify_img.load()
        except Exception as e:
            raise RuntimeError(f"Output validation failed: {e}")

        if in_place and temp_path is not None:
            os.replace(str(temp_path), str(out_path))
            temp_path = None
            if not _same_resolved_path(out_path, src) and src.exists():
                try:
                    src.unlink()
                    result.src_deleted = True
                except OSError as e:
                    result.warnings.append(f"source delete failed after vips conversion: {e}")

        result.dst = out_path
        result.success = True
        result.size_after = out_path.stat().st_size
        result.elapsed = time.perf_counter() - t0
        result.metadata_report = {
            "before": {kind: False for kind in METADATA_KINDS},
            "after": {kind: False for kind in METADATA_KINDS},
            "dropped": [],
            "preserve_requested": False,
        }
        result.warnings.append("backend: vips quality-only conversion; metadata and advanced transforms not applied")
        return result
    except Exception as e:
        result.error = str(e)
        result.elapsed = time.perf_counter() - t0
        return result
    finally:
        if temp_path is not None:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass


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
    backend: str = "pillow",
    strip_fields: frozenset[str] | None = None,
    *,
    opts: "ConvertOptions | None" = None,
) -> ConvertResult:
    """Convert a single image file. Thread-safe.

    Accepts either individual keyword arguments (legacy) or a single
    ``opts=ConvertOptions(...)`` object. When ``opts`` is provided,
    its fields take precedence over positional defaults.
    """
    if opts is not None:
        fmt = opts.fmt
        jpeg_quality = opts.jpeg_quality
        preserve_metadata = opts.preserve_metadata
        preserve_structure = opts.preserve_structure
        base_dir = opts.base_dir
        in_place = opts.in_place
        skip_existing = opts.skip_existing
        resize_mode = opts.resize_mode
        resize_value = opts.resize_value
        prefix = opts.prefix
        suffix = opts.suffix
        lossless_webp = opts.lossless_webp
        progressive_jpeg = opts.progressive_jpeg
        chroma_subsampling = opts.chroma_subsampling
        convert_to_srgb = opts.convert_to_srgb
        tiff_compression = opts.tiff_compression
        png_compress_level = opts.png_compress_level
        use_exiftool = opts.use_exiftool
        name_template = opts.name_template
        only_if_smaller_pct = opts.only_if_smaller_pct
        dpi = opts.dpi
        icc_override = opts.icc_override
        emit_xmp_sidecar = opts.emit_xmp_sidecar
        recompress_lossless = opts.recompress_lossless
        quality_mode = opts.quality_mode
        watermark = opts.watermark
        canvas = opts.canvas
        canvas_bg = opts.canvas_bg
        tone_map = opts.tone_map
        avif_speed = opts.avif_speed
        avif_codec = opts.avif_codec
        png_lossy = opts.png_lossy
        backend = opts.backend
        strip_fields = opts.strip_fields
    if backend == "vips":
        return _convert_file_vips(
            src=src,
            output_dir=output_dir,
            fmt=fmt,
            jpeg_quality=jpeg_quality,
            preserve_structure=preserve_structure,
            base_dir=base_dir,
            in_place=in_place,
            skip_existing=skip_existing,
            prefix=prefix,
            suffix=suffix,
        )

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
        result.metadata_report = {
            "before": _metadata_presence_from_image(img, meta, src),
            "after": {kind: False for kind in METADATA_KINDS},
            "dropped": [],
            "preserve_requested": bool(preserve_metadata),
        }

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
        plugin_encoder = None
        fmt_key = str(fmt).lower()
        if fmt_key in PLUGIN_ENCODERS:
            plugin_encoder = PLUGIN_ENCODERS[fmt_key]
            out_fmt = fmt_key
        elif fmt == "auto":
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
            raise RuntimeError(f"Unsupported output format: {fmt}")

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
            or (plugin_encoder is not None and src_ext == getattr(plugin_encoder, "extension", ""))
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
            same_output_as_source = in_place and _same_resolved_path(out_path, src)
            if skip_existing and out_path.exists() and not same_output_as_source:
                result.skipped = True
                result.dst = out_path
                result.size_after = out_path.stat().st_size
                result.elapsed = time.perf_counter() - t0
                return result
            if same_output_as_source:
                ok, tool = False, "same-path in-place recompress uses standard re-encode"
            else:
                ok, tool = _recompress_jpeg_lossless(src, out_path, not preserve_metadata)
            if ok:
                result.dst = out_path
                result.size_after = out_path.stat().st_size
                result.success = True
                result.warnings.append(f"recompress: pixel-lossless via {tool}")
                if in_place and result.success and not _same_resolved_path(out_path, src):
                    src.unlink()
                    result.src_deleted = True
                result.elapsed = time.perf_counter() - t0
                return result
            else:
                result.warnings.append(
                    f"recompress: {tool}; falling back to standard re-encode"
                )

        ext_map = {"JPEG": ".jpg", "PNG": ".png", "WEBP": ".webp", "AVIF": ".avif", "TIFF": ".tiff", "JXL": ".jxl"}
        ext = getattr(plugin_encoder, "extension", None) if plugin_encoder is not None else ext_map.get(out_fmt, ".jpg")

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
        same_output_as_source = in_place and _same_resolved_path(out_path, src)
        if skip_existing and out_path.exists() and not same_output_as_source:
            result.skipped = True
            result.dst = out_path
            result.size_after = out_path.stat().st_size
            result.elapsed = time.perf_counter() - t0
            return result

        # Handle name collisions
        counter = 1
        collision_dir = out_path.parent
        while out_path.exists() and not same_output_as_source:
            out_path = collision_dir / f"{stem}_{counter}{ext}"
            counter += 1

        # Gather metadata
        save_kwargs = {}
        _active_strip = strip_fields or frozenset()
        if preserve_metadata and meta:
            if "exif" in meta:
                exif_data = meta["exif"]
                if _active_strip and "all" not in _active_strip:
                    exif_data = _strip_exif_fields(exif_data, _active_strip)
                save_kwargs["exif"] = exif_data
            if "icc_profile" in meta:
                save_kwargs["icc_profile"] = meta["icc_profile"]
            if "xmp" in meta and out_fmt in ("JPEG", "WEBP", "TIFF", "AVIF", "JXL"):
                save_kwargs["xmp"] = meta["xmp"]
            if _active_strip:
                result.warnings.append(
                    f"metadata: selectively stripped {', '.join(sorted(_active_strip))}"
                )

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
            # Pillow's AVIF encoder is 8-bit; warn instead of implying high
            # bit-depth preservation for 10/12-bit HEIC sources.
            bit_depth = meta.get("bit_depth")
            if bit_depth and bit_depth > 8:
                result.warnings.append(
                    f"avif: Pillow AVIF output is 8-bit; source is {bit_depth}-bit. "
                    "Use JPEG XL to preserve high bit depth."
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
        plugin_save_options = None
        if plugin_encoder is not None:
            plugin_save_options = dict(save_kwargs)
            plugin_save_options.setdefault("quality", jpeg_quality)
            plugin_save_options.update({
                "fmt": out_fmt,
                "extension": ext,
                "preserve_metadata": preserve_metadata,
                "source": str(src),
            })

        # Atomic write: use temp file for in-place mode
        if in_place:
            fd, tmp_str = tempfile.mkstemp(
                suffix=".imgconverter.tmp", dir=str(out_path.parent),
            )
            os.close(fd)
            temp_path = Path(tmp_str)
            if plugin_encoder is not None:
                plugin_encoder.save(img, temp_path, dict(plugin_save_options or {}))
                result.warnings.append(f"plugin encoder: {out_fmt}")
            else:
                img.save(str(temp_path), out_fmt, **save_kwargs)
        else:
            if plugin_encoder is not None:
                plugin_encoder.save(img, out_path, dict(plugin_save_options or {}))
                result.warnings.append(f"plugin encoder: {out_fmt}")
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
            if plugin_encoder is not None:
                result.warnings.append("plugin output: skipped Pillow integrity decode")
            else:
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
            ok, msg = _run_exiftool_copy(src, tagcopy_target,
                                         strip_groups=_active_strip)
            if ok:
                result.warnings.append(f"metadata: exiftool tag-copy ok ({msg})")
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

        _finalize_metadata_report(
            result,
            _metadata_presence_from_path(out_path),
            preserve_metadata,
            src=src,
            dst=out_path,
        )

        # Optional quality verification — butteraugli / ffmpeg-quality-metrics.
        # Cheap shell-out; only runs when caller asked for it.
        # Note: convert_file doesn't currently expose verify_quality directly,
        # so the GUI/CLI pass it via the result.warnings post-write
        # block; see post-loop verify in _run_cli.

        _run_sidecar_hooks(src, out_path, meta, result, emit_xmp_sidecar, in_place)

        # In-place mode: delete the original after successful conversion
        if in_place and result.success and not _same_resolved_path(out_path, src):
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

    def __init__(self, files: list[Path], output_dir: Path,
                 opts: ConvertOptions, workers: int = 4,
                 frames: str = "first"):
        super().__init__()
        self.files = list(files)
        self.output_dir = Path(output_dir)
        self.opts = opts
        self.workers = workers
        self.frames = frames
        self._stop_event = threading.Event()
        self._pause_event = threading.Event()
        self._pause_event.set()

    def stop(self):
        self._stop_event.set()
        self._pause_event.set()

    def pause(self):
        self._pause_event.clear()

    def resume(self):
        self._pause_event.set()

    @property
    def is_paused(self) -> bool:
        return not self._pause_event.is_set()

    def run(self):
        results = []
        total = len(self.files)
        done = 0
        queued = 0

        self.log.emit(f"Starting conversion of {total} files with {self.workers} workers...")

        if self.frames in ("all", "animate"):
            animated_files = [f for f in self.files if count_frames(f) > 1]
            if animated_files:
                self.log.emit(
                    f"[multi-frame] {len(animated_files)} sources have >1 frame; "
                    f"--frames={self.frames} active"
                )
                for f in animated_files:
                    r = _convert_animated_or_sequence(
                        f, self.output_dir, self.opts.fmt,
                        extract_frames=(self.frames == "all"),
                        base_dir=self.opts.base_dir,
                        preserve_structure=self.opts.preserve_structure,
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

        max_inflight = self.workers * 2
        file_iter = iter(enumerate(self.files, start=1))

        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            futures: dict = {}

            def _submit_batch():
                nonlocal queued
                while len(futures) < max_inflight:
                    if self._stop_event.is_set():
                        return
                    try:
                        seq_i, f = next(file_iter)
                    except StopIteration:
                        return
                    fut = pool.submit(
                        convert_file, f, self.output_dir, seq=seq_i,
                        opts=self.opts,
                    )
                    futures[fut] = f
                    queued += 1

            _submit_batch()

            while futures:
                self._pause_event.wait()
                if self._stop_event.is_set():
                    pool.shutdown(wait=False, cancel_futures=True)
                    unqueued = total - done - len(futures)
                    self.log.emit(
                        f"Cancelled. {done} done, {len(futures)} in-flight discarded, "
                        f"{max(0, unqueued)} never queued."
                    )
                    break

                completed = []
                for fut in list(futures):
                    if fut.done():
                        completed.append(fut)
                if not completed:
                    time.sleep(0.02)
                    continue

                for fut in completed:
                    try:
                        result = fut.result()
                    except Exception as exc:
                        f = futures[fut]
                        result = ConvertResult(src=f, size_before=0)
                        result.error = str(exc)
                    del futures[fut]
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

                _submit_batch()

        self.finished_all.emit(results)


class ScanWorker(QThread):
    finished = pyqtSignal(object)  # ScanResult
    log = pyqtSignal(str)
    scan_progress = pyqtSignal(int, int, str, int)  # total_count, total_bytes, dir_path, dir_file_count

    def __init__(self, directory, recursive, extensions=None,
                 exclude_patterns=None, max_file_size=None):
        super().__init__()
        self.directory = Path(directory)
        self.recursive = recursive
        self.extensions = extensions
        self.exclude_patterns = exclude_patterns or []
        self.max_file_size = max_file_size

    def run(self):
        self.log.emit(f"Scanning {'recursively' if self.recursive else ''}: {self.directory}")
        result = scan_directory(
            self.directory, self.recursive, self.extensions,
            exclude_patterns=self.exclude_patterns,
            max_file_size=self.max_file_size,
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
PRESET_SCHEMA_VERSION = 2

FORMAT_CHOICES = ("auto", "jpeg", "png", "webp", "avif", "tiff", "jxl")
TIFF_COMPRESSION_CHOICES = ("none", "lzw", "deflate")
RESIZE_MODE_CHOICES = ("max_dim", "scale")
AVIF_CODEC_CHOICES = ("auto", "aom", "rav1e", "svt")
FRAMES_CHOICES = ("first", "all", "animate")
TONE_MAP_CHOICES = ("none", "reinhard", "hable", "clip")


def _preset_bool(value) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _preset_choice(value, choices: tuple[str, ...], default: str | None = None) -> str | None:
    if value is None:
        return default
    if isinstance(value, int) and 0 <= value < len(choices):
        return choices[value]
    normalized = str(value).strip().lower().replace("-", "_").replace(" ", "_")
    aliases = {"jpg": "jpeg", "jpeg_xl": "jxl", "jpegxl": "jxl"}
    normalized = aliases.get(normalized, normalized)
    if normalized in choices:
        return normalized
    return default


def _choice_index(value, choices: tuple[str, ...], default: int = 0) -> int:
    selected = _preset_choice(value, choices)
    return choices.index(selected) if selected in choices else default


def _split_patterns(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return [str(v).strip() for v in value if str(v).strip()]
    return [p.strip() for p in str(value).replace("\n", ";").split(";") if p.strip()]


def _resize_from_preset(preset: dict) -> tuple[bool | None, str | None, int | None, str | None]:
    if "resize" in preset and preset["resize"]:
        raw = str(preset["resize"]).strip()
        parts = raw.split(":", 1)
        if len(parts) == 2:
            mode = _preset_choice(parts[0], RESIZE_MODE_CHOICES)
            try:
                value = int(parts[1])
            except ValueError:
                value = None
            if mode and value:
                return True, mode, value, f"{mode}:{value}"
        return True, None, None, raw
    if "resize_enabled" in preset:
        enabled = _preset_bool(preset["resize_enabled"])
        if not enabled:
            return False, None, None, None
        mode = _preset_choice(preset.get("resize_mode", 0), RESIZE_MODE_CHOICES, "max_dim")
        try:
            value = int(preset.get("resize_value", 1920))
        except (TypeError, ValueError):
            value = 1920
        return True, mode, value, f"{mode}:{value}"
    return None, None, None, None


def normalize_preset(preset: dict) -> dict:
    """Normalize legacy GUI-shaped and CLI-shaped preset payloads to CLI keys."""
    norm: dict = {"schema_version": int(preset.get("schema_version", 1) or 1)}
    if "fmt" in preset or "format" in preset:
        raw_fmt = preset.get("format", preset.get("fmt"))
        fmt = _preset_choice(raw_fmt, FORMAT_CHOICES)
        if fmt is None:
            candidate = str(raw_fmt).strip().lower()
            fmt = candidate if candidate in PLUGIN_ENCODERS else "auto"
        norm["format"] = fmt
        if fmt in FORMAT_CHOICES:
            norm["fmt"] = FORMAT_CHOICES.index(fmt)
    simple_keys = {
        "quality": int,
        "workers": int,
        "prefix": str,
        "suffix": str,
        "template": str,
        "report": str,
        "max_file_size": str,
        "dpi": int,
        "icc": str,
        "watermark": str,
        "canvas": str,
        "canvas_bg": str,
        "avif_speed": int,
        "target_kb": float,
        "target_psnr": float,
        "only_if_smaller": float,
        "watch_interval": float,
        "backend": str,
    }
    for key, caster in simple_keys.items():
        if key in preset and preset[key] not in (None, ""):
            try:
                norm[key] = caster(preset[key])
            except (TypeError, ValueError):
                continue
    bool_aliases = {
        "progressive": ("progressive_jpeg", "progressive"),
        "chroma_420": ("chroma_subsampling", "chroma_420"),
        "lossless": ("lossless_webp", "lossless"),
        "srgb": ("convert_to_srgb", "srgb"),
        "in_place": ("in_place", "inplace"),
        "skip_existing": ("skip_existing",),
        "strip_metadata": ("strip_metadata",),
        "strip_gps": ("strip_gps",),
        "strip_device": ("strip_device",),
        "no_exiftool": ("no_exiftool",),
        "xmp_sidecar": ("xmp_sidecar", "emit_xmp_sidecar"),
        "recompress": ("recompress", "recompress_lossless"),
        "png_lossy": ("png_lossy",),
        "recursive": ("recursive",),
        "dry_run": ("dry_run",),
        "use_cache": ("use_cache",),
        "clear_cache": ("clear_cache",),
        "resume": ("resume",),
        "watch": ("watch",),
        "use_processes": ("use_processes",),
        "sidecar_history": ("sidecar_history",),
        "verify_quality": ("verify_quality",),
    }
    for dest, aliases in bool_aliases.items():
        for alias in aliases:
            if alias in preset:
                norm[dest] = _preset_bool(preset[alias])
                break
    if "preserve_metadata" in preset:
        norm["strip_metadata"] = not _preset_bool(preset["preserve_metadata"])
    if "metadata" in preset:
        norm["strip_metadata"] = not _preset_bool(preset["metadata"])
    if "no_structure" in preset:
        norm["no_structure"] = _preset_bool(preset["no_structure"])
    elif "preserve_structure" in preset:
        norm["no_structure"] = not _preset_bool(preset["preserve_structure"])
    elif "structure" in preset:
        norm["no_structure"] = not _preset_bool(preset["structure"])
    if "png_level" in preset or "png_compress_level" in preset:
        try:
            norm["png_level"] = int(preset.get("png_level", preset.get("png_compress_level")))
        except (TypeError, ValueError):
            pass
    if "tiff_compression" in preset:
        norm["tiff_compression"] = _preset_choice(preset["tiff_compression"], TIFF_COMPRESSION_CHOICES, "none")
    if "avif_codec" in preset:
        norm["avif_codec"] = _preset_choice(preset["avif_codec"], AVIF_CODEC_CHOICES, "auto")
    if "frames" in preset or "frames_mode" in preset:
        norm["frames"] = _preset_choice(preset.get("frames", preset.get("frames_mode")), FRAMES_CHOICES, "first")
    if "tone_map" in preset:
        norm["tone_map"] = _preset_choice(preset["tone_map"], TONE_MAP_CHOICES, "none")
    enabled, mode, value, resize = _resize_from_preset(preset)
    if enabled is not None:
        norm["resize_enabled"] = enabled
        if resize:
            norm["resize"] = resize
        if mode:
            norm["resize_mode"] = mode
            norm["resize_mode_index"] = RESIZE_MODE_CHOICES.index(mode)
        if value is not None:
            norm["resize_value"] = value
    if "exclude" in preset:
        norm["exclude"] = _split_patterns(preset["exclude"])
    return norm



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


def _redaction_replacements() -> dict[str, str]:
    replacements: dict[str, str] = {}
    try:
        home = Path.home()
        for token in {str(home), home.as_posix()}:
            if token and len(token) > 2:
                replacements[token] = "~"
    except Exception:
        pass
    for var, label in (
        ("USERPROFILE", "~"),
        ("HOME", "~"),
        ("LOCALAPPDATA", "<localappdata>"),
        ("APPDATA", "<appdata>"),
        ("TEMP", "<temp>"),
        ("TMP", "<temp>"),
    ):
        token = os.environ.get(var)
        if not token or len(token) <= 2:
            continue
        replacements[token] = label
        replacements[token.replace("\\", "/")] = label
    return dict(sorted(replacements.items(), key=lambda item: len(item[0]), reverse=True))


def _redact_text(value: str) -> str:
    """Redact local user paths from support diagnostics."""
    text = str(value)
    for token, replacement in _redaction_replacements().items():
        text = text.replace(token, replacement)
    return text


def _tail_text(path: Path, max_lines: int = 200, max_bytes: int = 128_000) -> str:
    """Read the tail of a UTF-8-ish text file without loading large logs."""
    try:
        with path.open("rb") as fp:
            fp.seek(0, os.SEEK_END)
            size = fp.tell()
            fp.seek(max(0, size - max_bytes), os.SEEK_SET)
            raw = fp.read(max_bytes)
        text = raw.decode("utf-8", errors="replace")
        return "\n".join(text.splitlines()[-max_lines:])
    except OSError:
        return ""


def _module_version(module_name: str, package_name: str) -> tuple[bool, str | None]:
    try:
        if module_name == "PyQt6":
            from PyQt6.QtCore import PYQT_VERSION_STR
            return True, PYQT_VERSION_STR
        module = importlib.import_module(module_name)
        version = getattr(module, "__version__", None)
        if version:
            return True, str(version)
        try:
            from importlib.metadata import version as package_version
            return True, package_version(package_name)
        except Exception:
            return True, None
    except Exception:
        try:
            from importlib.metadata import version as package_version
            return False, package_version(package_name)
        except Exception:
            return False, None


def _dependency_versions() -> dict[str, dict[str, object]]:
    deps: dict[str, dict[str, object]] = {}
    for module_name, (package_name, floor) in DEP_FLOORS.items():
        available, version = _module_version(module_name, package_name)
        deps[package_name] = {
            "module": module_name,
            "available": available,
            "version": version,
            "minimum": floor,
            "required": module_name in REQUIRED_DEPS,
        }
    return deps


def _native_codec_versions() -> dict[str, dict[str, object]]:
    """Detect versions of bundled native codec libraries where possible.

    These are the C/C++ libraries that actually decode untrusted image data.
    Python package versions alone don't capture the native binary that shipped.
    """
    codecs: dict[str, dict[str, object]] = {}

    try:
        info = pillow_heif.libheif_info()
        codecs["libheif"] = {
            "version": info.get("libheif"),
            "decoders": sorted(info.get("decoders", {}).keys()) if isinstance(info.get("decoders"), dict) else [],
            "encoders": sorted(info.get("encoders", {}).keys()) if isinstance(info.get("encoders"), dict) else [],
        }
        for codec_name in ("libde265", "x265", "dav1d", "aom"):
            dec = info.get("decoders", {})
            enc = info.get("encoders", {})
            if codec_name in dec or codec_name in enc:
                codecs[codec_name] = {"available": True}
    except Exception:
        codecs["libheif"] = {"version": None, "error": "libheif_info() unavailable"}

    try:
        from PIL import __version__ as pil_ver
        codecs["pillow"] = {"version": pil_ver}
        features = {}
        try:
            from PIL import features as pil_features
            for feat in ("libjpeg", "libjpeg_turbo", "zlib", "libtiff",
                         "freetype2", "littlecms2", "webp", "webp_anim",
                         "webp_mux", "xcb", "avif"):
                ver = pil_features.version(feat)
                if ver:
                    features[feat] = ver
        except Exception:
            pass
        if features:
            codecs["pillow_native"] = features
    except Exception:
        pass

    if HAS_RAWPY:
        try:
            codecs["libraw"] = {"version": getattr(rawpy, "libraw_version", None) or getattr(rawpy, "__version__", None)}
        except Exception:
            codecs["libraw"] = {"version": None}

    if HAS_JXL:
        try:
            codecs["libjxl"] = {"version": getattr(pillow_jxl, "__version__", None)}
        except Exception:
            codecs["libjxl"] = {"version": None}

    if HAS_VIPS:
        try:
            codecs["libvips"] = {"version": pyvips.version(0) * 10000 + pyvips.version(1) * 100 + pyvips.version(2)}
        except Exception:
            try:
                codecs["libvips"] = {"version": str(getattr(pyvips, "__version__", None))}
            except Exception:
                codecs["libvips"] = {"version": None}

    return codecs


def _optional_tool_status() -> dict[str, dict[str, object]]:
    tools = ("exiftool", "ffprobe", "jpegoptim", "jpegtran", "pngquant",
             "butteraugli", "ffmpeg-quality-metrics", "c2patool")
    status: dict[str, dict[str, object]] = {}
    for tool in tools:
        path = shutil.which(tool)
        status[tool] = {
            "available": path is not None,
            "path": _redact_text(path) if path else None,
        }
    return status


def _format_support_payload() -> list[dict[str, object]]:
    return [
        {
            "name": name,
            "available": bool(available),
            "extensions": sorted(exts),
        }
        for name, (exts, available) in sorted(get_format_families().items())
    ]


def _plugin_trust_payload() -> list[dict[str, object]]:
    rows = []
    for row in get_plugin_trust_rows():
        safe = dict(row)
        if "path" in safe:
            safe["path"] = _redact_text(str(safe["path"]))
        rows.append(safe)
    return rows


def _build_support_bundle_payload(settings_snapshot: dict | None = None) -> dict:
    from datetime import datetime, timezone

    return {
        "app": {
            "name": "ImgConverter",
            "version": APP_VERSION,
            "frozen": _is_frozen(),
        },
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "privacy": {
            "redacted": True,
            "source_images_included": False,
            "notes": [
                "Local user paths are redacted.",
                "No source images or converted outputs are included.",
                "Recent logs may include redacted filenames from prior app activity.",
            ],
        },
        "platform": {
            "system": platform.system(),
            "release": platform.release(),
            "version": platform.version(),
            "machine": platform.machine(),
            "python": sys.version,
            "python_executable": _redact_text(sys.executable),
            "gil": _gil_status(),
        },
        "paths": {
            "config_dir": _redact_text(str(USER_CONFIG_DIR)),
            "cache_dir": _redact_text(str(USER_CACHE_DIR)),
            "log_path": _redact_text(str(USER_LOG_PATH)),
        },
        "schemas": {
            "settings": SETTINGS_SCHEMA,
            "presets": PRESET_SCHEMA_VERSION,
            "plugin_trust": PLUGIN_TRUST_SCHEMA,
        },
        "dependencies": _dependency_versions(),
        "native_codecs": _native_codec_versions(),
        "optional_tools": _optional_tool_status(),
        "backends": build_backend_info(),
        "formats": _format_support_payload(),
        "plugins": {
            "trust_rows": _plugin_trust_payload(),
            "loaded_capabilities": PLUGIN_CAPABILITIES,
        },
        "settings": settings_snapshot or {},
    }


def export_support_bundle(
    path: Path,
    *,
    settings_snapshot: dict | None = None,
    recent_log: str | None = None,
) -> Path:
    """Write a redacted diagnostic zip for support without source images."""
    bundle_path = Path(path).expanduser()
    if bundle_path.exists() and bundle_path.is_dir():
        raise OSError(f"support bundle path is a directory: {bundle_path}")
    bundle_path.parent.mkdir(parents=True, exist_ok=True)

    payload = _build_support_bundle_payload(settings_snapshot=settings_snapshot)
    disk_log = _redact_text(_tail_text(USER_LOG_PATH))
    gui_log = _redact_text(recent_log or "")

    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{bundle_path.name}.",
        suffix=".tmp",
        dir=str(bundle_path.parent),
    )
    os.close(fd)
    tmp_path = Path(tmp_name)
    try:
        with zipfile.ZipFile(tmp_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("support.json", json.dumps(payload, indent=2, sort_keys=True))
            zf.writestr("recent-log.txt", disk_log)
            if gui_log:
                zf.writestr("gui-log.txt", gui_log)
        os.replace(tmp_path, bundle_path)
    finally:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
    return bundle_path


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
        "schema_version": PRESET_SCHEMA_VERSION,
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
        "schema_version": PRESET_SCHEMA_VERSION,
        "fmt": 2,              # PNG
        "quality": 92,
        "png_compress_level": 6,
        "resize_enabled": False,
    },
    "Mobile Friendly": {
        "schema_version": PRESET_SCHEMA_VERSION,
        "fmt": 3,              # WebP
        "quality": 75,
        "convert_to_srgb": True,
        "resize_enabled": True,
        "resize_mode": 0,      # Max Dimension
        "resize_value": 1080,
    },
    "Print / TIFF": {
        "schema_version": PRESET_SCHEMA_VERSION,
        "fmt": 5,              # TIFF
        "tiff_compression": 1, # LZW
        "resize_enabled": False,
    },
}


def _apply_preset_to_gui_controls(window, preset: dict):
    """Apply a normalized or legacy preset to MainWindow-like controls."""
    norm = normalize_preset(preset)
    if "format" in norm:
        fmt_values = getattr(window, "_fmt_values", list(FORMAT_CHOICES))
        if norm["format"] in fmt_values:
            window.fmt_combo.setCurrentIndex(fmt_values.index(norm["format"]))
    if "quality" in norm:
        window.quality_slider.setValue(norm["quality"])
    if "workers" in norm:
        window.workers_spin.setValue(norm["workers"])
    for key, attr in (
        ("progressive", "progressive_jpeg_chk"),
        ("lossless", "lossless_webp_chk"),
        ("chroma_420", "chroma_chk"),
        ("srgb", "srgb_chk"),
        ("in_place", "inplace_chk"),
        ("skip_existing", "skip_existing_chk"),
        ("xmp_sidecar", "xmp_sidecar_chk"),
        ("recompress", "recompress_chk"),
        ("png_lossy", "png_lossy_chk"),
    ):
        if key in norm:
            getattr(window, attr).setChecked(norm[key])
    if "strip_metadata" in norm:
        if norm["strip_metadata"]:
            window.meta_combo.setCurrentIndex(3)
        else:
            window.meta_combo.setCurrentIndex(0)
    if norm.get("strip_gps") and not norm.get("strip_metadata"):
        if norm.get("strip_device"):
            window.meta_combo.setCurrentIndex(2)
        else:
            window.meta_combo.setCurrentIndex(1)
    if "no_structure" in norm:
        window.structure_chk.setChecked(not norm["no_structure"])
    if "recursive" in norm:
        window.recursive_chk.setChecked(norm["recursive"])
    if "resize_enabled" in norm:
        window.resize_chk.setChecked(norm["resize_enabled"])
        if norm["resize_enabled"]:
            if "resize_mode_index" in norm:
                window.resize_combo.setCurrentIndex(norm["resize_mode_index"])
            if "resize_value" in norm:
                window.resize_spin.setValue(norm["resize_value"])
    if "tiff_compression" in norm:
        window.tiff_comp_combo.setCurrentIndex(_choice_index(norm["tiff_compression"], TIFF_COMPRESSION_CHOICES))
    if "png_level" in norm:
        window.png_level_spin.setValue(norm["png_level"])
    for key, attr in (
        ("prefix", "prefix_edit"),
        ("suffix", "suffix_edit"),
        ("template", "template_edit"),
        ("icc", "icc_edit"),
        ("watermark", "watermark_edit"),
        ("canvas", "canvas_edit"),
        ("canvas_bg", "canvas_bg_edit"),
        ("max_file_size", "max_file_size_edit"),
    ):
        if key in norm:
            getattr(window, attr).setText(norm[key])
    if "exclude" in norm:
        window.exclude_edit.setText("; ".join(norm["exclude"]))
    if "dpi" in norm:
        window.dpi_spin.setValue(norm["dpi"])
    if "avif_speed" in norm:
        window.avif_speed_spin.setValue(norm["avif_speed"])
    if "avif_codec" in norm:
        window.avif_codec_combo.setCurrentIndex(_choice_index(norm["avif_codec"], AVIF_CODEC_CHOICES))
    if "frames" in norm:
        window.frames_combo.setCurrentIndex(_choice_index(norm["frames"], FRAMES_CHOICES))
    if "tone_map" in norm:
        window.tone_map_combo.setCurrentIndex(_choice_index(norm["tone_map"], TONE_MAP_CHOICES))
    if "only_if_smaller" in norm:
        window.only_if_smaller_chk.setChecked(True)
        window.only_if_smaller_spin.setValue(int(norm["only_if_smaller"]))
    if "target_kb" in norm:
        window.target_kb_spin.setValue(int(norm["target_kb"]))


# ── Disk Space Estimation ─────────────────────────────────────────────────────

SIZE_ESTIMATE_FACTORS = {"jpeg": 0.8, "auto": 0.8, "png": 1.2, "webp": 0.7, "avif": 0.5, "tiff": 1.5, "jxl": 0.45}


def _estimate_output_size(total_input_bytes: int, fmt: str) -> int:
    """Estimate total output size based on format and input size."""
    factor = SIZE_ESTIMATE_FACTORS.get(fmt, 1.0)
    return int(total_input_bytes * factor)


class PluginTrustDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Plugin Trust")
        self.resize(820, 420)
        self._rows: list[dict] = []

        layout = QVBoxLayout(self)
        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["Plugin", "Path", "Status", "Hash", "Reason"])
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setAccessibleName("Plugin trust inventory")
        self.table.setAccessibleDescription("Installed plugin files with trust status and hash prefix")
        layout.addWidget(self.table)

        buttons = QHBoxLayout()
        self.refresh_btn = QPushButton("Refresh")
        self.trust_btn = QPushButton("Trust Selected")
        self.untrust_btn = QPushButton("Untrust Selected")
        self.close_btn = QPushButton("Close")
        buttons.addWidget(self.refresh_btn)
        buttons.addStretch()
        buttons.addWidget(self.trust_btn)
        buttons.addWidget(self.untrust_btn)
        buttons.addWidget(self.close_btn)
        layout.addLayout(buttons)

        self.refresh_btn.clicked.connect(self._refresh)
        self.trust_btn.clicked.connect(self._trust_selected)
        self.untrust_btn.clicked.connect(self._untrust_selected)
        self.close_btn.clicked.connect(self.accept)
        for button, name, desc in (
            (self.refresh_btn, "Refresh plugin trust inventory", "Reload plugin trust status from disk"),
            (self.trust_btn, "Trust selected plugin", "Record the selected plugin file hash as trusted"),
            (self.untrust_btn, "Untrust selected plugin", "Remove the selected plugin from the local trust manifest"),
            (self.close_btn, "Close plugin trust", "Close the plugin trust dialog"),
        ):
            button.setAccessibleName(name)
            button.setAccessibleDescription(desc)
            button.setStatusTip(desc)
        self._refresh()

    def _refresh(self):
        self._rows = get_plugin_trust_rows()
        self.table.setRowCount(len(self._rows))
        for row_idx, row in enumerate(self._rows):
            for col_idx, key in enumerate(("name", "path", "status", "hash_prefix", "reason")):
                self.table.setItem(row_idx, col_idx, QTableWidgetItem(str(row.get(key, ""))))
        self.table.resizeColumnsToContents()

    def _selected_row(self) -> dict | None:
        row = self.table.currentRow()
        if row < 0 or row >= len(self._rows):
            return None
        return self._rows[row]

    def _trust_selected(self):
        row = self._selected_row()
        if not row or row.get("status") == "missing":
            QMessageBox.information(self, "Plugin Trust", "Select a plugin file to trust.")
            return
        ok, msg = _trust_plugin(row["path"])
        (QMessageBox.information if ok else QMessageBox.warning)(self, "Plugin Trust", msg)
        self._refresh()

    def _untrust_selected(self):
        row = self._selected_row()
        if not row:
            QMessageBox.information(self, "Plugin Trust", "Select a plugin entry to untrust.")
            return
        ok, msg = _untrust_plugin(row["name"])
        (QMessageBox.information if ok else QMessageBox.warning)(self, "Plugin Trust", msg)
        self._refresh()


MAIN_WINDOW_ACCESSIBILITY_LABELS = (
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
    ("meta_combo",          "Metadata handling",         "Choose which metadata fields to preserve or strip"),
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
    ("exclude_edit",        "Exclude patterns",         "Semicolon-separated glob patterns to skip during scan"),
    ("max_file_size_edit",  "Maximum input file size",  "Skip files larger than this size, such as 500MB or 2GB"),
    ("xmp_sidecar_chk",     "Emit XMP sidecar",         "Write .xmp sidecar alongside output"),
    ("recompress_chk",      "Lossless JPEG recompress", "Pixel-lossless JPEG size reduction via jpegoptim/jpegtran"),
    ("only_if_smaller_chk", "Only if smaller",          "Discard output when not meaningfully smaller than input"),
    ("target_kb_spin",      "Target file size KB",      "Binary-search quality to hit a target output size in KB"),
    ("avif_codec_combo",    "AVIF codec",               "AVIF encoder: auto, aom, rav1e, or svt"),
    ("png_lossy_chk",       "Lossy PNG",                "Run pngquant for lossy PNG size reduction"),
    ("chroma_chk",          "Chroma subsampling",       "Use 4:2:0 chroma for smaller JPEG files"),
    ("srgb_chk",            "Convert to sRGB",          "Convert embedded ICC profiles to sRGB"),
    ("structure_chk",       "Preserve folder structure", "Mirror source directory layout in output"),
    ("only_if_smaller_spin","Only-if-smaller threshold", "Percentage by which output must be smaller"),
    ("png_level_spin",      "PNG compression level",    "PNG compression 1 (fast) to 9 (smallest)"),
    ("tiff_comp_combo",     "TIFF compression",         "TIFF compression: None, LZW, or Deflate"),
    ("adv_toggle",          "Advanced output controls", "Show or hide advanced output controls"),
    ("scan_btn",            "Scan source",              "Scan the selected source for supported images"),
    ("convert_btn",         "Convert batch",            "Start converting the scanned batch"),
    ("stop_btn",            "Cancel conversion",        "Stop the current conversion batch"),
    ("paste_btn",           "Paste clipboard",          "Paste an image from clipboard as input"),
    ("manage_plugins_btn",  "Plugin trust",             "Review plugin trust status and trust or untrust plugin files"),
    ("auto_open_chk",       "Auto-open output",         "Automatically open the output folder when conversion finishes"),
    ("open_output_btn",     "Open output folder",       "Open the most recent output folder"),
    ("export_log_btn",      "Export log",               "Save the conversion log as a text file"),
    ("export_csv_btn",      "Export CSV",               "Export conversion results as a CSV report"),
    ("export_support_btn",  "Export support bundle",    "Save redacted diagnostics for support"),
    ("clear_log_btn",       "Clear log",                "Clear the activity log"),
)


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
        self._init_taskbar_progress()
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
        for attr, name, desc in MAIN_WINDOW_ACCESSIBILITY_LABELS:
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

    def _init_taskbar_progress(self):
        self._taskbar_hwnd = None
        if platform.system() != "Windows":
            return
        try:
            import ctypes.wintypes
            _CoInitialize = ctypes.windll.ole32.CoInitialize
            _CoCreateInstance = ctypes.windll.ole32.CoCreateInstance
            CLSID_TaskbarList = ctypes.c_char * 16
            clsid = CLSID_TaskbarList(
                b'\x44\xf3\xfd\x56\x6d\xfd\xd0\x11\x95\x8a\x00\x60\x97\xc9\xa0\x90'
            )
            IID_ITaskbarList3 = ctypes.c_char * 16
            iid = IID_ITaskbarList3(
                b'\x02\xd3\xea\xea\x1b\xdc\xcf\x4d\x9e\xb3\xf4\x49\x55\x00\x23\x18'
            )
            _CoInitialize(None)
            tbptr = ctypes.c_void_p()
            hr = _CoCreateInstance(
                ctypes.byref(clsid), None, 1,
                ctypes.byref(iid), ctypes.byref(tbptr),
            )
            if hr == 0 and tbptr.value:
                self._taskbar_hwnd = int(self.winId())
                self._taskbar_ptr = tbptr
            else:
                self._taskbar_ptr = None
        except Exception:
            self._taskbar_ptr = None

    def _set_taskbar_progress(self, current: int, total: int):
        if not getattr(self, "_taskbar_ptr", None) or not self._taskbar_hwnd:
            return
        try:
            vt = ctypes.cast(
                ctypes.cast(self._taskbar_ptr, ctypes.POINTER(ctypes.c_void_p))[0],
                ctypes.POINTER(ctypes.c_void_p),
            )
            SetProgressValue = ctypes.CFUNCTYPE(
                ctypes.c_long, ctypes.c_void_p, ctypes.c_void_p,
                ctypes.c_ulonglong, ctypes.c_ulonglong,
            )(vt[9])
            SetProgressState = ctypes.CFUNCTYPE(
                ctypes.c_long, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int,
            )(vt[10])
            SetProgressState(self._taskbar_ptr, self._taskbar_hwnd, 0x2)
            SetProgressValue(self._taskbar_ptr, self._taskbar_hwnd, current, total)
        except Exception:
            pass

    def _clear_taskbar_progress(self):
        if not getattr(self, "_taskbar_ptr", None) or not self._taskbar_hwnd:
            return
        try:
            vt = ctypes.cast(
                ctypes.cast(self._taskbar_ptr, ctypes.POINTER(ctypes.c_void_p))[0],
                ctypes.POINTER(ctypes.c_void_p),
            )
            SetProgressState = ctypes.CFUNCTYPE(
                ctypes.c_long, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int,
            )(vt[10])
            SetProgressState(self._taskbar_ptr, self._taskbar_hwnd, 0x0)
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
        for name, (exts, available) in get_format_families().items():
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
        self._fmt_values = ["auto", "jpeg", "png", "webp", "avif", "tiff", "jxl"]
        fmt_labels = [
            "Auto (JPEG for photos, PNG for transparency)",
            "JPEG", "PNG", "WebP", "AVIF", "TIFF", "JPEG XL"
        ]
        for plugin_fmt in sorted(PLUGIN_ENCODERS):
            self._fmt_values.append(plugin_fmt)
            fmt_labels.append(f"Plugin: {plugin_fmt}")
        self.fmt_combo.addItems(fmt_labels)
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
        for idx, plugin_fmt in enumerate(self._fmt_values[7:], start=7):
            encoder = PLUGIN_ENCODERS[plugin_fmt]
            self.fmt_combo.setItemData(
                idx,
                f"Registered by trusted plugin, writes {getattr(encoder, 'extension', '')}",
                Qt.ItemDataRole.ToolTipRole,
            )
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

        meta_label = QLabel("Metadata")
        meta_label.setObjectName("fieldLabel")
        opt_grid.addWidget(meta_label, 2, 2)
        self.meta_combo = QComboBox()
        self.meta_combo.addItems([
            "Preserve All",
            "Strip GPS Only",
            "Strip GPS + Device Info",
            "Strip All",
        ])
        self.meta_combo.setToolTip(
            "Preserve All: keep EXIF/ICC/XMP intact\n"
            "Strip GPS Only: remove location data, keep copyright and color\n"
            "Strip GPS + Device Info: remove location + camera make/model/serial\n"
            "Strip All: remove all EXIF, ICC, and XMP metadata"
        )
        self.meta_combo.setAccessibleName("Metadata handling")
        self.meta_combo.setAccessibleDescription(
            "Choose which metadata fields to preserve or strip during conversion"
        )
        opt_grid.addWidget(self.meta_combo, 2, 3)

        self.meta_chk = QCheckBox("Preserve metadata")
        self.meta_chk.setVisible(False)
        self.strip_meta_chk = QCheckBox("Strip metadata")
        self.strip_meta_chk.setVisible(False)

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

        exclude_label = QLabel("Exclude")
        exclude_label.setObjectName("fieldLabel")
        adv_grid.addWidget(exclude_label, 9, 0)
        self.exclude_edit = QLineEdit()
        self.exclude_edit.setPlaceholderText("*.thumb.*; cache/**")
        self.exclude_edit.setMinimumWidth(100)
        self.exclude_edit.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self.exclude_edit.setToolTip("Semicolon-separated glob patterns to skip during scan")
        adv_grid.addWidget(self.exclude_edit, 9, 1)

        max_file_size_label = QLabel("Max file")
        max_file_size_label.setObjectName("fieldLabel")
        adv_grid.addWidget(max_file_size_label, 9, 2)
        self.max_file_size_edit = QLineEdit()
        self.max_file_size_edit.setPlaceholderText("500MB")
        self.max_file_size_edit.setMinimumWidth(100)
        self.max_file_size_edit.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self.max_file_size_edit.setToolTip("Skip files larger than this size (B, KB, MB, GB, TB)")
        adv_grid.addWidget(self.max_file_size_edit, 9, 3)

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

        self.pause_btn = QPushButton("Pause")
        self.pause_btn.setEnabled(False)
        self.pause_btn.setToolTip("Pause conversion after the current in-flight files finish")
        self.pause_btn.setAccessibleName("Pause/Resume conversion")
        self.pause_btn.clicked.connect(self._toggle_pause)
        primary_actions.addWidget(self.pause_btn)

        self.paste_btn = QPushButton("Paste Clipboard")
        self.paste_btn.setToolTip("Paste an image from the clipboard as a temporary PNG input")
        self.paste_btn.clicked.connect(self._paste_clipboard)
        primary_actions.addWidget(self.paste_btn)

        primary_actions.addStretch()

        self.manage_plugins_btn = QPushButton("Plugins")
        self.manage_plugins_btn.setToolTip("Review plugin trust status")
        self.manage_plugins_btn.clicked.connect(self._open_plugin_trust)
        secondary_actions.addWidget(self.manage_plugins_btn)

        self.auto_open_chk = QCheckBox("Auto-open output")
        self.auto_open_chk.setChecked(False)
        self.auto_open_chk.setToolTip("Automatically open the output folder when conversion finishes")
        secondary_actions.addStretch()
        secondary_actions.addWidget(self.auto_open_chk)

        when_done_label = QLabel("When done:")
        when_done_label.setObjectName("fieldLabel")
        secondary_actions.addWidget(when_done_label)
        self.when_done_combo = QComboBox()
        self.when_done_combo.addItems(["Do Nothing", "Close App", "Sleep", "Shutdown"])
        self.when_done_combo.setToolTip("Action to take after batch conversion completes")
        self.when_done_combo.setAccessibleName("When done action")
        self.when_done_combo.setFixedWidth(130)
        secondary_actions.addWidget(self.when_done_combo)

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

        self.export_support_btn = QPushButton("Support Bundle")
        self.export_support_btn.setObjectName("miniBtn")
        self.export_support_btn.setToolTip("Save redacted diagnostics for support")
        self.export_support_btn.clicked.connect(self._export_support_bundle)
        log_header.addWidget(self.export_support_btn)

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
            "meta_combo", "skip_existing_chk",
            "progressive_jpeg_chk", "lossless_webp_chk", "resize_chk",
            "resize_combo", "resize_spin", "prefix_edit", "suffix_edit",
            "chroma_chk", "srgb_chk", "tiff_comp_combo", "png_level_spin",
            "adv_toggle", "template_edit", "dpi_spin", "avif_speed_spin",
            "avif_codec_combo", "frames_combo", "tone_map_combo", "icc_edit",
            "watermark_edit", "canvas_edit", "canvas_bg_edit",
            "exclude_edit", "max_file_size_edit",
            "xmp_sidecar_chk", "recompress_chk", "only_if_smaller_chk",
            "only_if_smaller_spin", "target_kb_spin", "png_lossy_chk",
            "paste_btn", "manage_plugins_btn", "auto_open_chk", "when_done_combo",
            "export_support_btn",
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

    def _open_plugin_trust(self):
        dialog = PluginTrustDialog(self)
        dialog.exec()
        rows = get_plugin_trust_rows()
        self._log(f"Plugin trust inventory: {len(rows)} entr{'y' if len(rows) == 1 else 'ies'}")

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
        _apply_preset_to_gui_controls(self, preset)
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
    def _gui_strip_fields(self) -> frozenset[str]:
        idx = self.meta_combo.currentIndex()
        if idx == 0:
            return frozenset()
        if idx == 1:
            return frozenset({"gps"})
        if idx == 2:
            return frozenset({"gps", "device"})
        return frozenset({"all"})

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
                                 "Delta", "Elapsed (s)", "Metadata", "Warnings"])
                for r in self._results:
                    status = "OK" if r.success else ("SKIP" if r.skipped else "FAIL")
                    delta = r.size_before - r.size_after if r.success else 0
                    writer.writerow([
                        str(r.src), str(r.dst or ""), status,
                        r.size_before, r.size_after, delta,
                        f"{r.elapsed:.3f}",
                        json.dumps(r.metadata_report, sort_keys=True),
                        "; ".join(r.warnings) if r.warnings else "",
                    ])
            self._log(f"CSV report exported to {path}")

    def _support_settings_snapshot(self) -> dict[str, object]:
        fmt = self._fmt_values[self.fmt_combo.currentIndex()] if hasattr(self, "_fmt_values") else "auto"
        return {
            "format": fmt,
            "quality": self.quality_slider.value(),
            "workers": self.workers_spin.value(),
            "recursive": self.recursive_chk.isChecked(),
            "preserve_structure": self.structure_chk.isChecked(),
            "in_place": self.inplace_chk.isChecked(),
            "skip_existing": self.skip_existing_chk.isChecked(),
            "metadata_mode": ["preserve_all", "strip_gps", "strip_gps_device", "strip_all"][self.meta_combo.currentIndex()],
            "advanced_expanded": self.adv_toggle.isChecked(),
            "resize_enabled": self.resize_chk.isChecked(),
            "resize_mode": "max_dim" if self.resize_combo.currentIndex() == 0 else "scale",
            "resize_value": self.resize_spin.value(),
            "frames": ["first", "all", "animate"][self.frames_combo.currentIndex()],
            "tone_map": ["none", "reinhard", "hable", "clip"][self.tone_map_combo.currentIndex()],
            "avif_codec": ["auto", "aom", "rav1e", "svt"][self.avif_codec_combo.currentIndex()],
            "avif_speed": self.avif_speed_spin.value(),
            "png_level": self.png_level_spin.value(),
            "tiff_compression": ["none", "lzw", "deflate"][self.tiff_comp_combo.currentIndex()],
            "xmp_sidecar": self.xmp_sidecar_chk.isChecked(),
            "recompress": self.recompress_chk.isChecked(),
            "png_lossy": self.png_lossy_chk.isChecked(),
            "only_if_smaller": (
                float(self.only_if_smaller_spin.value())
                if self.only_if_smaller_chk.isChecked()
                else None
            ),
            "target_kb": float(self.target_kb_spin.value()) if self.target_kb_spin.value() > 0 else None,
            "filters": {
                ext: chk.isChecked()
                for ext, chk in sorted(getattr(self, "_format_filters", {}).items())
            },
        }

    def _export_support_bundle(self):
        path, _ = QFileDialog.getSaveFileName(
            self, "Export Support Bundle", str(Path.home() / "imgconverter_support.zip"),
            "Zip Files (*.zip);;All Files (*)"
        )
        if not path:
            return
        try:
            written = export_support_bundle(
                Path(path),
                settings_snapshot=self._support_settings_snapshot(),
                recent_log=self.log_view.toPlainText(),
            )
            self._log(f"Support bundle exported to {written}")
            self._set_workflow_state("Support bundle exported", "Redacted diagnostics were saved.")
        except OSError as e:
            self._log(f"[ERROR] Support bundle export failed: {e}")
            self._set_workflow_state("Export failed", "Support bundle could not be written.")

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

        exclude_patterns = _split_patterns(self.exclude_edit.text())
        max_file_size_text = self.max_file_size_edit.text().strip()
        max_file_size = _parse_size_spec(max_file_size_text)
        if max_file_size_text and max_file_size is None:
            self.scan_btn.setEnabled(True)
            self._log(f"[ERROR] Invalid max file size: {max_file_size_text}")
            self._set_line_error(self.max_file_size_edit, "Use a size like 500MB, 2GB, or leave it blank.")
            return
        if exclude_patterns:
            self._log(f"[FILTER] Excluding: {', '.join(exclude_patterns)}")
        if max_file_size is not None:
            self._log(f"[FILTER] Skipping files larger than {_fmt_size(max_file_size)}")

        self._scanner = ScanWorker(
            src,
            self.recursive_chk.isChecked(),
            enabled_exts,
            exclude_patterns=exclude_patterns,
            max_file_size=max_file_size,
        )
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

        fmt = self._fmt_values[self.fmt_combo.currentIndex()] if hasattr(self, "_fmt_values") else "auto"

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
        self.pause_btn.setEnabled(True)
        self.pause_btn.setText("Pause")
        self.open_output_btn.setEnabled(False)
        self._set_conversion_busy(True)
        self._set_workflow_state("Converting", "Converting batch...")

        if in_place:
            self._log("In-place mode: converted files saved next to originals, source files will be deleted")

        gui_opts = ConvertOptions(
            fmt=fmt,
            jpeg_quality=self.quality_slider.value(),
            preserve_metadata=self.meta_combo.currentIndex() != 3,
            preserve_structure=self.structure_chk.isChecked(),
            base_dir=Path(self.src_edit.text().strip()),
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
            strip_fields=self._gui_strip_fields(),
        )
        self._worker = ConvertWorker(
            files=self._scan_result.files,
            output_dir=Path(dst),
            opts=gui_opts,
            workers=self.workers_spin.value(),
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
        self._set_taskbar_progress(current, total)
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
        self.pause_btn.setEnabled(False)
        self.pause_btn.setText("Pause")
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
        self._clear_taskbar_progress()
        if self.auto_open_chk.isChecked():
            self._open_output()
        self._save_state()

        when_done_idx = self.when_done_combo.currentIndex()
        if when_done_idx > 0:
            action = ["nothing", "close", "sleep", "shutdown"][when_done_idx]
            if action in ("sleep", "shutdown"):
                countdown = QMessageBox(self)
                countdown.setIcon(QMessageBox.Icon.Warning)
                countdown.setWindowTitle(f"ImgConverter — {action.title()} in 30 seconds")
                countdown.setText(
                    f"System will {action} in 30 seconds.\n"
                    f"Click Cancel to abort."
                )
                countdown.setStandardButtons(QMessageBox.StandardButton.Cancel)
                countdown.setDefaultButton(QMessageBox.StandardButton.Cancel)
                _remaining = [30]
                def _tick():
                    _remaining[0] -= 1
                    if _remaining[0] <= 0:
                        _timer.stop()
                        countdown.done(1)
                    else:
                        countdown.setText(
                            f"System will {action} in {_remaining[0]} seconds.\n"
                            f"Click Cancel to abort."
                        )
                _timer = QTimer(self)
                _timer.timeout.connect(_tick)
                _timer.start(1000)
                result_code = countdown.exec()
                _timer.stop()
                if result_code == 1:
                    _execute_when_done(action)
            elif action == "close":
                QTimer.singleShot(500, QApplication.instance().quit)

    def _stop(self):
        if self._worker:
            self._worker.stop()
            self.stop_btn.setEnabled(False)
            self.pause_btn.setEnabled(False)
            self.pause_btn.setText("Pause")
            self._set_workflow_state("Stopping", "Stopping after the current file finishes...")

    def _toggle_pause(self):
        if not self._worker:
            return
        if self._worker.is_paused:
            self._worker.resume()
            self.pause_btn.setText("Pause")
            self._set_workflow_state("Converting", "Resumed batch conversion...")
        else:
            self._worker.pause()
            self.pause_btn.setText("Resume")
            self._set_workflow_state("Paused", "Conversion paused. Click Resume to continue.")

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
        self.settings.setValue("metadata_mode", self.meta_combo.currentIndex())
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
        # strip_metadata persisted as metadata_mode combo index now
        self.settings.setValue("auto_open_output", self.auto_open_chk.isChecked())
        self.settings.setValue("when_done", self.when_done_combo.currentIndex())
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
        self.settings.setValue("exclude", self.exclude_edit.text())
        self.settings.setValue("max_file_size", self.max_file_size_edit.text())
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
        if (n := self._safe_int(self.settings.value("metadata_mode"))) is not None:
            if 0 <= n < self.meta_combo.count():
                self.meta_combo.setCurrentIndex(n)
        elif (v := self.settings.value("metadata")) is not None:
            if v == "true" or v is True:
                self.meta_combo.setCurrentIndex(0)
            else:
                self.meta_combo.setCurrentIndex(3)
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
            if (v == "true" or v is True) and self._safe_int(self.settings.value("metadata_mode")) is None:
                self.meta_combo.setCurrentIndex(3)
        if (v := self.settings.value("auto_open_output")) is not None:
            self.auto_open_chk.setChecked(v == "true" or v is True)
        if (n := self._safe_int(self.settings.value("when_done"))) is not None:
            if 0 <= n < self.when_done_combo.count():
                self.when_done_combo.setCurrentIndex(n)
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
        if v := self.settings.value("exclude"):
            self.exclude_edit.setText(v)
        if v := self.settings.value("max_file_size"):
            self.max_file_size_edit.setText(v)
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
    p.add_argument("--strip-gps", action="store_true",
                   help="Strip GPS/location data while preserving copyright and color profiles")
    p.add_argument("--strip-device", action="store_true",
                   help="Strip camera make/model/serial numbers (combine with --strip-gps for full privacy)")
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
    p.add_argument("--stdin-files", action="store_true",
                   help="Read file paths from stdin (one per line). Mutually exclusive with --input and --files. "
                        "Use --stdin-null for NUL-delimited input (find -print0 compatible).")
    p.add_argument("--stdin-null", action="store_true",
                   help="With --stdin-files, use NUL byte as delimiter instead of newline (for find -print0)")
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
    p.add_argument("--support-bundle", type=str, default=None, metavar="PATH",
                   help="Write a redacted diagnostic zip with app, platform, dependency, "
                        "optional-tool, plugin trust, settings schema, and recent log data, then exit")
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
    p.add_argument("--backend-info", action="store_true",
                   help="Print backend capability JSON for Pillow and vips, then exit")
    p.add_argument("--backend-benchmark", type=str, default=None, metavar="PATH",
                   help="With --backend-info, benchmark each available backend against one image")
    p.add_argument("--verify-quality", action="store_true",
                   help="After each conversion, run butteraugli (preferred) or "
                        "ffmpeg-quality-metrics if available, logging PSNR/SSIM "
                        "or butteraugli score in result.warnings.")
    p.add_argument("--progress", action="store_true",
                   help="Emit JSON Lines per-file events to stderr for machine consumption. "
                        "Events: scan_start, scan_done, file_start, file_done, batch_done.")
    p.add_argument("--when-done", type=str, default="nothing",
                   choices=["nothing", "close", "sleep", "shutdown"],
                   help="Action after batch completion: nothing (default), close (exit app), "
                        "sleep (system sleep), shutdown (system shutdown).")
    p.add_argument("--max-memory", type=int, default=None, metavar="PCT",
                   help="Reduce workers when free system memory drops below PCT%% (default: disabled). "
                        "Requires psutil or falls back to platform checks.")
    p.add_argument("--dedup-warn", action="store_true",
                   help="After scan, log near-duplicate image pairs using perceptual hashing. "
                        "Requires imagehash (pip install imagehash). Does not block conversion.")
    p.add_argument("--dedup-skip", action="store_true",
                   help="Skip near-duplicates: keep only the largest file in each duplicate group. "
                        "Requires imagehash (pip install imagehash).")
    return p


CLI_FLAG_PARITY = {
    "--version": {"surface": "cli-only", "gui": (), "readme": True, "note": "Version command"},
    "--install-deps": {"surface": "admin-only", "gui": (), "readme": True, "note": "Dependency installer"},
    "--input": {"surface": "gui", "gui": ("src_edit", "src_btn"), "readme": True, "note": "Source picker"},
    "--files": {"surface": "cli-only", "gui": (), "readme": True, "note": "Shell integration and direct file selection"},
    "--output": {"surface": "gui", "gui": ("dst_edit", "dst_btn"), "readme": True, "note": "Output picker"},
    "--format": {"surface": "gui", "gui": ("fmt_combo",), "readme": True, "note": "Output format selector"},
    "--quality": {"surface": "gui", "gui": ("quality_slider",), "readme": True, "note": "Quality slider"},
    "--workers": {"surface": "gui", "gui": ("workers_spin",), "readme": True, "note": "Worker count"},
    "--in-place": {"surface": "gui", "gui": ("inplace_chk",), "readme": True, "note": "Verified in-place conversion"},
    "--recursive": {"surface": "gui", "gui": ("recursive_chk",), "readme": True, "note": "Recursive scan toggle"},
    "--no-recursive": {"surface": "gui", "gui": ("recursive_chk",), "readme": True, "note": "Recursive scan toggle inverse"},
    "--dry-run": {"surface": "cli-only", "gui": (), "readme": True, "note": "Headless preview"},
    "--strip-metadata": {"surface": "gui", "gui": ("strip_meta_chk",), "readme": True, "note": "Metadata removal"},
    "--strip-gps": {"surface": "gui", "gui": ("meta_combo",), "readme": True, "note": "GPS/location privacy strip"},
    "--strip-device": {"surface": "gui", "gui": ("meta_combo",), "readme": True, "note": "Camera make/model/serial privacy strip"},
    "--resize": {"surface": "gui", "gui": ("resize_chk", "resize_combo", "resize_spin"), "readme": True, "note": "Resize controls"},
    "--skip-existing": {"surface": "gui", "gui": ("skip_existing_chk",), "readme": True, "note": "Resume by output existence"},
    "--progressive": {"surface": "gui", "gui": ("progressive_jpeg_chk",), "readme": True, "note": "Progressive JPEG toggle"},
    "--chroma-420": {"surface": "gui", "gui": ("chroma_chk",), "readme": True, "note": "JPEG chroma toggle"},
    "--lossless": {"surface": "gui", "gui": ("lossless_webp_chk",), "readme": True, "note": "Lossless WebP toggle"},
    "--srgb": {"surface": "gui", "gui": ("srgb_chk",), "readme": True, "note": "sRGB conversion"},
    "--prefix": {"surface": "gui", "gui": ("prefix_edit",), "readme": True, "note": "Filename prefix"},
    "--suffix": {"surface": "gui", "gui": ("suffix_edit",), "readme": True, "note": "Filename suffix"},
    "--tiff-compression": {"surface": "gui", "gui": ("tiff_comp_combo",), "readme": True, "note": "TIFF compression"},
    "--png-level": {"surface": "gui", "gui": ("png_level_spin",), "readme": True, "note": "PNG compression"},
    "--png-lossy": {"surface": "gui", "gui": ("png_lossy_chk",), "readme": True, "note": "pngquant toggle"},
    "--no-structure": {"surface": "gui", "gui": ("structure_chk",), "readme": True, "note": "Folder structure inverse"},
    "--exclude": {"surface": "gui", "gui": ("exclude_edit",), "readme": True, "note": "Glob exclusion"},
    "--stdin-files": {"surface": "cli-only", "gui": (), "readme": True, "note": "Read file paths from stdin"},
    "--stdin-null": {"surface": "cli-only", "gui": (), "readme": True, "note": "NUL-delimited stdin paths"},
    "--no-exiftool": {"surface": "cli-only", "gui": (), "readme": True, "note": "CLI metadata backend override"},
    "--template": {"surface": "gui", "gui": ("template_edit",), "readme": True, "note": "Filename template"},
    "--report": {"surface": "cli-only", "gui": ("export_csv_btn",), "readme": True, "note": "CLI JSON report; GUI has CSV export"},
    "--support-bundle": {"surface": "admin-only", "gui": ("export_support_btn",), "readme": True, "note": "Redacted diagnostics export"},
    "--preset": {"surface": "gui", "gui": ("_preset_btn",), "readme": True, "note": "Preset loader"},
    "--list-presets": {"surface": "cli-only", "gui": ("_preset_btn",), "readme": True, "note": "CLI preset inventory; GUI preset menu"},
    "--list-plugins": {"surface": "admin-only", "gui": (), "readme": True, "note": "Trust-safe plugin inventory"},
    "--trust-plugin": {"surface": "admin-only", "gui": (), "readme": True, "note": "Plugin trust manifest write"},
    "--untrust-plugin": {"surface": "admin-only", "gui": (), "readme": True, "note": "Plugin trust manifest removal"},
    "--only-if-smaller": {"surface": "gui", "gui": ("only_if_smaller_chk", "only_if_smaller_spin"), "readme": True, "note": "Keep smaller outputs only"},
    "--dpi": {"surface": "gui", "gui": ("dpi_spin",), "readme": True, "note": "DPI override"},
    "--icc": {"surface": "gui", "gui": ("icc_edit",), "readme": True, "note": "ICC override"},
    "--xmp-sidecar": {"surface": "gui", "gui": ("xmp_sidecar_chk",), "readme": True, "note": "XMP sidecar"},
    "--recompress": {"surface": "gui", "gui": ("recompress_chk",), "readme": True, "note": "Lossless JPEG recompress"},
    "--target-kb": {"surface": "gui", "gui": ("target_kb_spin",), "readme": True, "note": "Target file size"},
    "--target-psnr": {"surface": "cli-only", "gui": (), "readme": True, "note": "Quality metric automation"},
    "--watermark": {"surface": "gui", "gui": ("watermark_edit",), "readme": True, "note": "Watermark spec"},
    "--canvas": {"surface": "gui", "gui": ("canvas_edit",), "readme": True, "note": "Canvas size"},
    "--canvas-bg": {"surface": "gui", "gui": ("canvas_bg_edit",), "readme": True, "note": "Canvas fill"},
    "--avif-speed": {"surface": "gui", "gui": ("avif_speed_spin",), "readme": True, "note": "AVIF speed"},
    "--avif-codec": {"surface": "gui", "gui": ("avif_codec_combo",), "readme": True, "note": "AVIF codec"},
    "--max-file-size": {"surface": "gui", "gui": ("max_file_size_edit",), "readme": True, "note": "Large input guard"},
    "--register-shell": {"surface": "admin-only", "gui": (), "readme": True, "note": "Install shell integration"},
    "--unregister-shell": {"surface": "admin-only", "gui": (), "readme": True, "note": "Remove shell integration"},
    "--use-cache": {"surface": "cli-only", "gui": (), "readme": True, "note": "Headless repeat-run cache"},
    "--clear-cache": {"surface": "admin-only", "gui": (), "readme": True, "note": "Cache maintenance"},
    "--resume": {"surface": "cli-only", "gui": (), "readme": True, "note": "Interrupted CLI queue resume"},
    "--frames": {"surface": "gui", "gui": ("frames_combo",), "readme": True, "note": "Multi-frame handling"},
    "--watch": {"surface": "cli-only", "gui": (), "readme": True, "note": "Directory watch mode"},
    "--watch-interval": {"surface": "cli-only", "gui": (), "readme": True, "note": "Directory watch cadence"},
    "--tone-map": {"surface": "gui", "gui": ("tone_map_combo",), "readme": True, "note": "HDR tone mapping"},
    "--use-processes": {"surface": "cli-only", "gui": (), "readme": True, "note": "Executor selection"},
    "--sidecar-history": {"surface": "cli-only", "gui": (), "readme": True, "note": "Per-file reproducibility JSON"},
    "--backend": {"surface": "cli-only", "gui": (), "readme": True, "note": "Experimental backend selection"},
    "--backend-info": {"surface": "admin-only", "gui": (), "readme": True, "note": "Backend capability report"},
    "--backend-benchmark": {"surface": "admin-only", "gui": (), "readme": True, "note": "Optional backend benchmark input"},
    "--verify-quality": {"surface": "cli-only", "gui": (), "readme": True, "note": "External quality metric checks"},
    "--progress": {"surface": "cli-only", "gui": (), "readme": True, "note": "JSON Lines machine-readable progress events"},
    "--when-done": {"surface": "gui", "gui": ("when_done_combo",), "readme": True, "note": "Post-batch action"},
    "--max-memory": {"surface": "cli-only", "gui": (), "readme": True, "note": "RAM pressure worker throttle threshold"},
    "--dedup-warn": {"surface": "cli-only", "gui": (), "readme": True, "note": "Perceptual hash duplicate detection (warn)"},
    "--dedup-skip": {"surface": "cli-only", "gui": (), "readme": True, "note": "Perceptual hash duplicate detection (skip)"},
    "--help": {"surface": "cli-only", "gui": (), "readme": False, "note": "argparse built-in help"},
}


def _parser_long_flags(parser: argparse.ArgumentParser | None = None) -> list[str]:
    """Return every long option exposed by argparse, including --help."""
    parser = parser or _build_parser()
    flags: set[str] = set()
    for action in parser._actions:
        flags.update(opt for opt in action.option_strings if opt.startswith("--"))
    return sorted(flags)


def build_cli_parity_matrix(readme_text: str | None = None) -> list[dict]:
    """Generate parser / GUI / README parity rows for tests and audits."""
    rows = []
    for flag in _parser_long_flags():
        entry = CLI_FLAG_PARITY.get(flag)
        row = {
            "flag": flag,
            "surface": "unmapped",
            "gui": (),
            "readme_required": True,
            "in_readme": None,
            "note": "",
        }
        if entry:
            row.update(entry)
            row["gui"] = tuple(entry.get("gui", ()))
            row["readme_required"] = bool(entry.get("readme", True))
            if readme_text is not None:
                row["in_readme"] = f"`{flag}" in readme_text
        rows.append(row)
    return rows


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
    "no_structure", "workers", "no_exiftool", "exclude", "report",
    "only_if_smaller", "dpi", "icc", "xmp_sidecar", "recompress",
    "target_kb", "target_psnr", "watermark", "canvas", "canvas_bg",
    "avif_speed", "avif_codec", "max_file_size", "recursive", "dry_run",
    "use_cache", "clear_cache", "resume", "frames", "watch",
    "watch_interval", "tone_map", "use_processes", "sidecar_history",
    "backend", "verify_quality", "png_lossy",
)


def _apply_preset_to_args(args, preset: dict):
    """Overlay normalized preset values onto argparse Namespace."""
    norm = normalize_preset(preset)
    for key in _PRESET_ARG_KEYS:
        if key not in norm or not hasattr(args, key):
            continue
        setattr(args, key, norm[key])
    if norm.get("resize_enabled") is False and hasattr(args, "resize"):
        args.resize = None


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
    """Watch mode with optional watchdog filesystem events and polling fallback.

    When watchdog is installed, uses OS-level filesystem events (inotify/
    FSEvents/ReadDirectoryChangesW) for sub-second response. Falls back to
    polling when watchdog is absent. Debounces partial-write races by
    requiring file size stability before processing.
    """
    interval = max(1.0, float(getattr(args, "watch_interval", 2.0)))
    watch_opts = _build_convert_options(args, resize_mode=resize_mode,
                                         resize_value=resize_value,
                                         input_dir=input_dir)
    seen_sizes: dict[Path, int] = {}
    converted: set[Path] = set()
    supported = get_supported_extensions()
    pending_files: set[Path] = set()

    try:
        from watchdog.observers import Observer
        from watchdog.events import FileSystemEventHandler, FileCreatedEvent, FileModifiedEvent

        class _WatchHandler(FileSystemEventHandler):
            def on_created(self, event):
                if not event.is_directory:
                    p = Path(event.src_path)
                    if p.suffix.lower() in supported:
                        pending_files.add(p)

            def on_modified(self, event):
                if not event.is_directory:
                    p = Path(event.src_path)
                    if p.suffix.lower() in supported:
                        pending_files.add(p)

        observer = Observer()
        observer.schedule(_WatchHandler(), str(input_dir), recursive=args.recursive)
        observer.start()
        _watch_backend = "watchdog"
    except ImportError:
        observer = None
        _watch_backend = "polling"

    print(f"[watch] watching {input_dir} ({_watch_backend}) every {interval:.1f}s — Ctrl-C to stop")

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

            if observer is not None:
                candidates = list(pending_files)
                pending_files.clear()
            else:
                candidates = []
                try:
                    visited_dirs: set[str] = set()
                    candidates = list(_safe_walk(input_dir, visited_dirs))
                except OSError:
                    pass

            for p in candidates:
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

            for f in current:
                seq = len(converted) + 1
                try:
                    r = convert_file(
                        f, output_dir, seq=seq,
                        opts=watch_opts,
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
    finally:
        if observer is not None:
            observer.stop()
            observer.join(timeout=2)
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


def _build_convert_options(args, *, resize_mode: str = "none",
                           resize_value: int = 1920,
                           input_dir: Path | None = None) -> ConvertOptions:
    """Build a ConvertOptions from an argparse Namespace."""
    _sf: set[str] = set()
    if getattr(args, "strip_metadata", False):
        _sf.add("all")
    if getattr(args, "strip_gps", False):
        _sf.add("gps")
    if getattr(args, "strip_device", False):
        _sf.add("device")
    return ConvertOptions(
        fmt=args.format,
        jpeg_quality=args.quality,
        preserve_metadata=not getattr(args, "strip_metadata", False),
        preserve_structure=not getattr(args, "no_structure", False),
        base_dir=input_dir,
        in_place=getattr(args, "in_place", False),
        skip_existing=getattr(args, "skip_existing", False),
        resize_mode=resize_mode,
        resize_value=resize_value,
        prefix=getattr(args, "prefix", ""),
        suffix=getattr(args, "suffix", ""),
        lossless_webp=getattr(args, "lossless", False),
        progressive_jpeg=getattr(args, "progressive", False),
        chroma_subsampling=getattr(args, "chroma_420", False),
        convert_to_srgb=getattr(args, "srgb", False),
        tiff_compression=getattr(args, "tiff_compression", "none"),
        png_compress_level=getattr(args, "png_level", 6),
        use_exiftool=not getattr(args, "no_exiftool", False),
        name_template=getattr(args, "template", None),
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
        backend=getattr(args, "backend", "pillow"),
        strip_fields=frozenset(_sf),
    )


def _build_quality_mode(args) -> tuple[str, float] | None:
    """Translate --target-kb / --target-psnr into the (mode, target) tuple."""
    if getattr(args, "target_kb", None) is not None:
        return ("target-kb", float(args.target_kb))
    if getattr(args, "target_psnr", None) is not None:
        return ("target-psnr", float(args.target_psnr))
    return None


def _validate_cli_args(args) -> list[str]:
    """Return user-facing validation errors for argparse values."""
    errors: list[str] = []
    if getattr(args, "workers", 1) < 1 or getattr(args, "workers", 1) > 32:
        errors.append("--workers must be between 1 and 32")
    if getattr(args, "use_processes", False) and (PLUGIN_DECODERS or PLUGIN_ENCODERS or PLUGIN_STORAGE):
        errors.append("--use-processes is incompatible with loaded plugins; use thread workers")
    if getattr(args, "quality", 92) < 50 or getattr(args, "quality", 92) > 100:
        errors.append("--quality must be between 50 and 100")
    if getattr(args, "png_level", 6) < 1 or getattr(args, "png_level", 6) > 9:
        errors.append("--png-level must be between 1 and 9")
    if getattr(args, "avif_speed", 6) < 0 or getattr(args, "avif_speed", 6) > 10:
        errors.append("--avif-speed must be between 0 and 10")
    if getattr(args, "target_kb", None) is not None and args.target_kb <= 0:
        errors.append("--target-kb must be greater than 0")
    if getattr(args, "target_psnr", None) is not None and args.target_psnr <= 0:
        errors.append("--target-psnr must be greater than 0")
    if getattr(args, "only_if_smaller", None) is not None:
        if args.only_if_smaller <= 0 or args.only_if_smaller >= 100:
            errors.append("--only-if-smaller must be greater than 0 and less than 100")
    if getattr(args, "dpi", None) is not None and args.dpi <= 0:
        errors.append("--dpi must be greater than 0")
    if getattr(args, "resize", None):
        parts = str(args.resize).split(":")
        if len(parts) != 2 or parts[0] not in {"max_dim", "scale"}:
            errors.append("--resize must be max_dim:VALUE or scale:VALUE")
        else:
            try:
                if int(parts[1]) <= 0:
                    errors.append("--resize value must be greater than 0")
            except ValueError:
                errors.append("--resize value must be an integer")
    if getattr(args, "canvas", None) and _parse_canvas(args.canvas) is None:
        errors.append("--canvas must be WIDTHxHEIGHT with positive integers")
    if getattr(args, "max_file_size", None):
        if _parse_size_spec(args.max_file_size) is None:
            errors.append("--max-file-size must be a size like 500MB, 2GB, or 100KB")
    if getattr(args, "backend", "pillow") == "vips":
        if getattr(args, "format", "auto") == "auto":
            errors.append("--backend vips requires an explicit --format")
        if not getattr(args, "strip_metadata", False):
            errors.append("--backend vips does not preserve metadata; add --strip-metadata to acknowledge")
        unsupported = []
        if getattr(args, "resize", None):
            unsupported.append("--resize")
        if getattr(args, "watermark", None):
            unsupported.append("--watermark")
        if getattr(args, "canvas", None):
            unsupported.append("--canvas")
        if getattr(args, "tone_map", "none") != "none":
            unsupported.append("--tone-map")
        if getattr(args, "dpi", None):
            unsupported.append("--dpi")
        if getattr(args, "icc", None):
            unsupported.append("--icc")
        if getattr(args, "xmp_sidecar", False):
            unsupported.append("--xmp-sidecar")
        if getattr(args, "recompress", False):
            unsupported.append("--recompress")
        if getattr(args, "target_kb", None) is not None:
            unsupported.append("--target-kb")
        if getattr(args, "target_psnr", None) is not None:
            unsupported.append("--target-psnr")
        if getattr(args, "png_lossy", False):
            unsupported.append("--png-lossy")
        if unsupported:
            errors.append("--backend vips does not support: " + ", ".join(unsupported))
    return errors


def _collect_cli_input_refs(args) -> tuple[Path, list[Path], list[Path]]:
    """Resolve CLI file/directory selections into a common base, dirs, and files."""
    refs: list[Path] = []
    if getattr(args, "input", None):
        refs.append(Path(args.input).expanduser().resolve())
    refs.extend(Path(p).expanduser().resolve() for p in (getattr(args, "files", None) or []))

    if getattr(args, "stdin_files", False):
        if refs:
            print("[ERROR] --stdin-files is mutually exclusive with --input and --files.",
                  file=sys.stderr)
            sys.exit(EXIT_INPUT_ERROR)
        delim = "\0" if getattr(args, "stdin_null", False) else "\n"
        raw = sys.stdin.read()
        for line in raw.split(delim):
            line = line.strip()
            if line:
                refs.append(Path(line).expanduser().resolve())

    if not refs:
        print("[ERROR] Provide --input, --files, or --stdin-files.", file=sys.stderr)
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


def _execute_when_done(action: str):
    if action == "nothing" or not action:
        return
    if action == "close":
        sys.exit(EXIT_OK)
    if action == "sleep":
        if platform.system() == "Windows":
            subprocess.run(["rundll32.exe", "powrprof.dll,SetSuspendState", "0,1,0"],
                           capture_output=True)
        elif platform.system() == "Darwin":
            subprocess.run(["pmset", "sleepnow"], capture_output=True)
        else:
            subprocess.run(["systemctl", "suspend"], capture_output=True)
    elif action == "shutdown":
        if platform.system() == "Windows":
            subprocess.run(["shutdown", "/s", "/t", "60", "/c",
                            "ImgConverter: shutting down after batch conversion. "
                            "Run 'shutdown /a' to cancel."], capture_output=True)
        elif platform.system() == "Darwin":
            subprocess.run(["osascript", "-e",
                            'tell application "System Events" to shut down'],
                           capture_output=True)
        else:
            subprocess.run(["systemctl", "poweroff"], capture_output=True)


def _dedup_scan(files: list[Path], threshold: int = 8) -> list[tuple[Path, Path, int]]:
    """Find near-duplicate image pairs using perceptual hashing.

    Returns list of (file_a, file_b, hamming_distance) for pairs below threshold.
    Requires imagehash library; returns empty list if unavailable.
    """
    try:
        import imagehash
    except ImportError:
        return []

    hashes: list[tuple[Path, object]] = []
    for f in files:
        try:
            with Image.open(str(f)) as img:
                h = imagehash.average_hash(img.convert("RGB").resize((8, 8)))
                hashes.append((f, h))
        except Exception:
            continue

    dupes: list[tuple[Path, Path, int]] = []
    for i, (fa, ha) in enumerate(hashes):
        for fb, hb in hashes[i + 1:]:
            dist = ha - hb
            if dist <= threshold:
                dupes.append((fa, fb, dist))
    return dupes


def _dedup_groups(dupes: list[tuple[Path, Path, int]]) -> list[list[Path]]:
    """Build connected-component groups from pairwise duplicates."""
    parent: dict[Path, Path] = {}

    def find(x: Path) -> Path:
        while parent.get(x, x) != x:
            parent[x] = parent.get(parent[x], parent[x])
            x = parent[x]
        return x

    def union(a: Path, b: Path):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    for fa, fb, _ in dupes:
        parent.setdefault(fa, fa)
        parent.setdefault(fb, fb)
        union(fa, fb)

    groups: dict[Path, list[Path]] = {}
    for p in parent:
        root = find(p)
        groups.setdefault(root, []).append(p)
    return [g for g in groups.values() if len(g) > 1]


def _get_free_memory_pct() -> float | None:
    """Return percentage of free system memory, or None if unavailable."""
    try:
        import psutil
        return psutil.virtual_memory().available / psutil.virtual_memory().total * 100
    except ImportError:
        pass
    if platform.system() == "Windows":
        try:
            kernel32 = ctypes.windll.kernel32
            class MEMORYSTATUSEX(ctypes.Structure):
                _fields_ = [
                    ("dwLength", ctypes.c_ulong),
                    ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]
            stat = MEMORYSTATUSEX()
            stat.dwLength = ctypes.sizeof(stat)
            if kernel32.GlobalMemoryStatusEx(ctypes.byref(stat)):
                return stat.ullAvailPhys / stat.ullTotalPhys * 100
        except Exception:
            pass
    elif platform.system() == "Linux":
        try:
            with open("/proc/meminfo") as f:
                info = {}
                for line in f:
                    parts = line.split(":")
                    if len(parts) == 2:
                        info[parts[0].strip()] = int(parts[1].strip().split()[0])
                total = info.get("MemTotal", 0)
                avail = info.get("MemAvailable", 0)
                if total > 0:
                    return avail / total * 100
        except Exception:
            pass
    return None


def _emit_progress(event: str, data: dict | None = None, *, enabled: bool = False):
    if not enabled:
        return
    payload = {"event": event}
    if data:
        payload.update(data)
    print(json.dumps(payload, default=str), file=sys.stderr, flush=True)


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
    args.format = str(args.format).lower()

    validation_errors = _validate_cli_args(args)
    if validation_errors:
        for error in validation_errors:
            print(f"[ERROR] {error}", file=sys.stderr)
        sys.exit(EXIT_INPUT_ERROR)

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

    built_in_formats = {"auto", "jpeg", "png", "webp", "avif", "tiff", "jxl"}
    if args.format not in built_in_formats and args.format not in PLUGIN_ENCODERS:
        print(f"[ERROR] Unsupported output format: {args.format}", file=sys.stderr)
        sys.exit(EXIT_INPUT_ERROR)

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

    # Memory pressure threshold
    _mem_threshold = getattr(args, "max_memory", None)
    if _mem_threshold is not None:
        _mem_threshold = max(1, min(99, int(_mem_threshold)))
        print(f"[memory] throttle threshold: {_mem_threshold}% free")

    # Scan
    _progress_on = getattr(args, "progress", False)
    print(f"\nScanning{' recursively' if args.recursive else ''}...")
    _emit_progress("scan_start", {"input": str(input_dir)}, enabled=_progress_on)
    max_bytes = _parse_size_spec(getattr(args, "max_file_size", None) or "")
    if max_bytes:
        print(f"[filter] skipping files larger than {_fmt_size(max_bytes)}")
    scan = _scan_cli_inputs(args, input_dir, input_dirs, input_files, max_bytes)
    print(f"Found {len(scan.files)} files ({_fmt_size(scan.total_size)}) in {scan.elapsed:.2f}s")
    _emit_progress("scan_done", {"count": len(scan.files), "total_bytes": scan.total_size,
                                  "elapsed": round(scan.elapsed, 3)}, enabled=_progress_on)

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

    if getattr(args, "dedup_warn", False) or getattr(args, "dedup_skip", False):
        try:
            import imagehash  # noqa: F401
            print("[dedup] scanning for near-duplicates...")
            dupes = _dedup_scan(scan.files)
            if dupes:
                print(f"[dedup] found {len(dupes)} near-duplicate pair(s):")
                for fa, fb, dist in dupes[:20]:
                    print(f"  hamming={dist}: {fa.name} <-> {fb.name}")
                if len(dupes) > 20:
                    print(f"  ... and {len(dupes) - 20} more pairs")
                if getattr(args, "dedup_skip", False):
                    groups = _dedup_groups(dupes)
                    skip_set: set[Path] = set()
                    for group in groups:
                        largest = max(group, key=lambda p: p.stat().st_size)
                        skip_set |= set(group) - {largest}
                    pre = len(scan.files)
                    scan.files = [f for f in scan.files if f not in skip_set]
                    scan.total_size = sum(f.stat().st_size for f in scan.files)
                    print(f"[dedup] skipped {pre - len(scan.files)} near-duplicates, "
                          f"converting {len(scan.files)} unique files")
            else:
                print("[dedup] no near-duplicates found")
        except ImportError:
            print("[dedup] imagehash not installed — pip install imagehash to enable dedup",
                  file=sys.stderr)

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
    cli_opts = _build_convert_options(args, resize_mode=resize_mode,
                                      resize_value=resize_value,
                                      input_dir=input_dir)
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
                convert_file, f, output_dir, seq=seq_i,
                opts=cli_opts,
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
                done_paths.append(str(result.src))
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

            _emit_progress("file_done", {
                "file": str(result.src),
                "status": "ok" if result.success else ("skip" if result.skipped else "fail"),
                "size_before": result.size_before,
                "size_after": result.size_after,
                "elapsed": round(result.elapsed, 3),
                "current": done_count,
                "total": total,
            }, enabled=_progress_on)

            if _mem_threshold is not None and done_count % max(1, args.workers) == 0:
                free_pct = _get_free_memory_pct()
                if free_pct is not None and free_pct < _mem_threshold:
                    print(f"[WARN] memory pressure: {free_pct:.0f}% free < {_mem_threshold}% threshold")

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
    _emit_progress("batch_done", {
        "ok": ok_count, "failed": fail_count, "skipped": skip_count,
        "wall_seconds": round(wall_time, 2), "files_per_sec": round(speed, 1),
    }, enabled=_progress_on)

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
                    "metadata": r.metadata_report,
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

    when_done = getattr(args, "when_done", "nothing") or "nothing"
    if when_done != "nothing":
        _execute_when_done(when_done)

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

    if getattr(args, "support_bundle", None):
        try:
            written = export_support_bundle(Path(args.support_bundle))
            print(f"[support] wrote {written}")
            sys.exit(EXIT_OK)
        except OSError as e:
            print(f"[support] failed to write bundle: {e}", file=sys.stderr)
            sys.exit(EXIT_INPUT_ERROR)

    if getattr(args, "backend_info", False) or getattr(args, "backend_benchmark", None):
        try:
            benchmark = Path(args.backend_benchmark) if getattr(args, "backend_benchmark", None) else None
            print(json.dumps(build_backend_info(benchmark), indent=2, sort_keys=True))
            sys.exit(EXIT_OK)
        except OSError as e:
            print(f"[backend-info] failed: {e}", file=sys.stderr)
            sys.exit(EXIT_INPUT_ERROR)

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

    if args.input or getattr(args, "files", None) or getattr(args, "stdin_files", False):
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
    main()
