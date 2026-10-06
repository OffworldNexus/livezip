"""Unit tests for the command-line interface."""

import io
import sys
import zipfile
from pathlib import Path

import pytest
from helpers import read_archive

from livezip.cli import main


def _write(path: Path, data: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def test_local_roundtrip(tmp_path):
    first = _write(tmp_path / "a.txt", b"hello")
    out = tmp_path / "out.zip"

    assert main(["-o", str(out), str(first)]) == 0

    archive = read_archive(out.read_bytes())
    assert archive.read("a.txt") == b"hello"


def test_multiple_files_and_comment(tmp_path):
    first = _write(tmp_path / "a.txt", b"first")
    second = _write(tmp_path / "b.bin", b"second")
    out = tmp_path / "out.zip"

    assert main(["-c", "my comment", "-o", str(out), str(first), str(second)]) == 0

    archive = read_archive(out.read_bytes())
    assert sorted(archive.namelist()) == ["a.txt", "b.bin"]
    assert archive.read("a.txt") == b"first"
    assert archive.comment == b"my comment"


def test_store_method(tmp_path):
    source = _write(tmp_path / "a.txt", b"stored")
    out = tmp_path / "out.zip"

    assert main(["-m", "store", "-o", str(out), str(source)]) == 0

    archive = read_archive(out.read_bytes())
    assert archive.getinfo("a.txt").compress_type == zipfile.ZIP_STORED


def test_refuses_to_overwrite_without_force(tmp_path, capsys):
    source = _write(tmp_path / "a.txt", b"hello")
    out = tmp_path / "out.zip"
    out.write_bytes(b"keep me")

    assert main(["-o", str(out), str(source)]) == 1
    assert out.read_bytes() == b"keep me"
    assert "already exists" in capsys.readouterr().err


def test_force_overwrites(tmp_path):
    source = _write(tmp_path / "a.txt", b"hello")
    out = tmp_path / "out.zip"
    out.write_bytes(b"old")

    assert main(["-f", "-o", str(out), str(source)]) == 0
    assert read_archive(out.read_bytes()).read("a.txt") == b"hello"


def test_no_files_is_an_error(tmp_path, capsys):
    assert main(["-o", str(tmp_path / "out.zip")]) == 1
    assert "no input files" in capsys.readouterr().err


def test_missing_file_is_an_error(tmp_path, capsys):
    out = tmp_path / "out.zip"
    assert main(["-o", str(out), str(tmp_path / "nope.txt")]) == 1
    assert "error" in capsys.readouterr().err


def test_stdout_output(tmp_path, monkeypatch):
    source = _write(tmp_path / "a.txt", b"to stdout")

    class FakeStdout:
        def __init__(self):
            self.buffer = io.BytesIO()

    fake = FakeStdout()
    monkeypatch.setattr(sys, "stdout", fake)

    assert main([str(source)]) == 0
    assert read_archive(fake.buffer.getvalue()).read("a.txt") == b"to stdout"


def test_version_flag_exits_cleanly():
    with pytest.raises(SystemExit) as excinfo:
        main(["--version"])
    assert excinfo.value.code == 0
