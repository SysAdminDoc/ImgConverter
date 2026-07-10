# Changelog

All notable changes to ImgConverter will be documented in this file.

## [Unreleased]

## [v3.6.0] — 2026-07-10

### Added

- **Batch editing layer** (folded in from the retired ImageForge project) — per-image adjustments and effects applied across the whole batch, alpha-preserving, and composable with the existing resize/canvas/watermark pipeline:
  - Adjustments: `--brightness`, `--contrast`, `--saturation` (−100..100), `--sharpness` (0..100 unsharp mask), `--blur` (0..20 px Gaussian), `--hue` (0..360°).
  - Tonal toggles: `--grayscale`, `--sepia`, `--invert`.
  - Effects: `--vignette`, `--grain` (film noise), `--tint COLOR@STRENGTH` (multiply-blend colour cast).
  - Framing: `--border` width + `--border-color`.
  - `--adjust-preset` one-shot looks (`vivid`, `muted`, `bw`, `vintage`, `cold`, `warm`); numeric flags stack additively on the preset, boolean toggles OR together.
  - `--social` size presets (`instagram-post`, `instagram-story`, `facebook-cover`, `facebook-header`, `x-header`, `youtube-thumbnail`, `linkedin-banner`, `og-image`) that pad output to platform dimensions; an explicit `--canvas` overrides them.
- Editing operations apply on both the still and multi-frame/animated conversion paths.
- 22 new tests covering the editing helpers, argument parsing, preset stacking, social→canvas mapping, validation ranges, and an end-to-end convert.

### Notes

- ImageForge's interactive-only features (freehand canvas annotation, the before/after comparison slider) are intentionally not carried over — they don't map onto a headless batch converter.
- All new flags are classified `cli-only` in the parity matrix; GUI exposure is a follow-up.

## [v3.5.0] — 2026-07-09

### Changed

- **Reimagined batch workspace**: Reframed the main window around a clear Source → Output recipe → Batch summary workflow with a persistent action dock and supporting Activity panel.
- **Premium source experience**: Added a high-visibility drop surface, clearer local-first reassurance, recent-source access, aligned output routing, and progressive disclosure for input filters and advanced controls.
- **Contextual batch summary**: Replaced six always-visible zero cards with an intentional empty state, scan/readiness feedback, responsive statistics, dynamic “Convert N images” copy, semantic progress, and output actions.
- **Responsive composition**: Wide windows use the two-column workspace; compact windows stack the source/recipe and action surfaces while collapsing secondary controls to preserve reachability.
- **Unified icon and app identity**: Replaced platform-dependent action glyphs and legacy HEICShift package artwork with a consistent line-icon system and ImgConverter stacked-image mark.
- **Dialog hierarchy**: Standardized headers, inline status tones, empty states, human-readable labels, disabled-action explanations, and coherent actions across plugin trust, history, watch profiles, duplicate review, commands, and file-manager integration.
- **Activity hierarchy**: Moved diagnostics below the primary workflow, made the panel collapsible, clarified export labels, and retained searchable local details without competing with Scan and Convert.
- **Shared interaction system**: Strengthened focus rings, hover/pressed/disabled states, table selection, combo menus, sliders, splitters, validation fields, and progress tones across the main window and every dialog.
- **Consistent geometry**: Normalized every stylesheet corner radius to the 0/4/6/8/10/12px product scale and added a regression test that rejects drift back to pill or off-scale shapes.

### Fixed

- **Default and compact layout clipping**: Removed the unreachable fixed-width action rows and added an explicit responsive breakpoint with horizontal-scroll fallback for expanded expert controls.
- **Scan consistency**: Source, recipe, filter, and automation controls now lock during scanning so visible settings cannot drift away from the active scan result.
- **Stale scan invalidation**: Editing the source clears the old review and disables conversion until the new source is scanned.
- **Dependent-control restoration**: Resize and only-if-smaller inputs remain disabled after a busy cycle when their parent options are unchecked.
- **Truthful completion state**: Cancelled batches now report Stopped, partial failures report Partial failure, and Open Output is enabled only when the current batch produced output.
- **Truthful progress copy**: Completed-file feedback no longer claims a file is currently processing after its future has already finished.
- **Checkbox accessibility**: Restored native checked glyphs so checkbox state is never communicated by color alone.
- **Validation accessibility**: Source/output/size errors now use semantic validation properties with screen-reader descriptions and clear consistently on edit.
- **Command discoverability**: Added a visible More entry point and normal Close/Open controls while removing hidden application shortcuts.

### Added

- **Six GUI and packaging regression tests**: Responsive reflow, dependent-state restoration, semantic summary mirroring, shortcut absence, native checkbox rendering, and packaged multiprocessing startup coverage. 192 total tests.

## [v3.4.3] — 2026-07-01

### Fixed

- **P0 data loss: only-if-smaller + in-place**: The only-if-smaller check now runs BEFORE the in-place `os.replace`, preventing irrecoverable source file deletion when the output didn't meet the size threshold.
- **Same-format skip guard**: Now correctly re-encodes when `only_if_smaller_pct` or `quality_mode` is set, instead of incorrectly skipping.
- **Image handle leaks**: Tone-map and sRGB/ICC conversion paths now close the old image before replacing.
- **Binary search best-fit**: `_binary_search_quality` now tracks the actual best-fitting quality (highest q under target for target-kb, lowest q meeting PSNR/SSIMULACRA2 target) instead of always returning the last evaluated iteration.
- **Pause-time accounting**: Elapsed time, ETA, speed, and wall-time summary now correctly subtract paused time instead of counting it as conversion time.
- **WatchFolderDialog cleanup**: Dialog now stops the _RunNowWorker on close to prevent signal delivery to destroyed widgets.
- **Quality-mode mutual exclusivity**: `--target-kb`, `--target-psnr`, `--target-ssimulacra2` are now validated as mutually exclusive.
- **Prefix/suffix path traversal**: `--prefix` and `--suffix` reject path separators and `..` to prevent output directory escape.
- **HEIF handle leak**: `pillow_heif.open_heif()` handle now properly closed after bit-depth extraction.
- **Settings combo bounds**: Five combo boxes (resize mode, TIFF compression, AVIF codec, frames mode, tone map) now validate index bounds when restoring from QSettings.
- **Scan/convert guards**: Both `_scan` and `_convert` now have double-invocation guards to prevent command-palette bypass.
- **File drop stat safety**: `stat()` in file drop handler now tolerates files deleted between drop and stat.
- **--max-memory help text**: Corrected to say "warn" instead of "reduce workers" (warning only, no throttling).
- **--proof help text**: Corrected to say outputs are kept for inspection (not auto-cleaned).
- **Exit code constant**: `_install_deps` now returns `EXIT_DEP_MISSING` instead of magic number 3.

### Added

- **5 new regression tests**: in-place only-if-smaller source preservation (P0), template path-traversal guard, template absolute-path guard, conflicting quality-mode flag rejection, prefix/suffix path-separator rejection. 186 total.

## [v3.4.2] — 2026-07-01

### Fixed

- **WatchFolderDialog _run_now no longer blocks GUI**: One-shot conversion now runs in a background `QThread` with progress feedback.
- **Log filter debounced**: 250ms debounce timer prevents O(n) rebuilds on every keystroke during active conversion.
- **Log filter no-results feedback**: Shows "No log lines match" placeholder when filter matches nothing.
- **Window close timeout extended**: Worker wait increased from 3s to 10s with `terminate()` fallback for stuck conversions.
- **Source folder deletion detected**: When scan finds no files because the source folder was deleted, shows "Source folder no longer exists" instead of the generic "No supported files" message.
- **Quality-mode target warning**: Binary search now logs when the target was not achieved at the quality floor/ceiling.

### Added

- **F5/F6 keyboard shortcuts**: F5 scans source, F6 starts conversion. Documented in button tooltips.
- **Command palette border**: Frameless dialog now has a visible border and rounded corners.
- **Command palette type-to-search**: Typing while the command list is focused redirects keystrokes back to the search field.
- **Dialog geometry persistence**: All 6 dialogs remember their size and position across sessions via QSettings.
- **Plugin symlink rejection tests**: Two regression tests verifying symlink plugins are blocked on trust and load.
- **Preset import tests**: Malformed JSON, missing bundle key, empty preset, and valid bundle import tests.
- **CLI --proof test**: Verifies proof mode converts the requested subset and exits cleanly.

## [v3.4.1] — 2026-07-01

### Fixed

- **HEIC bit-depth extraction crash**: `HAS_HEIF` was referenced but never defined, causing every HEIC file to fail with a `NameError` during metadata extraction.
- **Tone-map HDR blackout on 8-bit images**: `_tone_map_hdr` always divided by 65535 due to checking `float64.itemsize` after cast; now checks original image mode.
- **SSIMULACRA2 crash on tiny images**: Images smaller than 8px now return 0.0 instead of crashing with an empty-scales error.
- **Thumbnail-to-wrong-row after sort**: Thumbnails are now matched by file path instead of integer index, so sorting the review table during thumbnail loading no longer assigns thumbnails to wrong files.
- **CPU priority never restored**: GUI conversion with `--cpu-priority low` now restores normal process priority when the batch finishes.
- **Elapsed timer during pause**: Per-file elapsed timer now stops when conversion is paused and resumes when unpaused.
- **Drag-out collision**: Same-named files in different subdirectories now map correctly in drag-out (full path key instead of filename-only).
- **Review table stale after format change**: Output and Est. Output columns now refresh when the format combo changes.
- **C2PA reader.close() compat**: Guarded with `hasattr` to avoid AttributeError on older c2pa-python versions.
- **Countdown cancel lambda**: Removed broken dead-code lambda that could corrupt a list if the disconnect/reconnect below it were ever removed.
- **_log_lines unbounded growth**: Capped to 5000 entries to match the log view's block limit.
- **Priority functions hardened**: `_set_process_priority_low()` and `_restore_process_priority()` now catch OSError on both platforms.
- **Stale version in module docstring**: Updated from v3.3.4 to match APP_VERSION.

### Added

- **Escape to cancel**: Pressing Escape cancels the active conversion.
- **Review table format refresh**: Changing the output format now updates Output and Est. Output columns in the review table.
- **Scan guard**: Blocks scan-during-conversion via internal guard.
- **Accessibility improvements**: Accessible names for log_view, progress_bar, log_filter_edit. Thumbnail column header now says "Preview" instead of blank.
- **9 new regression tests**: SSIMULACRA2 edge cases, _estimate_output_size, zero-byte file, 1x1 pixel conversion, --target-ssimulacra2 validation.

## [v3.4.0] — 2026-07-01

### Added

- **SSIMULACRA2 quality targeting**: `--target-ssimulacra2 SCORE` binary-searches quality to hit a minimum SSIMULACRA2 perceptual score vs source. Optional `ssimulacra2>=0.3` dep.
- **Thumbnail preview column**: Scan review table shows 48px thumbnails loaded lazily via QThread. Decode failures show a blank cell.
- **Per-file elapsed time indicator**: Progress bar shows elapsed time for files taking >2 seconds during conversion.
- **Estimated output size column**: Scan review table shows per-file estimated output size based on format.
- **Drag-out of converted files**: After conversion, drag files from the scan review table to other applications via `QMimeData.setUrls()`.
- **CPU priority option**: `--cpu-priority {normal,low}` sets BELOW_NORMAL (Windows) or nice(10) (Unix) for background batch conversion.
- **Log search/filter**: Filter bar above the activity log with Ctrl+F shortcut. Filters visible log lines as the user types.

### Fixed

- **C2PA SDK verification**: `_verify_c2pa_sdk()` now uses `reader.get_validation_state()` instead of the incorrect `reader.is_valid()` call, which was a Reader lifecycle property — not a manifest validation method — and silently returned `"not-verified"` for every C2PA-marked file.
- **Dependency floor warnings**: c2pa-python and watchdog are now included in `DEP_FLOORS`, so users with outdated versions get startup warnings instead of silent breakage.
- **Screen reader blank-line workaround**: Empty log lines now use a zero-width space so NVDA/JAWS announce "blank" instead of repeating the previous line.

### Changed

- **c2pa-python floor**: bumped from `>=0.6` to `>=0.35` across all manifests. The `>=0.6` floor allowed installing versions where the Reader API didn't exist.
- **watchdog floor**: bumped from `>=4.0` to `>=6.0` across all manifests. Aligns with the project's Python 3.10+ requirement.
- **PyQt6 floor**: bumped from `>=6.8` to `>=6.10` across all manifests. Brings Qt 6.9.1 (450+ bug fixes).
- **PyInstaller spec modernized**: `optimize=2`, `upx_exclude` for .pyd/Qt6 DLLs, dynamic `hiddenimports` for optional deps.
- **i18n scaffolding**: All 345 user-visible GUI strings wrapped with `self.tr()` for future Qt Linguist translation.
- **JPEG XL browser status**: README and GUI tooltip now mention Safari 17+, Chrome 145+ (flag), Firefox 152+ (Labs).
- **Privacy comparison**: README "Why ImgConverter?" table now highlights offline privacy advantage vs online converters.

## [v3.3.4] — 2026-06-28

### Added

- **Persistent batch session history**: Completed GUI and CLI batches now append redacted local history records with option summaries, counts, byte totals, failure counts, and report/support-bundle pointers. The GUI exposes a read-only Batch History dialog, and `--history` prints the same redacted history as JSON.

## [v3.3.3] — 2026-06-28

### Fixed

- **Entry-point plugin trust pinning**: Package entry-point plugins now store and verify a digest of installed module and distribution metadata files, so same-version package code changes are reported as changed and skipped until re-trusted.

## [v3.3.2] — 2026-06-28

### Fixed

- **SDK-only C2PA verification**: C2PA-marked sources now run provenance verification when `c2pa-python` is installed even if the `c2patool` binary is absent, while preserving the existing `c2patool` fallback.

## [v3.3.1] — 2026-06-27

### Fixed

- **Multi-frame ConvertOptions parity**: `--frames all` / `--frames animate` now routes through `ConvertOptions`, so quality, resize, compression, DPI, and metadata choices are honored consistently with single-frame conversion.

### Changed

- Drained `ROADMAP.md` to actionable-only state and kept true blockers in `Roadmap_Blocked.md`.
- Synced docs with local-build-only releases and Pillow-native QOI support.

## [v3.3.0] — 2026-06-19

### Improved

- **Premium workflow state polish**: The header workflow label now uses semantic visual tones for active, success, warning, and danger states instead of one static badge treatment.
- **Action hierarchy refinement**: Secondary actions now use a quieter visual role while destructive dialog actions use a distinct danger role, making the main Scan/Convert path easier to read.
- **Management dialog finish**: Plugin trust and watch-folder dialogs now show explicit empty states and clearer button roles.
- **Accessibility sync**: Live stat cards now update their accessible descriptions whenever scan/conversion counts change.
- **First-run trust copy**: The app subtitle now reinforces the local/private conversion model without adding clutter.

### Changed

- Watch-folder profile removal now follows the no-confirmation interaction rule: the profile is removed immediately and the dialog reports the result in-place.

## [v3.2.0] — 2026-06-19

### Fixed

- **Locale-independent disk-full detection**: `_on_file_done` no longer relies on English error message strings ("No space left", "not enough space") to detect full disks. A new `ConvertResult.error_code` field carries the OS errno from `OSError` exceptions, and the GUI checks `errno.ENOSPC` / `errno.EDQUOT` directly. Works on all Windows and Linux locales.
- **Non-blocking update check**: `_maybe_check_for_update` now runs the network call in a background `QThread` instead of blocking the main Qt event loop. Slow DNS or unreachable GitHub no longer freeze the GUI on startup.
- **ITaskbarList3 IID was wrong**: The COM IID bytes for `ITaskbarList3` were incorrect, causing `CoCreateInstance` to silently fail on every Windows session. Taskbar progress (green bar during conversion) now actually works on Windows 7+.
- **Dedup hash double-resize removed**: `_dedup_scan` no longer pre-resizes images to 8x8 before calling `imagehash.average_hash`, which already handles its own resize. Eliminates false-positive duplicate matches from destroyed spatial detail.
- **Thread-safe diagnostic log rotation**: `_diag_log` now holds a `threading.Lock` across the size-check → rename → write sequence, preventing concurrent worker threads from losing log lines during rotation.
- **Lossless WebP visible in Auto mode**: The lossless WebP checkbox is now shown when output format is Auto (not just WebP), since Auto can select WebP for transparent sources.

### Added

- 3 new tests for the `error_code` field on `ConvertResult`.

## [v3.1.1] — 2026-06-19

### Fixed

- **GPS zero-coordinate bug**: Google Photos sidecar GPS coordinates at latitude 0 (equator) or longitude 0 (prime meridian) were silently dropped due to Python truthiness check treating 0.0 as falsy.
- **Same-format skip guard missed `strip_fields`**: Selective metadata stripping (`--strip-gps`, `--strip-device`) was silently skipped when the input and output format matched and no other processing was requested.
- **HDR tone-map clipped to 8-bit before curve**: `_tone_map_hdr` converted 16-bit sources to 8-bit RGB before applying the tone-mapping curve, destroying the dynamic range the curve is meant to compress. Now normalizes from native bit depth using float64.
- **When-done countdown Cancel button non-functional**: The sleep/shutdown countdown dialog used `countdown.done(1)` for both timer expiry and Cancel click, making it impossible for users to abort the system action. Cancel now properly aborts.
- **O(n²) GUI stat counters**: `_on_file_done` recomputed ok/skip/fail/saved by scanning the full results list on every file completion. Replaced with incremental counters.
- **CLI queue state O(n) membership checks**: `done_paths` and `failed_paths` were lists checked with `in` operator. Converted to sets for O(1) lookups.
- **ffprobe looked up per-file**: `shutil.which("ffprobe")` was called inside the hot `convert_file` path for every file. Cached at module load like other external tools.
- **Variable shadow in canvas block**: Local `h` variable for hex parsing shadowed the image height `h` from the outer scope.
- **Watermark image file handle leak**: `Image.open(payload_path)` was not closed after `.convert("RGBA")`.
- **Update check fallback treated older versions as newer**: When `packaging` was unavailable, any version string different from the current was returned as "newer". Now uses tuple comparison as fallback.
- **Redundant app icon set in main()**: `app.setWindowIcon` was called twice with different icons; the second call overwrote the first. Removed the dead call.
- **Inline stat-value font sizes**: Hardcoded `font-size: 22px; font-weight: 700` in stat color overrides now uses the shared `_STAT_FONT` constant.
- **Disk-full detection robustness**: Added `[errno 28]` pattern match alongside the existing English string checks.

### Improved

- **Pillow 14 compatibility**: Test suite no longer uses deprecated `Image.getdata()` (removed in Pillow 14, 2027-10-15); uses `get_flattened_data` when available.
- **Duplicate import removed**: `from pathlib import Path` was imported twice.
- **Duplicate classifier removed**: `Programming Language :: Python :: 3.14` appeared twice in `pyproject.toml`.
- **CLI parity dict accuracy**: `--strip-metadata` GUI mapping now points to `meta_combo` (the actual control) instead of the hidden legacy `strip_meta_chk`.
- 2 new tests: same-format-with-strip-fields skip guard, auto-mode RGB→JPEG format selection.

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
