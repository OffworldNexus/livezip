# LiveZip

**Memory-bound, streamable ZIP64 archives — from any source.**

LiveZip builds a ZIP file as an iterator of byte chunks. It never holds the
archive and never holds a payload: at any moment it keeps only a small descriptor
per file, regardless of how large the files are. Where the bytes come from —
disk, HTTP, memory, S3 — is a detail, and sources can be mixed in one archive.

```python
from livezip import FileStream, Store, ZipEncoder, ZipFile

path = "clip.mp4"
files = [ZipFile("clip.mp4", Store(FileStream(path), size), modified, is_binary=True)]

encoder = ZipEncoder(files)
encoder.prepare()
print(encoder.file_size)  # exact size, before any byte is read
for chunk in encoder.get_data():  # stream it straight out
    ...
```

Swapping `FileStream` for `UrlStream`, `BytesStream` or `S3Stream` is the only
change needed to serve a different backend.

## Properties

- **Predictable size** — the exact archive size is known before a single payload
  byte is read, so it can be announced as `Content-Length`.
- **Bounded memory** — `O(k)` in the number of files, `O(n)` in time; size the
  machine by file count, not by total weight.
- **Any source, mixed** — local files, HTTP URLs, in-memory buffers and S3
  objects, all through the same encoder.

## Documentation

The docs target **developers integrating or maintaining LiveZip**.

| Page | What you will find |
| ---- | ------------------ |
| [Getting started](developer/index.md) | Install, the source-agnostic recipe, the mental model. |
| [Byte streams](developer/streams.md) | Sources and the lazy-open contract. |
| [Storage strategies](developer/storage.md) | How payloads are laid out. |
| [Streaming from S3](developer/s3-integration.md) | The logs-to-ZIP recipe. |
| [Architecture](developer/architecture.md) | Modules, complexity, HTTP serving. |
| [The ZIP format](developer/zip-format.md) | The bytes on the wire. |
| [Encoding pipeline](developer/encoding-pipeline.md) | Segments and offsets. |
| [Command line](developer/cli.md) | The `livezip` console script. |
| [Testing](developer/testing.md) | Unit tests and the SeaweedFS end-to-end suite. |
| [Contributing](developer/contributing.md) | Develop, test and ship changes. |
