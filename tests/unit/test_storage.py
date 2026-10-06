"""Unit tests for the storage strategies."""

import zlib

import pytest
from helpers import BIG_PAYLOAD

from livezip.models import CompressionMethod
from livezip.storage import DeflateStore, PrecompressedDeflate, Store
from livezip.stream import BytesStream, DataStream


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


def test_store_roundtrip_and_checksum():
    store = Store(BytesStream(BIG_PAYLOAD), len(BIG_PAYLOAD))
    assert store.compression_method is CompressionMethod.uncompressed
    assert store.compressed_size == len(BIG_PAYLOAD)
    assert store.uncompressed_size == len(BIG_PAYLOAD)

    assert b"".join(store.get_data()) == BIG_PAYLOAD
    assert store.crc32 == zlib.crc32(BIG_PAYLOAD)


def test_store_is_replayable_and_resets_the_checksum():
    store = Store(BytesStream(b"hello"), 5)
    assert b"".join(store.get_data()) == b"hello"
    first_crc = store.crc32
    assert b"".join(store.get_data()) == b"hello"
    assert store.crc32 == first_crc


def test_store_handles_fragmenting_streams():
    store = Store(FragmentingStream(b"abcdef"), 6)
    assert b"".join(store.get_data()) == b"abcdef"
    assert store.crc32 == zlib.crc32(b"abcdef")


def test_store_empty_payload():
    store = Store(BytesStream(b""), 0)
    assert b"".join(store.get_data()) == b""
    assert store.crc32 == 0


def test_deflate_store_roundtrip():
    store = DeflateStore(BytesStream(BIG_PAYLOAD), len(BIG_PAYLOAD))
    payload = b"".join(store.get_data())

    assert store.compression_method is CompressionMethod.deflate
    assert len(payload) == store.compressed_size
    assert zlib.decompressobj(-zlib.MAX_WBITS).decompress(payload) == BIG_PAYLOAD
    assert store.crc32 == zlib.crc32(BIG_PAYLOAD)


@pytest.mark.parametrize(
    ("size", "blocks"),
    [(0, 1), (1, 1), (0xFFFF, 1), (0x10000, 2), (0x1FFFE, 2), (0x1FFFF, 3)],
)
def test_deflate_store_block_count(size, blocks):
    store = DeflateStore(BytesStream(b"\x00" * size), size)
    assert store.blocks == blocks
    assert store.compressed_size == blocks * 5 + size


def test_deflate_store_empty_payload_is_valid_deflate():
    store = DeflateStore(BytesStream(b""), 0)
    payload = b"".join(store.get_data())
    assert store.compressed_size == 5
    assert zlib.decompressobj(-zlib.MAX_WBITS).decompress(payload) == b""


def _raw_deflate(data: bytes) -> bytes:
    compressor = zlib.compressobj(9, zlib.DEFLATED, -zlib.MAX_WBITS)
    return compressor.compress(data) + compressor.flush()


def test_precompressed_deflate_passes_bytes_through():
    compressed = _raw_deflate(BIG_PAYLOAD)
    strategy = PrecompressedDeflate(
        BytesStream(compressed),
        compressed_size=len(compressed),
        uncompressed_size=len(BIG_PAYLOAD),
        crc32=zlib.crc32(BIG_PAYLOAD),
    )

    assert strategy.compression_method is CompressionMethod.deflate
    assert strategy.compressed_size == len(compressed)
    assert strategy.uncompressed_size == len(BIG_PAYLOAD)
    assert strategy.crc32 == zlib.crc32(BIG_PAYLOAD)
    assert b"".join(strategy.get_data()) == compressed


def test_precompressed_deflate_rejects_a_length_mismatch():
    compressed = _raw_deflate(b"hello")
    strategy = PrecompressedDeflate(
        BytesStream(compressed),
        compressed_size=len(compressed) + 1,
        uncompressed_size=5,
        crc32=zlib.crc32(b"hello"),
    )

    with pytest.raises(ValueError, match="Expected"):
        b"".join(strategy.get_data())
