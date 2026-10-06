"""Storage strategies: how a file's bytes are laid out inside the archive.

The :class:`CompactFile` contract is what makes the whole library predictable:
the encoder must be able to tell, *before* reading a single byte, how long the
stored payload will be. Every strategy below honours that contract.
"""

from abc import ABC, abstractmethod
from collections.abc import Iterator
from struct import calcsize, pack
from zlib import crc32

from .models import CompressionMethod
from .stream import DataStream


class CompactFile(ABC):
    """
    Base interface for a storage method.

    The idea is to keep the promise of a predictable output size while still
    letting the caller choose how files are encoded. As long as the compressed
    size, the uncompressed size and the compression method are known up front,
    and :meth:`get_data` yields exactly ``compressed_size`` bytes, the encoder
    does not need to know anything else.
    """

    @property
    @abstractmethod
    def compressed_size(self) -> int:
        """
        Number of bytes of the stored (compressed) payload.

        Returns
        -------
        int
            Size of what :meth:`get_data` will yield.
        """

    @property
    @abstractmethod
    def uncompressed_size(self) -> int:
        """
        Number of bytes of the original data.

        Returns
        -------
        int
            Size of the payload once decompressed.
        """

    @property
    @abstractmethod
    def compression_method(self) -> CompressionMethod:
        """
        Compression method to advertise in the ZIP entries.

        Returns
        -------
        CompressionMethod
            The method matching the bytes returned by :meth:`get_data`.
        """

    @property
    @abstractmethod
    def crc32(self) -> int:
        """
        CRC32 of the *uncompressed* data.

        Returns
        -------
        int
            The checksum. It is only guaranteed to be valid once
            :meth:`get_data` has run to completion, except for strategies that
            are given the checksum up front.
        """

    @abstractmethod
    def get_data(self) -> Iterator[bytes]:
        """
        Iterate over the chunks that make up the stored payload.

        Yields
        ------
        bytes
            Successive chunks of arbitrary length.
        """


class Store(CompactFile):
    """
    Copy the raw, uncompressed bytes verbatim.

    This is the cheapest strategy: no framing, no CPU, no size growth. It is
    ideal for already-compressed media (videos, JPEGs, ...) which would not
    benefit from a second compression pass.
    """

    #: Read size used while streaming, in bytes.
    READ_SIZE = 1024**2  # 1 MiB

    def __init__(self, data: DataStream, size: int):
        """
        Construct the strategy.

        Parameters
        ----------
        data
            Stream producing the raw bytes.
        size
            Exact size of the data, in bytes.
        """

        self.data = data
        self._size = size
        self._crc32 = 0

    @property
    def compressed_size(self) -> int:
        """
        Raw data is not transformed, so both sizes are equal.

        Returns
        -------
        int
            The payload size.
        """

        return self._size

    @property
    def uncompressed_size(self) -> int:
        """
        Size of the original data.

        Returns
        -------
        int
            The payload size.
        """

        return self._size

    @property
    def compression_method(self) -> CompressionMethod:
        """
        The stored method.

        Returns
        -------
        CompressionMethod
            ``uncompressed``.
        """

        return CompressionMethod.uncompressed

    @property
    def crc32(self) -> int:
        """
        CRC32 computed while streaming.

        Returns
        -------
        int
            The checksum, valid once :meth:`get_data` has completed.
        """

        return self._crc32

    def get_data(self) -> Iterator[bytes]:
        """
        Yield the raw bytes, computing the checksum along the way.

        Yields
        ------
        bytes
            Successive chunks of the payload.
        """

        self._crc32 = 0
        self.data.open()

        try:
            remaining = self.uncompressed_size

            while remaining > 0:
                chunk = self.data.read_exact(min(self.READ_SIZE, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
                self._crc32 = crc32(chunk, self._crc32)
                yield chunk
        finally:
            self.data.close()


class DeflateStore(CompactFile):
    """
    Wrap the raw data in DEFLATE ``stored`` blocks.

    The bytes are not compressed, but they are framed as an RFC 1951 stream so
    that tools expecting a DEFLATE payload are satisfied. This is a common
    request for macOS and for clients that refuse "stored" entries without a
    DEFLATE variant. The framing overhead is a predictable 5 bytes per 65535
    byte block, so the output size remains known in advance.
    """

    #: Maximum payload size of a DEFLATE ``stored`` block.
    BLOCK_SIZE = 0xFFFF
    #: ``<BHH``: block header, LEN, one's complement of LEN.
    BLOCK_HEADER = "<BHH"
    #: Final (and only, for empty input) empty ``stored`` block.
    EMPTY_BLOCK = b"\x01\x00\x00\xff\xff"

    def __init__(self, data: DataStream, size: int):
        """
        Construct the strategy.

        Parameters
        ----------
        data
            Stream producing the raw bytes.
        size
            Exact size of the raw data, in bytes.
        """

        self.data = data
        self._size = size
        self._crc32 = 0

    @property
    def blocks(self) -> int:
        """
        Number of DEFLATE blocks that will be emitted.

        Notes
        -----
        DEFLATE allows unlimited stream lengths, so rather than trusting
        floating point precision the arithmetic is kept entirely in Python's
        arbitrary-precision integers. Empty input still produces a single empty
        final block, hence the ``max(1, ...)``.

        Returns
        -------
        int
            The number of blocks.
        """

        blocks = self._size // self.BLOCK_SIZE

        if self._size % self.BLOCK_SIZE:
            blocks += 1

        return max(1, blocks)

    @property
    def compressed_size(self) -> int:
        """
        Size of the framed payload.

        Returns
        -------
        int
            Data size plus the header of each block.
        """

        return self.blocks * calcsize(self.BLOCK_HEADER) + self._size

    @property
    def uncompressed_size(self) -> int:
        """
        Size of the original data.

        Returns
        -------
        int
            The payload size.
        """

        return self._size

    @property
    def compression_method(self) -> CompressionMethod:
        """
        The DEFLATE method.

        Returns
        -------
        CompressionMethod
            ``deflate``.
        """

        return CompressionMethod.deflate

    @property
    def crc32(self) -> int:
        """
        CRC32 computed while streaming.

        Returns
        -------
        int
            The checksum, valid once :meth:`get_data` has completed.
        """

        return self._crc32

    def get_data(self) -> Iterator[bytes]:
        """
        Yield the data wrapped in DEFLATE ``stored`` blocks.

        Yields
        ------
        bytes
            Successive framed blocks.
        """

        self._crc32 = 0
        self.data.open()

        try:
            if self._size == 0:
                yield self.EMPTY_BLOCK
                return

            last_offset = (self.blocks - 1) * self.BLOCK_SIZE

            for offset in range(0, self.uncompressed_size, self.BLOCK_SIZE):
                block_format = 0b00000001 if offset == last_offset else 0b00000000
                chunk = self.data.read_exact(min(self.BLOCK_SIZE, self._size - offset))
                header = pack(
                    self.BLOCK_HEADER,
                    block_format,
                    len(chunk),
                    len(chunk) ^ self.BLOCK_SIZE,
                )

                self._crc32 = crc32(chunk, self._crc32)

                yield header + chunk
        finally:
            self.data.close()


class PrecompressedDeflate(CompactFile):
    """
    Reuse an already DEFLATE-compressed payload.

    This is the strategy to reach for when the data is *already* compressed —
    for instance log files stored as ``*.deflate`` in an object store. The
    compressed size comes straight from the object listing, while the
    uncompressed size and CRC32 are supplied by the caller (typically from S3
    user metadata written at upload time). All three are known before the
    bytes are read, so the archive size stays perfectly predictable and the
    payload is streamed through untouched: no re-compression, no full download.

    Notes
    -----
    The payload must be a *raw* DEFLATE stream (RFC 1951), not a zlib-wrapped
    one (RFC 1950), because that is what the ZIP specification expects for
    compression method 8.
    """

    #: Read size used while streaming, in bytes.
    READ_SIZE = 1024**2  # 1 MiB

    def __init__(
        self,
        data: DataStream,
        *,
        compressed_size: int,
        uncompressed_size: int,
        crc32: int,
    ):
        """
        Construct the strategy.

        Parameters
        ----------
        data
            Stream producing the raw DEFLATE bytes.
        compressed_size
            Exact number of bytes the stream will yield.
        uncompressed_size
            Size of the payload once decompressed.
        crc32
            CRC32 of the uncompressed payload.
        """

        self.data = data
        self._compressed_size = compressed_size
        self._uncompressed_size = uncompressed_size
        self._crc32 = crc32

    @property
    def compressed_size(self) -> int:
        """
        Size of the pre-compressed payload.

        Returns
        -------
        int
            The stored size.
        """

        return self._compressed_size

    @property
    def uncompressed_size(self) -> int:
        """
        Size of the payload once decompressed.

        Returns
        -------
        int
            The original size.
        """

        return self._uncompressed_size

    @property
    def compression_method(self) -> CompressionMethod:
        """
        The DEFLATE method.

        Returns
        -------
        CompressionMethod
            ``deflate``.
        """

        return CompressionMethod.deflate

    @property
    def crc32(self) -> int:
        """
        CRC32 supplied by the caller.

        Returns
        -------
        int
            The uncompressed payload checksum.
        """

        return self._crc32

    def get_data(self) -> Iterator[bytes]:
        """
        Stream the pre-compressed payload, verifying its length.

        Yields
        ------
        bytes
            Successive chunks of the raw DEFLATE stream.

        Raises
        ------
        ValueError
            If the stream yields a number of bytes different from the announced
            ``compressed_size``. Silently producing a shorter or longer payload
            would corrupt the archive, so this is checked explicitly.
        """

        read = 0
        self.data.open()

        try:
            while read < self._compressed_size:
                chunk = self.data.read(
                    min(self.READ_SIZE, self._compressed_size - read)
                )
                if not chunk:
                    break
                read += len(chunk)
                yield chunk
        finally:
            self.data.close()

        if read != self._compressed_size:
            msg = (
                f"Expected {self._compressed_size} compressed bytes but the "
                f"stream yielded {read}"
            )
            raise ValueError(msg)
