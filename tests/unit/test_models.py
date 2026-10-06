"""Unit tests for the binary ZIP models."""

from datetime import UTC, datetime, timedelta, timezone
from struct import calcsize

import pytest

from livezip.models import (
    DOS_START,
    DOS_STOP,
    CentralDirectoryFile,
    CompressionMethod,
    DataDescriptor,
    EndOfCentralDirectoryRecord,
    LocalFileHeader,
    Zip64ExtraField,
    encode_version,
    make_dos_date_time,
    max_o,
    needs_zip64_sizes,
)

UINT32 = 0xFFFFFFFF


def test_make_dos_date_time_known_value():
    value = make_dos_date_time(datetime(2026, 10, 6, 12, 34, 56, tzinfo=UTC))
    assert value == (25692, 23878)


def test_make_dos_date_time_clamps_to_the_dos_range():
    assert make_dos_date_time(datetime(1970, 1, 1, tzinfo=UTC)) == make_dos_date_time(
        DOS_START
    )
    assert make_dos_date_time(datetime(2200, 1, 1, tzinfo=UTC)) == make_dos_date_time(
        DOS_STOP
    )


def test_make_dos_date_time_converts_to_utc():
    paris = datetime(2026, 10, 6, 14, 0, 0, tzinfo=timezone(timedelta(hours=2)))
    assert make_dos_date_time(paris) == make_dos_date_time(
        datetime(2026, 10, 6, 12, 0, 0, tzinfo=UTC)
    )


def test_encode_version():
    assert encode_version(4, 5) == 45
    assert encode_version(0, 0) == 0


@pytest.mark.parametrize(("major", "minor"), [(-1, 0), (0, -1), (0, 10), (7000, 0)])
def test_encode_version_rejects_invalid_values(major, minor):
    with pytest.raises(ValueError, match=r"[Mm]inor|[Vv]ersion"):
        encode_version(major, minor)


def test_max_o_clamps_and_can_prevent_overflow():
    assert max_o(10, 8) == 10
    assert max_o(0x1FF, 8) == 0xFF
    with pytest.raises(ValueError, match="does not fit"):
        max_o(0x1FF, 8, prevent=True)


def test_needs_zip64_sizes():
    assert needs_zip64_sizes(1, 1) is False
    assert needs_zip64_sizes(UINT32, UINT32) is False
    assert needs_zip64_sizes(UINT32 + 1, 1) is True
    assert needs_zip64_sizes(1, UINT32 + 1) is True


def test_zip64_extra_field_without_overflow_is_empty():
    field = Zip64ExtraField(
        original_size=1, compressed_size=1, header_offset=0, disk_start=0
    )
    assert field.pack() == b"\x01\x00\x00\x00"


def test_zip64_extra_field_only_contains_overflowing_fields():
    field = Zip64ExtraField(
        original_size=UINT32 + 1,
        compressed_size=UINT32 + 1,
        header_offset=0,
        disk_start=0,
    )
    packed = field.pack()
    assert packed[:4] == b"\x01\x00\x10\x00"  # id=1, size=16
    assert len(packed) == 4 + 16


def test_zip64_extra_field_all_fields():
    field = Zip64ExtraField(
        original_size=UINT32 + 1,
        compressed_size=UINT32 + 1,
        header_offset=UINT32 + 1,
        disk_start=0x10000,
    )
    assert len(field.pack()) == 4 + 8 + 8 + 8 + 4


def test_data_descriptor_picks_width_from_sizes():
    small = DataDescriptor(crc32=1, compressed_size=10, uncompressed_size=20)
    huge = DataDescriptor(crc32=1, compressed_size=UINT32 + 1, uncompressed_size=20)

    assert small.packed_size() == 16
    assert len(small.pack()) == 16
    assert small.pack()[:4] == b"PK\x07\x08"

    assert huge.packed_size() == 24
    assert len(huge.pack()) == 24


def test_local_file_header_signature_and_length():
    header = LocalFileHeader(
        version_needed=(4, 5),
        general_purpose=0,
        compression_method=CompressionMethod.deflate,
        last_modification=datetime(2026, 10, 6, tzinfo=UTC),
        crc32=0,
        compressed_size=0,
        uncompressed_size=0,
        file_name="hello.txt",
        extra_fields=[],
    )
    packed = header.pack()
    assert packed[:4] == b"PK\x03\x04"
    assert packed.endswith(b"hello.txt")
    assert len(packed) == calcsize("<IHHHHHIIIHH") + len("hello.txt")


def test_central_directory_signature():
    entry = CentralDirectoryFile(
        version_made_by=(4, 5),
        version_needed_to_extract=(4, 5),
        general_purpose=0,
        compression_method=CompressionMethod.deflate,
        last_modification=datetime(2026, 10, 6, tzinfo=UTC),
        crc32=0,
        compressed_size=1,
        uncompressed_size=1,
        file_name="a",
        extra_fields=[],
        comment="c",
        disk_number_start=0,
        internal_file_attributes=0,
        external_file_attributes=0,
        relative_offset_of_local_header=0,
    )
    assert entry.pack()[:4] == b"PK\x01\x02"


def test_end_of_central_directory_signature_and_comment():
    record = EndOfCentralDirectoryRecord(
        number_of_this_disk=0,
        number_of_the_disk_with_start=0,
        number_of_entries_on_this_disk=0,
        number_of_entries=0,
        size_of_central_directory=0,
        central_directory_offset=0,
        comment="hi",
    )
    packed = record.pack()
    assert packed[:4] == b"PK\x05\x06"
    assert packed.endswith(b"hi")
