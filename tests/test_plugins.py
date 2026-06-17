"""Plugin trust-gate regression tests."""

import sys

from PIL import Image


def _plugin_source(marker, text):
    return (
        "from pathlib import Path\n"
        "def register(opts):\n"
        f"    Path({str(marker)!r}).write_text({text!r}, encoding='utf-8')\n"
    )


def test_untrusted_plugin_is_not_executed(tmp_workdir, monkeypatch):
    import imgconverter

    plugin_dir = tmp_workdir / "plugins"
    plugin_dir.mkdir()
    marker = tmp_workdir / "loaded.txt"
    (plugin_dir / "01-hello.py").write_text(
        _plugin_source(marker, "loaded"),
        encoding="utf-8",
    )

    monkeypatch.setattr(imgconverter, "_plugin_dir", lambda: plugin_dir)

    assert imgconverter._load_plugins() == []
    assert not marker.exists()


def test_trusted_plugin_loads_then_hash_change_blocks_it(tmp_workdir, monkeypatch):
    import imgconverter

    plugin_dir = tmp_workdir / "plugins"
    plugin_dir.mkdir()
    marker = tmp_workdir / "loaded.txt"
    plugin = plugin_dir / "01-hello.py"
    plugin.write_text(_plugin_source(marker, "first"), encoding="utf-8")

    monkeypatch.setattr(imgconverter, "_plugin_dir", lambda: plugin_dir)

    ok, msg = imgconverter._trust_plugin(plugin)
    assert ok, msg
    assert imgconverter._load_plugins() == ["01-hello"]
    assert marker.read_text(encoding="utf-8") == "first"

    marker.unlink()
    plugin.write_text(_plugin_source(marker, "second"), encoding="utf-8")

    assert imgconverter._load_plugins() == []
    assert not marker.exists()


def test_untrust_plugin_removes_manifest_entry(tmp_workdir, monkeypatch):
    import imgconverter

    plugin_dir = tmp_workdir / "plugins"
    plugin_dir.mkdir()
    plugin = plugin_dir / "01-hello.py"
    plugin.write_text("def register(opts):\n    return None\n", encoding="utf-8")

    monkeypatch.setattr(imgconverter, "_plugin_dir", lambda: plugin_dir)

    ok, msg = imgconverter._trust_plugin(plugin)
    assert ok, msg
    assert "01-hello.py" in imgconverter._load_plugin_trust()

    ok, msg = imgconverter._untrust_plugin("01-hello")
    assert ok, msg
    assert "01-hello.py" not in imgconverter._load_plugin_trust()

    try:
        sys.path.remove(str(plugin_dir))
    except ValueError:
        pass


def test_trusted_plugin_registers_decoder_encoder_and_storage(tmp_workdir, monkeypatch):
    import imgconverter

    plugin_dir = tmp_workdir / "plugins"
    plugin_dir.mkdir()
    plugin = plugin_dir / "02-codecs.py"
    plugin.write_text(
        """
from pathlib import Path
from PIL import Image

class DemoDecoder:
    extensions = {'.demo'}
    def open(self, src):
        return Image.new('RGB', (4, 3), (12, 34, 56)), {'decoder': 'demo'}

class DemoEncoder:
    fmt = 'demoout'
    extension = '.demoout'
    def save(self, img, path, options):
        Path(path).write_text(f"{img.size[0]}x{img.size[1]} q={options.get('quality')}", encoding='utf-8')

class DemoStorage:
    scheme = 'mem'
    def write(self, src, dst_uri):
        return True

def register(opts):
    return {'decoders': [DemoDecoder()], 'encoders': [DemoEncoder()], 'storage': [DemoStorage()]}
""".strip(),
        encoding="utf-8",
    )

    monkeypatch.setattr(imgconverter, "_plugin_dir", lambda: plugin_dir)
    ok, msg = imgconverter._trust_plugin(plugin)
    assert ok, msg

    assert imgconverter._load_plugins() == ["02-codecs"]
    assert ".demo" in imgconverter.get_supported_extensions()
    support = imgconverter.get_format_support_summary()
    assert "Plugin decoders .demo" in support
    assert "Plugin encoders demoout" in support
    assert "Plugin storage mem://" in support

    src = tmp_workdir / "sample.demo"
    src.write_bytes(b"plugin bytes")
    decoded = imgconverter.convert_file(src, tmp_workdir / "decoded", fmt="png")
    assert decoded.success, decoded.error
    with Image.open(decoded.dst) as out:
        assert out.size == (4, 3)

    png = tmp_workdir / "source.png"
    Image.new("RGB", (5, 2), (1, 2, 3)).save(png)
    encoded = imgconverter.convert_file(png, tmp_workdir / "encoded", fmt="demoout", jpeg_quality=81)
    assert encoded.success, encoded.error
    assert encoded.dst.suffix == ".demoout"
    assert encoded.dst.read_text(encoding="utf-8") == "5x2 q=81"
    assert any("plugin encoder: demoout" in w for w in encoded.warnings)
