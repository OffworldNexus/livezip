# LiveZip

**Memory-bound, streamable ZIP64 archives.** LiveZip builds a ZIP file as an
iterator of byte chunks, holding only a small descriptor per entry — never the
archive, and never the payloads. It is built for the case where you have a huge
number of already-compressed files (videos, images, logs) sitting in object
storage and you want to hand a client a single archive *right now*.

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
  payload byte, so you can set `Content-Length` on an HTTP response and let the
  client show real progress.

This is possible because LiveZip does not compress on the fly. Compression is
the storage strategy's job, decided up front, so the compressed size of every
entry is known when the index is built. That is a feature for media and logs,
which either do not compress well or are compressed already.

## The flagship example: logs in S3 → ZIP

Say an S3 bucket holds log files that were DEFLATE-compressed at upload time,
each carrying its original size and CRC32 in object metadata. Listing the bucket
is then enough to build a perfectly-sized archive, and the payloads stream
straight through — no re-compression and no extra download.

```python
from livezip.s3 import create_client, list_deflate_objects, zip_from_s3

client = create_client(region_name="eu-west-3")

# One HEAD per object: the listing gives the compressed size, the metadata the
# uncompressed size and CRC32. No payload is downloaded yet.
objects = list_deflate_objects(client, "my-logs", prefix="2026/")

encoder = zip_from_s3(client, "my-logs", prefix="2026/")
print(encoder.file_size)  # the exact archive size, already known

for chunk in encoder.get_data():  # only now are objects fetched, one at a time
    response.write(chunk)
```

The same flow is available from the command line:

```bash
livezip --s3-bucket my-logs --s3-prefix 2026/ -o logs.zip
# or straight to stdout:
livezip --s3-bucket my-logs --s3-prefix 2026/ > logs.zip
```

A runnable, commented version lives in
[`examples/s3_to_zip.py`](examples/s3_to_zip.py). The
[documentation](https://offworldnexus.github.io/livezip/) walks through it in
depth.

## Install

LiveZip is `uv`-managed and needs Python 3.11 or newer.

```bash
uv add livezip            # core, no dependencies
uv add "livezip[s3]"      # with the S3 integration (boto3)
```

Or with pip:

```bash
pip install "livezip[s3]"
```

## Packing local files

```bash
livezip -o archive.zip photo.jpg video.mp4 notes.txt
livezip -m store -o archive.zip raw.bin   # copy bytes verbatim
```

From Python:

```python
from datetime import UTC, datetime
from pathlib import Path

from livezip import FileStream, Store, ZipEncoder, ZipFile

path = Path("video.mp4")
encoder = ZipEncoder(
    [
        ZipFile(
            path="video.mp4",
            data=Store(FileStream(path), path.stat().st_size),
            modification_date=datetime.fromtimestamp(path.stat().st_mtime, tz=UTC),
            is_binary=True,
        )
    ]
)
encoder.prepare()
print(encoder.file_size)  # known before any read
```

## Concepts

| Piece | Responsibility |
| ----- | -------------- |
| [`ZipEncoder`](src/livezip/encode.py) | Builds the segment list, computes offsets, streams the bytes. |
| [`storage`](src/livezip/storage.py) | Decides how a payload is laid out: `Store`, `DeflateStore`, `PrecompressedDeflate`. |
| [`stream`](src/livezip/stream.py) | Delays opening a resource until it is read: `FileStream`, `UrlStream`, `BytesStream`. |
| [`s3`](src/livezip/s3.py) | Turns a bucket of DEFLATE objects into a prepared encoder. |
| [`models`](src/livezip/models.py) | Byte-exact serialisation of the ZIP/ZIP64 records. |

The `PrecompressedDeflate` strategy is the one behind the S3 example: it takes
an existing raw DEFLATE stream plus the original size and CRC32, and passes the
bytes through untouched.

## Testing

The suite has two layers:

- **Unit tests** (`tests/unit`) are fast and dependency-free.
- **End-to-end tests** (`tests/e2e`) run against a real
  [SeaweedFS](https://github.com/seaweedfs/seaweedfs) container started with
  [testcontainers](https://testcontainers-python.readthedocs.io/). They upload
  logs, list them, stream a ZIP and verify every entry round-trips — the same
  flow as production.

```bash
make test-unit   # fast, no Docker
make test-e2e    # needs Docker
make test        # both
```

CI runs the whole thing on every push, with Docker available on the runner so
the SeaweedFS-backed tests execute for real.

## Development

Everything goes through `uv`; the `Makefile` is the entry point:

```bash
make sync        # install all dependencies (including the s3 extra)
make clean       # format then lint (ruff) and type-check (mypy)
make coverage    # run with a coverage report
make docs-serve  # preview the documentation on localhost:8000
make build       # build sdist + wheel
```

Ruff rules (and their intent) are inherited from the author's other projects;
see `[tool.ruff]` in [`pyproject.toml`](pyproject.toml).

## License

WTFPL — do what the fuck you want to. See [LICENSE](LICENSE).
