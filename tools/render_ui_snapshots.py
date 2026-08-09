#!/usr/bin/env python3
"""Render deterministic ImgConverter page snapshots without input injection."""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path


# The project QA display is 125% scaled. Render at 1x so a requested 1440x900
# client frame remains 1440x900 instead of becoming 1800x1125 physical pixels.
os.environ.setdefault("QT_SCALE_FACTOR", "1")
os.environ.setdefault("QT_FONT_DPI", "96")


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import imgconverter  # noqa: E402

from PyQt6.QtCore import QSettings, QTimer  # noqa: E402
from PyQt6.QtGui import QColor, QPainter, QPalette, QPixmap  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402


DEMO_HISTORY = [
    {
        "timestamp": "2026-08-08T14:14:00+00:00",
        "surface": "gui",
        "preset": "Web delivery",
        "options": {"format": "webp", "quality": 86, "workers": 8, "metadata_mode": "strip_gps"},
        "counts": {"converted": 142, "skipped": 0, "failed": 0},
        "bytes": {"before": 1_932_735_283, "after": 654_311_424},
        "timing": {"wall_seconds": 107},
        "artifacts": {"report": "report.html", "support_bundle": "support.zip"},
    },
    {
        "timestamp": "2026-08-08T11:02:00+00:00",
        "surface": "watch",
        "preset": "Archive",
        "options": {"format": "jpeg", "quality": 92, "workers": 6},
        "counts": {"converted": 68, "skipped": 2, "failed": 0},
        "bytes": {"before": 880_803_840, "after": 640_679_936},
        "timing": {"wall_seconds": 76},
        "artifacts": {},
    },
    {
        "timestamp": "2026-08-07T16:37:00+00:00",
        "surface": "cli",
        "preset": "Custom",
        "options": {"format": "png", "quality": 90, "workers": 4},
        "counts": {"converted": 32, "skipped": 0, "failed": 1},
        "bytes": {"before": 438_304_768, "after": 210_763_776},
        "timing": {"wall_seconds": 43},
        "artifacts": {},
    },
]

DEMO_WATCH = [
    {
        "source": r"C:\Media\Incoming",
        "output": r"C:\Media\Web",
        "preset": "Web delivery",
        "last_run": "2026-08-08T14:02:00+00:00",
        "last_error": None,
        "last_count": 142,
    },
    {
        "source": r"D:\Scans",
        "output": r"D:\Archive\Optimized",
        "preset": "Archive",
        "last_run": "2026-08-07T10:10:00+00:00",
        "last_error": None,
        "last_count": 68,
    },
    {
        "source": r"C:\Studio\Exports",
        "output": r"C:\Studio\Delivery",
        "preset": "Product",
        "last_run": "2026-08-07T09:15:00+00:00",
        "last_error": "Output folder unavailable",
        "last_count": 0,
    },
]

DEMO_PLUGINS = [
    {
        "name": "JPEG XL Codec",
        "path": r"C:\Python\site-packages\imgconverter_jxl.py",
        "trust_ref": "jxl",
        "status": "trusted",
        "hash_prefix": "1b79d0c44ea2",
        "api_version": 1,
        "capability_schema": 1,
        "capabilities": {"decoders": ["jxl"], "encoders": ["jxl"], "storage": [], "network": False},
    },
    {
        "name": "S3 Storage Export",
        "path": "~/.imgconverter/plugins/s3_export.py",
        "trust_ref": "s3_export.py",
        "status": "untrusted",
        "hash_prefix": "8f3a7c9e1d2b",
        "api_version": 1,
        "capability_schema": 1,
        "capabilities": {"decoders": [], "encoders": [], "storage": ["s3"], "network": True},
    },
    {
        "name": "Legacy PSD Reader",
        "path": "~/.imgconverter/plugins/legacy_psd.py",
        "trust_ref": "legacy_psd.py",
        "status": "incompatible",
        "hash_prefix": "552c16289fd1",
        "api_version": 0,
        "capability_schema": 0,
        "capabilities": {},
    },
    {
        "name": "Image Audit",
        "path": r"C:\Python\site-packages\image_audit.py",
        "trust_ref": "image_audit",
        "status": "trusted",
        "hash_prefix": "dea21f39b0aa",
        "api_version": 1,
        "capability_schema": 1,
        "capabilities": {"decoders": [], "encoders": [], "storage": [], "network": False},
    },
]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)

    qa_temp = tempfile.TemporaryDirectory(prefix="imgconverter-ui-qa-")
    qa_root = Path(qa_temp.name)
    imgconverter._app_settings = lambda: QSettings(
        str(qa_root / "settings.ini"), QSettings.Format.IniFormat
    )
    imgconverter._load_batch_history = lambda: list(reversed(DEMO_HISTORY))
    imgconverter._load_watch_profiles = lambda: list(DEMO_WATCH)
    imgconverter.get_plugin_trust_rows = lambda: list(DEMO_PLUGINS)
    imgconverter.MainWindow._shell_integration_installed = lambda self: True
    imgconverter.MainWindow._apply_dark_titlebar = lambda self: None
    imgconverter.MainWindow._init_taskbar_progress = lambda self: None
    imgconverter.MainWindow._maybe_check_for_update = lambda self: None

    app = QApplication.instance() or QApplication(sys.argv)
    app.setStyle("Fusion")
    app.setStyleSheet(imgconverter.STYLESHEET)
    palette = QPalette()
    palette.setColor(QPalette.ColorRole.Window, QColor(imgconverter.CAT["base"]))
    palette.setColor(QPalette.ColorRole.WindowText, QColor(imgconverter.CAT["text"]))
    palette.setColor(QPalette.ColorRole.Base, QColor(imgconverter.CAT["crust"]))
    palette.setColor(QPalette.ColorRole.AlternateBase, QColor(imgconverter.CAT["surface0"]))
    palette.setColor(QPalette.ColorRole.Text, QColor(imgconverter.CAT["text"]))
    palette.setColor(QPalette.ColorRole.Button, QColor(imgconverter.CAT["surface0"]))
    palette.setColor(QPalette.ColorRole.ButtonText, QColor(imgconverter.CAT["text"]))
    palette.setColor(QPalette.ColorRole.Highlight, QColor(imgconverter.CAT["blue"]))
    palette.setColor(QPalette.ColorRole.HighlightedText, QColor(imgconverter.CAT["crust"]))
    app.setPalette(palette)
    window = imgconverter.MainWindow()
    window.setFixedSize(1440, 900)
    window.src_edit.setText(r"C:\Users\user\Pictures")
    window.dst_edit.setText(r"C:\Users\user\Pictures\Converted")
    window.quality_slider.setValue(90)
    window._log_lines.clear()
    window.log_view.clear()
    window.show()

    pages = ["convert", "history", "watch", "plugins", "tools"]
    state = {"index": 0, "failures": [], "metrics": []}

    def capture_next():
        index = state["index"]
        if index >= len(pages):
            window.close()
            app.quit()
            return
        page = pages[index]
        window._select_workspace_page(page)
        if page == "plugins":
            window._apply_plugin_page_rows(DEMO_PLUGINS)
            window.plugins_page_table.selectRow(1)
        app.processEvents()
        current = window.page_stack.currentWidget()
        nav_geometry = window.nav_rail.geometry()
        page_geometry = current.geometry()
        frame_geometry = window.frameGeometry()
        state["metrics"].append({
            "page": page,
            "window": [window.width(), window.height()],
            "frame_geometry": [
                frame_geometry.x(), frame_geometry.y(),
                frame_geometry.width(), frame_geometry.height(),
            ],
            "minimum_size_hint": [
                window.minimumSizeHint().width(), window.minimumSizeHint().height(),
            ],
            "nav_geometry": [
                nav_geometry.x(), nav_geometry.y(),
                nav_geometry.width(), nav_geometry.height(),
            ],
            "page_geometry": [
                page_geometry.x(), page_geometry.y(),
                page_geometry.width(), page_geometry.height(),
            ],
        })
        target = args.output_dir / f"{page}.png"
        snapshot = QPixmap(window.size())
        snapshot.fill(QColor(imgconverter.CAT["base"]))
        painter = QPainter(snapshot)
        window.render(painter)
        painter.end()
        if not snapshot.save(str(target), "PNG"):
            state["failures"].append(str(target))
        state["index"] += 1
        QTimer.singleShot(350, capture_next)

    QTimer.singleShot(1600, capture_next)
    rc = app.exec()
    if state["failures"]:
        qa_temp.cleanup()
        print("Snapshot failures:", *state["failures"], sep=os.linesep, file=sys.stderr)
        return 1
    (args.output_dir / "layout-metrics.json").write_text(
        json.dumps(state["metrics"], indent=2) + os.linesep,
        encoding="utf-8",
    )
    for page in pages:
        target = args.output_dir / f"{page}.png"
        if not target.is_file() or target.stat().st_size < 10_000:
            qa_temp.cleanup()
            print(f"Missing or empty snapshot: {target}", file=sys.stderr)
            return 1
        print(target)
    qa_temp.cleanup()
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
