# HEICShift Roadmap

**Current version:** v2.8.0 · **Roadmap revision:** 2026-05-17 · **Supersedes:** 2026-04-23 draft

Universal image batch converter. 12+ input families → 6 output formats (JPEG / PNG / WebP / AVIF / TIFF / JXL) with metadata, color-profile, and orientation fidelity. PyQt6 GUI + headless CLI parity, single-file Python 3.10+, MIT, PyInstaller binaries for Windows / macOS / Linux.

This roadmap is the working plan. Every line item traces to a source in the Appendix. Tiers are stable; placement reasoning is one sentence per item.

---

## Philosophy (constraints on what gets in)

Anything new must respect these. Items that conflict are flagged explicitly.

1. **Correct defaults over breadth** — 4:4:4 chroma, ICC passthrough, EXIF auto-rotate, atomic writes. The whole "Why HEICShift" pitch lives here.
2. **CLI ↔ GUI parity** — every GUI control gets a `--flag` and vice versa.
3. **Local-first** — no telemetry, no cloud upload, no account. The HN "privacy-first converter" momentum ([HN 46230024](https://news.ycombinator.com/item?id=46230024), [HN 46427287](https://news.ycombinator.com/item?id=46427287)) is the wedge.
4. **Graceful optional-dep degradation** — missing rawpy / pillow-jxl / qoi quietly disables those families; nothing crashes.
5. **Single-file ship** — `heicshift.py` + PyInstaller binary. Plugin systems must be opt-in, not required.
6. **Non-destructive default** — never delete the source unless `--in-place` was explicit. Several PyPI "HEIC converter" tools delete originals by default; we don't ([Snyk audit](https://snyk.io/advisor/python/heic-image-converter), [nick8592 example](https://github.com/nick8592/HEIC-to-JPEG)).
7. **Catppuccin Mocha dark UI, square/8-px backdrops only** — no pill/oval badges anywhere (per global rules).

---

## Tier: Now (v2.9.0 → v3.0.0, next 1–2 releases)

Highest-leverage, lowest-risk. Closes known CVE exposure, ships the metadata-fidelity story end-to-end, and fixes today's quiet correctness bugs. Most are days of work, not weeks.

### Security & dependency floor

- **`requirements.txt` + `pyproject.toml` with pinned floors** — currently `_bootstrap()` runtime-pip-installs whatever is latest; venvs that froze on old wheels stay exposed to libheif / libjxl / Pillow CVEs forever. Pin `Pillow>=11.3.0, pillow-heif>=1.3.0, pillow-jxl-plugin>=1.3.6, rawpy>=0.27.0, PyQt6>=6.8`. Add a startup version check that warns when below floor. Effort 1, Impact 5. *Closes: [CVE-2025-48379](https://nvd.nist.gov/vuln/detail/CVE-2025-48379), [CVE-2024-28219](https://www.sentinelone.com/vulnerability-database/cve-2024-28219/), [CVE-2025-29482](https://dailycve.com/libheif-buffer-overflow-cve-2025-29482-critical/), [CVE-2024-41311](https://www.sentinelone.com/vulnerability-database/cve-2024-41311/), [CVE-2024-11403 / 11498](https://github.com/libjxl/libjxl/releases), [CVE-2026-28231](https://github.com/bigcat88/pillow_heif/blob/master/CHANGELOG.md).*
- **Replace runtime `_bootstrap()` pip-install with explicit install instructions** — auto-install-at-runtime violates the user's standard project pattern and surprises users in restricted environments. Keep an `--install-deps` opt-in subcommand instead. Effort 1, Impact 3.
- **libheif memory cap** — wire `pillow_heif`'s `set_security_limits()` / max-decode-memory knob so a hostile HEIC can't OOM the host. New in libheif 1.20 ([1.20 release notes](https://github.com/strukturag/libheif/releases)). Effort 1, Impact 4.
- **Symlink loop guard in `scan_directory`** — `p.is_file()` happily follows symlinks; a recursive scan over a directory containing a symlink to its parent loops until the path-length limit. Add a `visited_inodes` set; skip already-seen inodes. Effort 1, Impact 3.

### Correctness & metadata fidelity (closes today's quiet bugs)

- **ExifTool shell-out for metadata transport** — Pillow's EXIF model silently drops MakerNotes, GPS sub-IFDs, IPTC, and sidecar XMP, which is the #1 community complaint about HEIC→JPG converters ([Pillow #3270](https://github.com/python-pillow/Pillow/issues/3270), [robservatory](https://robservatory.com/retain-location-metadata-on-photos-sourced-from-photos/), [Apple Community 256257398](https://discussions.apple.com/thread/256257398)). Pattern: convert pixels with Pillow, then `exiftool -tagsfromfile src -all:all -unsafe -icc_profile dst` ([ExifTool FAQ](https://exiftool.org/faq.html)). Use `-stay_open True` for batch perf ([exiftool.org](https://exiftool.org/)). Bundled binary, optional fallback to Pillow when ExifTool missing. Effort 3, Impact 5.
- **ICC-aware CMYK → RGB conversion** — Pillow's `.convert("RGB")` ignores embedded ICC profile, causing Display P3 → sRGB color shifts that match the canonical iPhone-HEIC complaint ([Apple Community 254814534](https://discussions.apple.com/thread/254814534), [ImageMagick #4391](https://github.com/ImageMagick/ImageMagick/discussions/4391), [Willow changelog](https://willow.wagtail.org/latest/changelog.html)). Always run `ImageCms.profileToProfile()` when source ICC is non-sRGB and output mode requires RGB. Effort 2, Impact 5.
- **HEIC orientation double-rotation regression test** — `pillow_heif` reworked encode in v1.14 to write orientation to the HEIF header instead of pre-rotating ([CHANGELOG](https://github.com/bigcat88/pillow_heif/blob/master/CHANGELOG.md)); combined with `ImageOps.exif_transpose()` upstream regressions ([Pillow #9294](https://github.com/python-pillow/Pillow/issues/9294)) and the ImageMagick orientation discrepancy ([IM #1232](https://github.com/ImageMagick/ImageMagick/issues/1232), [Geeqie #923](https://github.com/BestImageViewer/geeqie/issues/923)), HEICShift needs a fixture-based round-trip test before every release. Effort 2, Impact 4.
- **Output verification by re-decode + dimensions match** — current `Image.verify()` only checks header validity, not that pixel count survived. Add `assert Image.open(dst).size == img.size`. Effort 1, Impact 3.

### Format coverage (cheap wins)

- **ISO 21496-1 HDR gain-map preservation** — finalized Mar 2025, adopted by iOS 18, Android 15, Adobe LR ([ISO standard](https://www.iso.org/standard/86775.html), [Greg Benz primer](https://gregbenzphotography.com/hdr-photos/iso-21496-1-gain-maps-share-hdr-photos/), [Android Authority](https://www.androidauthority.com/google-apple-hdr-photo-standard-3495035/)). libheif itself doesn't yet support it ([libheif #1685](https://github.com/strukturag/libheif/issues/1685)); libavif does. Path: extract gain map via `avifenc`-side tooling, emit alongside the SDR base or transcode JPEG-with-gainmap when target supports it. Sidecar `.gainmap.avif` is the v0.1 of this. Effort 4, Impact 5. *Single biggest "looks broken" fix for iPhone Adaptive HDR users.*
- **Live Photo .HEIC + .MOV pairing** — already in 2026-04 draft. Detect sibling `.MOV`, copy/move alongside converted still, optionally rename to match. Iconic feature nobody else gets right. Effort 2, Impact 4.
- **HEIC depth-map / aux-image preservation** — `pillow_heif >= 0.20` exposes `info["depth_images"]` (Portrait-mode depth, iPhone Pro ProRAW depth). Emit as sidecar `.depth.png` 16-bit. Effort 2, Impact 3. *Source: [pillow_heif CHANGELOG](https://github.com/bigcat88/pillow_heif/blob/master/CHANGELOG.md).*
- **AVIF output: switch from pillow-heif path to Pillow 11.3 native** — Pillow 11.3 ships native libaom/dav1d wheels ([Pillow 11.3.0 release notes](https://pillow.readthedocs.io/en/stable/releasenotes/11.3.0.html)); `pillow_heif >= 1.0` deprecated its own AVIF encoder ([pillow_heif CHANGELOG](https://github.com/bigcat88/pillow_heif/blob/master/CHANGELOG.md)). Smaller binary, less x265 GPL surface. Effort 1, Impact 3.

### CLI & automation

- **Output path template language** — converges IrfanView `$D`/`$N`/`###` ([helpmax docs](http://irfanview.helpmax.net/en/file-menu/batch-conversionrename/), [multi-folder pattern](https://onezeronull.com/2015/06/27/multi-folder-usage-of-irfanviews-batch-mode/)) and RawTherapee `%p1/%f/%s3` ([RawPedia Queue](https://rawpedia.rawtherapee.com/Queue)) into one substitution language: `{stem} {ext} {width} {height} {date:%Y-%m} {seq:###} {src_dir} {rel_dir}`. Replaces today's anemic prefix/suffix. Effort 2, Impact 4.
- **JSON report mode** (`--report out.json`) — current CSV is human-first; CI / Ansible / cron need NDJSON streamed to stdout *and* a final summary file. Schema per file: `{src, dst, fmt, size_in, size_out, elapsed, warnings[], ok}`. Effort 2, Impact 3.
- **Preset JSON load** (`--preset name` + `~/.heicshift/presets/*.json`) — already in 2026-04 draft. Dump the four built-in `PRESETS` to disk on first run; expose `--preset` and `--list-presets`. Effort 2, Impact 3.
- **Structured exit-code matrix** — `0 OK · 1 partial · 2 input-error · 3 dep-missing · 4 disk-full · 5 cancelled`. Today's `0 / 1 / 2` collapses "no JXL plugin" with "directory missing." Effort 1, Impact 2.
- **`--exclude PATTERN` glob filter** — extends scan with negation; matches XnConvert / find-style usage. Effort 1, Impact 2.

### Distribution & repo hygiene

- **Smoke-test CI** — even one round-trip fixture per output format with hash + size diff guard catches the 2024 / 2025 Pillow regressions in the dep table. Add to `.github/workflows/build.yml`. Effort 2, Impact 4. *Project rule normally says "no tests unless asked" — flagging the override: a converter that markets correctness needs a regression net.*
- **`pyproject.toml`** — make `pip install -e .` work; declare entry-point `heicshift = heicshift:main`. Lets contributors hack on CLI without launching GUI. Pattern: [NeverMendel/heif-convert](https://github.com/NeverMendel/heif-convert). Effort 1, Impact 2.
- **Branch protection on `main` + signed releases** — already de facto from project rules; verify and document in CONTRIBUTING.md. Effort 1, Impact 1.

---

## Tier: Next (v3.1.0 → v3.3.0)

Bigger surface-area features that need design first. All have a clear "why" but earn their place against the Now backlog.

### Multi-frame & animation

- **Image-sequence / animation handling** — Live Photo dual-image HEIC, animated WebP / AVIF / HEIF, APNG, GIF, multi-page TIFF. `libheif 1.21` shipped full HEIF sequence read/write across codecs ([1.21 release](https://github.com/strukturag/libheif/releases)). UX: when source has > 1 frame, prompt for "first frame only" / "extract all to {seq:###}.{ext}" / "preserve as animated {fmt}". Use `ImageSequence.Iterator(img)` for non-HEIC paths. Today the engine silently drops extra frames. Effort 5, Impact 4.
- **AVIF image sequence (`.avifs`) decode** — AOMedia spec v1.0.0 ([AOMedia AVIF spec](https://aomediacodec.github.io/av1-avif/v1.0.0.html), [Mozilla bug 1788119](https://bugzilla.mozilla.org/show_bug.cgi?id=1788119)). Free-rides on the HEIF sequence work above. Effort 2, Impact 2.

### Pipeline

- **Watch-folder mode** (`--watch DIR`) — already in 2026-04 draft. Apply a preset on filesystem events; debounce 500 ms for atomic-rename-aware copies; lockfile guards against double-processing across instances. Use `watchdog` PyPI dep. Found in XnConvert, ImBatch, BatchPhoto (Pro), ImageOptim. Effort 4, Impact 4.
- **Lossless JPEG → JXL transcoding mode** — libjxl's signature feature: bit-exact transcode with ~20 % size reduction, fully reversible ([Wikipedia JXL](https://en.wikipedia.org/wiki/JPEG_XL), [HN 35212522](https://news.ycombinator.com/item?id=35212522)). Trigger when source is JPEG, target is JXL, and `--lossless` is set. `pillow_jxl` already exposes this. Effort 2, Impact 4. *Earns its keep on archival workflows the moment Chrome 145 ships JXL default ([Chromium decision](https://devclass.com/2025/11/24/googles-chromium-team-decides-it-will-add-jpeg-xl-support-reverses-obsolete-declaration/), [Register coverage](https://www.theregister.com/2026/01/14/google_rekindles_relationship_with_jilted/)).*
- **Recompress-without-transcode mode** for JPEG → JPEG — call `jpegoptim` / `mozjpeg jpegtran` when input ext matches output ext and `--recompress` is set; preserves pixels exactly, strips per `--strip-metadata`, optionally progressive. Today's "same-format guard" just skips. Effort 3, Impact 3. *Pattern from [Caesium](https://github.com/Lymphatus/caesium-image-compressor), [ImageOptim](https://github.com/ImageOptim/ImageOptim), [jpegoptim](https://github.com/tjko/jpegoptim).*
- **Wide-gamut / 10-bit HEIC → AVIF / JXL preservation** — current pipeline always downcasts to 8-bit RGB before save. iPhone HEIC is 10-bit + Display P3; AVIF + JXL handle this natively. Path: detect source bit depth via `pillow_heif.read_heif().info["bit_depth"]`, route to `mode="I;16"` or 16-bit ndarray → save with `bits_per_sample=10`. Effort 4, Impact 4.
- **`--quality-mode {fixed, target-ssim, target-kb}`** — binary-search quality to hit an SSIM / size target. Useful for "make this ≤ 200 KB" or "stay above 0.98 SSIM." Effort 3, Impact 3.
- **Conditional re-encode** (`--only-if-smaller`) — keep original when re-encoded output is ≥ N % of source size. Avoids "compressed" output being larger than input. Effort 1, Impact 2.

### Color

- **BT.2020 / PQ / HLG HDR awareness** — already in 2026-04 draft; clarified by Android 15 dual-write of Ultra HDR + ISO 21496-1 ([PhoneArena](https://www.phonearena.com/news/hdr-photos-across-android-and-ios_id164255)). Detect HDR color space, offer tone-map to sRGB with curve choice (BT.2390, Hable, Reinhard). Effort 5, Impact 3. *Power-user feature; most users want "just preserve it."*
- **Explicit ICC profile override** — already in 2026-04 draft. Embed chosen profile (sRGB v4, Display P3, Rec.2020). Effort 2, Impact 2.
- **XMP sidecar emit mode** — already in 2026-04 draft. Emit `.xmp` next to stripped output. Effort 2, Impact 2.

### UX

- **Reorderable task-chain presets** — preset = ordered list of `[op, params]`. Order matters (rotate-then-crop ≠ crop-then-rotate). Pattern from [BIMP](https://github.com/alessandrofrancesconi/gimp-plugin-bimp), [ImBatch](https://www.highmotionsoftware.com/products/imbatch). Replaces today's flat dict preset. Effort 5, Impact 4.
- **Before/after compare slider** — debounced re-encode of a single sample file as quality slider moves; the Squoosh UX everyone loves ([Squoosh repo](https://github.com/GoogleChromeLabs/squoosh), [deepwiki](https://deepwiki.com/GoogleChromeLabs/squoosh)). PyQt6: `QGraphicsView` + `QGraphicsScene` with two overlapping `QGraphicsPixmapItem`, draggable `QGraphicsRectItem` clip mask. Effort 4, Impact 4.
- **"Already optimized" content-hash cache** — Pattern from ImageOptim ([howto](https://imageoptim.com/howto.html)). Key = `sha256(src_bytes) + sha256(preset_json)`; value = `(dst_bytes_sha256, dst_size)`. Skips re-work on repeat runs. Stored at `~/.cache/heicshift/seen.sqlite`. Effort 3, Impact 3.
- **Per-file override** — already in 2026-04 draft (right-click row → change format/quality just for that entry). Effort 3, Impact 3.
- **Drag converted files out of log to Explorer / Finder** — already in 2026-04 draft. Qt has `QMimeData` with file URIs. Effort 2, Impact 2.
- **Right-click "Convert with HEICShift"** shell integration — Windows Explorer (registry `HKCU\Software\Classes\*\shell\HEICShift`), macOS Quick Action (`.workflow`), Linux `.desktop` MIME action. Found in IrfanView, ImBatch, reaConverter ([reaConverter features](https://www.reaconverter.com/features/)). Effort 3, Impact 4.
- **Resume-able / pause-able queue** with state persisted to `~/.cache/heicshift/queue.json` — 10k-file batches survive power cycles. Pattern from RawTherapee ([Queue docs](https://rawpedia.rawtherapee.com/Queue)). Effort 3, Impact 3.

### Output

- **Watermark module (text + PNG overlay)** — paywalled feature in BatchPhoto, FastStone, Pixillion Plus ([BatchPhoto comparison](https://www.batchphoto.com/comparison.html), [FastStone review](https://www.avaide.com/photo-editing/faststone-photo-resizer-review/), [NCH Pixillion licensing](https://help.nchsoftware.com/help/en/pixillion/win/141.html)). Pillow `ImageDraw.text` + paste; opacity slider; 9-position grid. Effort 3, Impact 3.
- **DPI override** — write `dpi` save kwarg (JPEG / TIFF / PNG support it). Pattern from FastStone, ImBatch. Effort 1, Impact 2.
- **Canvas resize with background fill** — different from image resize: pads to canvas size, configurable bg color (transparent if format allows). Pattern from FastStone, Squoosh. Effort 2, Impact 2.
- **Output validator cross-check** (already in 2026-04 draft as `ffprobe`/`dcraw`/Pillow cross-pass) — for archival mode, run a second decoder over the output and compare dimensions / channels / hash. Effort 3, Impact 3.

### Auto-update

- **Update check against GitHub releases** — opt-in, runs on launch with 24-hour throttle. `requests.get("https://api.github.com/repos/SysAdminDoc/HEICShift/releases/latest")` → compare to `APP_VERSION`. Setting persisted in `QSettings`; off by default. Effort 1, Impact 2.

### Accessibility & diagnostics

- **Accessibility pass** — Qt offers `setAccessibleName` / `setAccessibleDescription` / explicit focus order via `setTabOrder` / `QShortcut` for screen-reader and keyboard-only users. Today's `MainWindow` sets neither (verified — zero `setAccessibleName` / `setStatusTip` calls in source). Add labels on every interactive control; verify focus order matches visual order; confirm Catppuccin Mocha palette passes WCAG AA contrast against the existing badge styles. Effort 2, Impact 2. *Project rule: "No keyboard shortcuts" stands — this is screen-reader / focus order, not new accelerators.*
- **Persistent diagnostic log** — current log panel is in-memory only; users reporting bugs have to manually export. Add `~/.cache/heicshift/heicshift.log` with rotating handler (5 MB × 3 files), opt-in via `--log-file` / settings. Captures startup dep versions, every conversion warning, every cancellation. Effort 1, Impact 3.
- **QSettings schema migration** — when settings shape changes (e.g. flat-dict presets → ordered task chains in Next tier), today's `_restore_state()` will quietly drop unknown keys. Add a `settings_version` int; on bump, run a typed migration that maps v1 keys to v2. Effort 2, Impact 2.

---

## Tier: Later (v3.4+ and beyond)

Architectural shifts. Each one is real work and changes the shape of the codebase.

### Performance & runtime

- **Free-threaded CPython 3.13t / 3.14t first-class support** — Python 3.14 promoted no-GIL to officially supported via PEP 779 ([Python 3.14 What's New](https://docs.python.org/3/whatsnew/3.14.html)). Pillow 11.0+ ([release notes](https://pillow.readthedocs.io/en/stable/releasenotes/11.0.0.html)) and pillow_heif 1.3 ([CHANGELOG](https://github.com/bigcat88/pillow_heif/blob/master/CHANGELOG.md)) opted in; reported 3.5× speedup on 4 cores via `ThreadPoolExecutor` ([Quansight rollout](https://labs.quansight.org/blog/free-threaded-python-rollout)). Guard with `sys._is_gil_enabled()` after imports — `qoi`/`rawpy`/PyQt6 may silently re-enable GIL. Path: ship a `heicshift-ft` build alongside `heicshift`. Effort 4, Impact 4.
- **Process-pool option for non-FT Python** (`--use-processes`) — `ProcessPoolExecutor` sidesteps GIL today; cost is per-image fork overhead. Use for batches > N images. Effort 3, Impact 3.
- **Optional pyvips backend** for files > 100 MP — libvips streams tiles instead of loading full bitmap ([libvips how-it-works](https://www.libvips.org/API/8.17/how-it-works.html), [pyvips intro](https://libvips.github.io/pyvips/intro.html)); shrink-on-load avoids decoding to full res when output is smaller. Add `--backend {pillow,vips}` flag; auto-select vips when source pixel count > 100M. Effort 5, Impact 3.
- **macOS native ImageIO path** — `pyobjc` → `CGImageDestination` writes HEIC / AVIF using Apple's hardware-accelerated, signed codecs; bypasses libheif entirely on Mac. Avoids the x265 GPL question for that platform. Effort 4, Impact 3.
- **GPU codec hooks (opt-in)** — nvJPEG decode for batches of huge JPEGs (NVIDIA-only); Windows Media Foundation HEIF / AVIF on Win11. Hard to ship as part of the single-file binary; flag as optional plugin. Effort 5, Impact 2.
- **Multi-encoder shootout for AVIF** — run rav1e, aom, SVT-AV1 in parallel, keep smallest output below quality target. Pattern from [ImageOptim](https://imageoptim.com/howto.html). Effort 3, Impact 2.

### Format coverage stretch

- **HTJ2K (High-Throughput JPEG 2000) encode** via OpenJPH ([OpenJPH](https://github.com/aous72/OpenJPH)) — decode already works free via OpenJPEG 2.5 ([Phoronix](https://www.phoronix.com/news/OpenJPEG-2.5-Brings-HTJ2K)). For DICOM / cinema users. Effort 4, Impact 1.
- **Apple ProRAW tone-map preservation** — Capture One reads ProRAW's `ProfileToneCurve`; rawpy discards it ([Capture One ProRAW](https://support.captureone.com/hc/en-us/articles/9335283604509-Apple-ProRAW-Support)). Path: parse DNG tone-curve tag, apply before save. Effort 4, Impact 2.
- **SVG / PDF page rasterization input** — converging pattern from Converseen ([itsfoss](https://itsfoss.com/converseen/)) and Qt 6.7's expanded SVG support ([Qt 6.7 blog](https://www.qt.io/blog/qt-6.7-released)). Pillow doesn't do either natively; needs `pypdfium2` + `cairosvg`. Effort 3, Impact 2.
- **PSD / XCF / EXR / DPX / FITS input** — territory currently owned by ImageMagick / Converseen. EXR (HDR) is the only one that fits the modern-codec philosophy; the rest are nice-to-have. Effort 4, Impact 2.

### Extensibility & distribution

- **Plugin system** — already in 2026-04 nice-to-haves. Drop `.py` into `~/.heicshift/plugins/`; auto-discovered classes implementing `Decoder.supports(suffix)` / `Encoder.save(img, path, opts)`. Lets users add formats without forking. Effort 5, Impact 3. *Tension with single-file ship — opt-in only, no required plugins.*
- **Sidecar JSON with reconstructable conversion params** — pattern from darktable's XMP sidecars ([sidecar docs](https://docs.darktable.org/usermanual/development/en/overview/sidecar-files/sidecar/)). On every conversion, write `output.jpg.heicshift.json` capturing source hash, all preset params, version, timestamp. Output becomes reproducible 5 years later. Optional via flag. Effort 2, Impact 3.
- **Storage module abstraction** — output destinations: local FS (default), S3, FTP, SFTP, Dropbox. Pattern from darktable ([export module docs](https://docs.darktable.org/usermanual/4.6/en/module-reference/utility-modules/shared/export/)). Each destination = small class with `write(bytes, key)`. Effort 4, Impact 2.
- **conda-forge recipe** — `pillow-heif`, `pillow`, `pyqt6`, `rawpy`, `pillow-jxl-plugin` all already on conda-forge ([conda-forge pillow-heif](https://anaconda.org/conda-forge/pillow-heif)). Recipe yields Linux distro packaging via grayskull. Effort 2, Impact 2.
- **Pyodide / WASM browser build** — `pillow_heif`, `libheif`, `libde265` all in Pyodide ≥ 0.26 ([Pyodide changelog](https://pyodide.org/en/stable/project/changelog.html), [pi-heif](https://pypi.org/project/pi-heif/)). Single-page HEICShift that runs entirely in-browser, no install, no upload. Strong fit for the local-first wedge. Effort 5, Impact 3.
- **Multi-platform shell integration installers** — Windows MSI (WiX), macOS PKG / DMG, Linux `.deb`/`.rpm`/`.AppImage` with desktop file + MIME associations. Effort 3, Impact 3.

### Quality measurement

- **VMAF / SSIM / butteraugli "verify lossless" mode** — `ffmpeg-quality-metrics` ([PyPI](https://pypi.org/project/ffmpeg-quality-metrics/)) wraps libvmaf; butteraugli ships from libjxl-tools ([butteraugli](https://github.com/google/butteraugli)). Per-file score in CSV / JSON report; warn when butteraugli > threshold. Effort 3, Impact 2.

---

## Tier: Under Consideration

Worth exploring; not committed.

- **Face-aware quality boost (ROI)** — already in 2026-04 nice-to-haves. JPEG ROI quality / AVIF film-grain-off in face regions. Needs face detector (MediaPipe / OpenCV Haar) which adds a heavy dep. Reject for now; revisit if HEICShift adds any ML deps for another reason.
- **i18n / localization** — Qt has `lupdate` / `lrelease`. Worth doing if external contributor volunteers a language; not worth solo effort given Slack/forum signal is English-dominant in the target user base.
- **Telemetry / opt-in crash report** — even a local-only `~/.cache/heicshift/crash.log` would help diagnose user reports. Hard rule: never phone home without explicit opt-in.
- **Mobile companion (iOS / Android)** — Permute, BatchPhoto, several "HEIC converter" mobile apps exist. Out of scope for the desktop project; if anything, build the Pyodide web app instead.
- **CDN bridge** — Cloudinary / Imgix style URL-API transforms. Contradicts local-first philosophy. Skip.

---

## Tier: Rejected (with reasoning)

- **HEIC output by default** — patent encumbrance via HEVC pools ([Access Advance](https://accessadvance.com/licensing-programs/hevc-advance/)) runs into the 2030s; x265 encoder is GPL contagion for the bundled PyInstaller binary. Optional behind a flag is OK; never default. *Confirmed by Firefox bug 1402293's 9-year stall.*
- **WebP 2** — Google's own position: "WebP 2 will not be released as an image format" ([chromium libwebp2 README](https://chromium.googlesource.com/codecs/libwebp2/)). Dead format.
- **AI features (upscale / colorize / background-remove)** — territory of Filestar, Topaz, Imagen. Out of philosophy: requires GPU, large model weights, cloud or ONNX-runtime deps; nothing about it complements "batch convert with correct defaults." Adjacent projects exist; HEICShift stays narrow.
- **Cloud sync / per-user accounts** — Cloudinary / Filestar territory; contradicts local-first.
- **Pill / oval / fully-rounded badge backdrops** — hard global UI rule. All badges, chips, status indicators use 4–12 px corner radius.
- **"Lite" `pi-heif` decoder-only build** — marginal license benefit (MIT is already permissive); doubles maintenance. The Mac native ImageIO path achieves the same x265-avoidance goal more cleanly.
- **Pre-release of every dep update auto-pulled** — `_bootstrap()` currently pulls latest; this is the bug we're fixing in the Now tier, not an architectural goal.

---

## Themes (cross-tier)

Every Now / Next item maps to one of these themes — if a theme is thin, that's a research gap.

| Theme | Now | Next | Later |
|---|---|---|---|
| Security & dep hygiene | requirements.txt floor; memory cap; symlink guard | — | — |
| Metadata fidelity | ExifTool transport; ICC CMYK; HDR gain map; depth; Live Photo | XMP sidecar; ICC override | Sidecar JSON history |
| Format coverage | Native AVIF via Pillow 11.3 | Image sequences; lossless JPEG→JXL; 10-bit/wide-gamut; AVIF sequences | HTJ2K encode; SVG/PDF/PSD/XCF/EXR; ProRAW tone-map |
| Pipeline | Verify decode | Watch mode; recompress-no-transcode; conditional encode; quality target | Free-threaded; process pool; pyvips backend; multi-encoder shootout; macOS ImageIO; GPU |
| UX | Output path template | Task-chain presets; A/B slider; cache; per-file override; drag-out; shell integration; resumable queue | Plugin system; storage modules |
| CLI / automation | JSON report; preset load; exit codes; `--exclude` | — | — |
| Output features | — | Watermark; DPI override; canvas resize; output validator | — |
| Distribution | pyproject; smoke-test CI; pinned wheels | Update check | conda-forge; Pyodide; installer packaging |
| Quality measurement | Round-trip dimension check | — | VMAF / SSIM / butteraugli mode |
| Accessibility | A11y pass; persistent diagnostic log; settings-schema migration | — | — |
| i18n / l10n | — | — | Considered, not committed |

---

## Out-of-band housekeeping (not features)

Discovered during recon — file alongside roadmap because they affect every release:

- `CHANGELOG.md` line 5 has a literal `%Y->- (HEAD -> master, origin/master)` artifact; fix on next release.
- README "Version" shields.io badge currently shows `preview` — bump to actual `2.8.0` on next release per the project's Release recipe.
- README ICC profile table cites Pillow strip behavior but doesn't mention HEICShift's ExifTool fallback once shipped — update when Now tier lands.
- `__pycache__/heicshift.cpython-312.pyc` is committed-adjacent; ensure `.gitignore` continues to exclude.
- `_bootstrap()` writes to stderr-suppressed pip subprocess; users on restricted networks (corporate proxy) get silent install hangs. Either show pip progress or remove auto-install (see Now tier).
- `from PyQt6.QtGui import (, QIcon` block at [heicshift.py:120](heicshift.py#L120) — confirm parser actually accepts this; possibly a stray edit during a previous refactor. Read both halves before next release.
- ExifTool bundling decision: macOS / Linux can rely on system `exiftool`; Windows ships `exiftool(-k).exe` ≈ 6 MB into PyInstaller bundle. Acceptable cost for the metadata-fidelity win, but document in README so users on bandwidth-limited installs aren't surprised.
- Windows installer signing certificate (Authenticode / EV) is currently unsigned — SmartScreen warnings on first run. Out-of-band: only matters if HEICShift ever publishes a Windows MSI in the Later tier; until then, the PyInstaller `.exe` flowed via GitHub Releases is the install path.

---

## Appendix A — Source inventory

Cited sources from external research. Every item in tiers above traces here.

### A1. OSS competitors

- [ImageMagick / GraphicsMagick](https://github.com/ImageMagick/ImageMagick), [IM #1232 HEIC orientation](https://github.com/ImageMagick/ImageMagick/issues/1232), [IM #4391 HEIC color shift](https://github.com/ImageMagick/ImageMagick/discussions/4391), [IM #5159 HEIC file-type sniff regression](https://github.com/ImageMagick/ImageMagick/issues/5159), [IM #5190 chroma at quality 100](https://github.com/ImageMagick/ImageMagick/discussions/5190), [IM forum t=35968 ICC regression](https://jqmagick.imagemagick.org/discourse-server/viewtopic.php?t=35968), [IM #6621 HDR profile loss](https://github.com/ImageMagick/ImageMagick/discussions/6621)
- [libvips](https://github.com/libvips/libvips), [libvips how-it-works](https://www.libvips.org/API/8.17/how-it-works.html), [pyvips](https://github.com/libvips/pyvips), [pyvips intro](https://libvips.github.io/pyvips/intro.html), [pyvips 2.2 numpy interop](https://forum.image.sc/t/pyvips-2-2-is-out-with-improved-numpy-and-pil-integration/66664), [vips IST paper 2025](https://www.southampton.ac.uk/~km2/papers/2025/vips-ist-preprint.pdf), [Drupal vips note](https://www.drupal.org/project/vips)
- [sharp](https://github.com/lovell/sharp), [sharp #4384 HEIC orientation](https://github.com/lovell/sharp/issues/4384)
- [XnConvert](https://www.xnview.com/en/xnconvert/), [XnConvert how-to](https://www.xnview.com/en/how-to-batch-convert-and-batch-process/), [NConvert](https://www.xnview.com/en/nconvert/), [XnView AVIF chroma thread](https://newsgroup.xnview.com/viewtopic.php?t=48635), [Softpedia XnConvert review](https://www.softpedia.com/reviews/windows/xnconvert-review-537360.shtml)
- [Caesium Image Compressor](https://github.com/Lymphatus/caesium-image-compressor)
- [FastStone Photo Resizer](https://www.faststone.org/FSResizerDetail.htm), [Avaide review](https://www.avaide.com/photo-editing/faststone-photo-resizer-review/), [AnyMP4 review](https://www.anymp4.com/photo-editing/faststone-photo-resizer-review.html)
- [IrfanView FAQ](https://www.irfanview.com/faq.htm), [Helpmax batch docs](http://irfanview.helpmax.net/en/file-menu/batch-conversionrename/), [Multi-folder usage](https://onezeronull.com/2015/06/27/multi-folder-usage-of-irfanviews-batch-mode/)
- [Squoosh repo](https://github.com/GoogleChromeLabs/squoosh), [Squoosh app](https://squoosh.app/), [Squoosh deepwiki](https://deepwiki.com/GoogleChromeLabs/squoosh), [jSquash derivative](https://github.com/jamsinclair/jSquash)
- [ImBatch](https://www.highmotionsoftware.com/products/imbatch), [ImBatch ghacks](https://www.ghacks.net/2018/11/25/imbatch-image-batch-processor-convert-images-in-bulk/)
- [cwebp tools](https://chromium.googlesource.com/webm/libwebp/+/HEAD/doc/tools.md), [cwebp Google docs](https://developers.google.com/speed/webp/docs/cwebp), [AVIF encoding settings](https://openaviffile.com/best-settings-for-avif-encoding/)
- [jpegoptim](https://github.com/tjko/jpegoptim), [mozjpeg](https://github.com/mozilla/mozjpeg), [pngquant](https://pngquant.org/), [oxipng](https://github.com/shssoichiro/oxipng), [zopfli](https://github.com/google/zopfli)
- [ExifTool](https://github.com/exiftool/exiftool), [ExifTool FAQ](https://exiftool.org/faq.html), [Sidecar files](https://exiftool.org/metafiles.html), [exiftool.org](https://exiftool.org/)
- [darktable](https://github.com/darktable-org/darktable), [Export module docs](https://docs.darktable.org/usermanual/4.6/en/module-reference/utility-modules/shared/export/), [Sidecar docs](https://docs.darktable.org/usermanual/development/en/overview/sidecar-files/sidecar/), [Metadata deepwiki](https://deepwiki.com/darktable-org/darktable/4.1-metadata-system)
- [RawTherapee Queue](https://rawpedia.rawtherapee.com/Queue), [RawTherapee File Browser](https://rawpedia.rawtherapee.com/File_Browser)
- [ImageOptim](https://github.com/ImageOptim/ImageOptim), [ImageOptim howto](https://imageoptim.com/howto.html)
- [Converseen](https://github.com/Faster3ck/Converseen), [Converseen itsfoss](https://itsfoss.com/converseen/)
- [BIMP](https://github.com/alessandrofrancesconi/gimp-plugin-bimp), [Batcher (GIMP 3 successor)](https://www.xda-developers.com/free-gimp-3-plugin-makes-batch-editing-images-breeze/)
- [nip2](https://github.com/libvips/nip2), [nip4 announce](https://www.libvips.org/2025/03/12/nip4-for-nip2-users.html)
- [PhotoBulk Macworld review](https://www.macworld.com/article/231745/photobulk-2-review.html), [PhotoBulk site](https://photobulkeditor.com/)
- [NeverMendel/heif-convert](https://github.com/NeverMendel/heif-convert), [dragonGR/PyHEIC2JPG](https://github.com/dragonGR/PyHEIC2JPG), [saschiwy/HeicConverter](https://github.com/saschiwy/HeicConverter), [borelg/HEIC2jpg](https://github.com/borelg/HEIC2jpg), [Jesikurr/Universal-Image-Converter](https://github.com/Jesikurr/Universal-Image-Converter), [versoindustries/HEIC-Converter](https://github.com/versoindustries/HEIC-Converter-Effortlessly-Convert-HEIC-to-JPG-PNG-or-WEBP), [Openize.HEIC C# decoder](https://github.com/Openize/Openize.HEIC), [nick8592 HEIC](https://github.com/nick8592/HEIC-to-JPEG)
- [sips osxdaily](https://osxdaily.com/2013/01/11/converting-image-file-formats-with-the-command-line-sips/), [Arkthinker batch roundup](https://www.arkthinker.com/convert-image/batch-image-converters/), [Snyk HEIC audit](https://snyk.io/advisor/python/heic-image-converter)

### A2. Commercial competitors (paywall mapping)

- [Adobe Lightroom plans](https://www.adobe.com/products/photoshop-lightroom/plans.html), [Adobe HEIC export thread](https://community.adobe.com/t5/camera-raw-ideas/p-ability-to-export-files-in-the-heic-heif-formats/idc-p/12718463), [HDR output docs](https://helpx.adobe.com/lightroom-classic/help/hdr-output.html)
- [BatchPhoto purchase](https://www.batchphoto.com/purchase.html), [BatchPhoto comparison](https://www.batchphoto.com/comparison.html)
- [reaConverter SaaSWorthy](https://www.saasworthy.com/product/reaconverter), [reaConverter features](https://www.reaconverter.com/features/)
- [NCH Pixillion](https://www.nchsoftware.com/imageconverter/index.html), [Pixillion licensing](https://help.nchsoftware.com/help/en/pixillion/win/141.html)
- [ImageConverter Plus](https://www.imageconverterplus.com/)
- [iMazing HEIC](https://imazing.com/converter), [Apeaksoft review](https://www.apeaksoft.com/tips/imazing-heic-converter/)
- [CopyTrans HEIC](https://www.copytrans.net/copytransheic/), [100-image limit](https://www.copytrans.net/support/copytrans-heic-pro-100-images-limit/)
- [TinyPNG](https://tinypng.com/), [Tinify pricing](https://tinify.com/pricing/api)
- [Cloudinary pricing](https://cloudinary.com/pricing), [Imgix pricing compared](https://www.smallpics.io/blog/image-transform-pricing-compared/)
- [Filestar pricing](https://filestar.com/pricing), [Filestar cloud credits](https://filestar.com/cloud-credits)
- [AnyMP4 HEIC desktop](https://www.anymp4.com/heic-jpg-converter-viewer/), [Free online](https://www.anymp4.com/free-online-heic-converter/)
- [Permute 3](https://software.charliemonroe.net/permute/), [App Store](https://apps.apple.com/us/app/permute-3/id1444998321)

### A3. Community signal (pain points)

- [HN 46230024 — privacy-first HEIC converter](https://news.ycombinator.com/item?id=46230024), [HN 46427287 — privacy-first bulk compressor](https://news.ycombinator.com/item?id=46427287), [HN 46833600 — Heic2Jpg client-side](https://news.ycombinator.com/item?id=46833600)
- [HN 35589179 — FSF slams Google on JXL](https://news.ycombinator.com/item?id=35589179), [HN 35212522 — JXL lossless transcode case](https://news.ycombinator.com/item?id=35212522), [Slashdot Google JXL](https://tech.slashdot.org/story/22/10/31/2236220/why-google-is-removing-jpeg-xl-support-from-chrome), [FSF JPEG-XL](https://news.slashdot.org/story/23/04/16/002204/fsf-says-googles-decision-to-deprecate-jpeg-xl-emphasizes-need-for-browser-choice)
- [Apple Community thread 254814534 — HEIC color shift](https://discussions.apple.com/thread/254814534), [thread 256257398 — GPS stripped](https://discussions.apple.com/thread/256257398)
- [Apple Dev forum 678109 — EXIF stripping](https://developer.apple.com/forums/thread/678109)
- [robservatory — retain location](https://robservatory.com/retain-location-metadata-on-photos-sourced-from-photos/)
- [Cablek chroma subsampling](https://www.cablek.com/chroma-subsampling-4-4-4-vs-4-2-2-vs-4-2-0), [MeshCentral #2411 chroma](https://github.com/Ylianst/MeshCentral/issues/2411)
- [Willow Wagtail changelog ICC](https://willow.wagtail.org/latest/changelog.html), [Pillow #3270 ICC missing](https://github.com/python-pillow/Pillow/issues/3270), [Pillow #1529 EXIF+ICC error](https://github.com/python-pillow/Pillow/issues/1529), [Pillow #9294 exif_transpose regression](https://github.com/python-pillow/Pillow/issues/9294)
- [Geeqie #923 HEIC orientation](https://github.com/BestImageViewer/geeqie/issues/923)
- [PicTomo HEIC history](https://pic-tomo.com/en/blog/heic-format-history-ios-adoption)

### A4. Standards, formats, browser status

- [ISO 21496-1:2025 HDR gain map](https://www.iso.org/standard/86775.html), [Greg Benz primer](https://gregbenzphotography.com/hdr-photos/iso-21496-1-gain-maps-share-hdr-photos/), [Android Authority](https://www.androidauthority.com/google-apple-hdr-photo-standard-3495035/), [PhoneArena Android 15 dual-write](https://www.phonearena.com/news/hdr-photos-across-android-and-ios_id164255), [libheif #1685 gain map](https://github.com/strukturag/libheif/issues/1685)
- [AOMedia AVIF spec v1.0.0](https://aomediacodec.github.io/av1-avif/v1.0.0.html), [Mozilla bug 1788119 AVIS](https://bugzilla.mozilla.org/show_bug.cgi?id=1788119), [caniuse AVIF](https://caniuse.com/avif), [AVIF 2026 browser support](https://orquitool.com/en/blog/avif-browser-support-2026-compatibility-webp-switch/), [TestMuAI AVIF QuickLook gap](https://www.testmuai.com/learning-hub/avif-browser-support/)
- [Chromium reverses on JXL](https://devclass.com/2025/11/24/googles-chromium-team-decides-it-will-add-jpeg-xl-support-reverses-obsolete-declaration/), [The Register Chrome JXL](https://www.theregister.com/2026/01/14/google_rekindles_relationship_with_jilted/), [Neowin Chrome JXL](https://www.neowin.net/news/chrome-to-support-much-smaller-faster-images-as-jpeg-xl-makes-a-security-first-comeback/), [ChromeStatus JXL](https://chromestatus.com/feature/5188299478007808), [Coywolf JXL coming back](https://coywolf.com/news/web-development/jpeg-xl-jxl-is-coming-back-to-chrome/), [CoreWebVitals JXL](https://www.corewebvitals.io/pagespeed/jpeg-xl-core-web-vitals-support), [JPEG XL Wikipedia](https://en.wikipedia.org/wiki/JPEG_XL)
- [WebP 2 — dead](https://chromium.googlesource.com/codecs/libwebp2/)
- [Access Advance HEVC pool](https://accessadvance.com/licensing-programs/hevc-advance/)
- [OpenJPEG 2.5 HTJ2K (Phoronix)](https://www.phoronix.com/news/OpenJPEG-2.5-Brings-HTJ2K), [OpenJPH](https://github.com/aous72/OpenJPH)
- [Apple iOS 18 RAW formats](https://support.apple.com/en-us/120534), [LibRaw supported cameras](https://www.libraw.org/supported-cameras), [Capture One ProRAW](https://support.captureone.com/hc/en-us/articles/9335283604509-Apple-ProRAW-Support)
- [ICC iccMAX overview](https://www.color.org/iccmax.xalter), [iccMAX status](https://www.color.org/iccmax-status.xalter)

### A5. Dependency release notes

- [Pillow release-notes index](https://pillow.readthedocs.io/en/stable/releasenotes/index.html), [Pillow 11.0.0](https://pillow.readthedocs.io/en/stable/releasenotes/11.0.0.html), [Pillow 11.3.0](https://pillow.readthedocs.io/en/stable/releasenotes/11.3.0.html)
- [pillow_heif CHANGELOG](https://github.com/bigcat88/pillow_heif/blob/master/CHANGELOG.md), [pillow_heif docs (orientation workaround)](https://pillow-heif.readthedocs.io/en/latest/pillow-plugin.html), [pi-heif PyPI](https://pypi.org/project/pi-heif/), [conda-forge pillow-heif](https://anaconda.org/conda-forge/pillow-heif)
- [libheif releases](https://github.com/strukturag/libheif/releases)
- [libjxl releases](https://github.com/libjxl/libjxl/releases), [pillow-jpegxl-plugin releases](https://github.com/Isotr0py/pillow-jpegxl-plugin/releases)
- [rawpy releases](https://github.com/letmaik/rawpy/releases), [LibRaw releases](https://github.com/LibRaw/LibRaw/releases)
- [Qt 6.7 blog](https://www.qt.io/blog/qt-6.7-released), [What's New in Qt 6.8](https://doc.qt.io/qt-6/whatsnew68.html), [Qt 6.9 blog](https://www.qt.io/blog/qt-6.9-released)
- [PyInstaller changelog](https://pyinstaller.org/en/stable/CHANGES.html), [PyInstaller 6.11.1](https://pyinstaller.org/en/v6.11.1/CHANGES.html), [PyInstaller hooks](https://pyinstaller.org/en/stable/hooks.html)

### A6. Security advisories

- [NVD CVE-2025-48379 (Pillow DDS)](https://nvd.nist.gov/vuln/detail/CVE-2025-48379)
- [CVE-2024-28219 Sentinel (Pillow ICC)](https://www.sentinelone.com/vulnerability-database/cve-2024-28219/), [CVEDetails](https://www.cvedetails.com/cve/CVE-2024-28219/)
- [CVE-2023-44271 Red Hat (Pillow textlength DoS)](https://access.redhat.com/security/cve/cve-2023-44271), [NVD](https://nvd.nist.gov/vuln/detail/CVE-2023-44271)
- [NVD CVE-2023-4863 (libwebp Chrome 0-day)](https://nvd.nist.gov/vuln/detail/CVE-2023-4863)
- [CVE-2025-29482 libheif SAO RCE](https://dailycve.com/libheif-buffer-overflow-cve-2025-29482-critical/)
- [CVE-2024-41311 libheif ImageOverlay (Sentinel)](https://www.sentinelone.com/vulnerability-database/cve-2024-41311/)
- [Talos TALOS-2026-2364 (LibRaw DNG)](https://talosintelligence.com/vulnerability_reports/TALOS-2026-2364), [TALOS-2026-2359 (LibRaw X3F)](https://talosintelligence.com/vulnerability_reports/TALOS-2026-2359)
- [GHSA-m6xw-mq4p-x7xv (libtiff)](https://github.com/advisories/GHSA-m6xw-mq4p-x7xv)

### A7. Python / runtime signals

- [Python 3.14 What's New](https://docs.python.org/3/whatsnew/3.14.html), [Free-threading howto](https://docs.python.org/3/howto/free-threading-python.html), [Quansight free-threaded rollout](https://labs.quansight.org/blog/free-threaded-python-rollout)
- [Pyodide changelog](https://pyodide.org/en/stable/project/changelog.html)
- [Netflix VMAF](https://github.com/Netflix/vmaf), [Google butteraugli](https://github.com/google/butteraugli), [ffmpeg-quality-metrics PyPI](https://pypi.org/project/ffmpeg-quality-metrics/)

---

## Appendix B — Roadmap conventions

- **Effort** scale 1 (hours) → 5 (multi-week refactor).
- **Impact** scale 1 (one user notices) → 5 (visible in README / shipped to all users).
- **Tier mobility**: items can move up (Later → Next → Now) when a dependency lands or user demand spikes; they should rarely move down (would mean we overcommitted).
- **Source-or-drop**: any item without a working URL when re-audited gets dropped from the roadmap. The Appendix is the canon.
- **One commit per item** when implementing — keeps blame log usable.
- **Version-string sync**: every release passes through the project's "Release vX.Y.Z" recipe (memory: [recipe-release-build.md](https://github.com/SysAdminDoc/HEICShift)). README badge, CLAUDE.md, CHANGELOG, memory file all updated in the same commit.
