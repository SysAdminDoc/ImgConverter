"""Tests for sidecar companion outputs (Live Photo, depth map, HDR gain map)."""
import json

import pytest

from imgconverter import _build_parser, _run_cli, convert_file, EXIT_OK, METADATA_KINDS


def test_live_photo_mov_paired_through_conversion(rgb_image, tmp_workdir):
    """A sibling .MOV next to source should be copied next to the converted still."""
    src = tmp_workdir / "IMG_0001.bmp"
    rgb_image.save(src)
    mov = tmp_workdir / "IMG_0001.mov"
    mov.write_bytes(b"\x00\x00\x00\x14ftypqt  ")  # minimal MOV header bytes

    out_dir = tmp_workdir / "out"
    result = convert_file(src, out_dir, fmt="png")
    assert result.success

    # The mov should land next to the converted png with the same stem.
    sidecar = result.dst.with_suffix(".mov")
    assert sidecar.exists(), (
        f"Live Photo sidecar not copied: {sidecar} missing. "
        f"Out dir contents: {list(out_dir.iterdir())}"
    )
    assert sidecar.read_bytes().startswith(b"\x00\x00\x00\x14ftyp")
    assert any("live-photo" in w for w in result.warnings), (
        f"expected 'live-photo' warning; got {result.warnings}"
    )


def test_no_mov_no_sidecar(rgb_image, tmp_workdir):
    """No sibling MOV -> no sidecar, no warning."""
    src = tmp_workdir / "plain.bmp"
    rgb_image.save(src)
    out_dir = tmp_workdir / "out"
    result = convert_file(src, out_dir, fmt="png")
    assert result.success
    assert not any("live-photo" in w for w in result.warnings)


def test_metadata_report_records_provenance_drop(rgb_image, tmp_workdir):
    src = tmp_workdir / "signed.bmp"
    rgb_image.save(src)
    with src.open("ab") as fp:
        fp.write(b"\nimgconverter-test-c2pa-marker\n")

    out_dir = tmp_workdir / "out"
    report = tmp_workdir / "report.json"
    args = _build_parser().parse_args([
        "--input", str(src),
        "--output", str(out_dir),
        "--format", "png",
        "--report", str(report),
    ])

    with pytest.raises(SystemExit) as exc:
        _run_cli(args)

    assert exc.value.code == EXIT_OK
    data = json.loads(report.read_text(encoding="utf-8"))
    metadata = data["files"][0]["metadata"]
    assert set(METADATA_KINDS).issubset(metadata["before"])
    assert metadata["before"]["c2pa"] is True
    assert metadata["after"]["c2pa"] is False
    assert "c2pa" in metadata["dropped"]
    assert any("metadata dropped: c2pa" in w for w in data["files"][0]["warnings"])


def test_adjacent_xmp_sidecar_ingested(rgb_image, tmp_workdir):
    """An adjacent .xmp sidecar should be ingested into meta during conversion."""
    src = tmp_workdir / "photo.bmp"
    rgb_image.save(src)
    xmp_path = tmp_workdir / "photo.xmp"
    xmp_path.write_text(
        '<?xpacket begin="" id="W5M0MpCehiHzreSzNTczkc9d"?>'
        '<x:xmpmeta xmlns:x="adobe:ns:meta/">'
        '<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
        '</rdf:RDF></x:xmpmeta><?xpacket end="w"?>',
        encoding="utf-8",
    )
    out_dir = tmp_workdir / "out"
    result = convert_file(src, out_dir, fmt="png")
    assert result.success
    assert any("sidecar-import" in w and "xmp" in w.lower() for w in result.warnings)


def test_adjacent_google_photos_json_ingested(rgb_image, tmp_workdir):
    """A Google Photos JSON sidecar should be detected and ingested."""
    src = tmp_workdir / "IMG_001.bmp"
    rgb_image.save(src)
    gp_path = tmp_workdir / "IMG_001.bmp.json"
    gp_path.write_text(json.dumps({
        "title": "My Photo",
        "photoTakenTime": {"timestamp": "1718712000"},
    }), encoding="utf-8")
    out_dir = tmp_workdir / "out"
    result = convert_file(src, out_dir, fmt="png")
    assert result.success
    assert any("Google Photos" in w for w in result.warnings)
