"""Shared pytest fixtures for HEICShift regression tests."""
import sys
import tempfile
from pathlib import Path

import pytest

# Make `heicshift` importable when running `pytest` from repo root.
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))


@pytest.fixture
def tmp_workdir():
    """Provide an isolated working directory per test."""
    with tempfile.TemporaryDirectory(prefix="heicshift-test-") as td:
        yield Path(td)


@pytest.fixture
def rgb_image():
    """Synthetic RGB PIL image with a few non-trivial pixel patches."""
    from PIL import Image, ImageDraw
    img = Image.new("RGB", (200, 150), (40, 40, 60))
    d = ImageDraw.Draw(img)
    d.rectangle([10, 10, 60, 60], fill=(220, 50, 100))
    d.rectangle([70, 30, 130, 90], fill=(50, 220, 100))
    d.rectangle([140, 60, 190, 130], fill=(50, 100, 220))
    return img


@pytest.fixture
def rgba_image():
    """Synthetic RGBA PIL image with real transparency."""
    from PIL import Image, ImageDraw
    img = Image.new("RGBA", (100, 100), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse([10, 10, 90, 90], fill=(200, 100, 50, 200))
    return img
