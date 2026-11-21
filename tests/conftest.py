"""Explicit Docker opt-in; required container checks never silently skip offline."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from docker import DockerClient

_CLIENT: pytest.StashKey[DockerClient] = pytest.StashKey()


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--run-docker",
        action="store_true",
        default=False,
        help="run required container checks; fail if Docker is unavailable",
    )


def pytest_sessionstart(session: pytest.Session) -> None:
    if not session.config.getoption("--run-docker"):
        return
    import docker
    from docker.errors import DockerException
    from requests.exceptions import RequestException

    client: DockerClient | None = None
    try:
        client = docker.from_env(timeout=10)
        client.ping()
    except (DockerException, RequestException, OSError, ValueError):
        if client is not None:
            client.close()
        raise pytest.UsageError("Docker must be reachable when --run-docker is requested") from None
    session.config.stash[_CLIENT] = client


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    if _CLIENT in session.config.stash:
        session.config.stash[_CLIENT].close()


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if config.getoption("--run-docker"):
        return
    opt_in = pytest.mark.skip(reason="requires explicit --run-docker")
    for item in items:
        if item.get_closest_marker("docker") is not None:
            item.add_marker(opt_in)


@pytest.fixture(scope="session")
def docker_client(pytestconfig: pytest.Config) -> DockerClient:
    if _CLIENT not in pytestconfig.stash:
        pytest.fail("docker_client requires --run-docker; mark container tests with docker")
    return pytestconfig.stash[_CLIENT]


@pytest.fixture(scope="session")
def worker_image(docker_client: DockerClient) -> str:
    # Import only on opted-in fixture use: ordinary unit collection needs no image.
    from revertbench.sandbox.image import build_worker_image

    return build_worker_image(docker_client)
