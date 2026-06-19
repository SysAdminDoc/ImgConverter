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
