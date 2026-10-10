from __future__ import annotations

import hashlib
import zlib
from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image

from app.persistence.atomic_file_publisher import ADR_0087_RETRY_POLICY, AtomicFilePublisher
from app.persistence.windows_paths import DataRootIdentity
from app.services.rar5_store_writer import (
    RAR5_SIGNATURE,
    Rar5StoreFile,
    Rar5StoreWriter,
    Rar5WriteError,
    inspect_rar5_store_archive,
)


def _writer(root: Path, *, max_archive_bytes: int = 2 * 1024 * 1024 * 1024) -> Rar5StoreWriter:
    identity = DataRootIdentity.capture(root)
    return Rar5StoreWriter(
        publisher=AtomicFilePublisher(
            data_root_identity=identity,
            retry_policy=ADR_0087_RETRY_POLICY,
            sleep=lambda _delay: None,
            clock=lambda: 0.0,
        ),
        data_root_identity=identity,
        max_archive_bytes=max_archive_bytes,
    )


def _png_payload(seed: bytes) -> bytes:
    color = tuple(hashlib.sha256(seed).digest()[:3])
    with BytesIO() as output:
        Image.new("RGB", (3, 3), color).save(output, format="PNG")
        return output.getvalue()


def _store_file(root: Path, name: str, seed: bytes) -> Rar5StoreFile:
    payload = _png_payload(seed)
    path = root / f"source-{hashlib.sha256(name.encode('utf-8')).hexdigest()}.png"
    path.write_bytes(payload)
    return Rar5StoreFile(
        member_name=name,
        source_path=path,
        size_bytes=len(payload),
        sha256=hashlib.sha256(payload).hexdigest(),
    )


def test_writes_verified_rar5_store_archive_without_changing_sources(tmp_path: Path) -> None:
    source_root = tmp_path / "sources"
    source_root.mkdir()
    output_root = tmp_path / "temp"
    output_root.mkdir()
    first = _store_file(source_root, "Capitulo 2 - 001.png", b"first page")
    second = _store_file(source_root, "Capitulo 10 - 001.png", b"second page")
    original_sources = {
        entry.member_name: entry.source_path.read_bytes() for entry in (first, second)
    }
    output = output_root / "series.cbr"

    result = _writer(tmp_path).write(
        (first, second),
        temporary_path=output_root / "series.cbr.tmp",
        destination_path=output,
    )

    assert output.read_bytes().startswith(RAR5_SIGNATURE)
    assert result.path == output
    assert result.page_count == 2
    assert result.size_bytes == output.stat().st_size
    assert result.sha256 == hashlib.sha256(output.read_bytes()).hexdigest()
    members = inspect_rar5_store_archive(output)
    assert [member.name for member in members] == [first.member_name, second.member_name]
    assert [member.sha256 for member in members] == [first.sha256, second.sha256]
    assert {
        entry.member_name: entry.source_path.read_bytes() for entry in (first, second)
    } == original_sources


def test_archive_is_deterministic_for_identical_inputs(tmp_path: Path) -> None:
    source_root = tmp_path / "sources"
    source_root.mkdir()
    output_root = tmp_path / "temp"
    output_root.mkdir()
    page = _store_file(source_root, "Capitulo 1 - 001.png", b"same page")
    writer = _writer(tmp_path)

    first = writer.write(
        (page,),
        temporary_path=output_root / "first.cbr.tmp",
        destination_path=output_root / "first.cbr",
    )
    second = writer.write(
        (page,),
        temporary_path=output_root / "second.cbr.tmp",
        destination_path=output_root / "second.cbr",
    )

    assert first.sha256 == second.sha256
    assert first.path.read_bytes() == second.path.read_bytes()


def test_rejects_source_changed_after_metadata_was_captured(tmp_path: Path) -> None:
    source_root = tmp_path / "sources"
    source_root.mkdir()
    output_root = tmp_path / "temp"
    output_root.mkdir()
    page = _store_file(source_root, "Capitulo 1 - 001.png", b"original")
    page.source_path.write_bytes(_png_payload(b"tampered"))
    assert page.source_path.stat().st_size == page.size_bytes
    output = output_root / "series.cbr"

    with pytest.raises(Rar5WriteError) as raised:
        _writer(tmp_path).write(
            (page,),
            temporary_path=output_root / "series.cbr.tmp",
            destination_path=output,
        )

    assert raised.value.stage == "source_verify"
    assert not output.exists()
    assert page.source_path.read_bytes() == _png_payload(b"tampered")


def test_preserves_existing_destination_and_staging_files(tmp_path: Path) -> None:
    source_root = tmp_path / "sources"
    source_root.mkdir()
    output_root = tmp_path / "temp"
    output_root.mkdir()
    page = _store_file(source_root, "Capitulo 1 - 001.png", b"page")
    output = output_root / "series.cbr"
    output.write_bytes(b"owned destination")
    temporary = output_root / "series.cbr.tmp"
    temporary.write_bytes(b"owned temporary")

    with pytest.raises(Rar5WriteError) as raised:
        _writer(tmp_path).write(
            (page,),
            temporary_path=temporary,
            destination_path=output,
        )

    assert raised.value.stage == "temporary_conflict"
    assert output.read_bytes() == b"owned destination"
    assert temporary.read_bytes() == b"owned temporary"


def test_rejects_archive_over_limit_before_creating_staging_file(tmp_path: Path) -> None:
    source_root = tmp_path / "sources"
    source_root.mkdir()
    output_root = tmp_path / "temp"
    output_root.mkdir()
    page = _store_file(source_root, "Capitulo 1 - 001.png", b"page payload")
    output = output_root / "series.cbr"
    temporary = output_root / "series.cbr.tmp"

    with pytest.raises(Rar5WriteError) as raised:
        _writer(tmp_path, max_archive_bytes=80).write(
            (page,),
            temporary_path=temporary,
            destination_path=output,
        )

    assert raised.value.stage == "archive_size_limit_preflight"
    assert not temporary.exists()
    assert not output.exists()


@pytest.mark.parametrize(
    "member_name",
    ("../page.png", "folder/page.png", "page.jpg", "page.png/"),
)
def test_rejects_member_names_outside_png_basename_profile(
    tmp_path: Path,
    member_name: str,
) -> None:
    source_root = tmp_path / "sources"
    source_root.mkdir()
    output_root = tmp_path / "temp"
    output_root.mkdir()
    page = _store_file(source_root, member_name, b"page")

    with pytest.raises(Rar5WriteError) as raised:
        _writer(tmp_path).write(
            (page,),
            temporary_path=output_root / "series.cbr.tmp",
            destination_path=output_root / "series.cbr",
        )

    assert raised.value.stage == "member_name_invalid"
    assert not (output_root / "series.cbr").exists()


def test_internal_archive_inspector_detects_corrupt_data_crc(tmp_path: Path) -> None:
    source_root = tmp_path / "sources"
    source_root.mkdir()
    output_root = tmp_path / "temp"
    output_root.mkdir()
    page = _store_file(source_root, "Capitulo 1 - 001.png", b"CRC-protected page data")
    output = output_root / "series.cbr"
    _writer(tmp_path).write(
        (page,),
        temporary_path=output_root / "series.cbr.tmp",
        destination_path=output,
    )
    archive = bytearray(output.read_bytes())
    member = inspect_rar5_store_archive(output)[0]
    offset = member.data_offset
    assert offset >= len(RAR5_SIGNATURE)
    archive[offset] ^= 0x01
    output.write_bytes(archive)

    with pytest.raises(Rar5WriteError) as raised:
        inspect_rar5_store_archive(output)

    assert raised.value.stage == "member_data_crc"
    assert zlib.crc32(archive[offset : offset + member.size_bytes]) != member.crc32
