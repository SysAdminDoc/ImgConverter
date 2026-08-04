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
