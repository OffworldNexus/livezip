# LiveZip

**Memory-bound, streamable ZIP64 archives — from any source.** LiveZip builds a
ZIP file as an iterator of byte chunks, holding only a small descriptor per entry
— never the archive, and never the payloads. Where the bytes come from (disk,
HTTP, memory, S3) is a detail, and sources can be mixed in one archive.

[![CI](https://github.com/OffworldNexus/livezip/actions/workflows/ci.yml/badge.svg)](https://github.com/OffworldNexus/livezip/actions/workflows/ci.yml)
[![Documentation](https://github.com/OffworldNexus/livezip/actions/workflows/deploy-docs.yml/badge.svg)](https://offworldnexus.github.io/livezip/)

## Why LiveZip

Three properties define the project, and everything else follows from them:

- **Memory-bound** — for `k` files totalling `n` bytes, memory is `O(k)`
  (roughly a kilobyte per file) and independent of `n`. You size the machine by
  the *number* of files, not their weight.
- **Streamable** — the output is an iterator of chunks. Nothing is ever fully
  buffered, on disk or in RAM.
- **Predictable** — the total archive size is known *before* reading a single
  payload byte, so you can set `Content-Length` on an HTTP response.

This is possible because LiveZip does not compress on the fly. Compression is
the storage strategy's job, decided up front, so the compressed size of every
entry is known when the index is built — exactly what you want for media or logs
that are incompressible or compressed already.

## Install

LiveZip is `uv`-managed and needs Python 3.11 or newer.

```bash
uv add livezip            # core, zero runtime dependencies
uv add "livezip[s3]"      # with the S3 integration (boto3)
# or: pip install "livezip[s3]"
```

## Usage

Every archive is built the same way, **regardless of where the bytes live**:
pick a **source**, pick a **storage strategy**, wrap each file in a `ZipFile`,
then `prepare()` to learn the exact size and `get_data()` to stream it.

```python
from datetime import UTC, datetime
from pathlib import Path

from livezip import BytesStream, FileStream, Store, ZipEncoder, ZipFile

when = datetime.now(UTC)
clip = Path("clip.mp4")
manifest = b'{"generated": true}'

encoder = ZipEncoder(
    [
        # from disk
        ZipFile(
            "clip.mp4",
            Store(FileStream(clip), clip.stat().st_size),
            when,
            is_binary=True,
        ),
        # from memory
        ZipFile(
            "manifest.json",
            Store(BytesStream(manifest), len(manifest)),
            when,
            is_binary=True,
        ),
    ]
)
encoder.prepare()
print(encoder.file_size)  # exact, before any read

for chunk in encoder.get_data():  # sources are opened one at a time
    ...
```

Swapping `FileStream` for `UrlStream`, `BytesStream` or `S3Stream` is the only
change needed to read from a different backend — the strategies, offsets and
predicted size are identical.

| Source | `DataStream` | Size comes from |
| ------ | ------------ | --------------- |
| Local file | `FileStream(path)` | `path.stat().st_size` |
| In-memory bytes | `BytesStream(data)` | `len(data)` |
| HTTP(S) URL | `UrlStream(lambda: url)` | a `HEAD` / your metadata |
| S3 object | `S3Stream(client, bucket, key)` | the object listing |
| Bucket of DEFLATE logs | `zip_from_s3(...)` | listing + object metadata |

Strategies: `Store` (method 0), `DeflateStore` (method 8, no compression),
`PrecompressedDeflate` (reuse an existing raw DEFLATE payload). See the
[documentation](https://offworldnexus.github.io/livezip/developer/storage/) for
the details.

## The flagship example: logs in S3 → ZIP

A bucket holds log files that were DEFLATE-compressed at upload time, each
carrying its original size and CRC32 in object metadata. Listing the bucket is
then enough to build a perfectly-sized archive, and the payloads stream straight
through — no re-compression, no extra download.

```python
from livezip.s3 import create_client, zip_from_s3

client = create_client(region_name="eu-west-3")

# Lists the prefix (one HEAD per object) — no payload is downloaded yet.
encoder = zip_from_s3(client, "my-logs", prefix="2026/")
print(encoder.file_size)  # the exact archive size, already known

for chunk in encoder.get_data():  # only now are objects fetched, one at a time
    response.write(chunk)
```

A runnable, commented version lives in
[`examples/s3_to_zip.py`](examples/s3_to_zip.py); the
[S3 guide](https://offworldnexus.github.io/livezip/developer/s3-integration/)
covers the metadata contract and the operational details.

## Command line

```bash
livezip -o archive.zip photo.jpg video.mp4 notes.txt
livezip -m store -o raw.zip blob.bin                       # copy bytes verbatim
livezip --s3-bucket my-logs --s3-prefix 2026/ -o logs.zip  # bucket of DEFLATE logs
livezip --s3-bucket my-logs > logs.zip                     # stream to stdout
```

## Concepts

| Piece | Responsibility |
| ----- | -------------- |
| [`ZipEncoder`](src/livezip/encode.py) | Builds the segment list, computes offsets, streams the bytes. |
| [`storage`](src/livezip/storage.py) | Decides how a payload is laid out: `Store`, `DeflateStore`, `PrecompressedDeflate`. |
| [`stream`](src/livezip/stream.py) | Delays opening a resource until it is read: `FileStream`, `UrlStream`, `BytesStream`. |
| [`s3`](src/livezip/s3.py) | Turns a bucket of DEFLATE objects into a prepared encoder. |
| [`models`](src/livezip/models.py) | Byte-exact serialisation of the ZIP/ZIP64 records. |

## Testing

- **Unit tests** (`tests/unit`) are fast and dependency-free.
- **End-to-end tests** (`tests/e2e`) run against a real
  [SeaweedFS](https://github.com/seaweedfs/seaweedfs) container started with
  [testcontainers](https://testcontainers-python.readthedocs.io/): they upload
  logs, list them, stream a ZIP and verify every entry round-trips.

```bash
make test-unit   # fast, no Docker
make test-e2e    # needs Docker
make test        # both
```

CI runs lint, type-checking, unit tests on Python 3.11–3.13, and the
SeaweedFS-backed end-to-end job on every push.

## Development

Everything goes through `uv`; the `Makefile` is the entry point:

```bash
make sync        # install all dependencies (including the s3 extra)
make clean       # format then lint (ruff) and type-check (mypy)
make coverage    # run with a coverage report
make docs-serve  # preview the documentation on localhost:8000
make build       # build sdist + wheel
```

## License

WTFPL — do what the fuck you want to. See [LICENSE](LICENSE).
