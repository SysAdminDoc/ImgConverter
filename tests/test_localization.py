"""Headless smoke coverage for the Qt localization pipeline."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


def _qt_probe(tmp_path: Path, locale: str, *, persist_locale: bool = False) -> dict:
    profile = tmp_path / "profile"
    profile.mkdir()
    environment = os.environ.copy()
    environment.update({
        "HOME": str(profile),
        "USERPROFILE": str(profile),
        "QT_QPA_PLATFORM": "offscreen",
        "PYTHONUTF8": "1",
    })
    probe = """
import json
from pathlib import Path
from PyQt6.QtCore import QSettings
from PyQt6.QtWidgets import QApplication

import imgconverter

settings_file = Path(PROFILE_PATH) / "settings.ini"
imgconverter._app_settings = lambda: QSettings(str(settings_file), QSettings.Format.IniFormat)

app = QApplication([])
active, catalog = imgconverter._install_gui_translator(app, LOCALE)
window = imgconverter.MainWindow()
if PERSIST_LOCALE:
    window._select_gui_locale("es")
print(json.dumps({
    "active": active,
    "catalog": str(catalog) if catalog else None,
    "workflow": window.workflow_state.text(),
    "workflow_name": window.workflow_state.accessibleName(),
    "workflow_description": window.workflow_state.accessibleDescription(),
    "command_palette": QApplication.translate("CommandPaletteDialog", "Command Palette"),
    "plugin_trust": QApplication.translate("PluginTrustDialog", "Plugin Trust"),
    "language_actions": sorted(action.text() for action in window._locale_menu.actions()),
    "tab_order": window.src_edit.nextInFocusChain() == window.src_btn,
    "saved_preference": imgconverter._gui_locale_preference(),
}))
window.deleteLater()
""".replace("PERSIST_LOCALE", repr(persist_locale)).replace("PROFILE_PATH", repr(str(profile))).replace("LOCALE", repr(locale))
    completed = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=REPO_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout
    return json.loads(completed.stdout.strip().splitlines()[-1])


def test_translation_assets_and_workflow_are_shipped():
    translation_dir = REPO_ROOT / "translations"
    assert (translation_dir / "imgconverter_es.ts").is_file()
    qm = translation_dir / "imgconverter_es.qm"
    assert qm.is_file() and qm.stat().st_size > 0
    assert 'language="es_ES"' in (translation_dir / "imgconverter_es.ts").read_text(encoding="utf-8")
    localize_script = (REPO_ROOT / "tools" / "localize.py").read_text(encoding="utf-8")
    assert "pylupdate6" in localize_script
    assert "lrelease" in localize_script


def test_spanish_catalog_translates_status_dialog_accessibility_and_keyboard_flow(tmp_path):
    payload = _qt_probe(tmp_path, "es-MX", persist_locale=True)

    assert payload["active"] == "es"
    assert payload["catalog"].endswith("translations\\imgconverter_es.qm")
    assert payload["workflow"] == "Listo para escanear"
    assert payload["workflow_name"] == "Estado del flujo de trabajo"
    assert payload["workflow_description"] == "Estado actual del flujo por lotes"
    assert payload["command_palette"] == "Paleta de comandos"
    assert payload["plugin_trust"] == "Confianza de plugins"
    assert payload["language_actions"] == [
        "Español",
        "Inglés",
        "Predeterminado del sistema",
    ]
    assert payload["tab_order"] is True
    assert payload["saved_preference"] == "es"


def test_unsupported_explicit_locale_falls_back_to_english(tmp_path):
    payload = _qt_probe(tmp_path, "fr-FR")

    assert payload["active"] == "en"
    assert payload["catalog"] is None
    assert payload["workflow"] == "Ready to scan"
    assert payload["workflow_name"] == "Workflow status"
    assert payload["saved_preference"] == "system"
