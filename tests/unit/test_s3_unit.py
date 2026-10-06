"""Unit tests for the S3 helpers, using an in-memory fake client."""

import io
import zlib
from datetime import UTC, datetime

import pytest
from helpers import BIG_PAYLOAD, read_archive, stream

from livezip.s3 import (
    CRC32_METADATA,
    UNCOMPRESSED_SIZE_METADATA,
    DeflateObject,
    S3Stream,
    compress_deflate,
    list_deflate_objects,
    put_deflate_object,
    zip_from_s3,
    zip_path,
)

NOW = datetime(2026, 10, 6, 12, 0, 0, tzinfo=UTC)


class FakeS3Client:
    """A tiny in-memory stand-in for a boto3 S3 client."""

    def __init__(self, page_size: int = 1000):
        self.objects: dict[tuple[str, str], dict] = {}
        self.page_size = page_size
        self.get_calls: list[tuple[str, str]] = []

    def put_object(self, **kwargs):
        bucket = kwargs["Bucket"]
        key = kwargs["Key"]
        body = kwargs["Body"]
        self.objects[(bucket, key)] = {
            "Body": body,
            "Metadata": kwargs.get("Metadata", {}),
            "ContentLength": len(body),
            "LastModified": NOW,
        }
        return {}

    def list_objects_v2(self, **kwargs):
        bucket = kwargs["Bucket"]
        prefix = kwargs.get("Prefix", "")
        keys = sorted(
            key
            for (owner, key) in self.objects
            if owner == bucket and key.startswith(prefix)
        )

        start = int(kwargs.get("ContinuationToken", 0))
        page = keys[start : start + self.page_size]
        response = {
            "Contents": [
                {"Key": key, "Size": self.objects[(bucket, key)]["ContentLength"]}
                for key in page
            ],
            "IsTruncated": start + self.page_size < len(keys),
        }

        if response["IsTruncated"]:
            response["NextContinuationToken"] = str(start + self.page_size)

        return response

    def head_object(self, **kwargs):
        return self.objects[(kwargs["Bucket"], kwargs["Key"])]

    def get_object(self, **kwargs):
        key = (kwargs["Bucket"], kwargs["Key"])
        self.get_calls.append(key)
        return {"Body": io.BytesIO(self.objects[key]["Body"])}


def test_zip_path_strips_the_deflate_suffix():
    assert zip_path("logs/2026/app.deflate") == "logs/2026/app"
    assert zip_path("/logs/app.deflate") == "logs/app"
    assert zip_path("logs/app.log") == "logs/app.log"


def test_compress_deflate_produces_raw_deflate():
    compressed = compress_deflate(BIG_PAYLOAD)
    assert zlib.decompressobj(-zlib.MAX_WBITS).decompress(compressed) == BIG_PAYLOAD


def test_put_and_list_roundtrip():
    client = FakeS3Client()
    put_deflate_object(client, "b", "logs/a.log.deflate", b"hello")
    put_deflate_object(client, "b", "logs/b.log.deflate", BIG_PAYLOAD)

    objects = list_deflate_objects(client, "b", "logs/")

    assert [obj.key for obj in objects] == ["logs/a.log.deflate", "logs/b.log.deflate"]

    first = objects[0]
    assert first.compressed_size == len(compress_deflate(b"hello"))
    assert first.uncompressed_size == len(b"hello")
    assert first.crc32 == zlib.crc32(b"hello")
    assert first.last_modified == NOW
    assert first.bucket == "b"


def test_list_pagination():
    client = FakeS3Client(page_size=2)
    for index in range(5):
        put_deflate_object(client, "b", f"logs/{index}.deflate", b"x" * (index + 1))

    objects = list_deflate_objects(client, "b", "logs/")
    assert len(objects) == 5


def test_list_rejects_objects_without_metadata():
    client = FakeS3Client()
    client.put_object(Bucket="b", Key="logs/a.deflate", Body=b"raw")

    with pytest.raises(ValueError, match="missing valid livezip metadata"):
        list_deflate_objects(client, "b", "logs/")


def test_list_rejects_invalid_crc_metadata():
    client = FakeS3Client()
    client.put_object(
        Bucket="b",
        Key="logs/a.deflate",
        Body=b"raw",
        Metadata={UNCOMPRESSED_SIZE_METADATA: "1", CRC32_METADATA: "not-hex"},
    )

    with pytest.raises(ValueError, match="missing valid livezip metadata"):
        list_deflate_objects(client, "b", "logs/")


def test_zip_from_s3_defers_downloads_until_streaming():
    client = FakeS3Client()
    for index in range(3):
        put_deflate_object(client, "b", f"logs/{index}.deflate", BIG_PAYLOAD)

    encoder = zip_from_s3(client, "b", prefix="logs/")

    # Listing + preparing only needs HEAD requests; no payload was fetched yet.
    assert client.get_calls == []
    assert encoder.file_size > 0

    data = stream(encoder)
    assert len(client.get_calls) == 3
    assert len(data) == encoder.file_size


def test_zip_from_s3_roundtrip():
    client = FakeS3Client()
    payloads = {
        "logs/alpha.log.deflate": b"alpha" * 1000,
        "logs/beta.log.deflate": BIG_PAYLOAD,
    }
    for key, payload in payloads.items():
        put_deflate_object(client, "b", key, payload)

    encoder = zip_from_s3(client, "b", prefix="logs/")
    archive = read_archive(stream(encoder))

    assert archive.testzip() is None
    for key, payload in payloads.items():
        assert archive.read(zip_path(key)) == payload


def test_zip_from_s3_empty_prefix_raises():
    client = FakeS3Client()
    with pytest.raises(ValueError, match="No DEFLATE objects"):
        zip_from_s3(client, "b", prefix="nope/")


def test_to_zip_file_uses_an_explicit_path():
    client = FakeS3Client()
    put_deflate_object(client, "b", "logs/a.deflate", b"hello")
    obj = list_deflate_objects(client, "b")[0]

    zip_file = obj.to_zip_file(client, path="custom/name.txt", comment="hey")
    assert zip_file.path == "custom/name.txt"
    assert zip_file.comment == "hey"


def test_deflate_object_compression_ratio():
    obj = DeflateObject(
        bucket="b",
        key="k",
        compressed_size=50,
        uncompressed_size=100,
        crc32=0,
        last_modified=NOW,
    )
    assert obj.compression_ratio == 0.5

    empty = DeflateObject(
        bucket="b",
        key="k",
        compressed_size=0,
        uncompressed_size=0,
        crc32=0,
        last_modified=NOW,
    )
    assert empty.compression_ratio == 1.0


def test_s3_stream_requires_open():
    client = FakeS3Client()
    stream_obj = S3Stream(client, "b", "k")
    with pytest.raises(RuntimeError, match="not open"):
        stream_obj.read(1)
