# Research — ImgConverter

## Executive Summary

ImgConverter is a mature local-first Python 3.10+ PyQt6/Pillow batch image converter at v3.3.4 with category-leading format coverage (12+ input families, 6 output formats), metadata fidelity (EXIF/ICC/XMP/IPTC/MakerNotes/C2PA), privacy controls (selective GPS/device stripping, redacted support bundles), and a CLI/GUI parity model backed by 165 tests across 9,258 lines of single-file source. Prior research items (C2PA SDK-only verification, entry-point plugin hashing, batch history) shipped in v3.3.2–v3.3.4. The roadmap is clean.

**P0 bug found**: `_verify_c2pa_sdk()` at line 334 calls `reader.is_valid()`, which does not exist in current c2pa-python (v0.35+). The correct method is `reader.get_validation_state()`. The broad `except Exception` silently swallows the `AttributeError`, returning `"not-verified"` instead of a proper verification result. Combined with the `>=0.6` floor (which allows API-incompatible installs), C2PA verification via the SDK path is silently broken on any recent c2pa-python version.

Top opportunities in priority order:

1. **Fix c2pa-python `is_valid()` → `get_validation_state()` + bump floor to `>=0.35`** — P0 correctness bug.
2. **Bump dependency floors** (watchdog `>=6.0`, PyQt6 `>=6.10`) — align with Python 3.10+ and pick up 450+ Qt bug fixes.
3. **Add c2pa-python and watchdog to `DEP_FLOORS`** — these optional deps get no version warning today even when installed below floor.
4. **SSIMULACRA2 quality targeting** — add `--target-ssimulacra2 SCORE` as an alternative to `--target-psnr`. PyPI package `ssimulacra2` (v0.3.0) exists with pure-Python implementation.
5. **i18n scaffolding** — zero `self.tr()` usage blocks community translation. Caesium ships 20 translations; File Converter added 6 new translations in v2.2.
6. **Thumbnail preview in scan review table** — users are blind to image content before committing a batch.
7. **PyInstaller spec modernization** — `optimize=0`, blanket UPX, no `upx_exclude` for Qt DLLs.

## Product Map

- **Core workflows**: Source selection (browse/drag-drop/clipboard/CLI) → recursive scan with format/size/exclude filters → optional review (scan table, duplicate check) → batch conversion with metadata/color/privacy/quality/resize/watermark controls → validation/report/support bundle → repeat automation (presets, cache, resume, watch, shell integration, plugins)
- **User personas**: Phone-photo users (HEIC/AVIF→JPEG), web developers (WebP/AVIF/JXL assets), archivists (ICC/EXIF/XMP preservation), privacy-conscious users (GPS/device strip), sysadmins (CLI automation, watch folders)
- **Platforms**: Windows (primary, dark title bar, taskbar progress), macOS, Linux; PyQt6 GUI + argparse CLI; PyInstaller single-file exe
- **Key integrations**: Pillow 12.2+, pillow-heif 1.4/libheif 1.23.0, PyQt6 6.8+, optional rawpy 0.27, pillow-jxl-plugin 1.3.7, ExifTool, pngquant/jpegtran/jpegoptim, watchdog 6.0, imagehash, pyvips, c2pa-python 0.36/c2patool

## Competitive Landscape

**XnConvert** (closed-source, C++/Qt, 500+ formats, 80+ chained actions): Gold standard for action pipelines. ImgConverter should learn from action-chain ergonomics but not chase format count. The reorderable task-chain preset is already design-blocked in `Roadmap_Blocked.md`.

**File Converter** (14.7k stars, C#/WPF, v2.2 Feb 2026): Added AVIF support and 6 new translations in v2.2. Explorer right-click integration is its killer feature. ImgConverter already has shell integration; keep it reliable.

**Caesium** (6.1k stars, C++/Qt, v2.8.5): 20 language translations. v3.0.0 in development. Still lacks AVIF/JXL. ImgConverter should learn from its translation infrastructure.

**XL Converter** (506 stars, Python/PyQt, v1.2.3): Most architecturally similar. Differentiates on Jpegli and SVT-AV1-PSY integration. Does NOT use SSIMULACRA2 (prior session's claim was incorrect). ImgConverter already has lossless JXL; Jpegli is blocked on external binary packaging.

**ConvertX** (17.1k stars, TypeScript/Docker, v0.18.0): Self-hosted web converter with 1000+ formats. Different product shape. Path traversal security fix in 2025 — validates that file-path sanitization matters.

**File_Converter_Pro** (399 stars, Python/PySide6, Apr 2026): New entrant validating Python+Qt desktop converter approach. Multi-engine fallback, dark/light theme. Small but growing.

**Transmute** (1,009 stars, Feb 2026): Self-hosted converter gaining traction fast. Images/video/audio/JSON/Excel. Validates demand for broad format coverage in self-hosted tools.

**Dinky** (459 stars, Swift, macOS-only, v2.12.0): Per-preset watch folders, clipboard compress, Homebrew tap install. ImgConverter already has watch profiles and batch history.

## Security, Privacy, and Reliability

- **Verified bug**: `_verify_c2pa_sdk()` at `imgconverter.py:334` calls `reader.is_valid()` which does not exist in c2pa-python v0.35+. The correct API is `reader.get_validation_state()` (returns `ValidationState` enum) or `reader.get_validation_results()` (returns detailed list). The `except Exception` on line 341 silently catches the `AttributeError`, returning `"not-verified"` with a generic error message. The c2patool subprocess fallback path (lines 345-378) still works correctly.
- **Dependency floor stale**: `c2pa-python>=0.6` in `pyproject.toml` (lines 45-46) and `requirements.txt` (line 15) allows installing API-incompatible versions. The `Reader` class was introduced in v0.5.0 with completely different API from the old `c2pa.read_file()` model. `try_create()` exists in current API. `is_valid()` does not. Minimum safe floor: `>=0.35`.
- **Missing floor checks**: `DEP_FLOORS` at line 48 does not include `c2pa` or `watchdog`. These optional deps get no startup warning when installed below floor.
- **Pillow security posture is current**: Floor `>=12.2.0` covers CVE-2026-25990 (PSD OOB write, CVSS 8.9), CVE-2026-40192 (FITS decompression bomb), CVE-2026-42308 (font glyph overflow), CVE-2026-42309 (coordinate heap overflow), and CVE-2026-42311 (PSD memory corruption, CVSS 8.6). All fixed in 12.2.0.
- **FBI online-converter warning (Mar 2025)**: FBI Denver warned that free online file converters install ransomware and scrape EXIF GPS. Directly validates ImgConverter's local-first, no-network model.
- **PyInstaller spec risks**: `upx=True` with empty `upx_exclude=[]` can corrupt Qt6 DLLs on some platforms. `optimize=0` leaves assertions and docstrings in the binary. `console=False` means CLI mode in a PyInstaller build has no stdout.

## Architecture Assessment

- **Single-file shape**: 9,258 lines with section markers. Manageable. Extract only when a subsystem needs 100+ lines of changes.
- **ConvertOptions boundary**: Clean dataclass at line 1814 with 35 fields. Parity test (`build_cli_parity_matrix()`) catches drift. Continue flowing all new options through this boundary.
- **No i18n infrastructure**: Zero `self.tr()`, `QCoreApplication.translate()`, `.ts` files, or `QTranslator` usage across 400+ user-visible strings. Blocks community translation entirely. Caesium ships 20 translations; File Converter has 6+ languages.
- **Test coverage**: 165 tests across 7 files. Strong for CLI/GUI/README parity, round-trips, sidecars, plugins, accessibility, watch, and vips. Missing: c2pa-python API compatibility test, SSIMULACRA2 quality test, i18n smoke test.
- **PyInstaller spec**: Basic. Missing: `optimize=2`, `upx_exclude` for Qt6 DLLs (`*.pyd`, `Qt6*.dll`), `hiddenimports` for optional deps when installed, and a console-mode spec variant for CLI-only builds.

## Rejected Ideas

- **Server mode / multi-user accounts**: ConvertX (17.1k stars) and Transmute (1,009 stars) validate demand but contradict local-first model. Source: ConvertX/Transmute GitHub.
- **AI upscaling in core**: Real-ESRGAN (31k stars) adds 16MB+ model weights and GPU complexity. Better as plugin shape. Source: Real-ESRGAN GitHub.
- **WebP 2**: Shelved by Google (Oct 2022). Source: libwebp2 README.
- **Mobile app**: Desktop batch + CLI is the product. Source: competitive landscape.
- **Full action-chain editor now**: Requires GUI preset-editor rebuild. Already design-blocked. Source: XnConvert, `Roadmap_Blocked.md`.
- **Before/after compare slider now**: Already design-blocked pending real display testing. Source: Squoosh, Caesium, `Roadmap_Blocked.md`.
- **Bundle Jpegli/cjxl now**: External binary packaging is blocker. Already in `Roadmap_Blocked.md`. Source: XL Converter.
- **Flatpak/Homebrew/winget now**: Each requires platform infrastructure. Already in `Roadmap_Blocked.md`.
- **PNG 3.0 HDR/APNG/EXIF now**: W3C Recommendation shipped Jun 2025 but Pillow has no cICP/HDR chunk support yet. Source: W3C PNG 3 spec.
- **tufup auto-update**: Adds complexity. Current GitHub Releases version-check suffices. Source: tufup GitHub.
- **Nuitka as default build**: Known PyQt6 threading caveats. Source: Nuitka docs.
- **C2PA signing**: Verification/reporting must be correct first (it isn't — see P0 bug). Source: C2PA spec.
- **VVC/H.266 or AV2**: Tooling years away. Source: AOM.
- **SSIMULACRA2 for XL Converter parity**: XL Converter does not actually use SSIMULACRA2 — the prior research session's claim was incorrect. The value of SSIMULACRA2 stands on its own merits as a perceptual quality metric, not as competitive parity. Source: XL Converter release notes, SSIMULACRA2 PyPI.

## Sources

### Competitors and adjacent tools
- https://github.com/JacobDev1/xl-converter
- https://github.com/Tichau/FileConverter
- https://github.com/Lymphatus/caesium-image-compressor
- https://github.com/C4illin/ConvertX
- https://github.com/Faster3ck/Converseen
- https://github.com/heyderekj/dinky
- https://github.com/Hyacinthe-primus/File_Converter_Pro
- https://github.com/transmute-app/transmute
- https://www.xnview.com/en/xnconvert/

### Standards, formats, and codecs
- https://pillow.readthedocs.io/en/stable/releasenotes/12.2.0.html
- https://github.com/bigcat88/pillow_heif/blob/master/CHANGELOG.md
- https://jpeg.org/jpegxl/
- https://aomediacodec.github.io/av1-avif/
- https://www.w3.org/TR/png-3/
- https://contentauth.github.io/c2pa-python/api/c2pa/index.html
- https://opensource.contentauthenticity.org/docs/c2pa-python/docs/release-notes/
- https://pypi.org/project/ssimulacra2/

### Security and privacy
- https://www.sentinelone.com/vulnerability-database/cve-2026-25990/
- https://www.sentinelone.com/vulnerability-database/cve-2026-40192/
- https://osv.dev/vulnerability/UBUNTU-CVE-2026-42311
- https://www.fbi.gov/contact-us/field-offices/denver/news/fbi-denver-warns-of-online-file-converter-scam
- https://www.malwarebytes.com/blog/news/2025/03/warning-over-free-online-file-converters-that-actually-install-malware

### Distribution and packaging
- https://pyinstaller.org/en/stable/CHANGES.html
- https://pypi.org/project/PyQt6/
- https://pypi.org/project/watchdog/
- https://pypi.org/project/c2pa-python/

### Project evidence
- imgconverter.py (9,258 lines, v3.3.4)
- tests/ (165 tests across 7 files)
- ROADMAP.md, Roadmap_Blocked.md, CHANGELOG.md, PLUGINS.md, CONTRIBUTING.md
- pyproject.toml, requirements.txt, ImgConverter.spec, packaging/conda-forge/meta.yaml

## Open Questions

1. **c2pa-python minimum version for `try_create()`**: The `Reader` class appeared in v0.5.0 but `try_create()` may be newer. The safe floor is `>=0.35` (verified to have all methods used). A live test with `pip install c2pa-python==0.35 && python -c "from c2pa import Reader; print(dir(Reader))"` would confirm the exact minimum.
