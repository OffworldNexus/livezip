"""The flagship end-to-end scenario, backed by a real SeaweedFS container.

It walks the exact flow the documentation advertises: upload DEFLATE-compressed
log files to an S3 bucket, list them to learn their sizes and checksums, then
stream a ZIP straight out of the bucket and verify every entry round-trips.
"""

import zlib

import pytest
from helpers import BIG_PAYLOAD, read_archive, stream

from livezip.s3 import list_deflate_objects, put_deflate_object, zip_from_s3, zip_path

pytestmark = pytest.mark.e2e


def _sample_logs() -> dict[str, bytes]:
    """Return a handful of log payloads, including one that needs ZIP64."""
    return {
        "2026/10/06/app-0001.log.deflate": b"INFO boot\n" * 100,
        "2026/10/06/app-0002.log.deflate": BIG_PAYLOAD,
        "2026/10/07/app-0003.log.deflate": b"ERROR boom\n" * 42,
    }


def test_bucket_listing_is_enough_to_build_the_archive(bucket):
    client, name = bucket
    logs = _sample_logs()

    for key, payload in logs.items():
        put_deflate_object(client, name, key, payload)

    objects = list_deflate_objects(client, name)

    assert {obj.key for obj in objects} == set(logs)
    for obj in objects:
        expected = logs[obj.key]
        assert obj.uncompressed_size == len(expected)
        assert obj.crc32 == zlib.crc32(expected)

    encoder = zip_from_s3(client, name, comment="logs archive")
    assert encoder.file_size > 0

    data = stream(encoder)
    assert len(data) == encoder.file_size

    archive = read_archive(data)
    assert archive.testzip() is None
    assert archive.comment == b"logs archive"
    assert sorted(archive.namelist()) == sorted(zip_path(key) for key in logs)

    for key, payload in logs.items():
        assert archive.read(zip_path(key)) == payload


def test_prefix_only_includes_matching_objects(bucket):
    client, name = bucket
    put_deflate_object(client, name, "day-1/a.log.deflate", b"day one")
    put_deflate_object(client, name, "day-2/b.log.deflate", b"day two")

    objects = list_deflate_objects(client, name, "day-2/")
    assert [obj.key for obj in objects] == ["day-2/b.log.deflate"]

    encoder = zip_from_s3(client, name, prefix="day-2/")
    archive = read_archive(stream(encoder))
    assert archive.namelist() == ["day-2/b.log"]
    assert archive.read("day-2/b.log") == b"day two"


def test_empty_bucket_produces_no_archive(bucket):
    client, name = bucket

    with pytest.raises(ValueError, match="No DEFLATE objects"):
        zip_from_s3(client, name)
