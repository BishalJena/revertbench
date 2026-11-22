"""Explicit Docker opt-in; required container checks never silently skip offline."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from docker import DockerClient

_CLIENT: pytest.StashKey[DockerClient] = pytest.StashKey()
_REPRODUCTION_SHARD: pytest.StashKey[tuple[int, int]] = pytest.StashKey()


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--run-docker",
        action="store_true",
        default=False,
        help="run required container checks; fail if Docker is unavailable",
    )

    parser.addoption(
        "--case-reproduction-shard",
        default=None,
        metavar="INDEX/TOTAL",
        help="select one of at most two sorted published-case reproduction shards",
    )


def pytest_configure(config: pytest.Config) -> None:
    value: str | None = config.getoption("--case-reproduction-shard")
    if value is not None:
        from case_reproduction_shards import parse_shard

        try:
            config.stash[_REPRODUCTION_SHARD] = parse_shard(value)
        except ValueError as error:
            raise pytest.UsageError(str(error)) from None


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
    if _REPRODUCTION_SHARD in config.stash:
        from case_reproduction_shards import selected_case_ids

        reproductions = [
            item
            for item in items
            if isinstance(item, pytest.Function)
            and item.path == Path(__file__).resolve().with_name("test_public_cases.py")
            and item.originalname == "test_accepted_case_reproduces_with_its_pinned_grader"
        ]
        selected = selected_case_ids(
            tuple(item.callspec.id for item in reproductions), config.stash[_REPRODUCTION_SHARD]
        )
        deselected = [item for item in reproductions if item.callspec.id not in selected]
        if deselected:
            config.hook.pytest_deselected(items=deselected)
            items[:] = [item for item in items if item not in deselected]
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
