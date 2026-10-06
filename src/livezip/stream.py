"""Byte-stream abstractions consumed by the storage layer.

The point of :class:`DataStream` is to delay opening the underlying resource
until the moment its bytes are actually needed. This is what allows
:class:`livezip.encode.ZipEncoder` to describe an archive (and compute its final
size) while holding only an O(1) descriptor per file, and to open/close sockets
one at a time rather than all at once.
"""

from abc import ABC, abstractmethod
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import TYPE_CHECKING, BinaryIO
from urllib.request import urlopen

if TYPE_CHECKING:
    from http.client import HTTPResponse


class DataStream(ABC):
    """
    Asynchronous-friendly interface for a stream of bytes.

    Implementations are opened right before being read and closed right after,
    so that resources such as files or HTTP connections are only held while the
    encoder is working on that particular entry.
    """

    @abstractmethod
    def open(self) -> None:
        """
        Acquire whichever resources are needed for reading.
        """

    @abstractmethod
    def read(self, length: int) -> bytes:
        """
        Read at most ``length`` bytes.

        Parameters
        ----------
        length
            Maximum number of bytes to return.

        Returns
        -------
        bytes
            Up to ``length`` bytes, or an empty value at end of stream.
        """

    @abstractmethod
    def close(self) -> None:
        """
        Release whichever resources :meth:`open` acquired.
        """

    def read_exact(self, length: int) -> bytes:
        """
        Read exactly ``length`` bytes unless the stream ends first.

        ``DataStream.read`` implementations are only required to return *at
        most* ``length`` bytes (that is the contract of a socket), so callers
        that need a fixed-size buffer — the DEFLATE block writer, for instance —
        use this helper to loop until the buffer is full.

        Parameters
        ----------
        length
            Number of bytes to gather.

        Returns
        -------
        bytes
            ``length`` bytes, or fewer if the stream ended early.
        """

        chunks = bytearray()
        remaining = length

        while remaining > 0:
            chunk = self.read(remaining)
            if not chunk:
                break
            chunks += chunk
            remaining -= len(chunk)

        return bytes(chunks)


class BytesStream(DataStream):
    """
    Stream an in-memory ``bytes`` object.

    Mostly useful for tests and for callers that already have the payload in
    RAM; :meth:`open` rewinds so the stream can be read more than once.
    """

    def __init__(self, data: bytes):
        """
        Construct the stream.

        Parameters
        ----------
        data
            The payload to serve.
        """

        self.data = data
        self.offset = 0

    def open(self) -> None:
        """
        Rewind the cursor to the beginning of the buffer.
        """

        self.offset = 0

    def read(self, length: int) -> bytes:
        """
        Return the next ``length`` bytes of the buffer.

        Parameters
        ----------
        length
            Maximum number of bytes to return.

        Returns
        -------
        bytes
            The next slice of the payload.
        """

        chunk = self.data[self.offset : self.offset + length]
        self.offset += len(chunk)
        return chunk

    def close(self) -> None:
        """
        Nothing to release: the buffer is owned by the caller.
        """


class FileStream(DataStream):
    """
    Stream the contents of a file on the local filesystem.
    """

    def __init__(self, file_path: str | Path):
        """
        Construct the stream.

        Parameters
        ----------
        file_path
            Path of the file to read.
        """

        self.file_path = Path(file_path)
        self.file: BinaryIO | None = None

    def open(self) -> None:
        """
        Open the file for binary reading.
        """

        self.file = self.file_path.open("rb")

    def read(self, length: int) -> bytes:
        """
        Read at most ``length`` bytes from the file.

        Parameters
        ----------
        length
            Maximum number of bytes to return.

        Returns
        -------
        bytes
            The read data.
        """

        if self.file is None:
            msg = "the stream is not open"
            raise RuntimeError(msg)

        return self.file.read(length)

    def close(self) -> None:
        """
        Close the file handle if it is open.
        """

        if self.file is not None:
            self.file.close()
            self.file = None


class UrlStream(DataStream):
    """
    Stream the content found at the specified URL.
    """

    #: ``urlopen`` timeout, in seconds.
    TIMEOUT = 30

    def __init__(self, url: Callable[[], str]):
        """
        Construct the stream.

        Parameters
        ----------
        url
            A callable evaluated at :meth:`open` time. Deferring it allows the
            caller to generate a time-limited signed URL only when the encoder
            is about to read the bytes.
        """

        self.url = url
        self.response: HTTPResponse | None = None

    def open(self) -> None:
        """
        Perform the HTTP request.
        """

        self.response = urlopen(self.url(), timeout=self.TIMEOUT)  # noqa: S310

    def read(self, length: int) -> bytes:
        """
        Read at most ``length`` bytes from the HTTP response body.

        Parameters
        ----------
        length
            Maximum number of bytes to return.

        Returns
        -------
        bytes
            The read data.
        """

        if self.response is None:
            msg = "the stream is not open"
            raise RuntimeError(msg)

        return self.response.read(length)

    def close(self) -> None:
        """
        Close the HTTP response.
        """

        if self.response is not None:
            self.response.close()
            self.response = None


def iter_chunks(data: bytes, chunk_size: int) -> Iterator[bytes]:
    """
    Yield ``data`` in fixed-size chunks.

    Parameters
    ----------
    data
        Buffer to split.
    chunk_size
        Maximum size of each yielded chunk.

    Yields
    ------
    bytes
        Successive slices of ``data``.
    """

    for offset in range(0, len(data), chunk_size):
        yield data[offset : offset + chunk_size]
