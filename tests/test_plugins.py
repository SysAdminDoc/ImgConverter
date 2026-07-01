"""Plugin trust-gate regression tests."""

import sys
from pathlib import Path

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


def test_plugin_trust_rows_do_not_execute_plugin_code(tmp_workdir, monkeypatch):
    import imgconverter

    plugin_dir = tmp_workdir / "plugins"
    plugin_dir.mkdir()
    marker = tmp_workdir / "executed.txt"
    plugin = plugin_dir / "03-danger.py"
    plugin.write_text(
        f"from pathlib import Path\nPath({str(marker)!r}).write_text('ran', encoding='utf-8')\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(imgconverter, "_plugin_dir", lambda: plugin_dir)
    rows = imgconverter.get_plugin_trust_rows()

    assert not marker.exists()
    assert rows[0]["name"] == "03-danger.py"
    assert rows[0]["status"] == "untrusted"
    assert rows[0]["hash_prefix"]
    assert "trust-plugin" in rows[0]["reason"]
    assert rows[0]["trust_ref"] == str(plugin)


def test_entrypoint_plugin_rows_can_be_trusted_by_trust_ref(tmp_workdir, monkeypatch):
    import imgconverter

    plugin_dir = tmp_workdir / "plugins"
    plugin_dir.mkdir()
    ep_info = {
        "name": "demo",
        "package": "imgconverter-demo",
        "version": "1.2.3",
        "module": "imgconverter_demo:register",
        "trust_key": "ep:imgconverter-demo==1.2.3:demo",
        "sha256": "a" * 64,
    }

    monkeypatch.setattr(imgconverter, "_plugin_dir", lambda: plugin_dir)
    monkeypatch.setattr(imgconverter, "_discover_entrypoint_plugins", lambda: [ep_info])

    rows = imgconverter.get_plugin_trust_rows()
    assert rows[0]["name"] == ep_info["trust_key"]
    assert rows[0]["path"] == ep_info["module"]
    assert rows[0]["trust_ref"] == ep_info["trust_key"]
    assert rows[0]["status"] == "untrusted"

    ok, msg = imgconverter._trust_plugin(rows[0]["trust_ref"])
    assert ok, msg
    trust_record = imgconverter._load_plugin_trust()[ep_info["trust_key"]]
    assert trust_record["sha256"] == ep_info["sha256"]

    rows = imgconverter.get_plugin_trust_rows()
    assert rows[0]["status"] == "trusted"
    assert rows[0]["hash_prefix"] == "a" * 12


def test_entrypoint_distribution_digest_tracks_module_changes(tmp_workdir):
    import imgconverter

    package_file = tmp_workdir / "imgconverter_demo.py"
    package_file.write_text("def register(opts):\n    return None\n", encoding="utf-8")
    record_file = tmp_workdir / "imgconverter_demo-1.2.3.dist-info" / "RECORD"
    record_file.parent.mkdir()
    record_file.write_text("imgconverter_demo.py,,\n", encoding="utf-8")

    class FakeDist:
        name = "imgconverter-demo"
        version = "1.2.3"
        files = [
            Path("imgconverter_demo.py"),
            Path("imgconverter_demo-1.2.3.dist-info/RECORD"),
        ]

        def locate_file(self, file_ref):
            return tmp_workdir / str(file_ref)

    class FakeEntryPoint:
        name = "demo"
        value = "imgconverter_demo:register"
        dist = FakeDist()

    first_digest = imgconverter._entrypoint_distribution_digest(FakeEntryPoint())
    package_file.write_text("def register(opts):\n    return {'encoders': []}\n", encoding="utf-8")
    second_digest = imgconverter._entrypoint_distribution_digest(FakeEntryPoint())

    assert len(first_digest) == 64
    assert len(second_digest) == 64
    assert first_digest != second_digest


def test_entrypoint_same_version_digest_change_blocks_load(tmp_workdir, monkeypatch):
    import imgconverter

    plugin_dir = tmp_workdir / "plugins"
    plugin_dir.mkdir()
    marker = tmp_workdir / "entrypoint-loaded.txt"
    ep_info = {
        "name": "demo",
        "package": "imgconverter-demo",
        "version": "1.2.3",
        "module": "imgconverter_demo:register",
        "trust_key": "ep:imgconverter-demo==1.2.3:demo",
        "sha256": "1" * 64,
    }

    monkeypatch.setattr(imgconverter, "_plugin_dir", lambda: plugin_dir)
    monkeypatch.setattr(imgconverter, "_discover_entrypoint_plugins", lambda: [ep_info])

    ok, msg = imgconverter._trust_plugin(ep_info["trust_key"])
    assert ok, msg
    assert imgconverter.get_plugin_trust_rows()[0]["status"] == "trusted"

    changed_ep = {**ep_info, "sha256": "2" * 64}
    monkeypatch.setattr(imgconverter, "_discover_entrypoint_plugins", lambda: [changed_ep])

    def fail_import(_name):
        marker.write_text("imported", encoding="utf-8")
        raise AssertionError("changed entry-point plugin should not be imported")

    monkeypatch.setattr(imgconverter.importlib, "import_module", fail_import)
    rows = imgconverter.get_plugin_trust_rows()

    assert rows[0]["status"] == "changed"
    assert "content hash changed" in rows[0]["reason"]
    assert imgconverter._load_plugins() == []
    assert not marker.exists()


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


def test_symlink_plugin_rejected_on_trust(tmp_workdir, monkeypatch):
    import imgconverter

    plugin_dir = tmp_workdir / "plugins"
    plugin_dir.mkdir()
    real = plugin_dir / "real.py"
    real.write_text("def register(opts):\n    return None\n", encoding="utf-8")
    link = plugin_dir / "link.py"
    link.symlink_to(real)

    monkeypatch.setattr(imgconverter, "_plugin_dir", lambda: plugin_dir)
    ok, msg = imgconverter._trust_plugin(link)
    assert not ok
    assert "symlink" in msg.lower()


def test_symlink_plugin_blocked_on_load(tmp_workdir, monkeypatch):
    import imgconverter

    plugin_dir = tmp_workdir / "plugins"
    plugin_dir.mkdir()
    real = plugin_dir / "real.py"
    real.write_text("def register(opts):\n    return None\n", encoding="utf-8")

    monkeypatch.setattr(imgconverter, "_plugin_dir", lambda: plugin_dir)
    ok, msg = imgconverter._trust_plugin(real)
    assert ok, msg

    real.unlink()
    link = plugin_dir / "real.py"
    link.symlink_to(tmp_workdir / "nowhere.py")

    rows = imgconverter.get_plugin_trust_rows()
    real_row = [r for r in rows if r["name"] == "real.py"]
    if real_row:
        assert real_row[0]["status"] != "trusted"
