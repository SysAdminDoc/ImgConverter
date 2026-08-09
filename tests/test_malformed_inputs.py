"""Deterministic malformed-input and metadata conformance coverage."""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageCms, ImageDraw, ImageSequence

from imgconverter import (
    ConvertOptions,
    ERROR_CODE_RESOURCE_LIMIT,
    EXIT_OK,
    EXIT_PARTIAL_FAILURE,
    EXIT_TOTAL_FAILURE,
    _convert_animated_or_sequence,
    convert_file,
    count_frames,
)


REPO_ROOT = Path(__file__).resolve().parents[1]
BOUNDED_CASE_TIMEOUT = 30


@dataclass(frozen=True)
class CorpusCase:
    name: str
    suffix: str
    payload: bytes
    expected: str = "failure"


def _encoded_image(fmt: str, *, size: tuple[int, int] = (32, 24)) -> bytes:
    image = Image.new("RGB", size, (22, 48, 78))
    draw = ImageDraw.Draw(image)
    draw.rectangle((3, 4, min(size[0] - 2, 18), min(size[1] - 2, 17)), fill=(220, 80, 42))
    buffer = io.BytesIO()
    image.save(buffer, fmt)
    return buffer.getvalue()


def _malformed_corpus() -> list[CorpusCase]:
    jpeg = _encoded_image("JPEG")
    png = _encoded_image("PNG")
    return [
        CorpusCase("jpeg-signature-only", ".jpg", b"\xff\xd8\xff"),
        CorpusCase("jpeg-truncated-header", ".jpg", jpeg[:19]),
        CorpusCase("png-signature-only", ".png", b"\x89PNG\r\n\x1a\n"),
        CorpusCase("png-truncated-idat", ".png", png[:41]),
        CorpusCase("webp-truncated-riff", ".webp", b"RIFF\x24\x00\x00\x00WEBPVP8 "),
        CorpusCase(
            "heif-truncated-ftyp", ".heic",
            b"\x00\x00\x00\x18ftypheic\x00\x00\x00\x00heicmif1",
        ),
        CorpusCase(
            "avif-truncated-ftyp", ".avif",
            b"\x00\x00\x00\x18ftypavif\x00\x00\x00\x00avifmif1",
        ),
        CorpusCase("jxl-signature-only", ".jxl", b"\xff\x0a\x00\x00\x00\x00"),
        CorpusCase("raw-truncated-container", ".cr2", b"II*\x00\x08\x00\x00\x00\x00\x00"),
        # A depth/auxiliary-like HEIF box exercises the same optional decoder
        # boundary without requiring a camera-specific binary fixture.
        CorpusCase(
            "heif-auxiliary-truncated", ".heif",
            b"\x00\x00\x00\x28ftypmif1\x00\x00\x00\x00mif1heic\x00\x00\x00\x10auxl",
        ),
    ]


def _write_corpus(directory: Path, cases: list[CorpusCase]) -> list[Path]:
    directory.mkdir(parents=True, exist_ok=True)
    paths = []
    for case in cases:
        path = directory / f"{case.name}{case.suffix}"
        path.write_bytes(case.payload)
        paths.append(path)
    return paths


def _bounded_cli(
    input_dir: Path,
    output_dir: Path,
    report_path: Path,
    *extra: str,
    timeout: int = BOUNDED_CASE_TIMEOUT,
) -> subprocess.CompletedProcess[str]:
    profile = input_dir.parent / "profile"
    profile.mkdir(parents=True, exist_ok=True)
    environment = os.environ.copy()
    environment["HOME"] = str(profile)
    environment["USERPROFILE"] = str(profile)
    environment["QT_QPA_PLATFORM"] = "offscreen"
    environment["PYTHONUTF8"] = "1"
    command = [
        sys.executable,
        str(REPO_ROOT / "imgconverter.py"),
        "--input", str(input_dir),
        "--output", str(output_dir),
        "--format", "png",
        "--no-recursive",
        "--workers", "1",
        "--report", str(report_path),
        *extra,
    ]
    return subprocess.run(
        command,
        cwd=str(REPO_ROOT),
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def test_malformed_corpus_fails_closed_without_partial_outputs(tmp_workdir):
    cases = _malformed_corpus()
    source_dir = tmp_workdir / "corpus"
    output_dir = tmp_workdir / "output"
    paths = _write_corpus(source_dir, cases)

    for case, source in zip(cases, paths):
        result = convert_file(
            source,
            output_dir / case.name,
            opts=ConvertOptions(
                fmt="png",
                preserve_metadata=False,
                max_pixels=4_000_000,
                max_decoded_bytes=32 * 1024 * 1024,
                max_decode_seconds=2.0,
            ),
        )
        assert not result.success, case.name
        assert result.error, case.name
        case_output_dir = output_dir / case.name
        if case_output_dir.exists():
            assert not list(case_output_dir.glob("*.png")), case.name


def test_profile_and_metadata_fixture_preserves_presence_report(tmp_workdir):
    source = tmp_workdir / "profiled.jpg"
    output_dir = tmp_workdir / "output"
    image = Image.new("RGB", (40, 30), (80, 120, 160))
    exif = image.getexif()
    exif[0x010F] = "ImgConverter test camera"
    icc_profile = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
    image.save(
        source,
        "JPEG",
        exif=exif.tobytes(),
        icc_profile=icc_profile,
        quality=92,
    )

    result = convert_file(
        source,
        output_dir,
        opts=ConvertOptions(fmt="png", preserve_metadata=True),
    )

    assert result.success
    assert result.dst is not None and result.dst.is_file()
    assert result.metadata_report["before"]["exif"]
    assert result.metadata_report["before"]["icc"]
    assert result.metadata_report["preserve_requested"]


def test_animated_frame_limit_and_output_invariant(tmp_workdir):
    source = tmp_workdir / "animated.gif"
    frames = [Image.new("RGB", (12, 10), color) for color in ((255, 0, 0), (0, 255, 0), (0, 0, 255))]
    frames[0].save(
        source,
        "GIF",
        save_all=True,
        append_images=frames[1:],
        duration=[40, 60, 80],
        loop=0,
    )

    limited = _convert_animated_or_sequence(
        source,
        tmp_workdir / "limited",
        "webp",
        extract_frames=False,
        opts=ConvertOptions(fmt="webp", max_frames=2),
    )
    assert not limited.success
    assert limited.error_code == ERROR_CODE_RESOURCE_LIMIT

    converted = _convert_animated_or_sequence(
        source,
        tmp_workdir / "converted",
        "png",
        extract_frames=False,
        opts=ConvertOptions(fmt="png", max_frames=4),
    )
    assert converted.success
    assert converted.dst is not None and converted.dst.is_file()
    assert count_frames(converted.dst) == 3
    with Image.open(converted.dst) as image:
        assert [frame.info.get("duration") for frame in ImageSequence.Iterator(image)] == [40, 60, 80]


def test_bounded_mutation_fuzz_and_decode_memory_boundary(tmp_workdir):
    source_dir = tmp_workdir / "fuzz"
    source_dir.mkdir()
    seed = bytearray(_encoded_image("JPEG", size=(64, 48)))
    for index in range(12):
        mutated = bytearray(seed)
        offset = 2 + ((index * 37) % max(1, len(mutated) - 2))
        mutated[offset] ^= (0x11 + index * 7) & 0xFF
        if index % 3 == 0:
            mutated = mutated[: max(3, len(mutated) - (index + 1) * 11)]
        (source_dir / f"mutation-{index:02d}.jpg").write_bytes(mutated)

    fuzz_report = tmp_workdir / "fuzz-report.json"
    fuzz_result = _bounded_cli(
        source_dir,
        tmp_workdir / "fuzz-output",
        fuzz_report,
        "--max-pixels", "4000000",
        "--max-decoded-bytes", "32MB",
        "--max-decode-seconds", "2",
    )
    assert fuzz_result.returncode in {EXIT_OK, EXIT_PARTIAL_FAILURE, EXIT_TOTAL_FAILURE}
    assert "Traceback" not in fuzz_result.stderr
    report = json.loads(fuzz_report.read_text(encoding="utf-8"))
    assert report["summary"]["cancelled"] is False
    assert len(report["files"]) == 12
    assert all(
        (not record["ok"]) or (record["dst"] and record["dst"].endswith(".png"))
        for record in report["files"]
    )
    assert str(tmp_workdir) not in json.dumps(report)

    large = Image.new("RGB", (2048, 2048), (0, 0, 0))
    large_path = source_dir / "decode-budget.png"
    large.save(large_path, "PNG")
    memory_report = tmp_workdir / "memory-report.json"
    memory_result = _bounded_cli(
        source_dir,
        tmp_workdir / "memory-output",
        memory_report,
        "--max-decoded-bytes", "1MB",
        "--max-pixels", "4000000",
        "--max-decode-seconds", "2",
    )
    assert memory_result.returncode in {EXIT_PARTIAL_FAILURE, EXIT_TOTAL_FAILURE}
    memory_payload = json.loads(memory_report.read_text(encoding="utf-8"))
    budget_records = [
        record for record in memory_payload["files"]
        if record["src"].endswith("decode-budget.png")
    ]
    assert budget_records and budget_records[0]["error_code"] == ERROR_CODE_RESOURCE_LIMIT
