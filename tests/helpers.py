"""Helpers shared by the test modules.

Kept out of ``conftest.py`` because these are plain functions rather than
fixtures; ``pythonpath = ["tests"]`` (see ``pyproject.toml``) makes them
importable from every test package.
"""

import io
import zipfile
from datetime import UTC, datetime

from livezip.encode import ZipEncoder

#: A deterministic payload that spans several DEFLATE blocks and, once
#: wrapped, exceeds the 16-bit threshold used by the ZIP64 extra field.
BIG_PAYLOAD = b"the quick brown fox jumps over the lazy dog\n" * 5000


def read_archive(data: bytes) -> zipfile.ZipFile:
    """
    Open an in-memory archive for inspection.

    Parameters
    ----------
    data
        Raw archive bytes.

    Returns
    -------
    zipfile.ZipFile
        A reader positioned at the start of the archive.
    """

    return zipfile.ZipFile(io.BytesIO(data))


def stream(encoder: ZipEncoder) -> bytes:
    """
    Concatenate every chunk produced by an encoder.

    Parameters
    ----------
    encoder
        A prepared encoder.

    Returns
    -------
    bytes
        The complete archive.
    """

    return b"".join(encoder.get_data())


def fixed_now() -> datetime:
    """
    A timezone-aware timestamp used to build deterministic archives.

    Returns
    -------
    datetime
        A fixed point in time.
    """

    return datetime(2026, 10, 6, 12, 0, 0, tzinfo=UTC)
