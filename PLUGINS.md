# HEICShift Plugin Authoring Guide

HEICShift v2.9.0+ scans `~/.heicshift/plugins/*.py` at startup and runs any
top-level `register(opts)` callable it finds.

## Why plugins?

HEICShift's single-file `heicshift.py` ships as a small PyInstaller binary
on purpose. Adding niche format support — say, an EXR decoder for one
particular VFX pipeline, or a Storage destination that pushes outputs to
a self-hosted MinIO bucket — would bloat the core bundle for every user.
Plugins keep the core lean and let power users wire in extras.

## Minimum viable plugin

`~/.heicshift/plugins/01-hello.py`:

```python
def register(opts):
    print(f"[hello] plugin loaded under HEICShift v{opts['app_version']}")
```

That's it. HEICShift's startup log will show `[plugins] loaded: 01-hello`.

## Plugin shapes the core looks for

These are documented hooks. Today's loader calls `register()`; specific
shapes below describe what the plugin module should *contain*. Future
HEICShift versions will read these out of `register()`'s return value.

### Decoder

```python
from pathlib import Path

class MyDecoder:
    extensions = {".myfmt"}

    def open(self, src: Path):
        # return (PIL.Image.Image, metadata_dict)
        ...

def register(opts):
    return {"decoders": [MyDecoder()]}
```

### Encoder

```python
class MyEncoder:
    fmt = "myfmt"
    extension = ".myfmt"

    def save(self, img, path: Path, options: dict):
        # write img to path; options carries quality / strip_metadata / etc.
        ...

def register(opts):
    return {"encoders": [MyEncoder()]}
```

### Storage destination

```python
from pathlib import Path

class S3Storage:
    scheme = "s3"

    def write(self, src: Path, dst_uri: str) -> bool:
        # upload src bytes to dst_uri (e.g. s3://bucket/key.jpg)
        ...

def register(opts):
    return {"storage": [S3Storage()]}
```

## Where plugins live

| Path | Purpose |
|---|---|
| `~/.heicshift/plugins/*.py` | User plugins (auto-loaded) |
| `~/.heicshift/presets/*.json` | Built-in + user presets (`--preset NAME`) |
| `~/.cache/heicshift/heicshift.log` | Diagnostic log (rotated at 5 MB) |
| `~/.cache/heicshift/seen.sqlite` | `--use-cache` content-hash cache |
| `~/.cache/heicshift/queue.json` | `--resume` queue state |

## Naming convention

Plugins load in alphabetical order. Prefix with `00-`/`01-`/etc. when load
order matters. Modules with a leading `_` are skipped (handy for shared
helpers your other plugins import).

## Sandbox / safety

Plugins run in the same Python process as HEICShift. There's no sandbox.
**Treat plugin code as if you wrote it yourself.** Audit before dropping
a plugin into the directory.

## Distribution

Plugin authors should publish a `pip install heicshift-plugin-<name>`
package that drops a single file into `~/.heicshift/plugins/` via a
`post_install` hook, or document the manual copy step.
