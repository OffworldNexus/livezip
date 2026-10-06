"""Unit tests for the byte-stream abstractions."""

from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import pytest
from helpers import BIG_PAYLOAD

from livezip.stream import BytesStream, DataStream, FileStream, UrlStream, iter_chunks


class FragmentingStream(DataStream):
    """A stream that deliberately returns one byte at a time."""

    def __init__(self, data: bytes):
        self.data = data
        self.offset = 0

    def open(self):
        self.offset = 0

    def read(self, length):
        if self.offset >= len(self.data):
            return b""
        chunk = self.data[self.offset : self.offset + 1]
        self.offset += 1
        return chunk

    def close(self):
        pass


def test_bytes_stream_rewinds_on_open():
    stream = BytesStream(b"abcdef")

    stream.open()
    assert stream.read(3) == b"abc"
    assert stream.read(10) == b"def"

    stream.open()
    assert stream.read(2) == b"ab"


def test_read_exact_fills_the_buffer():
    stream = FragmentingStream(b"abcdef")
    assert stream.read_exact(4) == b"abcd"


def test_read_exact_stops_at_end_of_stream():
    stream = FragmentingStream(b"ab")
    assert stream.read_exact(4) == b"ab"


def test_file_stream(tmp_path):
    path = tmp_path / "payload.bin"
    path.write_bytes(BIG_PAYLOAD)

    stream = FileStream(path)
    stream.open()
    assert stream.read_exact(len(BIG_PAYLOAD)) == BIG_PAYLOAD
    stream.close()

    with pytest.raises(RuntimeError, match="not open"):
        stream.read(1)


def test_file_stream_accepts_string_paths(tmp_path):
    path = tmp_path / "payload.bin"
    path.write_bytes(b"hello")

    stream = FileStream(str(path))
    stream.open()
    assert stream.read_exact(5) == b"hello"
    stream.close()


def test_iter_chunks():
    assert list(iter_chunks(b"abcdef", 4)) == [b"abcd", b"ef"]
    assert list(iter_chunks(b"", 4)) == []


class _Handler(BaseHTTPRequestHandler):
    body = BIG_PAYLOAD

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Length", str(len(self.body)))
        self.end_headers()
        self.wfile.write(self.body)

    def log_message(self, fmt, *args):
        pass


@pytest.fixture
def http_server() -> Iterator[str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()

    host, port = server.server_address
    yield f"http://{host}:{port}/"

    server.shutdown()
    server.server_close()


def test_url_stream(http_server):
    stream = UrlStream(lambda: http_server)
    stream.open()
    assert stream.read_exact(len(BIG_PAYLOAD)) == BIG_PAYLOAD
    stream.close()

    with pytest.raises(RuntimeError, match="not open"):
        stream.read(1)


def test_url_stream_resolves_the_url_lazily():
    calls: list[int] = []
    UrlStream(lambda: calls.append(1) or "http://127.0.0.1:1/")
    assert calls == []
