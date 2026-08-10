# Research — ImgConverter

Date: 2026-08-08 — replaces all prior research.

## Executive Summary

ImgConverter v3.7.0 is a mature local-first Windows-oriented image conversion
workbench: one Python 3.10+ module, PyQt6 GUI, argparse CLI, Pillow/pillow-heif
decoding, optional RAW/JPEG XL/vips paths, metadata and ICC controls, batch
history, cache/resume, watch profiles, shell integration, redacted support
bundles, and SHA-256-trusted plugins. The live tree contains 316 collected
tests. The active roadmap has no actionable rows; `Roadmap_Blocked.md` already
holds platform, codec, installer, and design decisions that cannot be resolved
from repository research.

The product should deepen safety, recoverability, and proof rather than chase
format-count parity. Direct competitors repeatedly compete on Explorer
integration, presets/action chains, watch folders, target-size workflows,
translation, and visual comparison. ImgConverter already covers shell
integration, presets, watch mode, target size/quality, metadata reporting,
quality verification, and local privacy. The net-new gaps with the best
cost-to-risk ratio are:

1. A single pixel/decompression/resource policy across every decoder and
   plugin boundary.
2. A durable per-file batch journal that makes resume and in-place deletion
   auditable after interruption or power loss.
3. A reproducible, isolated release-validation gate for the unsigned
   PyInstaller artifact and its native codec inventory.
4. A malformed-input and metadata conformance corpus with bounded fuzz and
   regression coverage.
5. Runtime Qt localization, unified persisted-state migrations, offline
   performance evidence, and a versioned plugin capability contract.

These are the eight additions recorded in `ROADMAP.md` as RD-1 through RD-8.
No new P0 is justified by the evidence. The earlier C2PA API allegation,
stale dependency-floor findings, and other shipped items were rechecked and
intentionally excluded.

## Product Map

The current product loop is:

`select / drag / stdin / shell` → `recursive scan and filters` → `review /
dedup` → `ConvertOptions` parity boundary → `decode / transform / encode` →
`atomic output validation` → `report / support bundle / redacted history` →
`cache / resume / watch / preset repeat`.

Primary users are phone-photo owners converting HEIC/AVIF, web and content
teams producing JPEG/PNG/WebP/AVIF/JXL, archivists preserving ICC and metadata,
privacy-conscious users stripping GPS/device fields, and operators running
repeatable CLI or watch-folder jobs.

Strengths verified in the live source:

- `ConvertOptions` is the shared GUI/CLI/watch/preset boundary.
- Pillow is the stable fidelity path; vips is explicitly experimental and
  tile-oriented when metadata fidelity is not required.
- Outputs use temporary files, validation, and atomic replacement; in-place
  paths also carry an explicit source-deleted result flag.
- Batch history is redacted and schema-tagged; support bundles include
  dependency/native-codec/tool/format/plugin-trust evidence without source
  images or full private paths.
- Plugin trust is digest-pinned for files and entry points, with a capability
  inventory that does not import plugins for listing.
- The CLI exposes structured reports, progress events, cache/resume, quality
  targets, frame handling, backend information, and format matrices.

Known product boundaries that should remain explicit in documentation:

- HDR gain-map preservation is not complete.
- Apple Live Photo motion/depth content is not preserved as a complete package.
- RAW metadata does not survive the rawpy decode path.
- QOI has no metadata channel.
- Some HEIC odd-dimension cases need codec-specific handling.
- Trusted plugins execute in the ImgConverter process; the project documents
  that this is not a sandbox.

## Competitive Landscape

### Direct open-source competitors

File Converter and PowerToys show the value of a dependable Explorer
right-click path. Caesium and Curtail emphasize compression presets, metadata
choices, translations, and approachable batch progress. XL Converter is the
closest Python/Qt comparison and differentiates through newer codecs and
parallel encoding. Converseen and ImageMagick win on breadth and scripting;
Squoosh wins on interactive before/after comparison; ImageOptim and Trimage
win on focused optimization. Transmute and ConvertX demonstrate demand for
private, self-hosted broad conversion, but their server shape is not the
ImgConverter product.

The useful lesson is not “add every format.” It is to make a selected recipe
predictable, inspectable, repeatable, and safe when a batch is interrupted.

### Commercial competitors

XnConvert and ReaConverter set the bar for reusable action recipes, watch
folders, metadata operations, filename rules, and CLI/config-file automation.
CloudConvert demonstrates the convenience of a broad conversion API but also
the privacy and retention questions that ImgConverter avoids by staying local.
Lightroom demonstrates the archive workflow around RAW sidecars, export
metadata, and non-destructive provenance. ImgConverter should borrow explicit
policies and artifacts, not adopt cloud upload or catalog-account complexity.

### Adjacent workflows and discovery signals

digiKam and darktable treat batch conversion as a queue of explicit operations
with metadata templates and export controls. ExifTool remains the reference
for tag-level fidelity. copyparty and libvips show how local galleries,
thumbnailing, tiling, and streaming can scale, but they also show why a vips
fast path must remain opt-in when it cannot preserve ImgConverter's metadata
and editing contract. Awesome lists continue to surface small focused tools
more often than all-in-one desktop suites; discoverability should therefore
come from clear claims and proof artifacts.

### Community signals

Reddit and Stack Overflow discussions repeatedly report HEIC color/profile
loss, missing EXIF/GPS/date information, batch rename/delete expectations,
target-file-size requests, and confusion about whether a converter uploads
images. JPEG XL discussions on Hacker News and Lobsters show that archival and
reversible-transcode users care about generation loss, metadata, decoder
availability, and long-term reproducibility. These are direct support for
better policy reporting and recovery evidence, not for a new online service.

## Security, Privacy, and Reliability

### High-confidence local findings

- The HEIF initialization applies a 64-megapixel libheif guard. The general
  `--max-file-size` option filters by compressed file bytes, and
  `--max-memory` only emits a free-memory warning during a run. `_open_image()`
  otherwise routes Pillow, rawpy, and trusted plugin decoders through separate
  paths without one application-wide pixel, frame, decoded-byte, or time
  budget.
- Pillow 12.2.0 and 12.3.0 continue to fix decompression-bomb, bounds, loop,
  font, PDF, FITS, and other malformed-input issues. The Pillow PSD advisory,
  the 2026 TGA advisory, libheif v1.23.1 security fixes, and libjxl's
  0.12 hardening release demonstrate that a format allow-list and package floor
  are necessary but not sufficient for a batch tool opening untrusted files.
- libjxl specifically recommends v0.12 because of numerous decoder/encoder
  hardening fixes; the optional JXL path needs a release-time native-codec
  inventory rather than only a Python package version.
- The queue at `~/.cache/imgconverter/queue.json` is atomically written, but it
  is a coarse state snapshot: it records input/output, format, quality,
  pending/done/failed paths, and app version, is checkpointed every five
  completions, and resume matching does not include the complete preset,
  source hash, output hash, or commit phase. This is adequate for convenience
  resume, not for proving a power-loss-safe transaction boundary.
- `PLUGINS.md` correctly warns that trusted plugins run in-process. Digest
  trust establishes user consent and change detection, not containment of a
  malicious or buggy trusted plugin.

### Privacy and provenance

The local/no-upload default, selective metadata stripping, redacted history,
and redacted support bundle are strong differentiators. C2PA verification is
already routed through the current SDK validation-state API with a tool
fallback; the former `is_valid()` concern is not a live finding. The research
does not recommend signing or re-issuing C2PA manifests until a deliberate
provenance policy and test corpus exist. For now, reports should clearly state
what was detected, preserved, dropped, or invalidated.

### Reliability direction

Atomic per-file output, cache, resume, and sidecar history are good primitives.
The next reliability step is a journal of intent, temporary output, validated
output hash, source-deletion decision, and committed state. It should make
recovery explainable without storing image bytes or full private paths.

## Architecture Assessment

| Area | Current assessment | Research disposition |
|---|---|---|
| Conversion core | Strong shared option boundary and atomic output path; single-file size increases change risk. | Add tests and bounded policies before extracting modules. |
| Decoders/codecs | Broad and optional, but native libraries receive hostile input and do not share one budget. | RD-1 and RD-4. |
| Metadata/color | Better than most lightweight converters; known format losses are visible. | Conformance fixtures and explicit disposition reporting. |
| Batch state | History, cache, and resume exist; queue state is not a transaction journal. | RD-2 and RD-6. |
| GUI/i18n/accessibility | `self.tr()` is used widely and accessible names/descriptions are present; no `QTranslator`, `.ts`, `.qm`, or lrelease pipeline is present. | RD-5; retain keyboard/accessibility checks. |
| Plugins | Digest trust, entry-point hashing, and capability inventory exist; execution is same-process. | RD-8; full sandbox remains under consideration. |
| Diagnostics | Redacted support bundle, native-codec inventory, JSON report, progress events, and per-result elapsed time exist. | RD-7 for a versioned performance/evidence schema. |
| Distribution | Unsigned PyInstaller artifacts and hash/SBOM/provenance conventions are documented; local release validation is not one reproducible gate. | RD-3. |

### Cross-cutting coverage

- Security: RD-1, RD-2, RD-4, and RD-8.
- Accessibility: retain the existing semantic-widget and keyboard tests; add
  translated-label and focus-order checks alongside RD-5.
- Internationalization: RD-5.
- Observability: RD-2 and RD-7.
- Testing: RD-1, RD-4, RD-5, RD-6, and RD-8.
- Documentation and distribution: RD-3 and the plugin contract in RD-8.
- Plugin behavior: RD-8; the trust model remains explicit and opt-in.
- Mobile: rejected as a separate product shape.
- Offline: preserve as a core requirement; no upload or telemetry is proposed.
- Multi-user/server: rejected for the desktop product.
- Migration and upgrade: RD-6, with release proof in RD-3.

## Opportunity Evaluation

The following tiering is based on user impact, implementation effort, security
and compatibility risk, dependency burden, and product novelty. “Now” means a
roadmap addition for the next implementation pass; “Next” is high-value work
after the safety foundation; “Later” is useful but not a release blocker.

| ID | Tier | Opportunity | Impact | Effort | Main risk/dependency |
|---|---|---|---|---|---|
| RD-1 | Now / P1 | Whole-pipeline decode resource policy | Very high | M–L | Decoder header probing and plugin contract must agree. |
| RD-2 | Now / P1 | Durable per-file batch journal and recovery | Very high | M | Must preserve current resume/cache behavior and privacy. |
| RD-3 | Now / P1 | Isolated reproducible release validation | High | M | Native wheels and optional extras vary by host. |
| RD-4 | Next / P2 | Conformance corpus and bounded fuzz regressions | High | M–L | Fixtures and malformed files need maintenance. |
| RD-5 | Next / P2 | Runtime Qt localization | High | M | Translation catalog lifecycle and layout expansion. |
| RD-6 | Next / P2 | Unified persisted-state migrations | High | M | Existing legacy preset compatibility must remain intact. |
| RD-7 | Later / P3 | Offline performance/evidence schema | Medium | S–M | Cross-platform peak-memory measurements are inconsistent. |
| RD-8 | Later / P3 | Versioned plugin API/capability contract | Medium–high | M | Compatibility policy must not imply a sandbox. |

### Under Consideration

- A full plugin sandbox or out-of-process decoder host could reduce the impact
  of trusted-code mistakes, but Python/Qt/native-codec isolation, storage
  plugins, Windows packaging, and cross-platform policy make this an
  architecture program. Research supports documenting the boundary now, not
  promising a universal sandbox in the next pass.
- A non-destructive visual before/after comparison remains useful, but the
  existing design blocker for a comparison slider and the invisible-QA
  constraint mean it should not be promoted without a concrete testable design.
- A richer reorderable action-chain editor and per-file overrides remain
  design-blocked in `Roadmap_Blocked.md`; competitor evidence confirms demand
  but does not resolve the product decision.

## Rejected Ideas

- Cloud conversion, server mode, accounts, and multi-user catalogs conflict
  with the local-first/no-upload trust model and add retention/security scope.
- A mobile application is a separate interaction and distribution product;
  desktop GUI + CLI + shell integration is the current product boundary.
- AI upscaling, GPU codec hooks, additional native codec families, macOS
  ImageIO, HTJ2K, SVG/PDF, PSD/XCF/EXR/DPX/FITS, external reversible JXL, and
  signed platform installers are already external or packaging blockers in
  `Roadmap_Blocked.md`; duplicating them in the active roadmap would make it
  less actionable.
- More format-count parity with ImageMagick, Converseen, or ConvertX is not a
  good next investment while malformed-input limits, batch recovery, and
  release proof remain weaker differentiators.
- C2PA signing/re-issuance is deferred until the project chooses a provenance
  policy and has fixtures proving manifest behavior through every transform.
- Automatic outbound telemetry is rejected. Offline reports and explicit user-
  exported support bundles provide the needed evidence without weakening the
  privacy promise.

## Sources

### Direct open-source competitors

- https://github.com/Tichau/FileConverter
- https://github.com/Lymphatus/caesium-image-compressor
- https://github.com/JacobDev1/xl-converter
- https://github.com/Faster3ck/Converseen
- https://github.com/Alkl58/MegaPixel
- https://github.com/GoogleChromeLabs/squoosh
- https://github.com/ImageOptim/ImageOptim
- https://trimage.org/
- https://apps.gnome.org/en-GB/Curtail/
- https://github.com/transmute-app/transmute
- https://github.com/C4illin/ConvertX
- https://github.com/ImageMagick/ImageMagick
- https://github.com/jarun/imgp

### Commercial competitors

- https://www.xnview.com/en/xnconvert/
- https://www.reaconverter.com/features/automation.html
- https://www.reaconverter.com/features/image-editing.html
- https://cloudconvert.com/privacy
- https://cloudconvert.com/apis/file-conversion
- https://helpx.adobe.com/content/dam/help/en/pdf/lightroom_reference.pdf

### Adjacent-domain projects

- https://learn.microsoft.com/en-us/windows/powertoys/image-resizer
- https://docs.digikam.org/en/batch_queue.html
- https://docs.digikam.org/en/batch_queue/metadata_tools.html
- https://darktable-org.github.io/dtdocs/en/module-reference/utility-modules/shared/export/
- https://exiftool.org/exiftool_pod2.html
- https://github.com/9001/copyparty

### Awesome lists

- https://awesome-python.com/categories/image-processing/
- https://github.com/rwsturm/awesome-selfhosted
- https://github.com/0PandaDEV/awesome-windows

### Community signal

- https://www.reddit.com/r/software/comments/1fboem0
- https://www.reddit.com/r/software/comments/1mi3of0
- https://www.reddit.com/r/software/comments/1tia43f
- https://www.reddit.com/r/software/comments/1uj2s48
- https://news.ycombinator.com/item?id=27577328
- https://lobste.rs/s/tadtob/jpeg_xl
- https://stackoverflow.com/questions/65045644/heic-to-jpeg-conversion-with-metadata

### Standards/specs/platform APIs

- https://spec.c2pa.org/specifications/specifications/2.2/specs/ContentCredentials.html
- https://spec.c2pa.org/specifications/specifications/2.2/specs/C2PA_Specification.html
- https://www.color.org/liveTopics/v4spec.xalter
- https://www.iso.org/standard/87576.html
- https://jpeg.org/jpegxl/software.html
- https://specifications.freedesktop.org/desktop-entry/latest-single/
- https://doc.qt.io/qt-6/localization.html
- https://doc.qt.io/qt-6/linguist-lupdate.html
- https://doc.qt.io/qt-6/qaccessible.html

### Academic and engineering research

- https://github.com/cloudinary/ssimulacra2
- https://www.libvips.org/API/8.16/How-it-works.html
- https://arxiv.org/abs/2506.05987
- https://arxiv.org/abs/2006.08060

### Core dependency changelogs

- https://pillow.readthedocs.io/en/stable/releasenotes/12.3.0.html
- https://pillow.readthedocs.io/en/stable/releasenotes/12.2.0.html
- https://github.com/bigcat88/pillow_heif/blob/master/CHANGELOG.md
- https://pypi.org/project/pillow-heif/
- https://github.com/gorakhargosh/watchdog
- https://github.com/pyinstaller/pyinstaller/blob/develop/doc/CHANGES.rst

### Security advisories and CVEs

- https://github.com/advisories/GHSA-cfh3-3jmp-rvhc
- https://osv.dev/vulnerability/PYSEC-2026-3494
- https://github.com/strukturag/libheif/releases
- https://github.com/libjxl/libjxl
- https://github.com/libjxl/libjxl/releases
- https://nvd.nist.gov/vuln/detail/CVE-2026-1837

## Open Questions

1. Which default pixel, decoded-byte, frame-count, and wall-time budgets are
   acceptable for normal phone, print, and panorama workflows? The code should
   make them configurable, but the safe defaults need measured fixtures.
2. Should the recovery journal preserve failed-path entries indefinitely, or
   quarantine them with an explicit retention limit like batch history?
3. Which first additional locale gives the best maintainer and user coverage?
   The technical Qt pipeline is clear; the language choice is a maintainer
   resource decision, not a reason to block the design.
4. Can peak memory be measured consistently enough on Windows, macOS, and Linux
   to include it in the default report, or should it remain an optional field?
