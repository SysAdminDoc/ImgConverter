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

- [ ] P2 — QImage built without bytesPerLine — thumbnails shear/garble, possible OOB read
  Why: `QImage(data, w, h, Format_RGB888)` assumes 32-bit-aligned scanlines; PIL rows are tightly packed `w*3`. Any thumbnail width not divisible by 4 (portrait thumbs at width 45/47…) shears rows and reads past buffer end.
  Where: `imgconverter.py:4393-4394`. Fix: pass `rgb.width * 3` as bytesPerLine (and `w*4` on the RGBA branch for symmetry). Related: `QPixmap.fromImage` runs in the worker thread (GUI-thread-only per Qt docs) — emit the QImage and convert in the slot.

- [ ] P2 — Thumbnail-loader swap race: stale queued `thumbnail_ready` indexes the NEW loader's file list
  Why: `_on_thumbnail_ready` does `self._thumb_loader._files[idx]`; queued cross-thread emissions from the OLD loader are delivered after the swap. Shorter new list (dedup Apply Skips, smaller drop) → IndexError in slot → abort; in-range stale indexes paint wrong thumbnails. The v3.4.x UserRole fix covers same-loader sorting only.
  Where: `imgconverter.py:8624-8632, 8530-8532, 8596, 4368/4396`. Fix: emit the path in the signal, or guard `if self.sender() is not self._thumb_loader or idx >= len(...): return`.

- [ ] P2 — Drag & drop not gated by the busy lock — drops mid-conversion clobber live batch state
  Why: While converting, drops still rewrite `src_edit`, replace `self._scan_result`, restart the thumbnail loader, ENABLE the Convert button mid-run, and overwrite stats/title. Same clobber during scans.
  Where: `imgconverter.py:8833-8904`. Fix: `event.ignore()` in dragEnter/drop when worker or scanner is running.

- [ ] P2 — "Find similar" runs the full perceptual-hash scan synchronously on the GUI thread
  Why: `_dedup_scan` decodes every scanned image + O(n²) hamming compare in the clicked slot; thousands of photos = minutes of "Not Responding" with no progress/cancel. The one hard UI freeze left (everything else is threaded).
  Where: `imgconverter.py:8803, 11222-11248`. Fix: move to a QThread with progress/cancel like ScanWorker.

- [ ] P2 — No KeyboardInterrupt handling in `_run_cli`; documented `EXIT_CANCELLED` (5) is dead
  Why: Ctrl-C propagates; executor `__exit__` does `shutdown(wait=True)` without cancel_futures so in-flight conversions keep running (appears hung), then traceback + wrong exit code; queue-state save, `--report`, history, and `batch_done` event all skipped. `EXIT_CANCELLED` defined at line 43 and referenced nowhere.
  Where: `imgconverter.py:11675-11815, 43`. Fix: try/except KeyboardInterrupt → `pool.shutdown(wait=False, cancel_futures=True)`, save queue state, exit 5; `_watch_directory` Ctrl-C returns 5 too (currently EXIT_OK at 10752).

- [ ] P2 — Windows piped/redirected stdout crashes on non-ANSI filenames
  Why: No `sys.stdout.reconfigure`; with output redirected, stdout encoding is the ANSI code page (cp1252) and per-file prints of `result.src.name` raise UnicodeEncodeError for e.g. CJK names, killing the batch mid-run.
  Where: `imgconverter.py:11711, 11718, 11725, 10723` (prints). Fix: at CLI entry, `s.reconfigure(encoding="utf-8", errors="replace")` for stdout/stderr (guard with hasattr).

- [ ] P2 — `--resume` only matches on input dir; ignores stored format/quality/output (version recorded, never checked)
  Why: Resume after changing `--format`/`--output` silently skips files "done" under old settings — outputs for the new format never produced.
  Where: `imgconverter.py:10567-10577, 11467-11477`. Fix: extend the guard to compare format/quality/output; warn on version mismatch.

- [ ] P2 — `--proof 0` (or negative) silently runs the FULL batch
  Why: `if proof_n is not None and proof_n > 0:` falls through to real conversion — the opposite of "sample only". No validation in `_validate_cli_args`.
  Where: `imgconverter.py:11519-11520, 10983-11092`. Fix: reject `--proof < 1` in validation.

- [ ] P2 — Watchdog `_WatchHandler` has no `on_moved` — atomic-rename drops invisible
  Why: `.tmp`/`.part` → final rename (browsers, rsync, camera importers) emits FileMovedEvent only; watchdog mode has no rescan, so these files are never converted. (Imported FileCreatedEvent/FileModifiedEvent at 10629 are unused.)
  Where: `imgconverter.py:10631-10644`. Fix: add `on_moved` enqueuing `Path(event.dest_path)` when suffix is supported.

- [ ] P2 — Watch backends have opposite semantics for pre-existing files
  Why: Polling converts everything already in the folder on the first loop; watchdog mode only processes post-start events. Same command, opposite behavior depending on whether watchdog is installed. Help says "convert new files as they arrive".
  Where: `imgconverter.py:10683-10691, 10287-10289`. Fix: pick one (seed watchdog with an initial `_safe_walk`, or record initial contents as converted in polling) and document.

- [ ] P2 — Shell-integration install/uninstall error paths raise into PyQt slots → app abort
  Why: Windows uninstall guards only FileNotFoundError (`PermissionError` → crash); Linux install `mkdir`/`_write_text_atomic` unguarded (re-raises). The "Removal failed. Check permissions" message is unreachable for exactly the failure it describes; uninstall returns EXIT_OK unconditionally.
  Where: `imgconverter.py:10795-10849`, slots `7004, 7013`. Fix: broaden excepts to OSError, return EXIT_INPUT_ERROR on failure, wrap slot bodies.

- [ ] P2 — ShellIntegrationDialog "Default preset" combo is dead UI
  Why: The combo is built with a tooltip promising "Preset applied when converting via shell context menu" but its value is never read; registered commands contain no `--preset`. Users select, install, get success feedback, shell conversions use defaults.
  Where: `imgconverter.py:6928-6935, 7004-7011, 10767-10867`. Fix: append `--preset "<name>"` to registered commands, or remove the combo.

- [ ] P2 — Watch-profile "Enabled/Paused" toggle has no effect — nothing watches the folders
  Why: The `enabled` flag is written by the dialog and read back only for display; no QFileSystemWatcher/timer/startup hook runs enabled profiles, and CLI `--watch` never reads `watch-profiles.json`. Accessible description says "monitored for automatic conversion" — automation that doesn't exist. Only Run Now executes (and ignores `enabled`).
  Where: `imgconverter.py:6356, 6377, 6490-6495, 6346, 6422, 8786`. Fix: wire enabled profiles to a real watcher in MainWindow, or drop the toggle and reword copy to "on-demand profiles".

- [ ] P2 — BatchHistoryDialog aborts on corrupt/foreign history records
  Why: `_load_batch_history` validates only that records are dicts; nested `counts`/`bytes` values untrusted. `"counts": "3"` or `"bytes": {"before": "n/a"}` → AttributeError/ValueError in `_refresh` inside the constructor slot → abort.
  Where: `imgconverter.py:6195, 6229-6232` (contrast guarded `_row_values` 6210-6213). Fix: apply the same isinstance/coercion guards + try/except around int conversions.

- [ ] P2 — WatchFolderDialog close during Run Now: up-to-5s GUI freeze; cancelled run recorded as completed
  Why: `done()` blocks the GUI thread in `wait(5000)` (single large AVIF/JXL encodes exceed 5s → thread orphaned). Either way `_on_run_now_done` still writes `last_run`/`last_count`/`last_error` for a run the user cancelled — displayed as if it completed.
  Where: `imgconverter.py:6590-6594, 6563-6588`. Fix: record `last_error = "cancelled"` (or skip the update) when `_stop` was set; replace blocking wait with a non-blocking finish.

- [ ] P2 — GUI exposure of the v3.6.0 editing layer
  Why: All 16 edit flags are CLI-only (intentional at merge time, "GUI exposure = follow-up"). The GUI recipe has no adjustments/effects/border/social controls.
  Where: `imgconverter.py` MainWindow advanced controls; parity matrix `CLI_FLAG_PARITY`. Fix: design a compact "Edits" group in advanced controls, wire to `ConvertOptions`, persist via QSettings, update parity matrix.

### P3 — Edge cases, polish, maintainability

- [ ] P3 — Hue rotation scale uses 255 instead of 256
  Why: PIL HSV hue wraps at 256 steps; `degrees/360*255` compresses the circle (~1.4° max error).
  Where: `imgconverter.py:2522`. Fix: `int(round((degrees % 360) / 360.0 * 256)) & 0xFF`.

- [ ] P3 — Animated save uses the LAST frame's duration for all frames
  Why: `img.info.get("duration")` is read after the iterator has seeked to the final frame; variable-delay GIFs re-encode with wrong constant timing.
  Where: `imgconverter.py:3319-3337`. Fix: collect per-frame durations during iteration, pass the list.

- [ ] P3 — Per-frame duplicate "edit: applied image adjustments" warnings
  Why: `_apply_edits` appends the warning once per frame; a 500-frame GIF → 500 identical entries each echoed as `[WARN]` log lines.
  Where: `imgconverter.py:2628-2629` via `3092-3093`, echo `4608-4609`. Fix: append once per file.

- [ ] P3 — Watermark text measurement allocates a full image-size RGBA layer
  Why: `Image.new("RGBA", (iw, ih))` exists only for `textbbox`; ~400 MB transient per 100 MP file. A 1×1 probe measures identically.
  Where: `imgconverter.py:2397-2398`.

- [ ] P3 — Plugin trust: TOCTOU between hash check and module execution
  Why: Executed bytes are re-read from disk after the trust hash was computed; concurrent write in the window loads unaudited code. (Same-user boundary — hardening, not a privilege escalation.)
  Where: `imgconverter.py:690 vs 953-956`. Fix: read bytes once, hash those bytes, `exec(compile(...))`.

- [ ] P3 — Entry-point trust digest blind to editable installs
  Why: PEP 660 editable installs produce a digest over metadata only; source edits keep "trusted" status, defeating v3.3.3 pinning for the dev-install case.
  Where: `imgconverter.py:705-743`. Fix: detect `direct_url.json` `dir_info.editable` → return `""` so status becomes "changed".

- [ ] P3 — `_verify_c2pa_sdk` leaks the native Reader on exception
  Why: `reader.close()` only on success path; `reader.json()`/`get_validation_state()` raise → handle leaked per C2PA-marked file.
  Where: `imgconverter.py:319-349`. Fix: try/finally or context manager.

- [ ] P3 — `--untrust-plugin` cannot remove entry-point records with dotless versions
  Why: `ep:pkg==2:plug` has no `Path.suffix` → `.py` appended → key mismatch → "no trusted entry". Dotted versions survive only by accident.
  Where: `imgconverter.py:807-814, 673-677`. Fix: bypass the `.py` mangling for `ep:`-prefixed refs.

- [ ] P3 — `_CLI_ONLY` detection misses `--input=PATH` form and GUI-free admin flags; dead PyQt6 stub block
  Why: `--input=x` (equals form), `--trust-plugin`, `--untrust-plugin`, `--watch` don't trigger CLI mode detection. The ~25-line Qt stub-class block (1139-1188) is reachable only for partially-broken PyQt6 installs while its own message advertises headless use that `_check_required_deps_or_exit` (hard exit 3) makes impossible.
  Where: `imgconverter.py:106-123, 1139-1188`. Fix: extend `_CLI_ONLY` detection; either make CLI genuinely PyQt6-optional or delete the stub and fix the message.

- [ ] P3 — Blanket `except Exception: pass` swallowers hide real failures
  Why: `_discover_entrypoint_plugins` (917-918) makes all entry-point plugins silently vanish on any importlib error; `_verify_quality` bare excepts (398-399, 413-414) kept its dead-code breakage invisible through four audits.
  Where: `imgconverter.py:917-918, 398-399, 413-414`. Fix: log to stderr/diag at minimum.

- [ ] P3 — Watch mode `converted`/`seen_sizes` grow unbounded; stale entries suppress re-dropped same-name files forever
  Why: Sets only grow; in hot-folder workflows a re-dropped same-name file is skipped permanently via `if p in converted`.
  Where: `imgconverter.py:10619-10620, 10696, 10724-10747`. Fix: periodic existence sweep dropping entries whose paths no longer exist.

- [ ] P3 — `_append_batch_history` read-modify-write with no lock; corrupt history silently replaced
  Why: Concurrent GUI/CLI sessions interleave load/write and lose records; `JSONDecodeError` → `[]` → corrupt file overwritten with single-record history, no user-visible signal.
  Where: `imgconverter.py:5170-5178, 5132-5146`. Fix: exclusive lock (msvcrt/fcntl) around load+write; rename corrupt file to `.corrupt-<ts>` before rewriting.

- [ ] P3 — `--stdin-files` reads text-mode stdin; `.strip()` mangles legal filenames
  Why: Windows pipe decodes with locale code page → UnicodeDecodeError on UTF-8 bytes aborts before any work; `line.strip()` strips whitespace that is legal in filenames.
  Where: `imgconverter.py:11107-11112`. Fix: `sys.stdin.buffer.read().decode("utf-8", errors="surrogateescape")`; split on `"\0"` in NUL mode without strip (at most `\r\n` in newline mode).

- [ ] P3 — `_file_contains_marker` re-reads up to 2 MB up to 3× per conversion, uncached
  Why: Read in `_open_image` (3003), again via metadata presence (3755→2922), again on output (4314/3363).
  Where: `imgconverter.py:2886-2893`. Fix: always record `meta["c2pa"]` after the first read; have `_metadata_presence_from_image` trust a present key. (Output read is genuinely needed.)

- [ ] P3 — `--max-memory` only warns; "throttle" strings are false
  Why: Startup message (11452) and parity matrix (11433) say "throttle threshold"; implementation prints `[WARN]` and does nothing (argparse help honestly says "Warn").
  Where: `imgconverter.py:11737-11740`. Fix: pause new submissions until free memory recovers, or change the two "throttle" strings to "warn".

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
