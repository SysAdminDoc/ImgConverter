# ImgConverter Roadmap

**Current version:** v3.3.0 (released 2026-06-19) · **Roadmap revision:** 2026-06-19

Universal image batch converter. 12+ input families → 6 output formats (JPEG / PNG / WebP / AVIF / TIFF / JXL) with metadata, color-profile, and orientation fidelity. PyQt6 GUI + headless CLI parity, single-file Python 3.10+, MIT, PyInstaller binaries for Windows / macOS / Linux.

> **2026-06-19 review:** shipped work was deduped against the current app before adding new items. The actionable backlog below is research-backed and scoped to the existing local-first CLI/GUI architecture. Blocked items live in `Roadmap_Blocked.md`.

---

## Philosophy (constraints on what gets in)

Anything new must respect these. Items that conflict are flagged explicitly.

1. **Correct defaults over breadth** — 4:4:4 chroma, ICC passthrough, EXIF auto-rotate, atomic writes. The whole "Why ImgConverter" pitch lives here.
2. **CLI ↔ GUI parity** — every GUI control gets a `--flag` and vice versa.
3. **Local-first** — no telemetry, no cloud upload, no account. The HN "privacy-first converter" momentum ([HN 46230024](https://news.ycombinator.com/item?id=46230024), [HN 46427287](https://news.ycombinator.com/item?id=46427287)) is the wedge.
4. **Graceful optional-dep degradation** — missing rawpy / pillow-jxl / qoi quietly disables those families; nothing crashes.
5. **Single-file ship** — `imgconverter.py` + PyInstaller binary. Plugin systems must be opt-in, not required.
6. **Non-destructive default** — never delete the source unless `--in-place` was explicit. Several PyPI "HEIC converter" tools delete originals by default; we don't ([Snyk audit](https://snyk.io/advisor/python/heic-image-converter), [nick8592 example](https://github.com/nick8592/HEIC-to-JPEG)).
7. **Catppuccin Mocha dark UI, square/8-px backdrops only** — no pill/oval badges anywhere (per global rules).

---

## Blocked items

All items that require external infrastructure (signing cert, GPU, macOS host,
encoder binary, separate repo, design decisions) live in `Roadmap_Blocked.md`.
When a blocker is resolved, move the item back here and implement it.

---

## Review basis - 2026-06-19

Comparable products checked and deduped against current ImgConverter capabilities:

- **XnConvert** - mature batch surface with 500+ formats and 80+ actions. Source: https://www.xnview.com/en/xnconvert/
- **reaConverter / BatchPhoto** - paid automation leaders with watch folders, background conversion, and workflow scripting. Sources: https://www.reaconverter.com/features/ and https://www.batchphoto.com/howto/monitor-folders.html
- **File Converter** - Windows Explorer context-menu adoption loop. Source: https://file-converter.io/
- **Squoosh** - local browser-based quality preview and before/after comparison expectations. Source: https://squoosh.app/
- **XL Converter / libjxl** - modern encoder lane with JPEG XL, Jpegli, and lossless JPEG transcoding. Sources: https://github.com/JacobDev1/xl-converter and https://github.com/libjxl/libjxl
- **Czkawka** - duplicate/similar-image review model that maps to ImgConverter's scan phase. Source: https://czkawka.net/

---

## Tier: Under Consideration

Worth exploring; not committed.

- **Face-aware quality boost (ROI)** — already in 2026-04 nice-to-haves. JPEG ROI quality / AVIF film-grain-off in face regions. Needs face detector (MediaPipe / OpenCV Haar) which adds a heavy dep. Reject for now; revisit if ImgConverter adds any ML deps for another reason.
- **i18n / localization** — Qt has `lupdate` / `lrelease`. Worth doing if external contributor volunteers a language; not worth solo effort given Slack/forum signal is English-dominant in the target user base.
- **Telemetry / opt-in crash report** — even a local-only `~/.cache/imgconverter/crash.log` would help diagnose user reports. Hard rule: never phone home without explicit opt-in.
- **Mobile companion (iOS / Android)** — Permute, BatchPhoto, several "HEIC converter" mobile apps exist. Rejected for the desktop project; if anything, build the Pyodide web app instead.
- **CDN bridge** — Cloudinary / Imgix style URL-API transforms. Contradicts local-first philosophy. Skip.

---

## Tier: Rejected (with reasoning)

- **HEIC output by default** — patent encumbrance via HEVC pools ([Access Advance](https://accessadvance.com/licensing-programs/hevc-advance/)) runs into the 2030s; x265 encoder is GPL contagion for the bundled PyInstaller binary. Optional behind a flag is OK; never default. *Confirmed by Firefox bug 1402293's 9-year stall.*
- **WebP 2** — Google's own position: "WebP 2 will not be released as an image format" ([chromium libwebp2 README](https://chromium.googlesource.com/codecs/libwebp2/)). Dead format.
- **AI features (upscale / colorize / background-remove)** — territory of Filestar, Topaz, Imagen. Out of philosophy: requires GPU, large model weights, cloud or ONNX-runtime deps; nothing about it complements "batch convert with correct defaults." Adjacent projects exist; ImgConverter stays narrow.
- **Cloud sync / per-user accounts** — Cloudinary / Filestar territory; contradicts local-first.
- **Pill / oval / fully-rounded badge backdrops** — hard global UI rule. All badges, chips, status indicators use 4–12 px corner radius.
- **"Lite" `pi-heif` decoder-only build** — marginal license benefit (MIT is already permissive); doubles maintenance. The Mac native ImageIO path achieves the same x265-avoidance goal more cleanly.
- **Pre-release of every dep update auto-pulled** — `_bootstrap()` currently pulls latest; this is the bug we're fixing in the Now tier, not an architectural goal.

---

## Appendix B — Roadmap conventions

- **Effort** scale 1 (hours) → 5 (multi-week refactor).
- **Impact** scale 1 (one user notices) → 5 (visible in README / shipped to all users).
- **Tier mobility**: items can move up (Later → Next → Now) when a dependency lands or user demand spikes; they should rarely move down (would mean we overcommitted).
- **Source-or-drop**: any item without a working URL when re-audited gets dropped from the roadmap. The Appendix is the canon.
- **One commit per item** when implementing — keeps blame log usable.
- **Version-string sync**: every release passes through the project's "Release vX.Y.Z" recipe. README badge, CLAUDE.md, CHANGELOG, memory file all updated in the same commit.

## Research-Driven Additions

### P1

- **GUI scan review table** (Effort 3, Impact 5) - add a first-class batch review surface before conversion: rows for source, destination, output format, estimated action, metadata/ICC warning, duplicate warning, and conversion status. This borrows the scan confidence of XnConvert/BatchPhoto without adding the blocked per-file override design yet.
  - Acceptance: drag/drop, file picker, and recursive scans populate the same model; rows are keyboard navigable and filterable by warning/error/status; conversion progress updates rows without changing current CLI behavior.
  - Sources: https://www.xnview.com/en/xnconvert/ and https://www.batchphoto.com/

- **Windows shell integration manager** (Effort 3, Impact 4) - promote the existing `--register-shell` capability into a GUI-managed setup flow with install/uninstall, default preset selection, admin-state detection, dry-run registry preview, and support-bundle capture. File Converter proves Explorer context menus are a major adoption path, but this stays limited to ImgConverter's current Windows registry integration rather than installer/signing work.
  - Acceptance: GUI can install/uninstall shell entries, choose a preset, display exact command lines, and recover gracefully when elevation is missing; CLI flags remain the source of truth.
  - Source: https://file-converter.io/

- **Watch-folder cockpit** (Effort 4, Impact 5) - turn current watch mode/profile pieces into a resilient GUI automation surface: enabled profiles, run-in-background tray mode, debounce/stability status, last-run counts, last error, manual run-now, pause/resume, and startup opt-in. reaConverter and BatchPhoto treat watch folders as a paid automation feature; ImgConverter can make this a local-first differentiator.
  - Acceptance: profiles persist enabled/paused state, show live health, survive app restart with clear recovery messaging, and share the same conversion engine and options as CLI `--watch`.
  - Sources: https://www.reaconverter.com/features/ and https://www.batchphoto.com/howto/monitor-folders.html

- **Optional lossless JPEG-to-JXL transcode lane** (Effort 4, Impact 4) - when `cjxl`/`djxl` from libjxl are present, expose reversible JPEG -> JXL conversion and JXL -> JPEG reconstruction as an advanced option with clear metadata/ExifTool handling and graceful disabled state when binaries are absent.
  - Acceptance: capability appears in backend info and GUI format support; missing tools do not fail normal conversion; tests cover command construction, disabled state, and metadata-copy fallback.
  - Sources: https://github.com/JacobDev1/xl-converter and https://github.com/libjxl/libjxl


### P2

- **Sample proof run** (Effort 3, Impact 4) - add a lightweight quality-preview workflow that converts the first N representative files to a temp proof folder, reports size/quality deltas, and offers reveal/open actions before a full batch. This captures the confidence value of Squoosh-style preview while avoiding the larger blocked before/after-slider design.
  - Acceptance: CLI and GUI can run sample-only proof mode; proof outputs are isolated from real destinations; report includes source size, output size, ratio, selected encoder settings, and any fidelity warnings.
  - Source: https://squoosh.app/

- **Backend policy advisor** (Effort 2, Impact 4) - use the existing backend-info/benchmark foundation to recommend Pillow, pyvips, or optional external tools based on file count, megapixels, requested metadata fidelity, target format, and installed dependencies.
  - Acceptance: GUI shows calm advisory copy before long jobs; CLI can emit the same advice; no automatic backend switch occurs without an explicit user opt-in.
  - Sources: https://www.reaconverter.com/ and https://github.com/libvips/pyvips

- **Preset import/export bundles** (Effort 3, Impact 4) - let users share repeatable conversion recipes as schema-versioned JSON bundles with a human summary, CLI equivalent, required optional dependencies, and trust warnings for plugins/hooks. XnConvert action chains and BatchPhoto scripts show that shareable recipes make batch tools feel professional.
  - Acceptance: exported presets re-import cleanly across platforms, reject unknown schema versions safely, and never auto-enable hooks/plugins without explicit confirmation.
  - Sources: https://www.xnview.com/en/xnconvert/ and https://www.batchphoto.com/

- **GUI duplicate review panel** (Effort 3, Impact 4) - build on existing `--dedup-warn`/`--dedup-skip` logic with a visual duplicate/similar-image review table before conversion. Keep deletion and destructive cleanup out of this tool; the goal is conversion confidence and wasted-work reduction.
  - Acceptance: duplicate groups show representative thumbnails where available, similarity/hash reason, selected action, and keyboard-accessible skip/convert controls; CLI dedup behavior remains unchanged.
  - Source: https://czkawka.net/

- **Format capability matrix** (Effort 2, Impact 3) - add a searchable GUI/support-bundle view showing detected input/output support, optional dependency or external-tool requirement, metadata caveats, color/HDR caveats, and CLI install hints. Competitors lead with format claims; ImgConverter should expose capability truth with more trust.
  - Acceptance: matrix is generated from the same runtime checks as conversion, exports into support bundles, and clearly distinguishes built-in, optional, unavailable, and experimental support.
  - Sources: https://www.xnview.com/en/xnconvert/ and https://www.reaconverter.com/

### P3

- **Optional Jpegli adapter** (Effort 3, Impact 3) - if `cjpegli` is installed, allow JPEG output through Jpegli as an advanced external encoder path. Keep Pillow JPEG as the default until a stable Python binding or bundled dependency story exists.
  - Acceptance: adapter is fully optional, reports tool/version in backend info, and preserves existing metadata/color-profile defaults through ExifTool sidecar copy where needed.
  - Source: https://github.com/libjxl/libjxl

- **Command palette for power workflows** (Effort 3, Impact 3) - add a keyboard-first GUI command palette for scan, add folder, choose preset, run proof, start conversion, open support bundle, and reveal output. This improves discoverability without adding visual clutter.
  - Acceptance: Ctrl+K opens a searchable command list, actions expose disabled reasons, and every command maps to an existing button/menu workflow.

## Research-Driven Additions (2026-06-19 pass 2)

### P0

- [ ] P0 — **CLI bounded future scheduling**
  Why: `_run_cli` submits all futures at once (`pool.submit()` in a tight loop at line 8292), unlike the GUI's bounded `_submit_batch()` with `max_inflight = workers * 2`. On 10k+ file batches, this queues thousands of futures and their argument closures in memory, causing memory pressure before any conversion starts.
  Evidence: Code inspection at `imgconverter.py:8292-8299` vs GUI's bounded scheduling at `imgconverter.py:3474-3548`. HandBrake and reaConverter both use bounded queues.
  Touches: `_run_cli()` in `imgconverter.py`
  Acceptance: CLI uses the same bounded `max_inflight` pattern as the GUI; memory footprint during 10k+ file batches stays proportional to `workers * 2`, not total file count.
  Complexity: S

- [ ] P0 — **Multi-frame conversion respects ConvertOptions**
  Why: `_convert_animated_or_sequence` hardcodes quality=92, ignores metadata preservation, resize, strip_fields, and all other ConvertOptions fields. Multi-frame output silently drops user settings.
  Evidence: Code inspection at `imgconverter.py:2359-2427` — `save_kwargs["quality"] = 92` on line 2399, no metadata/resize/strip logic.
  Touches: `_convert_animated_or_sequence()` in `imgconverter.py`
  Acceptance: Multi-frame output respects the same ConvertOptions as single-frame: quality slider, metadata handling, resize, strip fields. Test coverage for at least quality and metadata.
  Complexity: M

- [ ] P0 — **Dead code cleanup: pillow-heif 1.4 and unused constants**
  Why: `HEIF_MAX_DECODE_BYTES` (line 186) is defined but never used. The `ALLOW_INCORRECT_HEADERS` hasattr guard (line 189 area) references an attribute removed in pillow-heif 1.4.0. Both are harmless dead code but confusing.
  Evidence: Code inspection at `imgconverter.py:186-195`. pillow-heif 1.4.0 changelog confirms `ALLOW_INCORRECT_HEADERS` removal.
  Touches: Module-level constants in `imgconverter.py`
  Acceptance: Dead code removed. No behavioral change. Tests pass.
  Complexity: S

### P1

- [ ] P1 — **Animated file set should use set lookup**
  Why: `animated_files` membership check in ConvertWorker.run (line 3471) uses `not in` against a list — O(n) per file, O(n²) total for large batches with many animated files.
  Evidence: Code inspection at `imgconverter.py:3471`. Same pattern was already fixed for `done_paths`/`failed_paths` in v3.1.1.
  Touches: `ConvertWorker.run()` in `imgconverter.py`
  Acceptance: `animated_files` converted to a set before membership checks. No behavioral change.
  Complexity: S

- [ ] P1 — **Pillow 13 deprecation prep**
  Why: Pillow 13 (Oct 2026) removes `ImageCms.ImageCmsProfile.product_name`, `product_info`, and `Image.getdata()`. ImgConverter's test suite already migrated off `getdata()` (v3.1.1), but the `ImageCms` removals need verification that no transitive usage exists in the ICC conversion path.
  Evidence: Pillow deprecation docs (https://pillow.readthedocs.io/en/stable/deprecations.html). ImgConverter uses `ImageCms.profileToProfile()` and `ImageCms.ImageCmsProfile()` in convert_file.
  Touches: ICC handling in `convert_file()`, test suite, CI matrix (add Pillow 13 dev job when wheels ship)
  Acceptance: `grep -r product_name imgconverter.py` returns zero hits; CI job with Pillow 13 pre-release passes; pyproject.toml classifiers updated.
  Complexity: S

- [ ] P1 — **c2pa-python native integration**
  Why: Current C2PA verification shells out to `c2patool` binary. The `c2pa-python` SDK (v0.5.0, pip-installable, Apache 2.0/MIT) provides native signing/verification without subprocess overhead, with structured Python objects instead of JSON parsing.
  Evidence: https://opensource.contentauthenticity.org/docs/c2pa-python/. Samsung Galaxy S25 and Google Pixel 10 embed C2PA in camera output — the format is going mainstream.
  Touches: `_verify_c2pa()` in `imgconverter.py`, optional dependency in `pyproject.toml`
  Acceptance: `c2pa-python` used when installed, falls back to `c2patool` subprocess, falls back to no verification. Structured result replaces JSON parsing. Test for both paths.
  Complexity: M

- [ ] P1 — **Drop redundant `qoi` optional dependency for write**
  Why: Pillow 11.3+ (July 2025) ships native QOI write support. Pillow 9.5+ ships native QOI read. Since ImgConverter's floor is Pillow 12.2, the `qoi` package is fully redundant — both read and write are native.
  Evidence: Pillow 11.3.0 release notes (https://pillow.readthedocs.io/en/stable/releasenotes/11.3.0.html). ImgConverter `pyproject.toml` still lists `qoi>=0.7` as optional.
  Touches: `pyproject.toml`, `requirements.txt`, `HAS_QOI` detection in `imgconverter.py`, `FORMAT_FAMILIES`, README optional tools table, `--install-deps`
  Acceptance: QOI input/output works without the `qoi` package installed. `qoi` optional dep removed from all manifests. Format filter still shows QOI. Tests pass.
  Complexity: M

### P2

- [ ] P2 — **Watch mode integration test**
  Why: Watch mode has zero automated tests. The watch handler, debounce logic, watchdog vs polling fallback, and ConvertOptions forwarding are all untested. This is the largest untested code path.
  Evidence: `tests/` directory has no test file for watch mode. Watch mode is ~125 lines (`imgconverter.py:7384-7508`).
  Touches: New test in `tests/test_features.py` or new `tests/test_watch.py`
  Acceptance: At least 3 tests: (1) new file detected and converted, (2) debounce prevents partial-write processing, (3) ConvertOptions forwarded correctly. Can use a mock Observer or short polling interval.
  Complexity: M

- [ ] P2 — **vips backend test coverage**
  Why: The vips backend has zero tests. It's flagged experimental but ships in the main binary. Basic regression coverage would catch breakage from refactors.
  Evidence: `tests/` has no vips-related tests. vips backend is ~105 lines (`imgconverter.py:2616-2722`).
  Touches: New test in `tests/test_features.py`, conditional skip when pyvips not installed
  Acceptance: At least 2 tests: (1) basic JPEG conversion through vips backend, (2) vips rejects unsupported options (resize, metadata). Skipped when pyvips not installed.
  Complexity: S

- [ ] P2 — **conda-forge recipe sha256 placeholder**
  Why: `packaging/conda-forge/meta.yaml` has a placeholder sha256 (`0000...`). While it won't affect users until a conda-forge PR is submitted, it's confusing and could cause a failed build if someone copies it.
  Evidence: `packaging/conda-forge/meta.yaml:12`
  Touches: `packaging/conda-forge/meta.yaml`, optionally a CI step to auto-compute hash on release
  Acceptance: sha256 either computed from the actual release tarball or replaced with a comment explaining the manual step. Recipe-level test passes.
  Complexity: S

- [ ] P2 — **convert_file dual-interface consolidation**
  Why: `convert_file()` accepts 35 keyword args AND an `opts=ConvertOptions` object. When `opts` is provided, 33 lines (2771-2805) manually unpack every field from opts into local variables. Every new ConvertOptions field requires updating both the kwargs signature and the unpacking block — root cause of parity drift.
  Evidence: Code inspection at `imgconverter.py:2724-2805`. The ConvertOptions dataclass was introduced to solve this but the legacy kwargs remain for backward compat.
  Touches: `convert_file()` signature, all callers (ConvertWorker, _run_cli, tests)
  Acceptance: `convert_file()` accepts only `(src, output_dir, *, opts, seq)`. Legacy kwargs removed. All callers pass ConvertOptions. Tests updated.
  Complexity: L

- [ ] P2 — **CONTRIBUTING.md test count stale**
  Why: CONTRIBUTING.md says "19+ tests" but the actual count is 128. Stale documentation undermines contributor confidence.
  Evidence: `CONTRIBUTING.md:27` says "19+ tests"; `grep -c "def test_" tests/*.py` shows 128.
  Touches: `CONTRIBUTING.md`
  Acceptance: Test count updated to reflect reality. CI matrix description updated (mentions 3.11/3.12 but CI actually tests 3.11-3.14 + 3.14t).
  Complexity: S

### P3

- [ ] P3 — **Metadata extraction module consolidation**
  Why: Metadata handling (presence detection, selective stripping, ExifTool integration, report generation) is scattered across 6+ functions: `_open_image()`, `convert_file()`, `_metadata_presence_from_image()`, `_metadata_presence_from_path()`, `_strip_exif_fields()`, `_finalize_metadata_report()`, `_run_exiftool_copy()`. Consolidating into a coherent module would reduce the chance of metadata bugs.
  Evidence: Architecture assessment in RESEARCH.md. The GPS zero-coordinate bug (v3.1.1) and same-format strip-fields skip (v3.1.1) both originated from scattered metadata logic.
  Touches: 6+ functions in `imgconverter.py`, potentially extracted to a helper module
  Acceptance: Metadata operations go through a single coherent API surface. Existing tests still pass. No new features — pure refactor.
  Complexity: L

- [ ] P3 — **Smart quality detection for auto mode**
  Why: When output format is "auto", ImgConverter already selects JPEG vs PNG based on alpha. Dinky (macOS competitor, 443 stars, 2026) adds a second axis: detecting photo vs graphic content and adjusting quality accordingly. Photos get higher quality (92+), graphics/screenshots get lower quality (80-85) with lossless WebP consideration. This would improve auto mode's compression ratio without user configuration.
  Evidence: Dinky app feature set (https://github.com/heyderekj/dinky). XL Converter issue #140 also requests quality-aware auto settings.
  Touches: `convert_file()` auto-format detection path in `imgconverter.py`, new heuristic function
  Acceptance: Auto mode detects photo vs graphic content (histogram analysis or edge density) and adjusts quality. CLI `--format auto` gets the same behavior. No regression in existing auto-mode tests.
  Complexity: M

- [ ] P3 — **Retry failed conversions in watch mode**
  Why: Watch mode currently treats all failures as permanent — `converted.add(f)` is called even on failure (line 7496), so the file is never retried. For transient failures (file locked, disk full then freed, network drive hiccup), a retry queue with backoff would improve automation reliability.
  Evidence: Code inspection at `imgconverter.py:7483-7498`. reaConverter and BatchPhoto both retry transient failures in watch mode.
  Touches: `_watch_directory()` in `imgconverter.py`
  Acceptance: Failed files are retried up to 3 times with exponential backoff. Permanent failures (unsupported format, corrupt file) still marked as done. Watch mode log distinguishes retries from first attempts.
  Complexity: M
