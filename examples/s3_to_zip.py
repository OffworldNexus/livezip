"""Live-zip a bucket of DEFLATE-compressed logs.

This is the flagship example, written as a copy-pasteable script. It assumes the
bucket was populated by an uploader that stores each log as a raw DEFLATE object
and records its original size and CRC32 in user metadata — which is exactly what
:func:`livezip.s3.put_deflate_object` does.

Run it against any S3-compatible endpoint (SeaweedFS, MinIO, AWS):

    uv run --extra s3 python examples/s3_to_zip.py \
        --bucket my-logs --prefix 2026/ --output logs.zip

With ``--output -`` (the default) the archive is written to stdout, so it can be
piped straight into another process:

    uv run --extra s3 python examples/s3_to_zip.py --bucket my-logs --prefix 2026/ > logs.zip
"""

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from livezip.s3 import create_client, list_deflate_objects, zip_from_s3


def build_parser() -> argparse.ArgumentParser:
    """
    Build the argument parser.

    Returns
    -------
    argparse.ArgumentParser
        The parser.
    """

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bucket", required=True, help="Bucket to read from.")
    parser.add_argument(
        "--prefix",
        default="",
        help="Only include objects whose key starts with this prefix.",
    )
    parser.add_argument(
        "--output",
        default="-",
        help="Destination path, or '-' for stdout.",
    )
    parser.add_argument(
        "--comment",
        default="",
        help="Archive-level comment.",
    )
    parser.add_argument(
        "--endpoint-url",
        default=None,
        help="Custom S3 endpoint URL.",
    )
    parser.add_argument("--region", default=None, help="AWS region.")
    parser.add_argument(
        "--list-only",
        action="store_true",
        help="Only print the object catalogue, do not build an archive.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """
    Run the example.

    Parameters
    ----------
    argv
        Arguments to parse.

    Returns
    -------
    int
        Process exit code.
    """

    args = build_parser().parse_args(argv)

    client = create_client(endpoint_url=args.endpoint_url, region_name=args.region)

    objects = list_deflate_objects(client, args.bucket, args.prefix)
    total = sum(obj.uncompressed_size for obj in objects)

    for obj in objects:
        print(
            f"{obj.key}\t{obj.compressed_size} compressed\t"
            f"{obj.uncompressed_size} raw\t{obj.compression_ratio:.2%}",
            file=sys.stderr,
        )

    print(
        f"{len(objects)} object(s), {total} raw bytes",
        file=sys.stderr,
    )

    if args.list_only:
        return 0

    encoder = zip_from_s3(client, args.bucket, prefix=args.prefix, comment=args.comment)
    print(f"archive will be {encoder.file_size} bytes", file=sys.stderr)

    if args.output == "-":
        handle = sys.stdout.buffer
        for chunk in encoder.get_data():
            handle.write(chunk)
        handle.flush()
    else:
        with Path(args.output).open("wb") as handle:
            for chunk in encoder.get_data():
                handle.write(chunk)

    return 0


if __name__ == "__main__":
    sys.exit(main())
