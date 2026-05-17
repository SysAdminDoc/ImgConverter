# Contributing to HEICShift

HEICShift is MIT-licensed and accepts patches.

## Quick setup

```bash
git clone https://github.com/SysAdminDoc/HEICShift.git
cd HEICShift
pip install -e .            # uses pyproject.toml; installs all required deps
pip install -e .[all,dev]   # also installs pillow-jxl, rawpy, qoi, pytest, pyinstaller
python heicshift.py         # GUI mode
python heicshift.py -i ./photos --format jpeg -q 85   # CLI mode
```

ExifTool is an optional runtime helper that closes the biggest metadata-loss
gap in Pillow's EXIF model. On Linux: `apt-get install exiftool`. On macOS:
`brew install exiftool`. On Windows: download from [exiftool.org](https://exiftool.org/)
and put `exiftool.exe` on `PATH`.

## Tests

```bash
python -m pytest
```

19+ tests run on every push and PR via [.github/workflows/build.yml](.github/workflows/build.yml).
The CI matrix covers `{ubuntu, windows, macos} × {py3.11, py3.12}`. A build
won't ship if tests fail.

If you add a new output format, add a parametrised entry in
`tests/test_roundtrip.py`. If you touch the orientation / EXIF code, the
fixture-based tests in `tests/test_orientation.py` guard against the
double-rotation regression that hits ImageMagick, sharp, and Pillow upstream.

## Release process

Releases follow the project-wide "Release vX.Y.Z" recipe:

1. Bump `APP_VERSION` in `heicshift.py` and `version` in `pyproject.toml`.
2. Add an entry to `CHANGELOG.md` (top of file, current date).
3. `git commit -m "Release vX.Y.Z — <one-line why>"` then `git tag vX.Y.Z`.
4. `git push && git push --tags`.
5. Trigger the `Build` workflow with `version=vX.Y.Z` — the matrix builds
   Windows / macOS / Linux PyInstaller binaries and uploads them to the
   matching GitHub Release.

The `release` job in the workflow is gated on `workflow_dispatch` with a
`version` input — pushes alone never publish a release.

## Branch protection

`main` is protected. PRs require:

- All matrix CI tests green (Linux + Windows + macOS, Python 3.11 + 3.12).
- One approving review.
- No force-pushes (admins included).

## What lands in `master` vs `next` vs feature branches

There's only `master` today. Feature branches are encouraged for anything
larger than ~50 LOC; small fixes go straight to `master` via PR.

## Code style

- Single-file `heicshift.py` is the project's deliberate shape — keep it
  readable rather than splitting eagerly. Helpers can be extracted into
  modules when a single function exceeds ~100 lines AND has no GUI deps.
- Match the existing comment style — sparse, focused on "why". WHAT is in
  the code already.
- Catppuccin Mocha is the canonical palette. Square 4–12 px corner radii
  only; no pill / oval / fully-rounded backdrops on text-bearing widgets.
- Type hints on public functions. `Optional[T]` is `T | None` (we require
  Python 3.10+).
- New CLI flags get a corresponding test in `tests/`, an entry in the
  README CLI table, and a `--help` line.

## Bug reports

Open an issue with:

- HEICShift version (`heicshift --version`).
- OS + Python version.
- Dependency versions — paste the line `heicshift --input ./empty --dry-run`
  prints on startup.
- A minimal sample file when possible (zip + drag-drop into the issue).

If you can't share the file, paste the relevant lines from
`~/.cache/heicshift/heicshift.log`.
