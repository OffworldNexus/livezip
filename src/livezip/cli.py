"""Command-line interface for livezip.

Two modes share one command:

* **Local** — ``livezip -o out.zip file1 file2 ...`` packs local files.
* **S3** — ``livezip --s3-bucket my-logs --s3-prefix 2026/ -o out.zip`` lists a
  bucket of DEFLATE-compressed objects and streams them into an archive without
  downloading them twice.

When ``-o`` is ``-`` (the default), the archive is written to stdout so it can
be piped straight into ``curl``, another process, or a file. Progress messages
always go to stderr, never polluting the archive stream.
"""

import sys
from argparse import ArgumentParser, Namespace
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from . import __version__
from .encode import ZipEncoder, ZipFile, build_archive
from .storage import CompactFile, DeflateStore, Store
from .stream import FileStream

#: Valid values for the ``--method`` option.
METHODS = ("store", "deflate")


def build_parser() -> ArgumentParser:
    """
    Build the argument parser.

    Returns
    -------
    ArgumentParser
        The parser, ready to parse a command line.
    """

    parser = ArgumentParser(
        prog="livezip",
        description=(
            "Stream a ZIP64 archive without ever holding it in memory. Files "
            "are read one at a time and the output size is known before any "
            "payload byte is read."
        ),
    )

    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    parser.add_argument(
        "-o",
        "--output",
        default="-",
        help=("Where to write the archive. Use '-' (the default) to write to stdout."),
    )
    parser.add_argument(
        "-f",
        "--force",
        action="store_true",
        help="Overwrite the output file if it already exists.",
    )
    parser.add_argument(
        "-c",
        "--comment",
        default="",
        help="Archive-level comment.",
    )
    parser.add_argument(
        "-m",
        "--method",
        choices=METHODS,
        default="deflate",
        help=(
            'Storage method for local files. "store" copies bytes verbatim, '
            '"deflate" wraps them in stored DEFLATE blocks.'
        ),
    )
    parser.add_argument(
        "--s3-bucket",
        default=None,
        help=(
            "Read objects from this S3 bucket instead of local files. Each "
            "object must have been uploaded with livezip's size/CRC metadata."
        ),
    )
    parser.add_argument(
        "--s3-prefix",
        default="",
        help="Restrict S3 mode to objects whose key starts with this prefix.",
    )
    parser.add_argument(
        "--s3-endpoint-url",
        default=None,
        help="Custom S3 endpoint URL (SeaweedFS, MinIO, ...).",
    )
    parser.add_argument(
        "--s3-region",
        default=None,
        help="AWS region name for S3 mode.",
    )
    parser.add_argument(
        "files",
        nargs="*",
        help="Local files to pack (ignored in S3 mode).",
    )

    return parser


def _entry_name(path: Path) -> str:
    """
    Normalise a local path for use as a ZIP entry name.

    Files located under the current working directory keep their relative
    path; anything else falls back to its base name so that archives never
    contain an absolute path.

    Parameters
    ----------
    path
        Path as given on the command line.

    Returns
    -------
    str
        A clean, slash-separated, relative entry name.
    """

    try:
        return path.resolve().relative_to(Path.cwd().resolve()).as_posix()
    except ValueError:
        return path.name


def _encoder_from_local(args: Namespace) -> ZipEncoder:
    """
    Build an encoder from local files.

    Parameters
    ----------
    args
        Parsed command-line arguments.

    Returns
    -------
    ZipEncoder
        The prepared encoder.

    Raises
    ------
    ValueError
        If no input file was provided.
    """

    if not args.files:
        msg = "no input files were given"
        raise ValueError(msg)

    files: list[ZipFile] = []

    for raw in args.files:
        path = Path(raw)
        stat = path.stat()
        stream = FileStream(path)
        data: CompactFile

        if args.method == "store":
            data = Store(stream, stat.st_size)
        else:
            data = DeflateStore(stream, stat.st_size)

        files.append(
            ZipFile(
                path=_entry_name(path),
                data=data,
                modification_date=datetime.fromtimestamp(stat.st_mtime, tz=UTC),
                is_binary=True,
            )
        )

    return build_archive(files, args.comment)


def _encoder_from_s3(args: Namespace) -> ZipEncoder:
    """
    Build an encoder from a bucket of DEFLATE objects.

    Parameters
    ----------
    args
        Parsed command-line arguments.

    Returns
    -------
    ZipEncoder
        The prepared encoder.
    """

    from .s3 import create_client, zip_from_s3

    if args.files:
        print("livezip: warning: input files are ignored in S3 mode", file=sys.stderr)

    client = create_client(
        endpoint_url=args.s3_endpoint_url,
        region_name=args.s3_region,
    )

    return zip_from_s3(
        client,
        args.s3_bucket,
        prefix=args.s3_prefix,
        comment=args.comment,
    )


def _write_archive(encoder: ZipEncoder, output: str, *, force: bool) -> None:
    """
    Stream the archive to its destination.

    Parameters
    ----------
    encoder
        The prepared encoder.
    output
        Destination path, or ``-`` for stdout.
    force
        Whether an existing file may be overwritten.

    Raises
    ------
    FileExistsError
        If the destination exists and ``force`` is false.
    """

    if output == "-":
        stream = sys.stdout.buffer
        for chunk in encoder.get_data():
            stream.write(chunk)
        stream.flush()
        return

    destination = Path(output)

    if destination.exists() and not force:
        msg = f"{destination} already exists (use -f/--force to overwrite)"
        raise FileExistsError(msg)

    with destination.open("wb") as handle:
        for chunk in encoder.get_data():
            handle.write(chunk)


def main(argv: Sequence[str] | None = None) -> int:
    """
    Run the command-line interface.

    Parameters
    ----------
    argv
        Arguments to parse. Defaults to ``sys.argv[1:]``.

    Returns
    -------
    int
        ``0`` on success, ``1`` on a handled error.
    """

    args = build_parser().parse_args(argv)

    try:
        if args.s3_bucket:
            encoder = _encoder_from_s3(args)
        else:
            encoder = _encoder_from_local(args)

        print(
            f"livezip: prepared {len(encoder.files)} file(s), "
            f"archive size is {encoder.file_size} bytes",
            file=sys.stderr,
        )

        _write_archive(encoder, args.output, force=args.force)
    except (OSError, ValueError, ImportError) as exc:
        print(f"livezip: error: {exc}", file=sys.stderr)
        return 1

    return 0
