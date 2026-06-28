# Research - ImgConverter

## Executive Summary
ImgConverter is a local-first Python 3.10+ PyQt6/Pillow batch image converter with unusually strong fidelity controls: EXIF/ICC/XMP preservation, selective GPS/device stripping, ExifTool recovery, C2PA presence reporting, atomic writes, CLI/GUI parity tests, trusted plugins, watch folders, duplicate review, proof runs, backend advice, and local release trust artifacts. The v3.3.1 drain left no actionable items in `ROADMAP.md`, and most competitor parity work is already shipped or parked in `Roadmap_Blocked.md`. Highest-value new direction: close two trust gaps before adding more surface area, then improve repeat-run visibility. Top opportunities: fix SDK-only C2PA verification, hash-pin package entry-point plugins, add persistent batch session history, keep dependency-security floors current, and keep blocked installer/channel work separated until signing or package infrastructure exists.

## Product Map
- Core workflows: source selection or CLI intake; recursive scan/filter/review; batch conversion with metadata/color/privacy controls; validation/report/support bundle; repeat automation through presets, cache, resume, watch folders, shell integration, and plugins.
- User personas: phone-photo users converting HEIC/AVIF; web developers producing WebP/AVIF/JXL assets; archivists preserving ICC/EXIF/XMP/provenance; privacy-conscious users stripping location/device fields; sysadmins automating local batches.
- Platforms and distribution: Windows, macOS, Linux; PyQt6 GUI and argparse CLI; source install via `pyproject.toml`; local PyInstaller release artifacts; conda-forge recipe scaffold; native installer/channel work blocked in `Roadmap_Blocked.md`.
- Key integrations and data flows: Pillow 12.2+, pillow-heif 1.4/libheif 1.23.0, PyQt6 6.8+, optional rawpy, pillow-jxl-plugin, ExifTool, pngquant/jpegtran/jpegoptim, watchdog, imagehash, pyvips, c2pa-python/c2patool; local config/cache under `~/.imgconverter` and `~/.cache/imgconverter`.

## Competitive Landscape
- XL Converter: strong Jpegli, lossless JPEG-to-JXL, and encoder-bundling story. Learn from its compression-first presets and external encoder packaging. Avoid making heavyweight external encoders mandatory.
- XnConvert: broadest desktop batch surface with 500+ formats, 80+ actions, watch folders, presets, and command-line export. Learn from action-chain ergonomics and preview/review depth. Avoid sacrificing ImgConverter's metadata/color fidelity to chase raw format count.
- File Converter: Windows Explorer context-menu adoption is its main advantage. ImgConverter already has shell integration; keep GUI setup and selected-file flows reliable rather than adding a separate explorer extension.
- Caesium: polished cross-platform compression UI with portable/installable builds and active translation coverage. Learn from its concise task UI and package expectations. Avoid narrowing ImgConverter to JPEG/PNG/WebP-only compression.
- Squoosh and Mazanoke: prove demand for private local/browser-side conversion and quality comparison. ImgConverter already matches the privacy stance in a native app; before/after slider remains correctly blocked until interactive GUI testing is available.
- Dinky: best new automation/productivity signal: per-preset watch folders, session history, before/after preview, local CLI/API, and update/install polish. ImgConverter has watch profiles and CLI parity; persistent batch history is the missing non-blocked idea.
- ConvertX and reaConverter: show demand for broad conversion servers and professional automation, including watch folders, context menus, command-line/DLL integration, and huge format matrices. ImgConverter should borrow automation diagnostics, not server/accounts/multi-user behavior.
- Czkawka: adjacent duplicate/similar image tool with cache, CLI/GUI split, multilingual support, and explicit warnings about unofficial packages. ImgConverter already has dedup pre-pass/review; package trust messaging is relevant to plugins and release artifacts.

## Security, Privacy, and Reliability
- Verified bug: `_verify_c2pa()` prefers `c2pa-python`, but `_finalize_metadata_report()` only calls it when `C2PATOOL_PATH` is present (`imgconverter.py:308`, `imgconverter.py:2335`). SDK-only installs therefore detect C2PA markers but never verify manifests.
- Trust gap: file-drop plugins are SHA-256 pinned, but package entry-point plugins are trusted by `ep:<package>==<version>:<name>` plus recorded version only (`imgconverter.py:694`, `imgconverter.py:838`). A same-version reinstall or local package mutation can still execute after initial trust.
- Dependency floor is currently sane: `Pillow>=12.2.0` covers multiple 2026 Pillow security fixes, and `pillow-heif>=1.4.0` bundles libheif 1.23.0/libde265 1.1.0. Keep floors synchronized across `requirements.txt`, `pyproject.toml`, and `packaging/conda-forge/meta.yaml`.
- Privacy posture remains category-leading: support bundles redact paths and omit source images; conversion reports expose dropped EXIF/ICC/XMP/IPTC/MakerNotes/C2PA fields; selective stripping preserves copyright/color while removing GPS/device data.
- Recovery guardrails are strong: atomic output writes, queue resume, cache, skip-existing, disk-space checks, validation, and GUI cancellation are already covered. Persistent session history would improve post-run recovery without adding cloud state.

## Architecture Assessment
- Single-file shape is deliberate and currently manageable, but trust/provenance logic is now clustered enough to justify focused helper extraction only when implementing the C2PA/plugin fixes.
- `ConvertOptions` is the right execution boundary and now covers multi-frame conversion. Future CLI/GUI additions should continue to flow through it instead of reintroducing positional-argument drift.
- Plugin architecture is valuable but under-documented for entry points: `PLUGINS.md` still emphasizes copying files into `~/.imgconverter/plugins`, while code discovers `imgconverter.plugins` entry points. The implementation should hash package metadata/code before docs expansion.
- Tests are broad for parser/GUI/README parity, conversion behavior, sidecars, plugins, vips, watch, and accessibility. Missing focused tests: SDK-only C2PA verification and package entry-point trust invalidation after same-version code changes.
- No repo-level build/test workflow exists by design. Local release checks should stay in tests or local commands; do not add `.github/workflows`.

## Rejected Ideas
- Server mode, accounts, shared history, or cloud storage in core: ConvertX/reaConverter validate demand, but this conflicts with the local-first desktop/CLI product. Storage remains a plugin-shape concern.
- Broad action-chain editor now: XnConvert/BatchPhoto prove demand, but `Roadmap_Blocked.md` already tracks reorderable task-chain presets as a design-blocked GUI migration.
- Before/after compare slider now: Squoosh/Dinky validate it, but the repo already keeps this blocked pending real display testing.
- Mobile app: XnConvert/Czkawka have mobile signals, but ImgConverter's installable value is desktop batch + CLI automation; mobile would be a separate product.
- Full i18n rollout now: File Converter/Caesium/Czkawka show translation value, but no current implementation blocker or stronger signal than the trust/reliability fixes above.
- Bundle Jpegli/cjxl now: XL Converter proves value, but the repo already tracks external JXL/Jpegli lanes in `Roadmap_Blocked.md` because portable binary packaging is the blocker.
- PNG 3.0 HDR/APNG/EXIF implementation now: W3C PNG 3.0 is relevant, but Pillow/libpng support must land before ImgConverter can expose it cleanly.
- C2PA signing by default: the spec and tooling support provenance workflows, but signing identity/trust-list UX would be premature; verification/reporting should be correct first.

## Sources

### Competitors and adjacent tools
- https://github.com/JacobDev1/xl-converter
- https://github.com/Tichau/FileConverter
- https://github.com/Lymphatus/caesium-image-compressor
- https://github.com/GoogleChromeLabs/squoosh
- https://github.com/qarmin/czkawka
- https://github.com/civilblur/mazanoke
- https://github.com/heyderekj/dinky
- https://github.com/C4illin/ConvertX
- https://github.com/ImageOptim/ImageOptim
- https://www.xnview.com/en/xnconvert/
- https://www.reaconverter.com/features/
- https://www.batchphoto.com/features.html

### Standards, dependencies, and security
- https://pillow.readthedocs.io/en/stable/releasenotes/12.2.0.html
- https://github.com/bigcat88/pillow_heif/blob/master/CHANGELOG.md
- https://spec.c2pa.org/specifications/specifications/2.4/
- https://jpeg.org/jpegxl/
- https://aomediacodec.github.io/av1-avif/
- https://www.w3.org/TR/png-3/
- https://docs.python.org/3/whatsnew/3.14.html
- https://doc.qt.io/qt-6/accessible-qwidget.html

### Project evidence
- README.md
- CLAUDE.md
- AGENTS.md
- CHANGELOG.md
- ROADMAP.md
- Roadmap_Blocked.md
- PLUGINS.md
- imgconverter.py
- tests/test_features.py
- tests/test_plugins.py
- tests/test_sidecars.py
- packaging/conda-forge/meta.yaml

## Open Questions
None.
