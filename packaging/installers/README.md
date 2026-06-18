# Packaging installers

PyInstaller binaries (built by `.github/workflows/build.yml`) are the
canonical install. These scripts wrap those binaries into platform-native
installers when a more polished install path is wanted.

## Status

| Platform | Format | Script | Status |
|---|---|---|---|
| Windows | MSI (WiX) | `windows-msi.ps1` | Stub — needs Authenticode signing cert |
| Windows | NSIS | `windows-nsis.nsi` | Stub |
| macOS | PKG | `macos-pkg.sh` | Stub — needs Apple Developer ID + notarization |
| macOS | DMG | `macos-dmg.sh` | Stub |
| Linux | .deb | `linux-deb.sh` | Stub |
| Linux | .rpm | `linux-rpm.sh` | Stub |
| Linux | AppImage | `linux-appimage.sh` | Stub |

All stubs need a signing identity and platform tooling installed to
produce shippable artifacts. Today the PyInstaller `.exe` / `.app` /
ELF binary attached to each GitHub release is what users grab.

## Why these aren't built in CI yet

- Windows MSI signing needs an EV / Authenticode cert (~$300/yr).
  Without it, SmartScreen shows "Unknown Publisher" on every install.
- macOS PKG signing + notarization needs a $99/yr Apple Developer ID
  and a notarization round-trip (~30 s per submission).
- Linux .deb / .rpm need per-distro builders and their own signing
  keys.

Until the signing infrastructure is in place, the unsigned PyInstaller
binary from `gh release download` is the supported install path. The
recipe stubs here are scaffolding for the day signing is wired up.

## Unsigned release trust artifacts

Every PyInstaller release asset is accompanied by:

- `<artifact>.sha256` — per-binary SHA-256 line.
- `<artifact>.dependencies.txt` — sorted `pip freeze --all` for the build.
- `<artifact>.sbom.json` — minimal dependency SBOM with package names,
  versions, and PyPI purl values where available.
- `<artifact>.provenance.json` — repository, commit, Actions run, runner OS,
  PyInstaller args, unsigned flag, and binary hash.
- `SHA256SUMS` — release-level checksum manifest for all uploaded binaries.

Verification before wrapping an installer:

```bash
sha256sum -c SHA256SUMS
jq '.unsigned == true and .sha256' ImgConverter-Linux.provenance.json
```

## conda-forge

`packaging/conda-forge/meta.yaml` is the conda-forge recipe template.
On each release:
1. Compute the source-tarball sha256: `gh release download vX.Y.Z` then
   `sha256sum ImgConverter-X.Y.Z.tar.gz`.
2. Update `version` and `sha256` in `meta.yaml`.
3. Submit a PR to https://github.com/conda-forge/staged-recipes for the
   initial submission, or to the feedstock repo for subsequent updates.

## Repo-side reference

- [requirements.txt](../../requirements.txt) — pinned runtime floors
- [pyproject.toml](../../pyproject.toml) — package metadata + entry point
- [.github/workflows/build.yml](../../.github/workflows/build.yml) — CI builds
