# LiveZip

**Memory-bound, streamable ZIP64 archives — from any source.**

LiveZip builds a ZIP file as an iterator of byte chunks. It never holds the
archive and never holds a payload: at any moment it keeps only a small descriptor
per file, regardless of how large the files are. Where the bytes come from —
disk, HTTP, memory, an S3 bucket — is a detail, and sources can be **mixed in one
archive**.

- **Predictable size** — the exact archive size is known before a single payload
  byte is read, so it can be announced as `Content-Length`.
- **Bounded memory** — `O(k)` in the number of files, `O(n)` in time; size the
  machine by file count, not by total weight.
- **Any source, mixed** — local files, HTTP URLs, in-memory buffers and S3
  objects, all through the same encoder.

[![PyPI](https://img.shields.io/pypi/v/livezip)](https://pypi.org/project/livezip/)
[![CI](https://github.com/OffworldNexus/livezip/actions/workflows/ci.yml/badge.svg)](https://github.com/OffworldNexus/livezip/actions/workflows/ci.yml)
[![Docs](https://img.shields.io/badge/docs-offworldnexus.github.io-blue)](https://offworldnexus.github.io/livezip/)
[![Python](https://img.shields.io/badge/python-3.11%2B-blue)](https://offworldnexus.github.io/livezip/developer/)
[![License](https://img.shields.io/badge/license-WTFPL-brightgreen)](LICENSE)

## Documentation

**Full documentation: <https://offworldnexus.github.io/livezip/>**

| Guide | What it covers |
| ----- | -------------- |
| [Getting started](https://offworldnexus.github.io/livezip/developer/) | The source-agnostic recipe and the mental model. |
| [Byte streams](https://offworldnexus.github.io/livezip/developer/streams/) | `FileStream`, `UrlStream`, `BytesStream`, `S3Stream`, and the lazy-open contract. |
| [Storage strategies](https://offworldnexus.github.io/livezip/developer/storage/) | `Store`, `DeflateStore`, `PrecompressedDeflate`, and writing your own. |
| [Streaming from S3](https://offworldnexus.github.io/livezip/developer/s3-integration/) | The logs-to-ZIP recipe and the object-metadata contract. |
| [Architecture](https://offworldnexus.github.io/livezip/developer/architecture/) | Modules, complexity, and serving over HTTP. |
| [The ZIP format](https://offworldnexus.github.io/livezip/developer/zip-format/) | The PKZIP/ZIP64 records on the wire. |
| [Encoding pipeline](https://offworldnexus.github.io/livezip/developer/encoding-pipeline/) | Segments, offsets, and the streaming trick. |
| [Command line](https://offworldnexus.github.io/livezip/developer/cli/) | The `livezip` console script. |
| [Testing](https://offworldnexus.github.io/livezip/developer/testing/) | Unit tests and the SeaweedFS end-to-end suite. |
| [Contributing](https://offworldnexus.github.io/livezip/developer/contributing/) | Develop, test and release. |

## Install

LiveZip needs Python 3.11 or newer and is managed with
[uv](https://docs.astral.sh/uv/).

```bash
uv add livezip            # core, zero runtime dependencies
uv add "livezip[s3]"      # with the S3 integration (boto3)
# or: pip install "livezip[s3]"
```

## Quick start

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
predicted size are identical. See
[Getting started](https://offworldnexus.github.io/livezip/developer/) and
[Byte streams](https://offworldnexus.github.io/livezip/developer/streams/).

## Logs in S3 → ZIP

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
[`examples/s3_to_zip.py`](examples/s3_to_zip.py); see the
[S3 guide](https://offworldnexus.github.io/livezip/developer/s3-integration/)
for the metadata contract and operational details.

## Command line

```bash
livezip -o archive.zip photo.jpg video.mp4 notes.txt
livezip -m store -o raw.zip blob.bin                       # copy bytes verbatim
livezip --s3-bucket my-logs --s3-prefix 2026/ -o logs.zip  # bucket of DEFLATE logs
livezip --s3-bucket my-logs > logs.zip                     # stream to stdout
```

## Project layout

```
src/livezip/
├── encode.py     # ZipEncoder + segments (the streaming core)
├── storage.py    # Store / DeflateStore / PrecompressedDeflate
├── stream.py     # FileStream / UrlStream / BytesStream
├── s3.py         # optional S3 integration (boto3 behind a protocol)
├── models.py     # PKZIP/ZIP64 record serialisation
└── cli.py        # the `livezip` console script
tests/            # unit + SeaweedFS-backed end-to-end suites
doc/              # Zensical documentation site
```

## Development

Everything goes through `uv`; the `Makefile` is the entry point:

```bash
make sync        # install all dependencies (including the s3 extra)
make clean       # format then lint (ruff) and type-check (mypy)
make test        # unit + end-to-end (the latter needs Docker)
make coverage    # run with a coverage report
make docs-serve  # preview the documentation on localhost:8000
make build       # build sdist + wheel
```

See [Contributing](https://offworldnexus.github.io/livezip/developer/contributing/)
for the full workflow, the lint rules, and how releases are published.

## License

WTFPL — do what the fuck you want to. See [LICENSE](LICENSE).
