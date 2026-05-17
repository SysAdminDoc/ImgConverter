# Changelog

All notable changes to HEICShift will be documented in this file.

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
- **`--preset NAME`** loads a JSON preset from `~/.heicshift/presets/`; **`--list-presets`** dumps available. Built-in presets seeded on first launch.
- **`--exclude PATTERN`** glob filter (repeatable).
- **Structured exit-code matrix**: `0` OK · `1` partial · `2` input · `3` dep-missing · `4` disk-full · `5` cancelled · `6` total failure. README has the table.

### Distribution & hygiene

- **`pyproject.toml`** with `heicshift = heicshift:main` entry-point and `[dev]` / `[all]` extras.
- **CI smoke-test stage** — `{ubuntu, windows, macos} × {py3.11, py3.12}` runs `pytest` (19 tests, including format round-trips for all 6 output formats + orientation regression) before the PyInstaller `build` job.
- **`--collect-all pillow_heif`** in PyInstaller args closes the libheif binary-collection gotcha.
- **`CONTRIBUTING.md`** documents branch protection, release recipe, and code style.

### Accessibility & diagnostics

- **14 primary controls** got `setAccessibleName` + `setAccessibleDescription` + `setStatusTip` (screen reader / JAWS / NVDA / VoiceOver / Orca friendly).
- **Persistent diagnostic log** at `~/.cache/heicshift/heicshift.log`, 5 MB ceiling + one rotated backup. CLI invocations + GUI startup + update-check hits captured for support cases.
- **QSettings schema versioning** (`SETTINGS_SCHEMA = 2`) + one-shot migration on launch.
- **Opt-in GitHub release update check** with 24-hour cooldown (off by default).

### Bug fixes

- Fixed pre-existing **syntax error** on `from PyQt6.QtGui import (, QIcon` (line 119) — `heicshift.py` would not parse against `python -m ast`.
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
