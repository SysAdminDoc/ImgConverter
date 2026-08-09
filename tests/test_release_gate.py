"""Unit coverage for the isolated release-gate evidence helpers."""

import importlib.util
from pathlib import Path


def _release_gate_module():
    path = Path(__file__).resolve().parents[1] / "packaging" / "release_gate.py"
    spec = importlib.util.spec_from_file_location("imgconverter_release_gate", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_release_gate_reads_source_version_and_builds_pypi_sbom():
    gate = _release_gate_module()
    repo_root = Path(__file__).resolve().parents[1]
    version = gate._read_app_version(repo_root)
    sbom = gate._build_sbom([
        {"name": "PyQt6-Qt6", "version": "6.10.0"},
        {"name": "Pillow", "version": "12.3.0"},
    ])

    assert version == "3.9.1"
    assert sbom["schema_version"] == gate.GATE_SCHEMA_VERSION
    assert sbom["components"][0]["purl"] == "pkg:pypi/pillow/12.3.0"
    assert sbom["components"][1]["purl"] == "pkg:pypi/pyqt6-qt6/6.10.0"


def test_release_gate_smoke_environment_isolated_and_headless(tmp_path, monkeypatch):
    gate = _release_gate_module()
    monkeypatch.setenv("QT_QPA_PLATFORM", "wayland")
    environment = gate._smoke_environment(tmp_path / "home")

    assert environment["HOME"] == str(tmp_path / "home")
    assert environment["USERPROFILE"] == str(tmp_path / "home")
    assert environment["PIP_CACHE_DIR"] == str(tmp_path / "home" / "pip-cache")
    assert environment["QT_QPA_PLATFORM"] == "offscreen"


def test_release_gate_cleans_only_generated_outputs_and_redacts_temp_paths(tmp_path):
    gate = _release_gate_module()
    output_dir = tmp_path / "release"
    output_dir.mkdir()
    (output_dir / "ImgConverter.exe").write_bytes(b"old")
    (output_dir / "logs").mkdir()
    (output_dir / "logs" / "old.log").write_text("old", encoding="utf-8")
    keep = output_dir / "operator-note.txt"
    keep.write_text("keep", encoding="utf-8")

    gate._reset_output_dir(output_dir)

    assert not (output_dir / "ImgConverter.exe").exists()
    assert not (output_dir / "logs").exists()
    assert keep.read_text(encoding="utf-8") == "keep"
    assert gate._redact_temporary({"path": str(tmp_path)}, tmp_path) == {
        "path": "<temporary>"
    }
