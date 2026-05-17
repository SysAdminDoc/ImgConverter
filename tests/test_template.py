"""Output filename template substitution tests."""
import re

import pytest

from heicshift import _apply_output_template, convert_file
from pathlib import Path


def test_basic_token_substitution(tmp_workdir):
    src = tmp_workdir / "vacation.jpg"
    src.write_bytes(b"x")
    out = _apply_output_template(
        "{stem}_{width}x{height}.{ext}", src, tmp_workdir,
        width=1920, height=1080, fmt="jpeg", ext=".jpg", seq=7,
    )
    assert out == "vacation_1920x1080.jpg"


def test_seq_zero_padded(tmp_workdir):
    src = tmp_workdir / "x.jpg"
    src.write_bytes(b"")
    out = _apply_output_template(
        "img_{seq:####}.{ext}", src, tmp_workdir,
        width=1, height=1, fmt="jpeg", ext=".jpg", seq=42,
    )
    assert out == "img_0042.jpg"


def test_date_format_token(tmp_workdir):
    src = tmp_workdir / "x.jpg"
    src.write_bytes(b"")
    out = _apply_output_template(
        "{date:%Y}/photo.{ext}", src, tmp_workdir,
        width=1, height=1, fmt="jpeg", ext=".jpg", seq=1,
    )
    # Match YYYY/photo.jpg — exact year depends on the test machine
    assert re.match(r"\d{4}/photo\.jpg$", out), out


def test_rel_dir_token_preserves_subdirs(tmp_workdir):
    base = tmp_workdir / "scan_root"
    sub = base / "2026" / "march"
    sub.mkdir(parents=True)
    src = sub / "shot.bmp"
    src.write_bytes(b"")
    out = _apply_output_template(
        "{rel_dir}/{stem}.{ext}", src, base,
        width=1, height=1, fmt="png", ext=".png", seq=1,
    )
    assert out == "2026/march/shot.png"


def test_unknown_token_left_intact(tmp_workdir):
    src = tmp_workdir / "x.jpg"
    src.write_bytes(b"")
    out = _apply_output_template(
        "{stem}_{nonsense}.{ext}", src, tmp_workdir,
        width=1, height=1, fmt="jpeg", ext=".jpg", seq=1,
    )
    assert "{nonsense}" in out


def test_convert_file_with_template(rgb_image, tmp_workdir):
    src = tmp_workdir / "src.bmp"
    rgb_image.save(src)
    out_dir = tmp_workdir / "out"
    result = convert_file(
        src, out_dir, fmt="png",
        name_template="{stem}_{width}x{height}",
        seq=3,
    )
    assert result.success
    assert result.dst.name == "src_200x150.png"
