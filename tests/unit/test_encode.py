"""Unit tests for the streaming encoder."""

from collections.abc import Iterator

import pytest
from helpers import BIG_PAYLOAD, fixed_now, read_archive, stream

from livezip.encode import (
    DataDescriptorSegment,
    FileDataSegment,
    Zip64EndOfCentralDirectoryRecordSegment,
    ZipEncoder,
    ZipFile,
    build_archive,
)
from livezip.models import CompressionMethod
from livezip.storage import CompactFile, DeflateStore, Store
from livezip.stream import BytesStream


class FakeCompactFile(CompactFile):
    """A storage strategy that declares huge sizes without producing bytes."""

    def __init__(self, compressed_size: int, uncompressed_size: int):
        self._compressed_size = compressed_size
        self._uncompressed_size = uncompressed_size

    @property
    def compressed_size(self) -> int:
        return self._compressed_size

    @property
    def uncompressed_size(self) -> int:
        return self._uncompressed_size

    @property
    def compression_method(self) -> CompressionMethod:
        return CompressionMethod.deflate

    @property
    def crc32(self) -> int:
        return 0

    def get_data(self) -> Iterator[bytes]:
        return iter(())


class OverflowingCompactFile(CompactFile):
    """A storage strategy that yields more bytes than it announced."""

    def __init__(self, declared_size: int, payload: bytes):
        self.declared_size = declared_size
        self.payload = payload

    @property
    def compressed_size(self) -> int:
        return self.declared_size

    @property
    def uncompressed_size(self) -> int:
        return self.declared_size

    @property
    def compression_method(self) -> CompressionMethod:
        return CompressionMethod.uncompressed

    @property
    def crc32(self) -> int:
        return 0

    def get_data(self) -> Iterator[bytes]:
        yield self.payload


def make_files(payloads: dict[str, bytes], *, method: str = "deflate") -> list[ZipFile]:
    files = []

    for name, payload in payloads.items():
        if method == "store":
            data: CompactFile = Store(BytesStream(payload), len(payload))
        else:
            data = DeflateStore(BytesStream(payload), len(payload))

        files.append(ZipFile(name, data, fixed_now(), True))

    return files


@pytest.mark.parametrize("method", ["store", "deflate"])
def test_roundtrip_preserves_contents(method):
    payloads = {"hello.txt": b"hello world", "nested/data.bin": BIG_PAYLOAD}
    encoder = build_archive(make_files(payloads, method=method))

    archive = read_archive(stream(encoder))

    assert archive.namelist() == ["hello.txt", "nested/data.bin"]
    assert archive.testzip() is None
    for name, payload in payloads.items():
        assert archive.read(name) == payload


def test_predicted_size_matches_the_stream():
    encoder = build_archive(make_files({"a": BIG_PAYLOAD}))
    data = stream(encoder)
    assert len(data) == encoder.file_size


def test_archive_comment_is_preserved():
    encoder = build_archive(make_files({"a": b"x"}), comment="a nice comment")
    archive = read_archive(stream(encoder))
    assert archive.comment == b"a nice comment"


def test_per_file_comment_is_preserved():
    files = [
        ZipFile(
            "a.txt",
            DeflateStore(BytesStream(b"x"), 1),
            fixed_now(),
            True,
            comment="per file",
        )
    ]
    archive = read_archive(stream(build_archive(files)))
    assert archive.getinfo("a.txt").comment == b"per file"


def test_unicode_entry_names():
    encoder = build_archive(make_files({"café/naïve.txt": b"bonjour"}))
    archive = read_archive(stream(encoder))
    assert archive.read("café/naïve.txt") == b"bonjour"


def test_empty_files_list_is_rejected():
    with pytest.raises(ValueError, match="empty"):
        ZipEncoder([])


def test_empty_payload_roundtrip():
    encoder = build_archive(make_files({"empty.txt": b""}))
    archive = read_archive(stream(encoder))
    assert archive.read("empty.txt") == b""
    assert archive.testzip() is None


def test_unknown_reference_raises_key_error():
    encoder = build_archive(make_files({"a": b"x"}))
    with pytest.raises(KeyError):
        encoder.get_segment(("nope", 0))


def test_zip64_records_are_emitted_for_huge_declared_sizes():
    huge = 5 * 1024**3
    files = [ZipFile("huge.bin", FakeCompactFile(huge, huge), fixed_now(), True)]

    encoder = ZipEncoder(files)
    encoder.make_segments()
    encoder.compute_offsets()

    record = encoder.get_segment("eocd64_record")
    assert isinstance(record, Zip64EndOfCentralDirectoryRecordSegment)
    assert record.is_required is True

    entry = encoder.get_segment(("cd_file", 0))
    packed = entry.get_data().__next__()
    assert b"\xff\xff\xff\xff" in packed


def test_zip64_is_not_emitted_for_small_archives():
    encoder = build_archive(make_files({"a": b"small"}))
    record = encoder.get_segment("eocd64_record")
    assert isinstance(record, Zip64EndOfCentralDirectoryRecordSegment)
    assert record.is_required is False

    entry = encoder.get_segment(("cd_file", 0))
    packed = entry.get_data().__next__()
    assert b"\xff\xff\xff\xff" not in packed


def test_data_descriptor_segment_length_tracks_sizes():
    regular = ZipFile("a", Store(BytesStream(b"x"), 1), fixed_now(), True)
    huge = ZipFile("b", FakeCompactFile(5 * 1024**3, 5 * 1024**3), fixed_now(), True)

    encoder = ZipEncoder([regular, huge])
    encoder.make_segments()
    encoder.compute_offsets()

    assert encoder.get_segment(("file_data", 0)).get_length() == 1
    assert encoder.get_segment(("file_data", 1)).get_length() == 5 * 1024**3

    assert isinstance(
        encoder.get_segment(("file_descriptor", 0)), DataDescriptorSegment
    )
    assert encoder.get_segment(("file_descriptor", 0)).get_length() == 16
    assert encoder.get_segment(("file_descriptor", 1)).get_length() == 24


def test_file_data_segment_rejects_oversized_streams():
    zip_file = ZipFile(
        "a",
        OverflowingCompactFile(declared_size=1, payload=b"too long"),
        fixed_now(),
        True,
    )
    segment = FileDataSegment(ZipEncoder([zip_file]), 0, zip_file)

    with pytest.raises(ValueError, match="too much data"):
        b"".join(segment.get_data())
