# ImgConverter Roadmap

**Current version:** v3.6.0 (released 2026-07-15) · **Roadmap revision:** 2026-07-16

## Working Rules

- Keep `ROADMAP.md` actionable-only. Do not store completed items here.
- Move true blockers to `Roadmap_Blocked.md`.
- When a blocker is resolved, move it back here and implement it in priority order.
- Record completed work in `CHANGELOG.md` and git history.

## Deep-Audit Backlog (2026-07-16)

Findings from a five-agent deep audit of v3.6.0. Every item below was verified
reachable against the current source before being listed (high-false-positive
areas were re-checked; rejected suspicions are not listed). Line numbers refer
to `imgconverter.py` at commit `53fb9a3`.

### P1 — Broken flagship behavior / security

### P2 — Correctness / reliability

### P3 — Edge cases, polish, maintainability

- [ ] P3 — `--progress` help promises `file_start` that is never emitted; animated files emit no `file_done`
  Why: `file_start` appears only in help text; the `--frames all/animate` pre-pass appends results without emitting `file_done`, so machine consumers see fewer events than `scan_done.count`.
  Where: `imgconverter.py:10321-10323, 11594-11618, 11678-11688`. Fix: emit `file_start` before each submit and `file_done` in the animated loop (or fix the help).

- [ ] P3 — Unhandled OSError from `stat()` on files that vanish between scan and use (CLI + GUI)
  Why: `--resume` total-size recompute (11473), dedup max-size pick (11498, 11502), GUI post-dedup sum (8819-8821), DuplicateReviewDialog sort key TOCTOU (6661 — `exists()` then `stat()`) all raise on vanished files → traceback/abort.
  Where: as listed. Fix: `_size_or_zero(p)` helper returning 0 on OSError, use everywhere.

- [ ] P3 — Conflicting/ignored CLI flag combinations accepted silently
  Why: `--watch --dry-run` watches for real (watch short-circuits before dry-run); `--watch` silently ignores `--report/--progress/--use-cache/--resume/--dedup-*/--when-done` and never writes history; `--in-place --output` ignores output without a word; `--stdin-null` without `--stdin-files` is a no-op.
  Where: `imgconverter.py:11442-11446, 11512, 11357-11360, 11107`. Fix: reject or warn in `_validate_cli_args`.

- [ ] P3 — `--output` and `--report` skip `expanduser()` while inputs get it
  Why: `-o ~/out` from cmd/PowerShell creates a literal `~` directory.
  Where: `imgconverter.py:11360, 11873`. Fix: `.expanduser()` both.

- [ ] P3 — Watch retry heuristic treats EVERY OSError as transient
  Why: `is_transient = r.error_code is not None` retries EACCES/ENOSPC/ENOENT 3× with backoff (noise, wasted work) while genuinely transient non-OS errors are never retried.
  Where: `imgconverter.py:10731`. Fix: whitelist transient errnos.

- [ ] P3 — Missing `imagehash` reported as "No near-duplicates found."
  Why: ImportError → `return []` → success-shaped message; factually wrong result every time for users without the optional dep.
  Where: `imgconverter.py:11228-11231, 8807-8809`. Fix: distinguish unavailable from empty; log an install hint.

- [ ] P3 — `os.path.commonpath` crash on multi-root file drops
  Why: Drops spanning drives (Everything search results, UNC + local) raise ValueError in the slot → abort.
  Where: `imgconverter.py:8883`. Fix: try/except → fall back to first file's parent.

- [ ] P3 — `_paste_clipboard` ignores `QImage.save()` failure
  Why: save returns False (disk full, unwritable cache) → `tmp_path.stat()` FileNotFoundError in slot → abort.
  Where: `imgconverter.py:8918-8920`. Fix: check the return, surface via `_set_workflow_state`.

- [ ] P3 — Dead per-file elapsed feature; ALL progress-bar text invisible
  Why: `_file_timer` is created/connected but `start()` is never called — the slow-file indicator can never appear. Compounding: `setTextVisible(False)` (8223) is never re-enabled, so every `progress_bar.setFormat()` call ("Scanning...", "Ready to convert", "No files found", …) renders nothing. Several intended user-facing states silently dropped.
  Where: `imgconverter.py:7133-7137, 9630-9633, 8223`, setFormat sites `8935, 9353, 9378, 9424, 9447, 9451, 9633, 9713`. Fix: start the timer in `_on_current_file` and re-enable text, or delete the apparatus.

- [ ] P3 — Pause span leaks into wall time when batch stopped/closed while paused
  Why: `_paused_total` only accumulates on Resume; Pause → Cancel inflates "Wall time" in summary + history by the paused duration.
  Where: `imgconverter.py:9841-9854, 9721, 9748-9754`. Fix: fold the open pause span in `_on_convert_done`/`_stop`.

- [ ] P3 — `_restore_dialog_geometry` writes to a different QSettings store than the app
  Why: Bare `QSettings()` without org/app names set on the QApplication → dialog geometry persists under a default key path, fragmenting persisted state.
  Where: `imgconverter.py:5916-5927` vs `7118`. Fix: `app.setOrganizationName/ApplicationName` in `main()`.

- [ ] P3 — Update-check QThread not shut down on close
  Why: Closing while a check is in flight destroys a running QThread with the window (same class as the ScanWorker item; low reach — opt-in + 24h throttle).
  Where: `imgconverter.py:7258-7267, 10073-10085`. Fix: wait in closeEvent or use daemon thread + signal bridge.

- [ ] P3 — closeEvent `terminate()` can't kill in-flight conversions
  Why: `stop()` cancels pending futures but running encodes join at executor exit; `wait(10000)` timeout → `terminate()` kills the QThread while pool threads keep running (interpreter joins them at exit anyway; terminated thread may hold executor locks).
  Where: `imgconverter.py:10078-10083, 4540-4613`. Fix: hide window + "finishing…" status instead of terminate.

- [ ] P3 — Wholesale broken i18n: `tr()` wrapped around f-strings; other strings not wrapped at all
  Why: `self.tr(f"Found {n} files…")` submits interpolated text — can never match a catalog and can't be extracted. `_update_title` and status-bar strings aren't wrapped. As shipped, translation is impossible.
  Where: representative `7110, 7582, 8600-8603, 8771, 8825-8827, 8893-8899, 8931, 8989, 9366-9368, 9389-9391, 9427, 9607, 9628, 9769, 9780, 9799-9817`; unwrapped `7310-7323, 9370, 9615-9622`. Fix: whole-file pass to `tr("…{}…").format(…)` placeholders (large, mechanical).

- [ ] P3 — Recent-dirs list: case-sensitive dedup + unvalidated JSON shape
  Why: `C:\Photos` vs `c:\photos` occupy two of 10 slots on Windows; corrupt non-list JSON later raises AttributeError in a slot → abort.
  Where: `imgconverter.py:9035-9048`. Fix: `os.path.normcase` dedup; `isinstance(parsed, list)` guard.

- [ ] P3 — `_scan` validation-error paths leave the summary panel half-mutated
  Why: Panel visibility mutations run before the empty-format-filter and max-file-size checks; on those error returns the panel is stuck pseudo-scanning (progress bar visible at stale value, empty-state hidden).
  Where: `imgconverter.py:9313-9333`. Fix: validate before mutating, or restore visibility on error paths.

- [ ] P3 — Review-table same-format warning: dead nested condition drops PNG/WebP cases
  Why: Outer condition tests JPEG|PNG|WEBP suffixes but inner body only warns for JPEG; PNG→auto commonly no-ops with no warning.
  Where: `imgconverter.py:8555-8557`. Fix: warn for all three or simplify.

- [ ] P3 — `when_done_combo` persists Sleep/Shutdown across sessions
  Why: Picking Shutdown once shuts the machine down after every future batch, sessions later, with only the 30s countdown as a guard. Power actions should be per-run.
  Where: `imgconverter.py:9889, 10014-10016`. Fix: don't persist indexes > 0.

- [ ] P3 — `_add_profile` validation gaps (WatchFolderDialog)
  Why: No rejection of output == source / output inside source (feeds the Run Now self-conversion item) / duplicate profiles; cancelling the output picker silently defaults to `source/converted`; deleted preset silently falls back to all-default options.
  Where: `imgconverter.py:6451-6476, 6520`. Fix: validate at add time with `_set_dialog_status` feedback; warn when preset unresolvable.

- [ ] P3 — DuplicateReviewDialog inconsistent skip microcopy
  Why: Same checked state labeled "Skip smaller file" initially but "Skip this file" after any toggle round-trip.
  Where: `imgconverter.py:6673 vs 6717`. Fix: one label string.

- [ ] P3 — Plugin trust dialog never exposes the full SHA-256 it asks users to trust
  Why: Only the 12-char prefix is shown anywhere (tooltip repeats the truncation); cross-checking a published hash is impossible from the GUI.
  Where: `imgconverter.py:867/878/889, 6049-6050, 5999`. Fix: full digest in tooltip/details pane; retitle column "Hash (first 12)".

- [ ] P3 — Grammar: "1 need review." in plugin-trust and history status lines
  Why: Second clause of `"{total} plugin entries found; {needs_review} need review."` isn't pluralized for count == 1.
  Where: `imgconverter.py:6059, 6200`. Fix: `need{'s' if n == 1 else ''}`.

- [ ] P3 — ShellIntegrationDialog preview shows Windows registry syntax on Linux/macOS
  Why: Preview always shows `--files %*` / `"%1"`; Linux uses `%F` desktop syntax and macOS status text tells the user to paste the preview into Automator (needs `"$@"`).
  Where: `imgconverter.py:6917-6920, 10843, 6984-6987`. Fix: branch preview on `platform.system()`.

- [ ] P3 — Windows context menu registered under `*` (all file types), not images
  Why: "Convert with ImgConverter" appears on `.docx`, `.exe`, everything; docstring says "for image files"; Linux path correctly scopes via MimeType.
  Where: `imgconverter.py:10770-10771, 10790`. Fix: register under `SystemFileAssociations\image\shell` (update `_detect_state` 6975 + uninstall keys to match).

- [ ] P3 — Management dialogs leak one instance per open
  Why: Every `_open_*` creates a dialog parented to MainWindow, `exec()`s, drops the local — C++ objects accumulate for process lifetime.
  Where: `imgconverter.py:8776-8796, 7196-7198, 8812`. Fix: `WA_DeleteOnClose` or `deleteLater()` after exec.

- [ ] P3 — PluginTrustDialog hashes every plugin file + full entry-point distributions on the GUI thread
  Why: Large entry-point package → dialog open/Refresh stalls the UI for the hashing duration.
  Where: `imgconverter.py:6044-6045 → 846-892, 915`. Fix: move to a worker if entry-point plugins become a real use case.

- [ ] P3 — Microcopy: casing drift and small string bugs
  Why: Title Case stragglers in a sentence-case UI — "Paste Image" (8057) vs More-menu "Paste image" (8154); "Open Output" (8125), "Export Log" (8314), "Export CSV" (8321); log context menu fully Title Case (8945-8973); combo items "Do Nothing"/"Close App" (8119), "Preserve All"/"Strip GPS Only" (7694-7699), "First Frame Only" (7874), "Max Dimension" (7741). Also: non-recursive scans log "Scanning : C:\dir" (stray space, 4631); "All {fail} file(s) failed" uses "(s)" while every other string does real pluralization (9769); `dedup_btn.setToolTip` called twice back-to-back, first is dead (8100/8104).
  Where: as listed. Fix: normalize to sentence case, fix the three string bugs.

- [ ] P3 — Missing test coverage
  Why: Zero coverage for: watch loop (`_watch_directory`, `_WatchHandler`), --stdin-files, --use-cache/--clear-cache, --dedup, --sidecar-history, --progress JSON events, RAW/HEIC input roundtrips, RGBA-to-format transparency handling, the in-place same-format failure path (P0 above), edit flags on same-format sources (P1 above).
  Where: `tests/`. Fix: add alongside the corresponding fixes; regression test per bug.

### Audited and found sound (do not re-chase)

Verified non-issues this pass: subprocess calls are all list-form/shell=False (no injection); `_write_text_atomic` is a correct atomic pattern; template/prefix path-traversal guards hold; plugin symlink/hash pinning works as documented (except items above); c2pa-python SDK path already uses `Reader.try_create` + `get_validation_state()` (the researched `is_valid()` claim was fixed in v3.4.0); QSettings bool/combo restores are range-checked; pause/stop deadlock, countdown escape routes, log growth caps, stale-scan guards, dedup-dialog escape semantics all correct; stylesheet colors flow from CAT tokens.
