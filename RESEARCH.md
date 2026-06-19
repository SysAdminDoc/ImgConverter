# Research — ImgConverter

## Executive Summary

### 2026-06-19 competitive refresh

ImgConverter is now past much of the v3.1.0 opportunity set: selective metadata controls, JSONL progress, stdin file intake, duplicate warning/skip, watchdog watch mode, RAM throttling, when-done actions, taskbar progress, support bundles, and backend benchmarking are already present in the current v3.3.0 product surface. The next quality jump is less about raw conversion coverage and more about making large local batches feel inspectable, recoverable, and professionally operated.

Best comparable products and what they imply:

1. **XnConvert** shows how valuable a mature batch review surface and reusable action chains are. ImgConverter should not chase every action, but it should make scanned jobs easier to inspect before users commit to conversion.
2. **reaConverter and BatchPhoto** show that watch folders, background operation, and scheduled automation are paid-product trust signals. ImgConverter has the engine; the GUI needs a watch-folder cockpit with health, run history, pause/resume, and recovery.
3. **File Converter** shows that Explorer context menus are a primary adoption loop on Windows. ImgConverter already has shell registration, but setup should be manageable from the GUI with preset selection and clean uninstall.
4. **Squoosh** shows that users trust compression decisions when they can proof results before the full job. A lightweight sample proof run is a better near-term fit than a full before/after slider.
5. **XL Converter and libjxl** show that optional modern encoder lanes matter, especially lossless JPEG-to-JXL and Jpegli, but these should remain external-tool adapters until packaging is stable.
6. **Czkawka** shows that duplicate/similar-image review belongs in the scan phase. ImgConverter already has CLI dedup logic; the missing piece is a calm GUI review panel.

Top roadmap additions from this refresh:

1. GUI scan review table with per-row warning/status visibility.
2. Windows shell integration manager for the existing registry-based Explorer workflow.
3. Watch-folder cockpit with tray/background mode, pause/resume, health, and run history.
4. Optional lossless JPEG-to-JXL transcode/reconstruction lane via libjxl tools.
5. Sample proof run before full conversion.
6. Backend policy advisor that recommends, but does not silently switch, conversion engines.
7. Preset import/export bundles with schema, CLI equivalent, and trust warnings.
8. GUI duplicate review panel built on existing perceptual dedup logic.
9. Runtime format capability matrix for GUI and support bundles.
10. Optional Jpegli adapter and keyboard command palette as lower-priority polish.

### Legacy v3.1.0 baseline

ImgConverter is a local-first Python 3.10+/PyQt6 batch image converter with unusually strong fidelity defaults: 4:4:4 chroma, ICC passthrough, EXIF auto-rotate, atomic writes, ExifTool tag-copy, CLI/GUI parity tests, plugin trust gates, and release provenance artifacts. At v3.1.0 (6,456 lines, 113+ tests, 3-OS × 4-Python CI matrix), it is the most metadata-correct open-source batch converter surveyed — beating XnConvert on ICC handling and XL Converter on format breadth.

The highest-value direction is to sharpen three edges competitors leave dull: privacy-aware metadata control (no tool offers selective GPS/device-ID stripping with copyright preservation), automation polish (post-batch actions, taskbar progress, machine-readable CLI output), and ecosystem readiness (Python 3.14 free-threading, Jpegli JPEG encoding, C2PA via native Python SDK). These close the gap with commercial tools (reaConverter, BatchPhoto) without violating the local-first, single-file philosophy.

Top 10 new opportunities, in priority order:

1. Selective metadata stripping with privacy presets (GPS-only, device-info-only) — the #1 metadata complaint across all surveyed communities.
2. Python 3.14 free-threaded CI variant — 3x throughput for parallel batch conversion is now production-ready (PEP 779).
3. Post-batch "When Done" actions — shutdown/sleep/close after overnight conversions. Standard in HandBrake and reaConverter.
4. Windows taskbar progress indicator — expected UX for any long-running Windows desktop app.
5. Machine-readable CLI progress output — JSON Lines events for CI/monitoring integration.
6. Jpegli encoder for JPEG output — 35% better compression at same quality. XL Converter has it.
7. Accept file list from stdin — pipeline composition (`find ... | imgconverter --stdin-files`).
8. Perceptual hash duplicate detection before conversion — Czkawka (31.6k stars) proves the pattern.
9. RAM pressure monitoring with dynamic worker throttle — prevents OOM on large batches.
10. Optional watchdog-based filesystem events for watch mode — replace polling with OS-level events.

## Product Map

- Core workflows: drag/drop or CLI intake; recursive scan/filter; convert with fidelity controls; validate/log/report; resume/cache/watch for repeatable automation.
- User personas: phone-photo users converting HEIC/AVIF; web developers generating WebP/AVIF/JXL assets; archivists preserving metadata, ICC, and provenance; sysadmins running repeatable local batches; privacy-conscious users stripping GPS/device data.
- Platforms and distribution: Windows/macOS/Linux; PyQt6 GUI; argparse CLI; PyInstaller CI artifacts; unsigned release checksums/SBOM/provenance; conda recipe scaffold.
- Key integrations: Pillow 12.2+, pillow-heif 1.4/libheif 1.23, PyQt6 6.8+, optional rawpy/LibRaw, pillow-jxl/libjxl, qoi, ExifTool 13.55+, jpegoptim/jpegtran/pngquant, butteraugli/ffmpeg-quality-metrics, optional pyvips/libvips 8.18.

## Competitive Landscape

**XL Converter** (503 stars, Python/PyQt, GPL-3.0): Most architecturally similar. Has Jpegli integration (35% JPEG savings), lossless JPEG→JXL transcoding, RAM optimizer, "copy smaller" mode. Learn from its encoder diversity; ImgConverter's advantage is broader input format coverage, ExifTool integration, and plugin system.

**XnConvert** (closed-source freeware, Qt/C++): 500+ formats, 80+ chained actions, CLI export to NConvert. Learn from action-chain presets and mature batch UI. Avoid chasing format breadth at the cost of fidelity defaults — XnConvert strips ICC profiles with default settings.

**File Converter** (14.5k stars, C#, Windows): Shell integration is the primary UX — Explorer right-click drives adoption. Learn from context-menu-first workflow. ImgConverter already has `--register-shell` but it's CLI-only setup. Avoid Windows-only fragility.

**Caesium** (6k stars, C++/Qt6, GPL-3.0): Clean compression-focused UI. Community requests for AVIF (#120), HEIC, JXL remain unmet. Learn from target-file-size compression UX. ImgConverter already has `--target-kb`.

**Squoosh** (24.7k stars, WebAssembly, Google): Before/after split-view slider is the gold standard for quality comparison. CLI deprecated (2023). Learn from the preview UX. Avoid building a web app — Squoosh's Pyodide approach is heavy and the CLI is dead.

**Czkawka** (31.6k stars, Rust, MIT): Duplicate/similar image finder with 6 perceptual hash algorithms. Not a converter, but the dedup pre-pass concept maps directly to ImgConverter's scan phase — detect near-duplicates before wasting conversion time.

**reaConverter** ($99/yr) and **BatchPhoto** ($50-$130): Commercial tools paywall watch folders, scheduled jobs, and CLI automation. ImgConverter already has CLI watch mode; promoting it to GUI hot-folder profiles (existing P3 item) gives this away for free.

**HandBrake** (video, but queue UX model): Per-job JSON persistence, crash recovery, retry-on-failure, "When Done" actions with cancellable countdown, taskbar progress. The queue management patterns translate directly to image batch conversion.

## Security, Privacy, and Reliability

**Active CVE streams in the dependency stack (June 2026):**
- **libde265** (HEVC decoder used by libheif): CVE-2026-49346/49295/49337 — heap buffer overflows including 4GB-into-1KB write, RCE potential. CVE-2026-33164 PPS parsing crash. CVE-2026-45382/45383 heap OOB in WPP/tiles decode. Active churn; pin and monitor.
- **Pillow**: CVE-2026-42308 font glyph overflow, CVE-2026-40192 FITS decompression bomb, CVE-2026-25990 PSD tile OOB write. All fixed in 12.2.0 (current floor).
- **pillow-heif**: CVE-2026-28231 integer overflow in encode path. Fixed in 1.3.0 (current floor is 1.4.0).
- **LibRaw**: CVE-2026-20889 heap overflow in x3f_thumb_loader, CVE-2026-5318/5342 JPEG/TIFF OOB. Fixed in 0.22.1 (rawpy 0.27.0 bundles this).
- **libjxl**: CVE-2026-1837 use-after-free in color transform, CVE-2025-12474 OOB read. Fixed in 0.10.5+.
- **Qt6**: 9 CVEs in 2025 including SVG heap buffer overflow. Zero so far in 2026.

**Verified code findings:**
- `imgconverter.py:190-198`: `ALLOW_INCORRECT_HEADERS` attribute was removed in pillow-heif 1.4.0. The `hasattr` guard makes this harmless dead code, but it should be cleaned up to match the current API surface.
- `ConvertWorker.run()` and `_run_cli()` submit all futures before consuming results. Already tracked as P1 (bounded scheduling).
- `packaging/conda-forge/meta.yaml`: pins `numpy >=1.26` as runtime despite `pyproject.toml` keeping numpy optional. Already tracked as P1 (release metadata ratchets).
- `pyproject.toml`: lists Python 3.10-3.13 classifiers but CI tests 3.14 successfully.

**Privacy gap:** No tool surveyed (ImgConverter included) offers selective metadata stripping. The binary preserve/strip choice forces users to either keep GPS coordinates or lose copyright/keyword data. This is the #1 metadata complaint across Reddit r/photography, ExifTool forums, and privacy-focused communities.

## Architecture Assessment

- `convert_file()` signature has 35 keyword parameters. `ConvertWorker.__init__` mirrors these as positional args — the root cause of parity drift. Existing P1 item (ConvertOptions dataclass) is the right fix.
- The single-file architecture is deliberate and works well at 6,456 lines. Helper extraction has begun (`_run_sidecar_hooks()`). The next extraction candidate is the metadata handling (presence detection, selective stripping, report generation) — currently scattered across `_open_image()`, `convert_file()`, and `_metadata_presence_from_image()`.
- Plugin registry is well-shaped for decoder/encoder/storage. Entry-point discovery (existing P2) is the natural next step for distributable plugins.
- Test suite covers core logic well (113+ tests). Remaining high-value coverage: Qt event-loop keyboard navigation (existing P2), watch mode integration, ExifTool fidelity (both in Roadmap_Blocked pending test infrastructure).
- The vips backend is flagged experimental and correctly rejects unsupported options. libvips 8.18's new Camera RAW and UltraHDR JPEG support could expand its utility but metadata parity must come first.

## Rejected Ideas

- **Jpegli as a bundled core encoder now**: 35% JPEG compression improvement is real (XL Converter, Google benchmarks), but Jpegli is a C library without a PyPI wheel. Integration requires either subprocess wrapping of `cjpegli` binary or a Python binding that doesn't exist yet. Recommended as P2 when a pip-installable binding ships. Source: libjxl project, XL Converter integration.
- **Full AI editing suite** (upscale/denoise/background-remove): Topaz Photo AI charges $199/yr for this. Conflicts with local-first philosophy (model weights, GPU deps). Source: Topaz, Filestar.
- **Cloud sync / accounts / CDN transforms**: Contradicts local-first. Source: Cloudinary, Filestar pricing.
- **HEIC output by default**: Patent encumbrance via HEVC pools (Access Advance extended rate increase deadline to June 30, 2026). Source: Access Advance, Firefox bug 1402293.
- **WebP 2**: Confirmed dead by Google. Source: chromium libwebp2 README.
- **Nuitka as PyInstaller replacement**: PyQt6 threading broken in Nuitka; PySide6 recommended instead. PyInstaller remains correct for PyQt6 apps. Source: Nuitka PyQt6 plugin, 2026 comparison benchmarks.
- **Full i18n rollout now**: No community demand signal stronger than privacy, reliability, and automation gaps. Revisit when external contributors volunteer translations. Source: XnConvert has 20+ languages but ImgConverter's English-dominant user base hasn't requested it.
- **Duplicate image detection with ML models**: Czkawka proves perceptual hashing works without ML. Heavy deps (OpenCV/torch) contradict philosophy. Pillow + imagehash library is sufficient. Source: Czkawka architecture.
- **Mobile companion app**: Separate product, separate QA. Source: existing roadmap rejection.
- **PNG 3.0 HDR output**: Requires Pillow to implement the new HDR chunks first. No action for ImgConverter until upstream lands. Source: W3C PNG 3.0 Recommendation (June 2025).
- **SVT-AV1 4.0 tune parameters in GUI**: Pillow's AVIF encoder doesn't expose SVT-AV1 tune flags directly. Would require subprocess wrapping of `SvtAv1EncApp`. Too fragile for a roadmap item. Source: SVT-AV1 4.0 release, Pillow AvifImagePlugin source.

## Sources

### Competitors and adjacent tools
- https://www.xnview.com/en/xnconvert/
- https://github.com/Tichau/FileConverter
- https://github.com/Lymphatus/caesium-image-compressor
- https://github.com/GoogleChromeLabs/squoosh
- https://github.com/JacobDev1/xl-converter
- https://github.com/qarmin/czkawka
- https://github.com/Faster3ck/Converseen
- https://github.com/meowtec/Imagine
- https://github.com/ImageOptim/ImageOptim
- https://imazing.com/converter
- https://www.reaconverter.com/
- https://www.batchphoto.com/
- https://github.com/niclas-niclas/pillow-jxl-plugin
- https://github.com/libvips/pyvips

### Community signal
- https://www.reddit.com/r/jpegxl/
- https://www.reddit.com/r/photography/
- https://forum.level1techs.com/t/the-best-heic-to-jpg-converter-in-windows-10-without-losing-exif-data/250391

### Standards and specifications
- https://jpeg.org/jpegxl/
- https://aomediacodec.github.io/av1-avif/v1.2.0.html
- https://spec.c2pa.org/specifications/specifications/2.4/
- https://www.w3.org/TR/png-3/
- https://www.cipa.jp/std/documents/download_e.html?DC-008-Translation-2026-E

### Dependencies and security
- https://pillow.readthedocs.io/en/stable/releasenotes/12.2.0.html
- https://github.com/bigcat88/pillow_heif/blob/master/CHANGELOG.md
- https://github.com/libvips/libvips/releases
- https://exiftool.sourceforge.net/history.html
- https://nvd.nist.gov/vuln/
- https://security-tracker.debian.org/tracker/
- https://opensource.contentauthenticity.org/docs/c2pa-python/
- https://docs.python.org/3/whatsnew/3.14.html

### UX and accessibility
- https://www.w3.org/TR/wcag2ict-22/
- https://doc.qt.io/qt-6/accessible-qwidget.html
- https://konvrt.dev/blog/exif-metadata-stripping-guide-2026

## Open Questions

- None blocking the recommended roadmap. C2PA signing, Jpegli pip-installable bindings, and GUI preview/per-file design remain intentionally blocked or deferred elsewhere.
