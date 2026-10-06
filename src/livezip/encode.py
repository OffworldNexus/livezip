"""The streaming ZIP64 encoder itself.

The encoder works in three stages, all of which are exposed separately so that
callers can inspect the predicted outcome before committing to streaming:

1. :meth:`ZipEncoder.make_segments` builds the ordered list of records. Each
   record knows its own length and how to produce its bytes.
2. :meth:`ZipEncoder.compute_offsets` walks that list once, recording the
   offset of every record. This is what makes the archive *predictable*: the
   total size is known before a single payload byte is read.
3. :meth:`ZipEncoder.get_data` iterates the records and yields their bytes.

:meth:`ZipEncoder.prepare` is the shortcut that runs stages 1 and 2 and fills in
:attr:`ZipEncoder.file_size`.

The design relies on an important property of the ZIP format: an entry can be
declared as "streamed" (general purpose bit 3), which moves the CRC and sizes
into a trailer written *after* the payload. Everything the encoder emits before
a payload therefore only depends on data it already has, and everything after
it can use the checksum computed while streaming.
"""

from abc import ABC, abstractmethod
from collections.abc import Iterator, Sequence
from datetime import datetime
from struct import calcsize
from typing import Any, NamedTuple, cast

from .models import (
    GP_LANGUAGES_ENCODING,
    GP_STREAM,
    CentralDirectoryFile,
    DataDescriptor,
    EndOfCentralDirectoryRecord,
    LocalFileHeader,
    Zip64EndOfCentralDirectoryLocator,
    Zip64EndOfCentralDirectoryRecord,
    Zip64ExtraField,
    needs_zip64_sizes,
)
from .storage import CompactFile

#: Versions advertised in the archive. 4.5 is required to read ZIP64 records.
VERSION_NEEDED = (4, 5)
VERSION_USED = (4, 5)

#: Threshold above which a central directory value overflows its 32-bit field.
_UINT32_MAX = 0xFFFFFFFF


class ZipFile(NamedTuple):
    """
    A file scheduled to be added to an archive.

    Attributes
    ----------
    path
        Path of the file *inside* the archive, slash-separated. It must not
        contain a leading slash, a drive letter or ``..``.
    data
        Storage strategy describing how the payload is laid out.
    modification_date
        Timestamp stored in the entry.
    is_binary
        Whether the entry holds binary (as opposed to text) data.
    comment
        Per-entry comment.
    """

    path: str
    data: CompactFile
    modification_date: datetime
    is_binary: bool
    comment: str = ""


class Segment(ABC):
    """
    A single record of the archive.

    A segment can predict its length (:meth:`get_length`), expose a hashable
    reference so other segments can refer to it (:meth:`get_reference`), and
    eventually yield its bytes (:meth:`get_data`). Segments are resolved against
    the encoder, which owns the offset table.
    """

    def __init__(self, encoder: "ZipEncoder"):
        """
        Construct the segment.

        Parameters
        ----------
        encoder
            The encoder that owns this segment.
        """

        self.encoder = encoder

    @abstractmethod
    def get_length(self) -> int:
        """
        Predict the exact length of the segment.

        Returns
        -------
        int
            Number of bytes :meth:`get_data` will produce.

        Raises
        ------
        NotImplementedError
            Subclasses must implement this.
        """

        raise NotImplementedError

    @abstractmethod
    def get_reference(self) -> Any:
        """
        Return a hashable reference for this segment.

        Returns
        -------
        Any
            A hashable value other segments can use to reach this one.

        Raises
        ------
        NotImplementedError
            Subclasses must implement this.
        """

        raise NotImplementedError

    @abstractmethod
    def get_data(self) -> Iterator[bytes]:
        """
        Yield the bytes of this segment.

        Yields
        ------
        bytes
            Successive chunks.

        Raises
        ------
        NotImplementedError
            Subclasses must implement this.
        """

        raise NotImplementedError


class LocalFileHeaderSegment(Segment):
    """
    The local file header that precedes a file's payload.
    """

    def __init__(self, encoder: "ZipEncoder", file_id: int, file: ZipFile):
        """
        Construct the segment.

        Parameters
        ----------
        encoder
            The owning encoder.
        file_id
            Index of the file in the archive.
        file
            The file description.
        """

        super().__init__(encoder)

        self.file_id = file_id
        self.file = file

    @property
    def _struct(self) -> LocalFileHeader:
        """
        Build the header.

        The CRC and sizes are zeroed because they are not known yet; the
        ``GP_STREAM`` flag tells readers to look for a trailing data
        descriptor instead.

        Returns
        -------
        LocalFileHeader
            The header record.
        """

        return LocalFileHeader(
            version_needed=VERSION_NEEDED,
            general_purpose=(GP_LANGUAGES_ENCODING | GP_STREAM),
            compression_method=self.file.data.compression_method,
            last_modification=self.file.modification_date,
            crc32=0,
            compressed_size=0,
            uncompressed_size=0,
            file_name=self.file.path,
            extra_fields=[],
        )

    def get_length(self) -> int:
        """
        Length of the packed header.

        Returns
        -------
        int
            The header length.
        """

        return len(self._struct.pack())

    def get_data(self) -> Iterator[bytes]:
        """
        Yield the packed header.

        Yields
        ------
        bytes
            The header, as a single chunk.
        """

        yield self._struct.pack()

    def get_reference(self) -> Any:
        """
        Reference this header, embedding the file id.

        Returns
        -------
        tuple
            The ``("file_header", file_id)`` reference.
        """

        return "file_header", self.file_id


class FileDataSegment(Segment):
    """
    The payload of a file.
    """

    def __init__(self, encoder: "ZipEncoder", file_id: int, file: ZipFile):
        """
        Construct the segment.

        Parameters
        ----------
        encoder
            The owning encoder.
        file_id
            Index of the file in the archive.
        file
            The file description.
        """

        super().__init__(encoder)

        self.file_id = file_id
        self.file = file

    @property
    def crc32(self) -> int:
        """
        Proxy the file's CRC32 from its storage strategy.

        Returns
        -------
        int
            The checksum.
        """

        return self.file.data.crc32

    def get_length(self) -> int:
        """
        Length of the payload, as announced by the storage strategy.

        Returns
        -------
        int
            The payload length.
        """

        return self.file.data.compressed_size

    def get_data(self) -> Iterator[bytes]:
        """
        Stream the payload, guarding against a size mismatch.

        If the storage yields a number of bytes different from what it
        announced, the offsets — and therefore the whole archive — would be
        corrupt, so an exception is raised rather than producing a broken file.

        Yields
        ------
        bytes
            The payload chunks.

        Raises
        ------
        ValueError
            If the number of bytes read differs from ``compressed_size``.
        """

        read = 0

        for chunk in self.file.data.get_data():
            read += len(chunk)

            if read > self.file.data.compressed_size:
                msg = f'Received too much data for "{self.file.path}"'
                raise ValueError(msg)

            if not chunk:
                return

            yield chunk

        if read != self.file.data.compressed_size:
            msg = (
                f'Received a different file size for "{self.file.path}" than '
                f"what was announced"
            )
            raise ValueError(msg)

    def get_reference(self) -> Any:
        """
        Reference this payload, embedding the file id.

        Returns
        -------
        tuple
            The ``("file_data", file_id)`` reference.
        """

        return "file_data", self.file_id


class DataDescriptorSegment(Segment):
    """
    Trailer written after a payload, carrying CRC and sizes.

    It is technically redundant with the central directory, but the streaming
    flag makes it mandatory, and it is what allows the local header to be
    emitted before the payload has been read.
    """

    def __init__(self, encoder: "ZipEncoder", file_id: int, file: ZipFile):
        """
        Construct the segment.

        Parameters
        ----------
        encoder
            The owning encoder.
        file_id
            Index of the file in the archive.
        file
            The file description.
        """

        super().__init__(encoder)

        self.file_id = file_id
        self.file = file

    def get_length(self) -> int:
        """
        Length of the descriptor.

        The length only depends on whether the sizes overflow 32 bits, which is
        already known from the storage strategy, so this stays predictable.

        Returns
        -------
        int
            ``16`` or ``24`` bytes.
        """

        return (
            24
            if needs_zip64_sizes(
                self.file.data.compressed_size, self.file.data.uncompressed_size
            )
            else 16
        )

    def get_data(self) -> Iterator[bytes]:
        """
        Yield the packed descriptor, using the freshly computed CRC.

        Yields
        ------
        bytes
            The descriptor, as a single chunk.
        """

        payload = cast(
            "FileDataSegment", self.encoder.get_segment(("file_data", self.file_id))
        )

        yield DataDescriptor(
            crc32=payload.crc32,
            compressed_size=self.file.data.compressed_size,
            uncompressed_size=self.file.data.uncompressed_size,
        ).pack()

    def get_reference(self) -> Any:
        """
        Reference this descriptor, embedding the file id.

        Returns
        -------
        tuple
            The ``("file_descriptor", file_id)`` reference.
        """

        return "file_descriptor", self.file_id


class CentralDirectoryFileSegment(Segment):
    """
    The registration of a file in the central directory.
    """

    def __init__(self, encoder: "ZipEncoder", file_id: int, file: ZipFile):
        """
        Construct the segment.

        Parameters
        ----------
        encoder
            The owning encoder.
        file_id
            Index of the file in the archive.
        file
            The file description.
        """

        super().__init__(encoder)

        self.file_id = file_id
        self.file = file

    @property
    def _struct(self) -> CentralDirectoryFile:
        """
        Build the central directory entry.

        The ZIP64 extra field is only emitted when a value genuinely overflows
        its 32-bit field, as the specification demands.

        Returns
        -------
        CentralDirectoryFile
            The entry record.
        """

        payload = cast(
            "FileDataSegment", self.encoder.get_segment(("file_data", self.file_id))
        )

        header_offset = self.encoder.get_offset(("file_header", self.file_id))

        extra: list[Zip64ExtraField] = []

        if (
            self.file.data.compressed_size > _UINT32_MAX
            or self.file.data.uncompressed_size > _UINT32_MAX
            or header_offset > _UINT32_MAX
        ):
            extra.append(
                Zip64ExtraField(
                    original_size=self.file.data.uncompressed_size,
                    compressed_size=self.file.data.compressed_size,
                    header_offset=header_offset,
                    disk_start=0,
                )
            )

        return CentralDirectoryFile(
            version_made_by=VERSION_USED,
            version_needed_to_extract=VERSION_NEEDED,
            general_purpose=(GP_LANGUAGES_ENCODING | GP_STREAM),
            compression_method=self.file.data.compression_method,
            last_modification=self.file.modification_date,
            crc32=payload.crc32,
            compressed_size=self.file.data.compressed_size,
            uncompressed_size=self.file.data.uncompressed_size,
            file_name=self.file.path,
            extra_fields=extra,
            comment=self.file.comment,
            disk_number_start=0,
            internal_file_attributes=(1 if self.file.is_binary else 0),
            external_file_attributes=0,
            relative_offset_of_local_header=header_offset,
        )

    def get_reference(self) -> Any:
        """
        Reference this entry, embedding the file id.

        Returns
        -------
        tuple
            The ``("cd_file", file_id)`` reference.
        """

        return "cd_file", self.file_id

    def get_length(self) -> int:
        """
        Length of the packed entry.

        Returns
        -------
        int
            The entry length, which depends on the variable-length extras.
        """

        return len(self._struct.pack())

    def get_data(self) -> Iterator[bytes]:
        """
        Yield the packed entry.

        Yields
        ------
        bytes
            The entry, as a single chunk.
        """

        yield self._struct.pack()


class Zip64EndOfCentralDirectoryRecordSegment(Segment):
    """
    The ZIP64 end of central directory record.

    It is only emitted when the 32-bit record would overflow, and produces zero
    bytes otherwise.
    """

    @property
    def is_required(self) -> bool:
        """
        Tell whether the ZIP64 record is mandatory.

        Returns
        -------
        bool
            ``True`` when the entry count or either central directory offset
            overflows the 32-bit record.
        """

        offset_cd = self.encoder.get_offset(("cd_file", 0))
        offset_eocd = self.encoder.get_offset("eocd64_record")

        return (
            len(self.encoder.files) > 0xFFFF
            or offset_cd > _UINT32_MAX
            or offset_eocd > _UINT32_MAX
        )

    @property
    def _struct(self) -> Zip64EndOfCentralDirectoryRecord:
        """
        Build the ZIP64 record.

        Returns
        -------
        Zip64EndOfCentralDirectoryRecord
            The record.
        """

        offset_cd = self.encoder.get_offset(("cd_file", 0))
        offset_eocd = self.encoder.get_offset("eocd64_record")

        return Zip64EndOfCentralDirectoryRecord(
            version_made_by=VERSION_USED,
            version_needed_to_extract=VERSION_NEEDED,
            number_of_this_disk=0,
            number_of_the_disk_with_start=0,
            number_of_entries=len(self.encoder.files),
            number_of_entries_on_this_disk=len(self.encoder.files),
            size_of_central_directory=(offset_eocd - offset_cd),
            central_directory_offset=offset_cd,
        )

    def get_data(self) -> Iterator[bytes]:
        """
        Yield the record, unless it is not required.

        Yields
        ------
        bytes
            The record, or nothing.
        """

        if self.is_required:
            yield self._struct.pack()

    def get_length(self) -> int:
        """
        Length of the record, or zero when not required.

        Returns
        -------
        int
            The record length.
        """

        if self.is_required:
            return len(self._struct.pack())

        return 0

    def get_reference(self) -> Any:
        """
        Reference this record.

        Returns
        -------
        str
            The ``"eocd64_record"`` reference.
        """

        return "eocd64_record"


class Zip64EndOfCentralDirectoryLocatorSegment(Segment):
    """
    The locator pointing at the ZIP64 end of central directory record.

    It follows the exact same presence condition as the record it locates.
    """

    @property
    def is_required(self) -> bool:
        """
        Tell whether the locator is mandatory.

        Returns
        -------
        bool
            Same value as the ZIP64 record's ``is_required``.
        """

        record = cast(
            "Zip64EndOfCentralDirectoryRecordSegment",
            self.encoder.get_segment("eocd64_record"),
        )
        return record.is_required

    @property
    def _struct(self) -> Zip64EndOfCentralDirectoryLocator:
        """
        Build the locator.

        Returns
        -------
        Zip64EndOfCentralDirectoryLocator
            The locator.
        """

        offset = self.encoder.get_offset("eocd64_record")

        return Zip64EndOfCentralDirectoryLocator(
            number_of_the_disk_with_start=0,
            offset_to_end_of_central_directory_record=offset,
            number_of_disks=1,
        )

    def get_reference(self) -> Any:
        """
        Reference this locator.

        Returns
        -------
        str
            The ``"eocd64_locator"`` reference.
        """

        return "eocd64_locator"

    def get_length(self) -> int:
        """
        Length of the locator, or zero when not required.

        Returns
        -------
        int
            The locator length.
        """

        if self.is_required:
            return calcsize("<IIQI")

        return 0

    def get_data(self) -> Iterator[bytes]:
        """
        Yield the locator, unless it is not required.

        Yields
        ------
        bytes
            The locator, or nothing.
        """

        if self.is_required:
            yield self._struct.pack()


class EndOfCentralDirectoryRecordSegment(Segment):
    """
    The (32-bit) end of central directory record, always present and last.
    """

    @property
    def _struct(self) -> EndOfCentralDirectoryRecord:
        """
        Build the record.

        Returns
        -------
        EndOfCentralDirectoryRecord
            The record.
        """

        offset_cd = self.encoder.get_offset(("cd_file", 0))
        offset_eocd = self.encoder.get_offset("eocd64_record")

        return EndOfCentralDirectoryRecord(
            number_of_this_disk=0,
            number_of_the_disk_with_start=0,
            number_of_entries_on_this_disk=len(self.encoder.files),
            number_of_entries=len(self.encoder.files),
            size_of_central_directory=(offset_eocd - offset_cd),
            central_directory_offset=offset_cd,
            comment=self.encoder.comment,
        )

    def get_data(self) -> Iterator[bytes]:
        """
        Yield the packed record.

        Yields
        ------
        bytes
            The record, comment included.
        """

        yield self._struct.pack()

    def get_length(self) -> int:
        """
        Length of the packed record.

        Returns
        -------
        int
            The record length.
        """

        return len(self._struct.pack())

    def get_reference(self) -> Any:
        """
        Reference this record.

        Returns
        -------
        str
            The ``"eocd"`` reference.
        """

        return "eocd"


class ZipEncoder:
    """
    Stream ZIP64 archives with bounded memory usage.

    The memory footprint is :math:`O(k)` where :math:`k` is the number of files
    (roughly one kilobyte each), independent of the combined payload size
    :math:`n`. Execution time is :math:`O(n)`.

    Notes
    -----
    The encoder deliberately does not compress anything itself. Compression is
    the storage strategy's job, which is what keeps the output size knowable in
    advance. For huge, already-compressed assets this is a feature; for text
    the caller can pre-compress and use
    :class:`livezip.storage.PrecompressedDeflate`.
    """

    def __init__(self, files: Sequence[ZipFile], comment: str = ""):
        """
        Construct the encoder.

        Parameters
        ----------
        files
            Files to include, in order.
        comment
            Archive-level comment.

        Raises
        ------
        ValueError
            If ``files`` is empty.
        """

        if not files:
            msg = "Unexpected empty files list"
            raise ValueError(msg)

        self.files = list(files)
        self.comment = comment
        self.segments: list[Segment] = []
        self.offsets: dict[Any, int] = {}
        self.indexed_segments: dict[Any, Segment] = {}
        self.file_size = 0

    def get_segment(self, reference: Any) -> Segment:
        """
        Return a segment by reference.

        Parameters
        ----------
        reference
            Reference to resolve.

        Returns
        -------
        Segment
            The matching segment.

        Raises
        ------
        KeyError
            If the reference is unknown.
        """

        return self.indexed_segments[reference]

    def get_offset(self, reference: Any) -> int:
        """
        Return the offset of a segment by reference.

        Parameters
        ----------
        reference
            Reference to resolve.

        Returns
        -------
        int
            The offset of the segment.

        Raises
        ------
        KeyError
            If the reference is unknown.
        """

        return self.offsets[reference]

    def make_segments(self) -> None:
        """
        Build the ordered list of segments according to the ZIP specification.
        """

        segments: list[Segment] = []

        for file_id, file in enumerate(self.files):
            segments += [
                LocalFileHeaderSegment(self, file_id, file),
                FileDataSegment(self, file_id, file),
                DataDescriptorSegment(self, file_id, file),
            ]

        for file_id, file in enumerate(self.files):
            segments.append(CentralDirectoryFileSegment(self, file_id, file))

        segments += [
            Zip64EndOfCentralDirectoryRecordSegment(self),
            Zip64EndOfCentralDirectoryLocatorSegment(self),
            EndOfCentralDirectoryRecordSegment(self),
        ]

        self.segments = segments

    def compute_offsets(self) -> None:
        """
        Compute every segment's offset and the total archive size.
        """

        offset = 0

        for segment in self.segments:
            reference = segment.get_reference()
            self.offsets[reference] = offset
            self.indexed_segments[reference] = segment
            offset += segment.get_length()

        self.file_size = offset

    def prepare(self) -> None:
        """
        Run the preliminary stages.

        After this call, :attr:`file_size` is populated and :meth:`get_data`
        may be used.
        """

        self.make_segments()
        self.compute_offsets()

    def get_data(self) -> Iterator[bytes]:
        """
        Yield the archive bytes.

        Yields
        ------
        bytes
            Successive chunks forming the complete archive. Their concatenation
            is exactly :attr:`file_size` bytes long.
        """

        for segment in self.segments:
            yield from segment.get_data()


class ZippedStream(NamedTuple):
    """
    A prepared encoder together with its announced size.

    Attributes
    ----------
    encoder
        The prepared encoder.
    size
        Total size of the resulting archive, in bytes.
    """

    encoder: ZipEncoder
    size: int

    def iter_bytes(self) -> Iterator[bytes]:
        """
        Iterate over the archive bytes.

        Yields
        ------
        bytes
            The archive chunks.
        """

        return self.encoder.get_data()


def build_archive(files: Sequence[ZipFile], comment: str = "") -> ZipEncoder:
    """
    Build and prepare a streaming archive in one call.

    Parameters
    ----------
    files
        Files to include.
    comment
        Archive-level comment.

    Returns
    -------
    ZipEncoder
        An encoder whose :attr:`~ZipEncoder.file_size` is already known.
    """

    encoder = ZipEncoder(files, comment)
    encoder.prepare()
    return encoder
