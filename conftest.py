"""Repo-root conftest — runs before pytest plugin autoload.

pytest-qt eagerly imports a Qt binding when it loads as a plugin; on
systems with both PyQt6 and PySide6 installed it picks PySide6 by default,
which then collides with PyQt6 DLLs that imgconverter.py imports later. Pin
the binding to PyQt6 before pytest-qt loads.
"""
import os

os.environ.setdefault("PYTEST_QT_API", "pyqt6")
