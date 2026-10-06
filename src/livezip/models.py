"""Binary models for the ZIP and ZIP64 file formats.

This module is intentionally dumb: every class knows how to serialise itself to
bytes according to the PKZIP appnote (``zip_spec.txt``) and nothing else. The
high-level streaming logic lives in :mod:`livezip.encode`, which stitches these
records together without ever holding the archive in memory.

All multi-byte integers are little-endian, as mandated by the specification.
"""

from datetime import UTC, datetime
from enum import Enum
from struct import calcsize, pack
from typing import NamedTuple

#: DOS timestamps cannot represent dates outside this range; anything earlier
#: is clamped up, anything later is clamped down.
DOS_START = datetime(1980, 1, 1, tzinfo=UTC)
DOS_STOP = datetime(2099, 12, 31, 23, 59, 59, 999999, tzinfo=UTC)

#: General purpose bit 11: the file name is UTF-8 encoded.
GP_LANGUAGES_ENCODING = 1 << 11

#: General purpose bit 3: sizes and CRC live in a trailing data descriptor
#: instead of the local header. Mandatory for streaming archives because the
#: CRC is unknown until the bytes have actually been read.
GP_STREAM = 1 << 3

#: The number of bytes within a ZIP64 data descriptor is 8 for sizes.
_UINT32_MAX = 0xFFFFFFFF


def needs_zip64_sizes(compressed_size: int, uncompressed_size: int) -> bool:
    """
    Tell whether sizes must be serialised on 64 bits.

    Parameters
    ----------
    compressed_size
        Size of the stored (possibly compressed) payload.
    uncompressed_size
        Size of the original payload.

    Returns
    -------
    ``True`` when either size overflows a 32-bit field and therefore requires a
    ZIP64 data descriptor.
    """

    return compressed_size > _UINT32_MAX or uncompressed_size > _UINT32_MAX


def make_dos_date_time(date: datetime) -> tuple[int, int]:
    """
    Encode a date/time object into the DOS binary format.

    Parameters
    ----------
    date
        Date to encode. It must be between :data:`DOS_START` and
        :data:`DOS_STOP` (1980 to 2099), otherwise it will be replaced by the
        closest date in range.

    Returns
    -------
    tuple of int
        A DOS-encoded ``(time, date)`` tuple. As the DOS format doesn't let you
        set a time zone, the output is in the UTC time zone.
    """

    date = date.astimezone(UTC)
    date = max(DOS_START, date)
    date = min(DOS_STOP, date)

    dos_date = (date.year - 1980) << 9 | date.month << 5 | date.day
    dos_time = date.hour << 11 | date.minute << 5 | (date.second // 2)

    return dos_time, dos_date


def encode_version(major: int, minor: int) -> int:
    """
    Encode a version number the way the binary ZIP format understands it.

    Parameters
    ----------
    major
        Major version number (must not exceed 6553).
    minor
        Minor version number (must not exceed 9).

    Returns
    -------
    int
        Encoded version number.

    Raises
    ------
    ValueError
        If the major or minor values are inadequate.
    """

    if major < 0 or minor < 0:
        msg = "Negative version number was provided"
        raise ValueError(msg)

    if minor >= 10:
        msg = f'Minor "{minor}" cannot exceed 10.'
        raise ValueError(msg)

    version = major * 10 + minor

    if version > 0xFFFF:
        msg = f"Version {major}.{minor} is too high to be encoded"
        raise ValueError(msg)

    return version


def max_o(x: int, n: int, prevent: bool = False) -> int:
    """
    Ensure that ``x`` fits on ``n`` bits.

    Parameters
    ----------
    x
        Number to test.
    n
        Number of bits available.
    prevent
        If true then an exception will rise in case of overflow, otherwise all
        bits are simply set to one.

    Returns
    -------
    int
        A value which fits within the bit number constraint.

    Raises
    ------
    ValueError
        If the value overflows and ``prevent`` is true.
    """

    if x >= 1 << n:
        if prevent:
            msg = f"Value {x} does not fit on {n} bits"
            raise ValueError(msg)
        return (1 << n) - 1

    return x


def max_2(x: int, prevent: bool = False) -> int:
    """
    Fit ``x`` in 2 bytes.

    See Also
    --------
    max_o
    """

    return max_o(x, 16, prevent)


def max_4(x: int, prevent: bool = False) -> int:
    """
    Fit ``x`` in 4 bytes.

    See Also
    --------
    max_o
    """

    return max_o(x, 32, prevent)


def max_8(x: int, prevent: bool = False) -> int:
    """
    Fit ``x`` in 8 bytes.

    See Also
    --------
    max_o
    """

    return max_o(x, 64, prevent)


class CompressionMethod(Enum):
    """
    Supported compression methods.

    ``store`` copies bytes verbatim; ``deflate`` expects an RFC 1951 stream
    (optionally a pre-compressed one, see
    :class:`livezip.storage.PrecompressedDeflate`).
    """

    uncompressed = 0
    deflate = 8


class Zip64ExtraField(NamedTuple):
    """
    Extra field holding the 64-bit information about a file.

    See Also
    --------
    Section 4.5.3 of ``zip_spec.txt``.
    """

    original_size: int
    compressed_size: int
    header_offset: int
    disk_start: int

    def pack(self) -> bytes:
        """
        Serialise only the fields that overflow 32 bits.

        The specification mandates that the ZIP64 extra field only carries the
        fields whose 32-bit counterpart is set to ``0xFFFFFFFF``. This method
        therefore checks each value and appends it only when needed.

        Returns
        -------
        bytes
            The packed extra field, header included.
        """

        fields = [
            (self.original_size, "Q", 32, 64),
            (self.compressed_size, "Q", 32, 64),
            (self.header_offset, "Q", 32, 64),
            (self.disk_start, "I", 16, 32),
        ]

        fmt = "<HH"
        data: list[int] = [0x0001, 0x0]

        for value, field_fmt, length, length_64 in fields:
            if value >= (1 << length):
                fmt += field_fmt
                data.append(max_o(value, length_64, prevent=True))

        data[1] = calcsize(fmt) - calcsize("<HH")

        return pack(fmt, *data)


class LocalFileHeader(NamedTuple):
    """
    Local file header, prepended to each file's bytes.

    See Also
    --------
    Section 4.3.7 of ``zip_spec.txt``.
    """

    version_needed: tuple[int, int]
    general_purpose: int
    compression_method: CompressionMethod
    last_modification: datetime
    crc32: int
    compressed_size: int
    uncompressed_size: int
    file_name: str
    extra_fields: list[Zip64ExtraField]

    def pack(self) -> bytes:
        """
        Serialise the header.

        Returns
        -------
        bytes
            The packed local file header.
        """

        extra = b"".join(x.pack() for x in self.extra_fields)
        file_name = self.file_name.encode("utf-8")

        data = [
            0x04034B50,
            encode_version(*self.version_needed),
            self.general_purpose,
            self.compression_method.value,
            *make_dos_date_time(self.last_modification),
            self.crc32,
            max_4(self.compressed_size),
            max_4(self.uncompressed_size),
            max_2(len(file_name), prevent=True),
            max_2(len(extra), prevent=True),
        ]

        out = pack("<IHHHHHIIIHH", *data)

        return out + file_name + extra


class DataDescriptor(NamedTuple):
    """
    Trailer written after each streamed file.

    Its sizes are 32-bit wide unless either size overflows, in which case both
    are widened to 64 bits (see :func:`needs_zip64_sizes`).

    See Also
    --------
    Section 4.3.9 of ``zip_spec.txt``.
    """

    crc32: int
    compressed_size: int
    uncompressed_size: int

    def packed_size(self) -> int:
        """
        Return the exact number of bytes :meth:`pack` will produce.

        Returns
        -------
        int
            ``24`` for ZIP64 descriptors, ``16`` otherwise.
        """

        if needs_zip64_sizes(self.compressed_size, self.uncompressed_size):
            return 24
        return 16

    def pack(self) -> bytes:
        """
        Serialise the descriptor.

        Returns
        -------
        bytes
            The packed data descriptor.
        """

        if needs_zip64_sizes(self.compressed_size, self.uncompressed_size):
            return pack(
                "<IIQQ",
                0x08074B50,
                self.crc32,
                self.compressed_size,
                self.uncompressed_size,
            )

        return pack(
            "<IIII",
            0x08074B50,
            self.crc32,
            max_4(self.compressed_size),
            max_4(self.uncompressed_size),
        )


class CentralDirectoryFile(NamedTuple):
    """
    One entry of the central directory that closes the archive.

    See Also
    --------
    Section 4.3.12 of ``zip_spec.txt``.
    """

    version_made_by: tuple[int, int]
    version_needed_to_extract: tuple[int, int]
    general_purpose: int
    compression_method: CompressionMethod
    last_modification: datetime
    crc32: int
    compressed_size: int
    uncompressed_size: int
    file_name: str
    extra_fields: list[Zip64ExtraField]
    comment: str
    disk_number_start: int
    internal_file_attributes: int
    external_file_attributes: int
    relative_offset_of_local_header: int

    def pack(self) -> bytes:
        """
        Serialise the central directory entry.

        Returns
        -------
        bytes
            The packed entry.
        """

        extra = b"".join(x.pack() for x in self.extra_fields)
        file_name = self.file_name.encode("utf-8")
        comment = self.comment.encode("utf-8")

        data = [
            0x02014B50,
            encode_version(*self.version_made_by),
            encode_version(*self.version_needed_to_extract),
            self.general_purpose,
            self.compression_method.value,
            *make_dos_date_time(self.last_modification),
            self.crc32,
            max_4(self.compressed_size),
            max_4(self.uncompressed_size),
            max_2(len(file_name), prevent=True),
            max_2(len(extra), prevent=True),
            max_2(len(comment), prevent=True),
            max_2(self.disk_number_start, prevent=True),
            self.internal_file_attributes,
            self.external_file_attributes,
            max_4(self.relative_offset_of_local_header),
        ]

        out = pack("<IHHHHHHIIIHHHHHII", *data)

        return out + file_name + extra + comment


class Zip64EndOfCentralDirectoryRecord(NamedTuple):
    """
    ZIP64 end of central directory record.

    See Also
    --------
    Section 4.3.14 of ``zip_spec.txt``.
    """

    version_made_by: tuple[int, int]
    version_needed_to_extract: tuple[int, int]
    number_of_this_disk: int
    number_of_the_disk_with_start: int
    number_of_entries_on_this_disk: int
    number_of_entries: int
    size_of_central_directory: int
    central_directory_offset: int

    def pack(self) -> bytes:
        """
        Serialise the ZIP64 end of central directory record.

        Returns
        -------
        bytes
            The packed record.
        """

        fmt = "<IQHHIIQQQQ"

        data = [
            0x06064B50,
            calcsize(fmt) - calcsize("<IQ"),
            encode_version(*self.version_made_by),
            encode_version(*self.version_needed_to_extract),
            max_4(self.number_of_this_disk, prevent=True),
            max_4(self.number_of_the_disk_with_start, prevent=True),
            max_8(self.number_of_entries_on_this_disk, prevent=True),
            max_8(self.number_of_entries, prevent=True),
            max_8(self.size_of_central_directory, prevent=True),
            max_8(self.central_directory_offset, prevent=True),
        ]

        return pack(fmt, *data)


class Zip64EndOfCentralDirectoryLocator(NamedTuple):
    """
    Locator pointing at the ZIP64 end of central directory record.

    See Also
    --------
    Section 4.3.15 of ``zip_spec.txt``.
    """

    number_of_the_disk_with_start: int
    offset_to_end_of_central_directory_record: int
    number_of_disks: int

    def pack(self) -> bytes:
        """
        Serialise the locator.

        Returns
        -------
        bytes
            The packed locator.
        """

        data = [
            0x07064B50,
            max_4(self.number_of_the_disk_with_start, prevent=True),
            max_8(self.offset_to_end_of_central_directory_record, prevent=True),
            max_4(self.number_of_disks, prevent=True),
        ]

        return pack("<IIQI", *data)


class EndOfCentralDirectoryRecord(NamedTuple):
    """
    The (32-bit) end of central directory record, always the last record.

    See Also
    --------
    Section 4.3.16 of ``zip_spec.txt``.
    """

    number_of_this_disk: int
    number_of_the_disk_with_start: int
    number_of_entries_on_this_disk: int
    number_of_entries: int
    size_of_central_directory: int
    central_directory_offset: int
    comment: str

    def pack(self) -> bytes:
        """
        Serialise the end of central directory record.

        Returns
        -------
        bytes
            The packed record, comment included.
        """

        comment = self.comment.encode("utf-8")

        data = [
            0x06054B50,
            max_2(self.number_of_this_disk),
            max_2(self.number_of_the_disk_with_start),
            max_2(self.number_of_entries_on_this_disk),
            max_2(self.number_of_entries),
            max_4(self.size_of_central_directory),
            max_4(self.central_directory_offset),
            max_2(len(comment), prevent=True),
        ]

        out = pack("<IHHHHIIH", *data)

        return out + comment
