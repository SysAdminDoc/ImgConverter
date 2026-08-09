"""Extract and compile ImgConverter Qt translations."""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "imgconverter.py"
TRANSLATIONS = ROOT / "translations"
TS_FILE = TRANSLATIONS / "imgconverter_es.ts"
QM_FILE = TRANSLATIONS / "imgconverter_es.qm"


def _tool(names: tuple[str, ...]) -> str:
    for name in names:
        found = shutil.which(name)
        if found:
            return found
        suffix = ".exe" if sys.platform == "win32" else ""
        sibling = Path(sys.executable).resolve().parent / f"{name}{suffix}"
        if sibling.is_file():
            return str(sibling)
    joined = ", ".join(names)
    raise RuntimeError(
        f"Qt Linguist tool not found ({joined}). Install PyQt6/PySide6 tools "
        "or add Qt's bin directory to PATH."
    )


def _run(command: list[str]) -> None:
    print("[localize]", " ".join(command))
    subprocess.run(command, cwd=ROOT, check=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--extract", action="store_true", help="Update the TS source from imgconverter.py")
    parser.add_argument("--compile", action="store_true", help="Compile the TS source to a QM catalog")
    args = parser.parse_args()
    extract = args.extract or not (args.extract or args.compile)
    compile_catalog = args.compile or not (args.extract or args.compile)

    TRANSLATIONS.mkdir(parents=True, exist_ok=True)
    if extract:
        _run([_tool(("pylupdate6",)), str(SOURCE), "--ts", str(TS_FILE)])
    if compile_catalog:
        _run([
            _tool(("lrelease", "pyside6-lrelease")),
            "-nounfinished",
            "-fail-on-invalid",
            str(TS_FILE),
            "-qm",
            str(QM_FILE),
        ])
    print(f"[localize] wrote {TS_FILE.relative_to(ROOT)} and {QM_FILE.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
