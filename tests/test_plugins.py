"""Plugin trust-gate regression tests."""

import sys


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
