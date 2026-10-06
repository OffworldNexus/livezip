"""Stream logs straight out of an S3 bucket into a ZIP archive.

This module implements the flagship use case: a bucket holds log files that were
compressed with DEFLATE at upload time (an ``s3://bucket/logs/*.deflate`` set,
say). Listing that bucket is enough to know exactly how big the resulting ZIP
will be, because the listing gives the compressed size and the object metadata
gives the uncompressed size and the CRC32. The payloads are then streamed
through the encoder untouched — no re-compression, no full download, no
compromise on the memory bound.

The S3 client is not imported eagerly: the whole module only depends on a small
:class:`S3Client` protocol, so importing :mod:`livezip.s3` does not require
``boto3`` to be installed. Use :func:`create_client` (which needs the ``s3``
extra) to build a real client, or pass your own object that implements the
protocol.

Examples
--------
>>> from livezip.s3 import create_client, zip_from_s3
>>> client = create_client(region_name="eu-west-3")
>>> archive = zip_from_s3(client, "my-logs", prefix="2026/")
>>> archive.file_size
123456
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, BinaryIO, Protocol
from zlib import DEFLATED, MAX_WBITS, compressobj, crc32

from .encode import ZipEncoder, ZipFile, build_archive
from .storage import PrecompressedDeflate
from .stream import DataStream

#: Object-metadata key holding the size of the payload before compression.
UNCOMPRESSED_SIZE_METADATA = "uncompressed-size"

#: Object-metadata key holding the CRC32 of the payload before compression,
#: formatted as 8 lowercase hexadecimal digits.
CRC32_METADATA = "crc32"

#: Suffix conventionally stripped from object keys when naming ZIP entries.
DEFLATE_SUFFIX = ".deflate"


class S3Client(Protocol):
    """
    The subset of the ``boto3`` S3 client interface livezip relies on.

    Depending on this narrow protocol instead of ``boto3`` proper keeps the S3
    integration testable (any stub works) and lets the package be imported
    without the optional dependency installed.
    """

    def list_objects_v2(self, **kwargs: Any) -> dict[str, Any]:
        """
        List objects in a bucket.

        Parameters
        ----------
        **kwargs
            Passed through to the S3 API (``Bucket``, ``Prefix``,
            ``ContinuationToken``, ...).

        Returns
        -------
        dict
            The raw S3 response.
        """

    def head_object(self, **kwargs: Any) -> dict[str, Any]:
        """
        Fetch an object's metadata without its payload.

        Parameters
        ----------
        **kwargs
            Passed through to the S3 API (``Bucket``, ``Key``).

        Returns
        -------
        dict
            The raw S3 response.
        """

    def get_object(self, **kwargs: Any) -> dict[str, Any]:
        """
        Start downloading an object.

        Parameters
        ----------
        **kwargs
            Passed through to the S3 API (``Bucket``, ``Key``).

        Returns
        -------
        dict
            The raw S3 response, whose ``Body`` is a readable stream.
        """

    def put_object(self, **kwargs: Any) -> dict[str, Any]:
        """
        Upload an object.

        Parameters
        ----------
        **kwargs
            Passed through to the S3 API (``Bucket``, ``Key``, ``Body``, ...).

        Returns
        -------
        dict
            The raw S3 response.
        """


def create_client(
    *,
    endpoint_url: str | None = None,
    region_name: str | None = None,
    access_key: str | None = None,
    secret_key: str | None = None,
    session_token: str | None = None,
) -> S3Client:
    """
    Build a real S3 client, importing ``boto3`` lazily.

    Parameters
    ----------
    endpoint_url
        Custom endpoint, e.g. for SeaweedFS or MinIO.
    region_name
        AWS region name.
    access_key
        Explicit access key. When ``None``, the usual credential chain applies.
    secret_key
        Explicit secret key.
    session_token
        Optional session token.

    Returns
    -------
    S3Client
        A configured client.

    Raises
    ------
    ImportError
        If ``boto3`` is not installed. Install the ``s3`` extra to fix this.
    """

    try:
        import boto3
    except ImportError as exc:
        msg = (
            "The S3 integration requires boto3. Install it with "
            "`pip install livezip[s3]` or `uv sync --extra s3`."
        )
        raise ImportError(msg) from exc

    return boto3.client(
        "s3",
        endpoint_url=endpoint_url,
        region_name=region_name,
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        aws_session_token=session_token,
    )


class S3Stream(DataStream):
    """
    Stream a single S3 object's bytes.

    The ``GetObject`` request is only issued in :meth:`open`, so listing a
    bucket and preparing an archive never opens a connection. This matters when
    the archive serves many files: the encoder holds at most one socket at a
    time.
    """

    def __init__(self, client: S3Client, bucket: str, key: str):
        """
        Construct the stream.

        Parameters
        ----------
        client
            S3 client to read from.
        bucket
            Bucket holding the object.
        key
            Key of the object.
        """

        self.client = client
        self.bucket = bucket
        self.key = key
        self.body: BinaryIO | None = None

    def open(self) -> None:
        """
        Issue the ``GetObject`` request.
        """

        response = self.client.get_object(Bucket=self.bucket, Key=self.key)
        self.body = response["Body"]

    def read(self, length: int) -> bytes:
        """
        Read at most ``length`` bytes from the object body.

        Parameters
        ----------
        length
            Maximum number of bytes to return.

        Returns
        -------
        bytes
            The read data.
        """

        if self.body is None:
            msg = "the stream is not open"
            raise RuntimeError(msg)

        return self.body.read(length)

    def close(self) -> None:
        """
        Close the HTTP response and release the connection.
        """

        if self.body is not None:
            self.body.close()
            self.body = None


@dataclass(frozen=True, slots=True)
class DeflateObject:
    """
    A DEFLATE-compressed object, described without downloading it.

    Attributes
    ----------
    bucket
        Bucket the object lives in.
    key
        Key of the object.
    compressed_size
        Size of the stored bytes, straight from the S3 listing.
    uncompressed_size
        Size of the payload once decompressed, from object metadata.
    crc32
        CRC32 of the uncompressed payload, from object metadata.
    last_modified
        Server-side modification timestamp.
    """

    bucket: str
    key: str
    compressed_size: int
    uncompressed_size: int
    crc32: int
    last_modified: datetime

    @property
    def compression_ratio(self) -> float:
        """
        Ratio between stored and original size.

        Returns
        -------
        float
            ``compressed_size / uncompressed_size``; ``1.0`` for empty inputs.
        """

        if self.uncompressed_size == 0:
            return 1.0

        return self.compressed_size / self.uncompressed_size

    def to_zip_file(
        self,
        client: S3Client,
        *,
        path: str | None = None,
        comment: str = "",
    ) -> ZipFile:
        """
        Turn the object into a :class:`~livezip.encode.ZipFile`.

        Parameters
        ----------
        client
            Client used to stream the payload at encoding time.
        path
            Name of the entry inside the archive. Defaults to the key with the
            ``.deflate`` suffix stripped.
        comment
            Per-entry comment.

        Returns
        -------
        ZipFile
            The file description, ready to be handed to the encoder.
        """

        return ZipFile(
            path=path if path is not None else zip_path(self.key),
            data=PrecompressedDeflate(
                S3Stream(client, self.bucket, self.key),
                compressed_size=self.compressed_size,
                uncompressed_size=self.uncompressed_size,
                crc32=self.crc32,
            ),
            modification_date=self.last_modified,
            is_binary=True,
            comment=comment,
        )


def zip_path(key: str) -> str:
    """
    Derive a readable ZIP entry name from an object key.

    Parameters
    ----------
    key
        S3 object key.

    Returns
    -------
    str
        The key without its conventional ``.deflate`` suffix, and without a
        leading slash.
    """

    path = key[1:] if key.startswith("/") else key

    if path.endswith(DEFLATE_SUFFIX):
        path = path[: -len(DEFLATE_SUFFIX)]

    return path


def compress_deflate(data: bytes, level: int = 9) -> bytes:
    """
    Compress bytes into a raw DEFLATE stream.

    Parameters
    ----------
    data
        Payload to compress.
    level
        Compression level, from 0 to 9.

    Returns
    -------
    bytes
        An RFC 1951 stream (no zlib header or trailer), as ZIP expects.
    """

    compressor = compressobj(level, DEFLATED, -MAX_WBITS)
    return compressor.compress(data) + compressor.flush()


def put_deflate_object(
    client: S3Client,
    bucket: str,
    key: str,
    data: bytes,
    *,
    level: int = 9,
    content_type: str = "application/octet-stream",
) -> DeflateObject:
    """
    Compress ``data`` and upload it with the metadata livezip needs.

    This convenience helper is meant for tests and examples, where the payload
    fits in memory. It writes the :data:`UNCOMPRESSED_SIZE_METADATA` and
    :data:`CRC32_METADATA` so that a later listing is enough to build a valid,
    size-predictable archive.

    Parameters
    ----------
    client
        S3 client to upload with.
    bucket
        Target bucket.
    key
        Target key.
    data
        Uncompressed payload.
    level
        Compression level, from 0 to 9.
    content_type
        MIME type to store.

    Returns
    -------
    DeflateObject
        The description of the uploaded object.
    """

    compressed = compress_deflate(data, level=level)
    checksum = crc32(data)

    client.put_object(
        Bucket=bucket,
        Key=key,
        Body=compressed,
        ContentType=content_type,
        Metadata={
            UNCOMPRESSED_SIZE_METADATA: str(len(data)),
            CRC32_METADATA: f"{checksum:08x}",
        },
    )

    return DeflateObject(
        bucket=bucket,
        key=key,
        compressed_size=len(compressed),
        uncompressed_size=len(data),
        crc32=checksum,
        last_modified=datetime.now(UTC),
    )


def list_deflate_objects(
    client: S3Client,
    bucket: str,
    prefix: str = "",
) -> list[DeflateObject]:
    """
    List DEFLATE objects and read their size/CRC metadata.

    The listing itself provides keys and compressed sizes; a ``HeadObject`` per
    key retrieves the custom metadata. The result is sorted by key so that two
    listings of the same prefix produce byte-identical archives.

    Parameters
    ----------
    client
        S3 client to list with.
    bucket
        Bucket to list.
    prefix
        Only objects whose key starts with this prefix are considered.

    Returns
    -------
    list of DeflateObject
        One entry per object, sorted by key.

    Raises
    ------
    ValueError
        If an object is missing the livezip metadata, or if its CRC32 is not a
        valid hexadecimal number.
    """

    objects: list[DeflateObject] = []
    continuation: str | None = None

    while True:
        kwargs: dict[str, Any] = {"Bucket": bucket, "Prefix": prefix}
        if continuation is not None:
            kwargs["ContinuationToken"] = continuation

        response = client.list_objects_v2(**kwargs)

        for item in response.get("Contents", []):
            key = item["Key"]

            if key.endswith("/"):
                continue

            head = client.head_object(Bucket=bucket, Key=key)
            metadata = head.get("Metadata", {})

            try:
                uncompressed_size = int(metadata[UNCOMPRESSED_SIZE_METADATA])
                checksum = int(metadata[CRC32_METADATA], 16)
            except (KeyError, ValueError) as exc:
                msg = (
                    f"Object s3://{bucket}/{key} is missing valid livezip "
                    f"metadata ({UNCOMPRESSED_SIZE_METADATA!r}, "
                    f"{CRC32_METADATA!r}); upload it with put_deflate_object()"
                )
                raise ValueError(msg) from exc

            objects.append(
                DeflateObject(
                    bucket=bucket,
                    key=key,
                    compressed_size=int(head.get("ContentLength", item["Size"])),
                    uncompressed_size=uncompressed_size,
                    crc32=checksum,
                    last_modified=head["LastModified"],
                )
            )

        if not response.get("IsTruncated"):
            break

        continuation = response["NextContinuationToken"]

    objects.sort(key=lambda obj: obj.key)

    return objects


def zip_from_s3(
    client: S3Client,
    bucket: str,
    *,
    prefix: str = "",
    comment: str = "",
    path_for: Callable[[DeflateObject], str] | None = None,
) -> ZipEncoder:
    """
    Prepare a streaming archive of every DEFLATE object under a prefix.

    Parameters
    ----------
    client
        S3 client used for listing and streaming.
    bucket
        Bucket to read from.
    prefix
        Only objects whose key starts with this prefix are included.
    comment
        Archive-level comment.
    path_for
        Optional callable mapping an object to its ZIP entry name. Defaults to
        :func:`zip_path`, which strips the ``.deflate`` suffix.

    Returns
    -------
    ZipEncoder
        A prepared encoder whose :attr:`~livezip.encode.ZipEncoder.file_size`
        is already known.

    Raises
    ------
    ValueError
        If the prefix matches no object.
    """

    objects = list_deflate_objects(client, bucket, prefix)

    if not objects:
        msg = f"No DEFLATE objects found in s3://{bucket}/{prefix}"
        raise ValueError(msg)

    files = [
        obj.to_zip_file(
            client,
            path=path_for(obj) if path_for is not None else None,
        )
        for obj in objects
    ]

    return build_archive(files, comment)
