# Changelog

All notable changes to ImgConverter will be documented in this file.

## [v3.1.0] — 2026-06-16

### Audit hardening

- **Crash-safe state writes**: Plugin trust, watch profiles, CLI queue state, JSON reports, and sidecar-history files now use same-directory atomic writes to avoid torn JSON after interruption or disk errors.
- **Watch-profile schema guard**: Malformed `watch-profiles.json` entries are ignored or normalized instead of crashing the watch-folder dialog.
- **CLI completion safety**: `--when-done` now runs only after a clean batch; failures keep partial/total-failure exit codes and skip close/sleep/shutdown actions.
- **Shell integration quoting**: Linux `.desktop` entries now quote Python and script paths so installs under directories with spaces still route selected files correctly.
- **Watchdog race fix**: Watch mode now synchronizes pending filesystem events before conversion.
- **Output path fallback**: Preserve-structure conversion paths now fall back to the output root when a direct/shell-selected source is outside the supplied base directory.
- **Validation recovery**: The GUI max-file-size field now clears its error border when edited, matching source/output recovery behavior.
- **Export resilience**: GUI log and CSV exports now write atomically and show a recoverable workflow error instead of crashing on disk or permission failures.
- **First-run preset integrity**: Built-in preset seeding and Linux shell integration writes now use the shared atomic writer.

### UX/UI

- **Premium PyQt shell polish**: Reworked the first-run flow into a clearer source/output setup, visible workflow status, compact action bar, refined activity log placeholder, and cleaner Catppuccin surface layering.
- **True collapsed advanced controls**: Replaced the checkable `QGroupBox` that disabled advanced controls in-place with an explicit show/hide toggle, so dense expert controls no longer dominate the default workflow.
- **Primary workflow visibility**: Moved Scan/Convert/Paste/Open Output actions directly below source/output controls and moved optional input filters below the main conversion path.
- **State feedback polish**: Added workflow-state updates for invalid source, no clipboard image, no files found, disk-space block, conversion, stopping, completion, partial failure, and CSV-without-results states.
- **Interaction consistency**: Added focus styling, transparent label/checkbox rendering, compact directory buttons, stable stat cards, full settings freeze during conversion, and long-filename truncation in the progress bar.
- **Accessibility copy**: Added accessible names/descriptions for the workflow status, directory buttons, preset menu, advanced toggle, action buttons, and log/export controls.

### Security

- **Frozen executable install guard**: `--install-deps` now refuses to shell out through `sys.executable` when running from a packaged PyInstaller build, avoiding accidental app relaunch loops.
- **In-place recompress self-deletion fix**: Guard `src.unlink()` with `out_path.resolve() != src.resolve()` to prevent deleting the output when input/output paths resolve to the same file.
- **`--install-deps` no longer escalates to `--break-system-packages`**: PEP 668 system Python is never mutated; users are told to use a virtualenv instead.
- **Plugin loader no longer pollutes `sys.path`**: Removed the `sys.path.append(plugin_dir)` that let trusted plugins shadow stdlib modules. `importlib.util.spec_from_file_location` already has the full path.

### Fixed

- **Backend flag now controls conversion**: `--backend vips` now reaches the conversion path instead of only printing a warning, with validation that blocks unsupported metadata and transform combinations.
- **Process-pool plugin guard**: `--use-processes` now fails early when trusted plugins are loaded, because plugin decoder/encoder registries are in-process state and child workers cannot safely inherit them on all platforms.
- **Max-file-size validation**: `--max-file-size` and the GUI scan guard now reject zero or negative size limits instead of treating them as valid filters.
- **Resume queue correctness**: Skipped files are now recorded as completed in interrupted CLI queue state, so `--resume` does not revisit files already classified as skipped.
- **Packaging dependency floor sync**: The conda-forge recipe now matches the `Pillow>=12.2.0` security floor enforced by requirements and pyproject metadata.
- **In-place same-extension safety**: Same-format in-place processing now atomically replaces the source path without treating it as an output collision or deleting the finished file, and `--skip-existing` no longer skips the source itself.
- **Template collision paths**: Filename-template collisions now keep suffixed outputs inside the intended template subdirectory instead of falling back to the output root.
- **CLI value validation**: Invalid worker counts, quality ranges, resize/canvas specs, max-file-size values, DPI, AVIF speed, and quality-target constraints now fail early with clear input errors.
- **High-bit-depth HEIC → AVIF warning corrected**: AVIF output no longer claims 10/12-bit preservation through Pillow's 8-bit AVIF encoder; users are warned to use JPEG XL when high bit depth must survive.
- **Dependency onboarding docs now match runtime behavior**: README and working notes now describe explicit installation via `pip install -r requirements.txt` or opt-in `--install-deps`; startup no longer claims silent auto-install behavior.
- **Shell integration now accepts selected files**: `--files PATH...` handles one or more file selections directly, Windows file context menus route through `--files %*`, Linux desktop actions use `%F`, and directory context menus still use `--input`.
- **File handle leak on EXIF transpose**: The pre-transpose image handle is now explicitly closed, preventing Windows file-lock issues during in-place rename.
- **Canvas paste preserves alpha**: `_apply_canvas` now uses `mask=img` for RGBA/LA/PA sources so transparency isn't replaced by the canvas background.
- **CLI batch crash guard**: `fut.result()` in the CLI loop is now wrapped in try/except so a single file's `MemoryError` doesn't abort the entire batch without a summary.
- **Sidecar-history no longer crashes on deleted source**: In-place mode's `_file_sha256(result.src)` now checks `result.src_deleted` before hashing.
- **Same-format skip guard completeness**: The "no processing needed" check now accounts for watermark, canvas, tone-map, DPI, ICC override, and template. JPEG→JPEG with a watermark is no longer silently skipped.
- **AVIF output validation tolerance**: Codec chroma padding (±1 pixel) no longer triggers spurious "Output size mismatch" errors.
- **Done handler respects in-place mode**: Output directory controls stay disabled after conversion when in-place mode is active.
- **Watch mode full feature parity**: `_watch_directory` now passes all 30+ `convert_file` kwargs (watermark, canvas, tone-map, DPI, ICC, recompress, etc.) using keyword arguments instead of fragile positional args.
- **`--backend vips` warning**: Users are now warned that the vips backend is experimental and skips metadata, resize, watermark, and all advanced options.
- **numpy graceful error**: `--tone-map` and `--target-psnr` now raise a clear `RuntimeError` instead of crashing with `ImportError` when numpy is missing.
- **HEIC bit-depth cached**: `_open_image` now extracts HEIC bit-depth once, eliminating a redundant `pillow_heif.open_heif()` call per AVIF/JXL output.
- **Lazy frame iteration**: Per-frame export in `_convert_animated_or_sequence` no longer materializes all frames into RAM at once.
- **Cache hash progress**: `--use-cache` now prints progress (`[cache] checking N/M...`) during the pre-conversion hash pass.
- Removed `codex-branding` comments from source code.

### Improved

- **Accessibility regression guard**: Disabled and secondary stylesheet states now use AA-readable token pairs; tests verify readable Catppuccin contrast, focus selectors for interactive controls, and accessible names/descriptions on primary GUI widgets.
- **Bare except logging**: Critical `except Exception: pass` blocks (HEIF security, queue save/load, cache persist, depth/HDR detection) now log to `_diag_log` or `result.warnings` for discoverability.
- **`convert_file` decomposition**: Extracted XMP sidecar, Live Photo pairing, depth-map, and HDR gain-map detection into `_run_sidecar_hooks()`.
- **PyQt6 deferred for CLI**: CLI mode (`--input`, `--version`, `--install-deps`, etc.) no longer requires PyQt6 to be installed. GUI mode still requires it and prints a clear error if missing.

### Added

- **Backend capability report**: `--backend-info` now prints a structured Pillow/vips capability matrix, with optional `--backend-benchmark PATH` timing output for a single image.
- **Redacted support bundle export**: The GUI and CLI can now write a diagnostic zip with app/platform/dependency/tool status, plugin trust inventory, settings schema, and recent redacted logs without including source images.
- **GUI plugin trust manager**: The main window now exposes a Plugins dialog that lists plugin path, trust status, hash prefix, and reason without importing plugin code, with Trust/Untrust actions backed by the hash manifest.
- **Unsigned release trust artifacts**: CI builds now emit per-binary SHA-256, dependency manifest, SBOM JSON, provenance JSON, and release-level `SHA256SUMS` so unsigned PyInstaller downloads can be verified.
- **Metadata/provenance integrity reports**: Conversion results, GUI CSV export, and CLI JSON reports now include before/after presence for EXIF, ICC, XMP, IPTC, MakerNotes, and C2PA, with warnings when requested metadata preservation drops detected fields.
- **Plugin registry shapes are live**: Trusted plugin `register()` returns now populate decoder, encoder, and storage registries; plugin decoders join scans, plugin encoders can write custom formats, and storage schemes appear in support summaries.
- **Versioned advanced presets**: Presets now normalize legacy GUI keys and CLI-shaped keys through one schema (`schema_version: 2`), covering template, AVIF codec/speed, watermark, canvas, scan filters, quality targets, sidecars, and metadata options in both GUI and CLI paths.
- **GUI scan filters for advanced presets**: Advanced controls now include exclude patterns and max input file size, matching `--exclude` and `--max-file-size`.
- **CLI / GUI / README parity guard**: `build_cli_parity_matrix()` now generates the parser-to-GUI/docs matrix and tests require every long CLI flag to be classified and documented when user-facing.
- Version badge in README.
- 16 new tests: in-place mode (3), skip guard completeness (4), strip-metadata verification, canvas alpha preservation (2), queue persistence (2), multi-frame export, plus associated imports.

- Bumped dependency floors: `Pillow>=12.2.0`, `pillow-heif>=1.4.0` to close the latest Pillow PSD tile-extents advisory plus the documented libheif/libjxl/LibRaw CVE floor.
- Plugin loading is now default-deny unless a plugin file's SHA-256 is recorded in `~/.imgconverter/plugins/trusted-plugins.json`; changed or symlinked plugins are skipped.
- **Path traversal fix**: Template-generated absolute paths are now rejected; output paths that escape the dest dir are flattened. Prevents writing to arbitrary filesystem locations.
- Fixed jpegtran temp file leak on timeout (`.jpegtran.tmp` files left on disk).
- Canvas parser now rejects zero and negative dimensions (previously crashed with `ZeroDivisionError`).

### Fixed

- Fixed all remaining `heicshift` references in CHANGELOG.md (8 occurrences in v2.9.0/v3.0.0 entries).
- Fixed fallback app icon showing old "H" letter instead of "I" for ImgConverter.
- Added `multiprocessing.freeze_support()` to entry point for PyInstaller `--use-processes` on Windows.
- **GUI hang fix**: `ConvertWorker.run()` now catches exceptions from `fut.result()` so one file's crash doesn't prevent `finished_all` from firing. Previously, a deleted source file mid-conversion would freeze the GUI permanently.
- **convert_file crash guard**: `src.stat()` moved inside try block so a missing source file produces a failed result instead of an unhandled exception.
- **Grid collision fix**: `png_lossy_chk` no longer overlaps `canvas_bg_edit` in the Advanced Options layout (both were at row 5, cols 2-3).
- `png_lossy` checkbox state now persisted across app restarts.
- `_restore_state` no longer crashes on corrupt QSettings values (bare `int()` calls replaced with `_safe_int()` helper).
- Watch mode no longer leaks an unused `ThreadPoolExecutor` (pool was created but never submitted to).
- `--max-file-size 0B` now correctly treated as "skip all files" instead of "skip nothing" (truthiness fix).
- Source/output/format controls now disabled during conversion to prevent confusing state drift.
- `#stopBtn` now has `:pressed` and `:disabled` stylesheet states for proper visual feedback.
- Preset menu now dynamically refreshes on open (picks up user presets added at runtime).
- ExifTool availability now logged on GUI startup (was CLI-only).
- 11 additional controls gained accessible names for screen readers.
- Added tooltips to Export Log and Clear log buttons.
- Recursive scan excludes now prune matching directories, so patterns such as `cache/**` skip everything underneath.

- **Secure temp files**: Atomic writes in in-place mode now use `tempfile.mkstemp()` for unpredictable paths (was deterministic `.imgconverter.tmp` suffix — symlink race on multi-user systems).
- **Thread-safe stop flag**: `ConvertWorker._stop` replaced with `threading.Event` for correctness under free-threaded Python (3.13t+).
- **Disk-full auto-stop**: Batch conversion detects `[Errno 28]` / "No space left" mid-batch and halts remaining files instead of failing serially.
- **All-files-failed escalation**: When 100% of files fail, shows a `QMessageBox.warning` dialog and uses Critical tray icon instead of silent Information toast.
- **PEP 668 warning**: `--install-deps` now warns before escalating to `--break-system-packages`, suggesting virtualenv.
- **Advanced Options collapsed by default**: The 16-control Advanced group is hidden behind an explicit show/hide toggle on first launch. State persisted across sessions.

### Added

- **GUI parity with CLI v3.0.0 features**: All `convert_file()` parameters are now accessible from both CLI and GUI. New "Advanced Options" panel with controls for: output filename template, DPI override, AVIF encoding speed (0-10), multi-frame handling (first/all/animate), HDR tone mapping, ICC profile override, watermark, canvas resize with background, XMP sidecar emit, lossless JPEG recompression, only-if-smaller guard, and target file size.
- `--avif-speed N` CLI flag (0-10) for controlling AVIF encoding speed/quality tradeoff (was hardcoded to 6).
- `--avif-codec {auto,aom,rav1e,svt}` CLI flag + GUI combo for AVIF encoder selection. SVT-AV1 is dramatically faster.
- `--png-lossy` CLI flag + GUI checkbox for lossy PNG optimization via pngquant (50-80% size reduction).
- `--max-file-size SIZE` CLI flag to skip files larger than threshold (e.g. `500MB`, `2GB`). Prevents OOM on huge images.
- **Clipboard paste**: Paste Clipboard button saves clipboard QImage to temp PNG and feeds to pipeline.
- **BigTIFF**: TIFF output auto-detects when estimated raw size exceeds 4 GB and sets `big_tiff=True`.
- **Lazy numpy**: moved from required to optional dependency; imported inside `_tone_map_hdr()` and `_psnr()` only.
- **Screen reader announcements**: `QAccessibleAnnouncementEvent` (Qt 6.8+) announces batch completion summary.
- GUI preset menu now shows user presets from `~/.imgconverter/presets/` alongside built-in presets.
- `--list-plugins`, `--trust-plugin`, and `--untrust-plugin` manage the local plugin trust manifest without loading plugin code.
- 30 new tests in `test_features.py` covering CLI parsing, presets, watermark, canvas, tone-map, quality targeting, DPI, ICC, recompress, BigTIFF, multi-frame, and scan exclude patterns.

## [v3.0.0] — 2026-05-17

Major release. Bundles 14 Next-tier features and 6 Later-tier architectural items
the entire roadmap "Now" tier already shipped in v2.9.0.

### Pipeline (new flags)

- `--watch` + `--watch-interval SEC` — folder watch mode, polling-based, debounced
- `--frames {first,all,animate}` — multi-frame source handling (animated WebP / AVIF / GIF / APNG / multi-page TIFF / HEIC sequences)
- `--recompress` — pixel-lossless JPEG → JPEG via `jpegoptim` / `jpegtran` fast-path
- `--target-kb N` — binary-search quality to hit a target output size
- `--target-psnr DB` — binary-search quality to hit a minimum PSNR vs source
- `--only-if-smaller PCT` — discard re-encoded output when not meaningfully smaller
- `--tone-map {none,reinhard,hable,clip}` — HDR tone mapping for PQ / HLG / wide-gamut sources
- `--icc PROFILE` — embed a chosen ICC profile (`sRGB` built-in or path to `.icc`)
- `--xmp-sidecar` — emit `<output>.xmp` next to converted file (darktable convention)
- `--dpi N` — set output DPI tag for JPEG / PNG / TIFF
- `--watermark SPEC` — text or PNG overlay with 9 positions + opacity
- `--canvas WxH` + `--canvas-bg COLOR` — pad to fixed canvas with background fill
- `--use-cache` + `--clear-cache` — content-hash cache at `~/.cache/imgconverter/seen.sqlite`
- `--resume` — pick up where a Ctrl-C / power-cycle left off (queue state at `~/.cache/imgconverter/queue.json`)
- `--use-processes` — `ProcessPoolExecutor` for GIL-bound interpreters
- `--backend {pillow,vips}` — opt-in libvips streaming backend for huge images
- `--verify-quality` — post-conversion butteraugli / ffmpeg-quality-metrics check
- `--sidecar-history` — write `<output>.imgconverter.json` with full preset for reproducibility
- `--register-shell` / `--unregister-shell` — Windows Explorer menu / Linux `.desktop` integration

### Format wins

- **Lossless JPEG → JXL transcoding** — pillow_jxl's bit-exact reconstruction, ~20% size reduction
- **Wide-gamut / 10-bit HEIC → JXL preservation** — detects source `bit_depth` and passes through; AVIF outputs warn because Pillow's AVIF encoder is 8-bit.

### Extensibility

- **Plugin system** — `~/.imgconverter/plugins/*.py` auto-loaded at startup; PLUGINS.md documents Decoder / Encoder / Storage shapes
- **conda-forge recipe** at `packaging/conda-forge/meta.yaml`
- **Multi-platform installer stubs** at `packaging/installers/` (MSI / DMG / .deb / .rpm / AppImage)

### Diagnostics

- Free-threaded Python detection (`sys._is_gil_enabled`) surfaced in CLI dep banner
- ffprobe cross-decoder validation when on PATH (second opinion on the saved file)

### Deferred (see ROADMAP.md "Deferred" section for the why)

macOS native ImageIO path, GPU codec hooks, multi-encoder AVIF shootout,
HTJ2K via OpenJPH, Apple ProRAW tone-map, SVG/PDF/PSD/XCF/EXR input,
Pyodide WASM build. Each needs external infrastructure (signing cert,
GPU, macOS host, encoder binary) that a code-only pass can't conjure;
they remain `[ ]` on the roadmap until a contributor with the right
setup picks them up.

## [v2.9.0] — 2026-05-17

The roadmap's "Now" tier — 24 items shipped against a 2026-05-17 plan.

### Security & correctness

- **Pinned dependency floors** (`requirements.txt` + `pyproject.toml`) close CVE-2025-48379, CVE-2024-28219, CVE-2025-29482, CVE-2024-41311, CVE-2024-11403/11498, CVE-2026-28231.
- **`--install-deps` subcommand** replaces the silent runtime `_bootstrap()` pip-install. New `_warn_below_floor()` flags older-than-floor dep versions on startup.
- **libheif memory cap** wired via `pillow_heif.set_security_limits()` — hostile HEIC/AVIF inputs can no longer OOM the host.
- **Symlink loop guard** in `scan_directory` — recursive scans no longer hang on symlink-to-ancestor.
- **Output verification** now re-decodes the saved file and asserts dimensions match the source (catches truncated encodes).
- **HEIC orientation regression tests** — fixture-based round-trip guarantees no double-rotation (the canonical IM #1232 / sharp #4384 / Pillow #9294 bug class).
- **ICC-aware CMYK→RGB conversion** when writing JPEG from CMYK source with an embedded ICC profile (closes the Display P3 color-shift complaint).

### Metadata fidelity

- **ExifTool tag-copy pass** after every save when `exiftool` is on `PATH` — recovers MakerNotes, GPS sub-IFDs, IPTC, sidecar XMP that Pillow drops. `--no-exiftool` opts out.
- **Live Photo `.HEIC` + `.MOV` pairing** — sibling motion file is copied alongside the converted still.
- **HEIC depth-map preservation** — Portrait-mode depth + iPhone Pro ProRAW depth emitted as `.depth.png` sidecars.
- **ISO 21496-1 HDR gain-map detection** — when source HEIC carries an Adaptive HDR gain map, the original is archived as `.gainmap.heic` alongside the SDR output. Full transcoding awaits libheif support.

### Format coverage

- **AVIF output explicitly routed through Pillow 11.3 native** (libaom/dav1d). `HAS_AVIF` guard + CLI dep-missing exit code mirror the JXL pattern.

### CLI & automation

- **`--template STR`** filename-template language with tokens `{stem} {ext} {fmt} {src_dir} {rel_dir} {width} {height} {date[:FMT]} {seq[:###]}`.
- **`--report PATH`** writes a structured JSON report (summary + per-file array) for CI / Ansible / cron pipelines.
- **`--preset NAME`** loads a JSON preset from `~/.imgconverter/presets/`; **`--list-presets`** dumps available. Built-in presets seeded on first launch.
- **`--exclude PATTERN`** glob filter (repeatable).
- **Structured exit-code matrix**: `0` OK · `1` partial · `2` input · `3` dep-missing · `4` disk-full · `5` cancelled · `6` total failure. README has the table.

### Distribution & hygiene

- **`pyproject.toml`** with `imgconverter = imgconverter:main` entry-point and `[dev]` / `[all]` extras.
- **CI smoke-test stage** — `{ubuntu, windows, macos} × {py3.11, py3.12}` runs `pytest` (19 tests, including format round-trips for all 6 output formats + orientation regression) before the PyInstaller `build` job.
- **`--collect-all pillow_heif`** in PyInstaller args closes the libheif binary-collection gotcha.
- **`CONTRIBUTING.md`** documents branch protection, release recipe, and code style.

### Accessibility & diagnostics

- **14 primary controls** got `setAccessibleName` + `setAccessibleDescription` + `setStatusTip` (screen reader / JAWS / NVDA / VoiceOver / Orca friendly).
- **Persistent diagnostic log** at `~/.cache/imgconverter/imgconverter.log`, 5 MB ceiling + one rotated backup. CLI invocations + GUI startup + update-check hits captured for support cases.
- **QSettings schema versioning** (`SETTINGS_SCHEMA = 2`) + one-shot migration on launch.
- **Opt-in GitHub release update check** with 24-hour cooldown (off by default).

### Bug fixes

- Fixed pre-existing **syntax error** on `from PyQt6.QtGui import (, QIcon` (line 119) — `imgconverter.py` would not parse against `python -m ast`.
- Fixed missing `from pathlib import Path` import before its use at line 15 (`_branding_icon_path`).
- Fixed CHANGELOG `%Y->-` artifact from a prior automated commit.

## [v2.8.0] - 2026-03-17

- Added: Live scan feedback with per-directory progress, status bar updates, and pulsing progress bar
- Fixed: XMP metadata for AVIF/JXL, UI scaling for diverse screen sizes
- Added: JPEG XL output, CLI full parity, progress counters, sorted scans
- Added: AVIF output, CSV export, drag & drop files, CLI parity flags, wall-clock time
- Added: JPEG/PNG input support — universal cross-format converter
- Added: CLI mode, disk space check, strip metadata, auto-open output, title bar file count, resize guard, better warnings, stats color reset, dependency version logging
- Added: Atomic writes, output validation, dark title bar, presets, smart option visibility, log context menu, overlap guard, speed stats
- Added: Image resize, filename prefix/suffix, progressive JPEG, lossless WebP, recent dirs, dark scrollbars
- Added: Drag & drop, format filter, skip existing, EXIF auto-rotate, ETA progress, tray notifications, log export, PyInstaller CI/CD
