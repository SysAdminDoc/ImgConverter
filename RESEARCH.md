# Research — ImgConverter

## Executive Summary

### 2026-06-27 roadmap drain update

ImgConverter v3.3.1 drained the actionable backlog from the 2026-06-19 pass. CLI bounded scheduling, dead code cleanup, animated-file set lookups, native C2PA fallback, QOI dependency removal, watch/vips regression coverage, conda-forge sha guidance, smart auto quality, watch retries, scan/shell/watch/proof/advisor/preset/duplicate/matrix/palette GUI additions, and multi-frame `ConvertOptions` parity have shipped. `ROADMAP.md` is now actionable-only with no open codeable items; true external/design/test-infrastructure blockers remain in `Roadmap_Blocked.md`.

The latest functional fix is multi-frame conversion parity: `--frames all` / `--frames animate` now receives the same `ConvertOptions` object as single-frame conversion, covering quality, resize, compression, DPI, and metadata behavior, with tests for quality, resize, and EXIF preservation/stripping.

### 2026-06-19 competitive refresh (pass 2)

ImgConverter v3.3.0 was the most metadata-correct open-source batch converter surveyed: 4:4:4 chroma, ICC passthrough, selective GPS/device stripping, ExifTool tag-copy, atomic writes, CLI/GUI parity tests, plugin trust gates, and release provenance artifacts. At 8,610 lines in a single file with 128 tests, it shipped more fidelity controls than any competing OSS tool.

The prior research pass (v3.1.0 baseline) identified selective metadata stripping, Python 3.14 free-threading, post-batch actions, taskbar progress, machine-readable CLI output, perceptual dedup, RAM monitoring, and watchdog watch mode as the top 10 opportunities. **All 10 shipped** in v3.2.0–v3.3.0. The first competitive refresh added GUI scan review, watch-folder cockpit, proof runs, backend advice, preset bundles, duplicate review, format matrix, and command palette work; the codeable pieces are shipped as of v3.3.1.

This pass focuses on three areas the prior refresh didn't cover deeply enough:

1. **Security posture**: pillow-heif 1.4.0 upgraded to libheif 1.23.0 and libde265 1.1.0, covering all 18+ CVEs from the June 2026 wave. The current floor is adequate, but ongoing monitoring is needed.
2. **CLI bounded scheduling**: Fixed in v3.2/v3.3 drain work; CLI conversion now uses a bounded `max_inflight` queue.
3. **Test coverage gaps**: Watch mode, vips, and multi-frame coverage were added. ExifTool fidelity remains blocked on a local binary plus stable fixtures.

Top 5 new opportunities from this pass:

1. Security floor is adequate (pillow-heif 1.4.0 bundles libheif 1.23.0 + libde265 1.1.0).
2. CLI bounded future scheduling (shipped).
3. Dead code cleanup: `ALLOW_INCORRECT_HEADERS` guard, `HEIF_MAX_DECODE_BYTES` unused constant (shipped).
4. `_convert_animated_or_sequence` `ConvertOptions` parity (shipped in v3.3.1).
5. Pillow 13 deprecation prep: no direct `product_name` / `product_info` usage found; keep checking with prerelease Pillow wheels when available.

## Product Map

- **Core workflows**: drag/drop or CLI intake → recursive scan/filter → convert with fidelity controls → validate/log/report → resume/cache/watch for repeatable automation.
- **User personas**: phone-photo users converting HEIC/AVIF; web developers generating WebP/AVIF/JXL assets; archivists preserving metadata, ICC, and provenance; sysadmins running repeatable local batches; privacy-conscious users stripping GPS/device data.
- **Platforms and distribution**: Windows/macOS/Linux; PyQt6 GUI; argparse CLI; locally built PyInstaller artifacts (unsigned); conda-forge recipe scaffold; shell integration (Windows registry, Linux .desktop).
- **Key integrations**: Pillow 12.2+, pillow-heif 1.4/libheif 1.23.0, PyQt6 6.8+, optional rawpy/LibRaw, pillow-jxl/libjxl, ExifTool 13.55+, jpegoptim/jpegtran/pngquant, butteraugli/ffmpeg-quality-metrics, optional pyvips/libvips 8.18, watchdog 4.0+, imagehash, c2pa-python.

## Competitive Landscape

**XL Converter** (503 stars, Python/PyQt, GPL-3.0): Most architecturally similar. Has Jpegli integration (35% JPEG savings), lossless JPEG→JXL transcoding, RAM optimizer. ImgConverter's advantages: broader format coverage (12+ vs 4), ExifTool integration, plugin system, selective metadata stripping, CLI/GUI parity tests.

**XnConvert** (closed-source freeware, Qt/C++): 500+ formats, 80+ chained actions, CLI export to NConvert. Best batch review surface in the category. ImgConverter should learn from the pre-conversion review table, but avoid chasing format breadth at the cost of fidelity — XnConvert strips ICC profiles with default settings.

**File Converter** (14.5k stars, C#, Windows): Shell integration is the primary UX — Explorer right-click drives adoption. ImgConverter has `--register-shell` but CLI-only setup. The GUI shell integration manager (existing P1 roadmap item) is the right response.

**Caesium** (6k stars, C++/Qt6, GPL-3.0): Clean compression-focused UI. Community still requesting AVIF, HEIC, JXL support. ImgConverter already has all three. Target-file-size compression UX is good — ImgConverter already has `--target-kb`.

**Squoosh** (24.7k stars, WebAssembly, Google): Before/after split-view slider is gold standard for quality comparison. CLI deprecated (2023). Sample proof run (existing P2 roadmap item) is the pragmatic near-term alternative to the full slider design (blocked on interactive testing).

**Czkawka** (31.6k stars, Rust, MIT): Duplicate/similar image finder with 6 perceptual hash algorithms. Not a converter, but the dedup pre-pass concept maps to ImgConverter's scan phase. ImgConverter has CLI dedup; the GUI review panel (existing P2 roadmap item) completes the story.

**Converseen** (Qt/C++, 1,036 stars): Recently updated (0.15.2.5, June 2026). 100+ formats via ImageMagick. Minimal metadata handling. Code-signed Windows binaries via SignPath.io — a free code-signing service for open-source projects that could unblock ImgConverter's unsigned-binary distribution problem (currently in Roadmap_Blocked).

**Dinky** (443 stars, Swift/SwiftUI, macOS-only, 2026): Fastest-growing new competitor. Has smart quality detection (auto-detects photo vs graphic), per-preset watch folders, CLI + local API, URL compress, before/after preview, binary-search file size targeting. 35 MB installed. ImgConverter should learn from the smart quality detection concept and the per-preset watch folder model. Avoid: Apple-only, no metadata preservation story.

**Mazanoke** (2,624 stars, browser-based, GPL-3.0, 2025): Self-hosted privacy-first converter. All processing in-browser via WASM. Explosive growth. Confirms the privacy-first positioning ImgConverter already has.

**ConvertX** (17,033 stars, TypeScript/Bun, AGPL-3.0): Largest self-hosted converter. 1000+ formats. Server-side processing. Not a direct competitor (different deployment model) but shows the market for local-first converters.

**reaConverter** ($99/yr) and **BatchPhoto** ($50-$130): Commercial tools paywall watch folders, scheduled jobs, CLI automation, and action chains. ImgConverter's watch mode + hot-folder profiles match their core automation value. The watch-folder cockpit (existing P1) would close the GUI gap entirely.

**HandBrake** (video, queue UX model): Per-job JSON persistence, crash recovery, retry-on-failure, "When Done" actions with cancellable countdown, taskbar progress. ImgConverter has adopted the queue patterns (resume, when-done, taskbar progress) but lacks retry-on-failure for individual files.

## Security, Privacy, and Reliability

**libheif CVE tracking (June 2026)**
pillow-heif 1.4.0 updated its bundled libheif from 1.21.2 to 1.23.0 and libde265 from 1.0.16 to 1.1.0. libheif 1.22.0 fixed 18 CVEs (heap overflows, integer overflows, NULL pointer dereferences), and 1.23.0 fixed CVE-2026-50142 (unbounded heap allocation in sequence parser). The current `pillow-heif>=1.4.0` floor covers all known CVEs. However, CVE-2026-3950 (buffer overflow in Track::load, stsz/stts component, proof-of-concept public) affects libheif up to 1.21.2 and may need monitoring for future libheif patches. **libde265 CVE-2026-49346 (critical: signed integer overflow causing ~4 GB write into ~1 KB buffer) is fixed in v1.1.0, which pillow-heif 1.4.0 bundles. The floor is currently adequate.**

**Pillow 12.2.0 (current floor) — adequate:**
CVE-2026-42308 (font glyph overflow), CVE-2026-40192 (FITS decompression bomb), CVE-2026-25990 (PSD tile OOB write) all fixed. No newer CVEs at time of writing.

**Pillow 13 deprecations (Oct 2026):**
`ImageCms.ImageCmsProfile.product_name` and `product_info` will be removed. ImgConverter doesn't use these directly. Re-run the local test suite against Pillow 13 prerelease wheels when they are available.

**Python 3.14 free-threading:**
PEP 779 moves free-threaded builds to officially supported. 2.83x speedup benchmarked on image processing workloads. Keep free-threaded Python in local release testing; the 5-10% single-thread penalty is acceptable given the parallel batch use case.

**Code findings (verified):**
- `HEIF_MAX_DECODE_BYTES` and the removed pillow-heif `ALLOW_INCORRECT_HEADERS` guard were removed.
- CLI `_run_cli` now submits conversion futures through a bounded `max_inflight = workers * 2` queue.
- `_convert_animated_or_sequence` now honors `ConvertOptions` for quality, resize, compression, DPI, and metadata handling.
- `animated_files` membership checks now use a set after the multi-frame pre-pass.
- `packaging/conda-forge/meta.yaml` keeps an explicit `REPLACE_WITH_ACTUAL_HASH_BEFORE_CONDA_FORGE_PR` placeholder plus release-time instructions.

**Privacy position — strongest in category:**
ImgConverter is the only surveyed tool offering selective metadata stripping (`--strip-gps`, `--strip-device`) that preserves copyright and color profiles. The 2026 Reddit/HEIC GPS leak incident (HackerOne #1069039 — HEIC uploads preserving GPS through re-encoding) validates this approach. Community signal from r/photography, konvrt.dev, and ExifTool forums confirms selective stripping is the #1 unmet metadata need.

## Architecture Assessment

- **Single-file deliberate**: 8,610 lines in `imgconverter.py` is intentional and works. Helper extraction has begun (`_run_sidecar_hooks`, `_write_text_atomic`). Next extraction candidate: metadata handling (presence detection, selective stripping, report generation) — currently scattered across `_open_image()`, `convert_file()`, `_metadata_presence_from_image()`, `_strip_exif_fields()`, and `_finalize_metadata_report()`.
- **ConvertOptions boundary**: Exists and works. `convert_file()` still accepts 35 keyword args alongside `opts=` for backward compat. The dual interface is the root cause of parity drift — every new field requires updating both the dataclass and the kwargs-to-locals block (lines 2771-2805).
- **Plugin registry**: Well-shaped for decoder/encoder/storage. Entry-point discovery (existing P2) is the natural next step. Trust-by-SHA-256 is correct for file-drop plugins.
- **Test coverage**: 150+ tests cover core conversion, CLI parsing, presets, templates, plugins, sidecars, roundtrip, watch-mode option forwarding, vips regressions, and multi-frame `ConvertOptions` parity. Remaining gap: ExifTool fidelity fixture coverage.
- **Local release checks**: There is no GitHub Actions build/test pipeline. Run tests, packaging, and release verification locally before pushing or uploading artifacts.
- **vips backend**: Correctly flagged experimental. Rejects unsupported options. metadata/resize/watermark/canvas/tone-map all explicitly unsupported. libvips 8.18's Camera RAW and UltraHDR JPEG support could expand utility but metadata parity must come first.

## Rejected Ideas

- **Jpegli as bundled core encoder now**: 35% JPEG compression improvement is real (XL Converter, Google benchmarks), but no pip-installable Python binding exists. `cjpegli` subprocess wrapping is fragile. Revisit when a wheel ships. Source: libjxl project, XL Converter.
- **Full AI editing suite** (upscale/denoise/background-remove): Topaz Photo AI ($199/yr). Conflicts with local-first philosophy. Source: Topaz, Filestar.
- **Cloud sync / accounts / CDN transforms**: Contradicts local-first. Source: Cloudinary, Filestar.
- **HEIC output by default**: Patent encumbrance via HEVC pools (Access Advance extended to June 30, 2026). Source: Access Advance, Firefox bug 1402293.
- **WebP 2**: Confirmed dead by Google. Source: chromium libwebp2 README.
- **Nuitka as PyInstaller replacement**: PyQt6 threading broken in Nuitka. Source: Nuitka PyQt6 plugin.
- **Full i18n rollout now**: No community demand signal stronger than privacy, reliability, and automation gaps. Source: XnConvert has 20+ languages but ImgConverter's user base is English-dominant.
- **Duplicate detection with ML models**: Czkawka proves perceptual hashing works without ML. Heavy deps contradict philosophy. Source: Czkawka architecture.
- **Mobile companion app**: Separate product, separate QA. Source: existing roadmap rejection.
- **PNG 3.0 HDR output**: Requires Pillow to implement HDR chunks first. No action until upstream lands. Source: W3C PNG 3.0 Recommendation (June 2025).
- **SVT-AV1 4.0 tune parameters in GUI**: Pillow's AVIF encoder doesn't expose SVT-AV1 tune flags directly. Source: SVT-AV1 4.0 release, Pillow AvifImagePlugin source.
- **Per-file retry on failure**: HandBrake has this for video. For image conversion, failures are almost always deterministic (corrupt file, unsupported feature) — retrying won't help. Log the error and move on.
- **Automatic backend switching**: The backend policy advisor (existing P2) should recommend but never auto-switch. Users must opt in explicitly. Source: reaConverter's "auto-optimize" causes user confusion.

## Sources

### Competitors and adjacent tools
- https://www.xnview.com/en/xnconvert/
- https://github.com/Tichau/FileConverter
- https://github.com/Lymphatus/caesium-image-compressor
- https://github.com/GoogleChromeLabs/squoosh
- https://github.com/JacobDev1/xl-converter
- https://github.com/qarmin/czkawka
- https://github.com/Faster3ck/Converseen
- https://github.com/ImageOptim/ImageOptim
- https://github.com/heyderekj/dinky
- https://github.com/civilblur/mazanoke
- https://github.com/C4illin/ConvertX
- https://www.reaconverter.com/
- https://www.batchphoto.com/
- https://github.com/niclas-niclas/pillow-jxl-plugin
- https://github.com/libvips/pyvips
- https://opensource.contentauthenticity.org/docs/c2pa-python/

### Community signal
- https://www.reddit.com/r/jpegxl/
- https://www.reddit.com/r/photography/
- https://konvrt.dev/blog/exif-metadata-stripping-guide-2026
- https://fast.io/resources/social-media-photo-metadata-platforms-strip/

### Standards and specifications
- https://jpeg.org/jpegxl/
- https://aomediacodec.github.io/av1-avif/v1.2.0.html
- https://spec.c2pa.org/specifications/specifications/2.4/
- https://www.w3.org/TR/png-3/
- https://docs.python.org/3/whatsnew/3.14.html

### Dependencies and security
- https://pillow.readthedocs.io/en/stable/releasenotes/12.2.0.html
- https://github.com/bigcat88/pillow_heif/blob/master/CHANGELOG.md
- https://www.sentinelone.com/vulnerability-database/cve-2026-3950/
- https://ubuntu.com/security/notices/USN-7952-1
- https://github.com/libjxl/libjxl
- https://nvd.nist.gov/vuln/

### UX, accessibility, and distribution
- https://www.w3.org/TR/wcag2ict-22/
- https://doc.qt.io/qt-6/accessible-qwidget.html
- https://forum.qt.io/topic/159776/accessibility-issue-with-radio-buttons-in-pyqt6-setting
- https://signpath.io/ (free code signing for OSS — used by Converseen)
- https://github.com/MLT-solutions/Py2MSIX (MSIX packaging reduces AV false positives)

### Performance benchmarks
- https://catskull.net/libaom-vs-svtav1-vs-rav1e-2025.html
- https://webp2png.co/blog/avif-encoding-performance-benchmarks
- https://towardsdatascience.com/python-3-14-and-the-end-of-the-gil/

## Ecosystem Notes

- **QOI support is native in Pillow**: Since ImgConverter's floor is Pillow 12.2, QOI read/write is covered by Pillow and the standalone `qoi` package is no longer needed.
- **C2PA Python SDK v0.5.0**: `pip install c2pa-python` provides native signing/verification without shelling out to `c2patool`. Dual-licensed Apache 2.0/MIT. Could replace the subprocess-based `_verify_c2pa()` function for a more robust integration.
- **Jpegli has moved to google/jpegli**: Dedicated repo, still no PyPI wheel. Access via `imagecodecs` package or subprocess wrapping of `cjpegli`/`djpegli` binaries.
- **AVIF 1.2.0 spec** (Oct 2025): Sample transforms for >12-bit, gain map signaling for HDR. SVT-AV1 4.1 (March 2026) improved still image coding efficiency.
- **PNG 3.0** (June 2025): cICP chunk for HDR, native APNG, EXIF chunks. No Pillow support yet — blocked on libpng upstream.
- **SignPath.io**: Free code signing for open-source projects. Converseen uses it for Windows binaries. Could unblock ImgConverter's unsigned-binary problem without the $300+/yr Authenticode cost currently listed in Roadmap_Blocked.
- **Py2MSIX**: GUI tool for wrapping PyInstaller builds into signed MSIX packages. Significantly reduces AV false positives vs raw .exe. $19 one-time Microsoft Store developer account handles signing.

## Open Questions

- **Pillow 13 pre-release CI**: When do Pillow 13 dev wheels start appearing on PyPI/TestPyPI? Add a CI job at that point to catch deprecation breakage early. Key risk: `ImageCms.ImageCmsProfile.product_name` removal.
- **Free-threaded PyQt6**: Python 3.14t works in CI for CLI mode, but Qt's event loop + GIL-less threading is not production-ready. Keep the 3.14t CI job but don't recommend free-threaded builds for GUI mode.
