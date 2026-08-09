"""Upgrade, quarantine, and unsupported-schema coverage for app-owned state."""

import json
import sqlite3

import pytest


def _is_quarantine(path):
    return list(path.parent.glob(path.name + ".corrupt-*"))


@pytest.fixture
def isolated_state(tmp_workdir, monkeypatch):
    import imgconverter

    cache = tmp_workdir / "cache"
    monkeypatch.setattr(imgconverter, "USER_CACHE_DIR", cache)
    monkeypatch.setattr(imgconverter, "USER_LOG_PATH", cache / "imgconverter.log")
    return tmp_workdir


def test_schema_registry_covers_all_persisted_formats(isolated_state, monkeypatch):
    import imgconverter

    plugin_dir = isolated_state / "plugins"
    plugin_dir.mkdir()
    monkeypatch.setattr(imgconverter, "_plugin_dir", lambda: plugin_dir)

    expected = {
        "settings",
        "presets",
        "preset_bundle",
        "batch_history",
        "watch_profiles",
        "plugin_trust",
        "queue",
        "batch_journal",
        "hash_cache",
    }
    assert expected <= set(imgconverter.PERSISTED_STATE_SCHEMAS)
    for name in expected:
        policy = imgconverter.PERSISTED_STATE_SCHEMAS[name]
        assert isinstance(policy["schema_version"], int)
        assert policy["migration"]
        assert policy["newer"]
        assert policy["corrupt"]

    support = imgconverter._build_support_bundle_payload()
    assert expected <= set(support["schemas"])
    assert support["schema_policies"] == imgconverter.PERSISTED_STATE_SCHEMAS


def test_legacy_batch_history_list_is_wrapped_and_rewritten(isolated_state, monkeypatch):
    import imgconverter

    path = isolated_state / "batch-history.json"
    path.write_text(json.dumps([{"id": "legacy", "surface": "cli"}]), encoding="utf-8")
    monkeypatch.setattr(imgconverter, "BATCH_HISTORY_PATH", path)

    assert imgconverter._load_batch_history()[0]["id"] == "legacy"
    migrated = json.loads(path.read_text(encoding="utf-8"))
    assert migrated == {
        "records": [{"id": "legacy", "surface": "cli"}],
        "schema_version": imgconverter.BATCH_HISTORY_SCHEMA,
    }


def test_legacy_watch_profiles_are_wrapped_and_rewritten(isolated_state, monkeypatch):
    import imgconverter

    path = isolated_state / "watch-profiles.json"
    path.write_text(json.dumps([{"source": "src", "enabled": False}]), encoding="utf-8")
    monkeypatch.setattr(imgconverter, "WATCH_PROFILES_FILE", path)

    profiles = imgconverter._load_watch_profiles()

    assert profiles[0]["source"] == "src"
    migrated = json.loads(path.read_text(encoding="utf-8"))
    assert migrated["schema_version"] == imgconverter.WATCH_PROFILES_SCHEMA
    assert migrated["profiles"] == profiles


def test_legacy_plugin_trust_schema_key_is_canonicalized(isolated_state, monkeypatch):
    import imgconverter

    plugin_dir = isolated_state / "plugins"
    plugin_dir.mkdir()
    path = plugin_dir / imgconverter.PLUGIN_TRUST_FILE
    records = {"demo.py": {"sha256": "a" * 64}}
    path.write_text(json.dumps({"schema": 1, "plugins": records}), encoding="utf-8")
    monkeypatch.setattr(imgconverter, "_plugin_dir", lambda: plugin_dir)

    assert imgconverter._load_plugin_trust() == records
    migrated = json.loads(path.read_text(encoding="utf-8"))
    assert migrated["schema_version"] == imgconverter.PLUGIN_TRUST_SCHEMA
    assert "schema" not in migrated


def test_legacy_queue_is_stamped_without_changing_resume_fields(isolated_state, monkeypatch):
    import imgconverter

    path = isolated_state / "queue.json"
    state = {"input": "src", "output": "dst", "pending": ["one"], "done": [], "failed": []}
    path.write_text(json.dumps(state), encoding="utf-8")
    monkeypatch.setattr(imgconverter, "QUEUE_STATE_PATH", path)

    loaded = imgconverter._load_queue_state()

    assert loaded["pending"] == ["one"]
    migrated = json.loads(path.read_text(encoding="utf-8"))
    assert migrated["schema_version"] == imgconverter.QUEUE_SCHEMA
    assert migrated["pending"] == ["one"]


def test_legacy_batch_journal_is_stamped_when_structurally_valid(isolated_state):
    import imgconverter

    path = isolated_state / "batch-journal.json"
    path.write_text(json.dumps({"batch_id": "old", "files": {}}), encoding="utf-8")

    journal = imgconverter._load_batch_journal(path)

    assert journal["batch_id"] == "old"
    assert journal["schema_version"] == imgconverter.BATCH_JOURNAL_SCHEMA
    assert json.loads(path.read_text(encoding="utf-8"))["schema_version"] == 1


@pytest.mark.parametrize(
    ("kind", "filename", "payload", "loader"),
    [
        (
            "batch history",
            "batch-history.json",
            {"schema_version": 999, "records": [{"id": "future"}]},
            lambda ic: ic._load_batch_history(),
        ),
        (
            "queue",
            "queue.json",
            {"schema_version": 999, "pending": ["future"]},
            lambda ic: ic._load_queue_state(),
        ),
    ],
)
def test_newer_json_state_is_preserved_and_not_resumed(
    isolated_state, monkeypatch, kind, filename, payload, loader
):
    import imgconverter

    path = isolated_state / filename
    path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    if filename == "batch-history.json":
        monkeypatch.setattr(imgconverter, "BATCH_HISTORY_PATH", path)
    else:
        monkeypatch.setattr(imgconverter, "QUEUE_STATE_PATH", path)
    before = path.read_bytes()

    assert loader(imgconverter) in ([], None)
    assert path.read_bytes() == before
    assert not _is_quarantine(path)
    assert kind in imgconverter.USER_LOG_PATH.read_text(encoding="utf-8").lower()


def test_newer_plugin_trust_and_preset_are_preserved(isolated_state, monkeypatch):
    import imgconverter

    plugin_dir = isolated_state / "plugins"
    plugin_dir.mkdir()
    trust_path = plugin_dir / imgconverter.PLUGIN_TRUST_FILE
    trust_path.write_text(json.dumps({"schema_version": 999, "plugins": {}}), encoding="utf-8")
    monkeypatch.setattr(imgconverter, "_plugin_dir", lambda: plugin_dir)

    preset_dir = isolated_state / "presets"
    preset_dir.mkdir()
    preset_path = preset_dir / "future.json"
    preset_path.write_text(json.dumps({"schema_version": 999, "name": "Future"}), encoding="utf-8")
    monkeypatch.setattr(imgconverter, "USER_PRESET_DIR", preset_dir)

    trust_before = trust_path.read_bytes()
    preset_before = preset_path.read_bytes()
    assert imgconverter._load_plugin_trust() == {}
    assert "Future" not in imgconverter.list_presets()
    assert trust_path.read_bytes() == trust_before
    assert preset_path.read_bytes() == preset_before
    assert not _is_quarantine(trust_path)
    assert not _is_quarantine(preset_path)


def test_corrupt_json_is_quarantined_with_path_free_diagnostic(isolated_state, monkeypatch):
    import imgconverter

    path = isolated_state / "queue.json"
    path.write_text("{not-json", encoding="utf-8")
    monkeypatch.setattr(imgconverter, "QUEUE_STATE_PATH", path)

    assert imgconverter._load_queue_state() is None
    backups = _is_quarantine(path)
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == "{not-json"
    diagnostic = imgconverter.USER_LOG_PATH.read_text(encoding="utf-8")
    assert str(isolated_state) not in diagnostic
    assert "queue state quarantined" in diagnostic.lower()


def test_legacy_sqlite_cache_gets_schema_metadata(isolated_state, monkeypatch):
    import imgconverter

    path = isolated_state / "seen.sqlite"
    monkeypatch.setattr(imgconverter, "HASH_CACHE_PATH", path)
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE seen (src_hash TEXT, preset_hash TEXT, dst_hash TEXT, "
        "dst_size INTEGER, ts INTEGER, PRIMARY KEY (src_hash, preset_hash))"
    )
    conn.commit()
    conn.close()

    cache = imgconverter._open_hash_cache()
    assert cache is not None
    row = cache.execute(
        "SELECT value FROM imgconverter_meta WHERE key = 'schema_version'"
    ).fetchone()
    cache.close()
    assert row == (str(imgconverter.HASH_CACHE_SCHEMA),)


def test_newer_sqlite_cache_is_preserved(isolated_state, monkeypatch):
    import imgconverter

    path = isolated_state / "seen.sqlite"
    monkeypatch.setattr(imgconverter, "HASH_CACHE_PATH", path)
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE imgconverter_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    conn.execute(
        "INSERT INTO imgconverter_meta(key, value) VALUES ('schema_version', '999')"
    )
    conn.commit()
    conn.close()
    before = path.read_bytes()

    assert imgconverter._open_hash_cache() is None
    assert path.read_bytes() == before
    assert not _is_quarantine(path)


class _FakeSettings:
    def __init__(self, values):
        self.values = dict(values)

    def value(self, key, default=None):
        return self.values.get(key, default)

    def setValue(self, key, value):
        self.values[key] = value


def test_settings_legacy_migrates_and_newer_settings_use_defaults():
    import imgconverter

    legacy = imgconverter.MainWindow.__new__(imgconverter.MainWindow)
    legacy.settings = _FakeSettings({"settings_version": 1, "fmt": 99})
    legacy._settings_schema_supported = True
    assert legacy._maybe_migrate_settings() is True
    assert legacy.settings.values["fmt"] == 0
    assert legacy.settings.values["settings_version"] == imgconverter.SETTINGS_SCHEMA

    newer = imgconverter.MainWindow.__new__(imgconverter.MainWindow)
    newer.settings = _FakeSettings({"settings_version": 999, "fmt": 3})
    newer._settings_schema_supported = True
    assert newer._maybe_migrate_settings() is False
    assert newer._settings_schema_supported is False
    assert newer.settings.values == {"settings_version": 999, "fmt": 3}
