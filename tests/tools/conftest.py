"""Fixtures for the tool tests: a fake Home Assistant on a local aiohttp test server.

Nothing here reaches a real system: the server listens on 127.0.0.1 and the clients are
given its URL and a test token explicitly (never HASS_URL / HASS_TOKEN from the environment).
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from pathlib import Path
import sys

from aiohttp.test_utils import TestServer
import pytest

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
for p in (str(REPO), str(HERE)):
    if p not in sys.path:
        sys.path.insert(0, p)

from fake_hass import TOKEN, FakeHass  # noqa: E402

from tools.hass_client import HassClient  # noqa: E402


@pytest.fixture(autouse=True)
def no_real_connection(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Hide any real connection settings and config, and keep tool output in tmp."""
    for key in ("HASS_URL", "HASS_TOKEN", "HASS_CA_FILE"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr("tools.hass_client.ENV_FILE", tmp_path / "no.env")
    monkeypatch.setattr("tools.config.OUT_DIR", tmp_path / "out")
    # an owner's own tools/config.json must not change what the tests see
    monkeypatch.setattr("tools.config.OWNER_FILES", ())


@pytest.fixture
def fake() -> FakeHass:
    return FakeHass()


@pytest.fixture
async def server(fake: FakeHass, socket_enabled: None) -> AsyncGenerator[TestServer]:
    """The fake on 127.0.0.1. Home Assistant's test plugin still lets sockets connect only
    to 127.0.0.1, so a test cannot reach anything else."""
    srv = TestServer(fake.app(), host="127.0.0.1")
    await srv.start_server()
    try:
        yield srv
    finally:
        await srv.close()


@pytest.fixture
def url(server: TestServer) -> str:
    return str(server.make_url("")).rstrip("/")


@pytest.fixture
async def client(url: str) -> AsyncGenerator[HassClient]:
    c = HassClient(url, TOKEN)
    try:
        yield c
    finally:
        await c.close()


@pytest.fixture
def core():
    """The planner core, or a skip until it is integrated (or PLANNER_CORE_PATH is set)."""
    from tools.core import CoreNotFound, load_core

    try:
        return load_core()
    except CoreNotFound as err:
        pytest.skip(str(err))
