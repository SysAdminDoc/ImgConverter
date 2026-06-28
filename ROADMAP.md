# ImgConverter Roadmap

**Current version:** v3.3.2 (released 2026-06-28) · **Roadmap revision:** 2026-06-28

The v3.3.1 drain cleared the prior queue. Current actionable items are listed below.

## Working Rules

- Keep `ROADMAP.md` actionable-only. Do not store completed items here.
- Move true blockers to `Roadmap_Blocked.md`.
- When a blocker is resolved, move it back here and implement it in priority order.
- Record completed work in `CHANGELOG.md` and git history.

## Research-Driven Additions

### P0

- [ ] P0 - Hash-pin trusted package entry-point plugins
  Why: File plugins are content-hash pinned, but entry-point plugins are trusted by package/version only, so a same-version reinstall can change executable plugin code without a changed trust status.
  Evidence: `imgconverter.py:694`, `imgconverter.py:838`, `tests/test_plugins.py`, `PLUGINS.md`
  Touches: `imgconverter.py`, `tests/test_plugins.py`, `PLUGINS.md`
  Acceptance: Trust records for `imgconverter.plugins` entry points include a stable digest of the distribution `RECORD`/module files; changed code at the same package/version reports `changed` and is not imported until re-trusted.
  Complexity: M

### P1

- [ ] P1 - Add persistent batch session history
  Why: Competitors expose actionable past-session history, while ImgConverter only has per-output sidecar history and exportable reports; users cannot review previous batch totals without saved reports.
  Evidence: Dinky session history, `README.md`, `imgconverter.py:9435`
  Touches: `imgconverter.py`, `tests/test_features.py`, `README.md`
  Acceptance: Completed GUI and CLI batches append a redacted local history record with timestamp, preset/options summary, counts, bytes before/after, failure count, and report/support-bundle pointers; GUI exposes a read-only history dialog; CLI can print history without source images or full private paths.
  Complexity: M
