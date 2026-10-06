"""Fixtures for the SeaweedFS-backed end-to-end tests.

A single throwaway SeaweedFS container is started for the whole session (once
Docker is confirmed reachable) and every test gets its own bucket so parallel
runs and re-runs cannot interfere. This mirrors the way CI exercises the real
S3 code path instead of mocking boto3.
"""

import uuid
from collections.abc import Iterator
from typing import Any

import boto3
import pytest
from botocore.config import Config

#: Pinned SeaweedFS image so the tests are reproducible.
SEAWEEDFS_IMAGE = "chrislusf/seaweedfs:4.47"

#: Port the container exposes its S3 gateway on.
S3_PORT = 8333


def _docker_available() -> bool:
    """
    Check whether a usable Docker daemon is reachable.

    Returns
    -------
    bool
        ``True`` when the ``docker`` SDK can ping a daemon.
    """

    try:
        import docker
    except ImportError:
        return False

    try:
        docker.from_env().ping()
    except Exception:
        return False

    return True


@pytest.fixture(scope="session")
def seaweedfs_endpoint() -> Iterator[str]:
    """
    Start a SeaweedFS container and yield its S3 endpoint.

    Yields
    ------
    str
        Base URL of the S3 gateway, e.g. ``http://127.0.0.1:49153``.
    """

    if not _docker_available():
        pytest.skip("Docker is not available, skipping SeaweedFS end-to-end tests")

    from testcontainers.core.container import DockerContainer
    from testcontainers.core.wait_strategies import HttpWaitStrategy

    container = (
        DockerContainer(SEAWEEDFS_IMAGE)
        .with_command(f"server -s3 -s3.port={S3_PORT}")
        .with_exposed_ports(S3_PORT)
        .waiting_for(HttpWaitStrategy(S3_PORT).for_status_code(200))
    )

    with container:
        host = container.get_container_host_ip()
        port = container.get_exposed_port(S3_PORT)
        yield f"http://{host}:{port}"


@pytest.fixture(scope="session")
def s3_client(seaweedfs_endpoint: str) -> Any:
    """
    Build a boto3 client pointed at the throwaway SeaweedFS.

    Parameters
    ----------
    seaweedfs_endpoint
        Endpoint from the container fixture.

    Returns
    -------
    Any
        A configured boto3 S3 client.
    """

    return boto3.client(
        "s3",
        endpoint_url=seaweedfs_endpoint,
        aws_access_key_id="livezip",
        aws_secret_access_key="livezip-secret",
        region_name="us-east-1",
        config=Config(s3={"addressing_style": "path"}),
    )


@pytest.fixture
def bucket(s3_client: Any) -> Iterator[tuple[Any, str]]:
    """
    Create a uniquely named bucket for a single test and clean it up.

    Parameters
    ----------
    s3_client
        Client from the session fixture.

    Yields
    ------
    tuple
        The client and the bucket name.
    """

    name = f"livezip-{uuid.uuid4().hex[:12]}"
    s3_client.create_bucket(Bucket=name)

    yield s3_client, name

    response = s3_client.list_objects_v2(Bucket=name)
    for obj in response.get("Contents", []):
        s3_client.delete_object(Bucket=name, Key=obj["Key"])
    s3_client.delete_bucket(Bucket=name)
