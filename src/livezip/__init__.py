"""livezip — memory-bound, streamable ZIP64 archives.

The public surface is deliberately small:

* :class:`livezip.encode.ZipEncoder` and :class:`livezip.encode.ZipFile` are the
  streaming core.
* :mod:`livezip.storage` holds the strategies deciding how payloads are stored.
* :mod:`livezip.stream` holds the byte sources (files, URLs, memory).
* :mod:`livezip.s3` (optional, needs the ``s3`` extra) wires an S3 bucket of
  DEFLATE objects straight into the encoder.
"""

from .encode import ZipEncoder, ZipFile, ZippedStream, build_archive
from .storage import CompactFile, DeflateStore, PrecompressedDeflate, Store
from .stream import BytesStream, DataStream, FileStream, UrlStream

__version__ = "0.2.0"

__all__ = [
    "BytesStream",
    "CompactFile",
    "DataStream",
    "DeflateStore",
    "FileStream",
    "PrecompressedDeflate",
    "Store",
    "UrlStream",
    "ZipEncoder",
    "ZipFile",
    "ZippedStream",
    "__version__",
    "build_archive",
]
