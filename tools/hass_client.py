"""A minimal async Home Assistant client for the owner's test tools (aiohttp only).

Connection: ``HASS_URL`` (for example ``http://homeassistant.local:8123``) and ``HASS_TOKEN``
(a long-lived access token from your HA profile) from the environment, else from the
git-ignored ``tools/.env`` (``KEY=value`` lines). ``HASS_CA_FILE`` names a CA bundle for an
HTTPS instance with its own certificate authority.

REST: ``/api/``, ``/api/config``, ``/api/states``, ``/api/history/period``.
WebSocket: auth, ``recorder/statistics_during_period``, statistics metadata, the entity
registry, ``schedule/list`` and ``call_service``.

``call_service`` refuses unless the client was created with ``allow_service_calls=True``.
Only ``attended_test.py`` does that; every other tool is read-only by construction.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable, Mapping
from datetime import datetime
import json
import os
from pathlib import Path
import ssl
from typing import Any
from urllib.parse import quote

import aiohttp

ENV_FILE = Path(__file__).resolve().parent / ".env"
DEFAULT_ENV = Path("<tools/.env>")  # stands for ENV_FILE, read when called
WS_MAX_MSG = 256 * 1024 * 1024  # statistics for weeks of 5-min rows are large


class HassError(Exception):
    """Home Assistant answered with an error, or could not be reached."""


class HassAuthError(HassError):
    """The token was refused."""


class ServiceCallRefused(HassError):
    """A service call from a client that was not created to make them."""


def read_env_file(path: Path) -> dict[str, str]:
    """``KEY=value`` lines; ``#`` comments, ``export`` prefixes and quotes are allowed."""
    out: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return out
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.removeprefix("export ").partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        out[key.strip()] = value
    return out


def connection_settings(
    env: Mapping[str, str] | None = None, env_file: Path | None = DEFAULT_ENV
) -> dict[str, str]:
    """URL, token and CA file; the environment wins over the file (None: no file)."""
    if env_file == DEFAULT_ENV:
        env_file = ENV_FILE
    merged = dict(read_env_file(env_file)) if env_file is not None else {}
    environ = os.environ if env is None else env
    merged.update({k: v for k, v in environ.items() if k.startswith("HASS_") and v})
    if not merged.get("HASS_URL") or not merged.get("HASS_TOKEN"):
        where = f" or {env_file}" if env_file is not None else ""
        raise HassError(
            f"HASS_URL and HASS_TOKEN are needed (environment{where}); "
            "create a long-lived access token in your Home Assistant profile"
        )
    return merged


def iso(t: datetime) -> str:
    return t.isoformat()


class HassClient:
    """One connection to Home Assistant: REST calls, plus one WebSocket opened on demand."""

    def __init__(
        self,
        url: str,
        token: str,
        *,
        allow_service_calls: bool = False,
        timeout: float = 300.0,
        ca_file: str | None = None,
        session: aiohttp.ClientSession | None = None,
    ) -> None:
        self.url = url.rstrip("/")
        self._token = token
        self.allow_service_calls = allow_service_calls
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        self._ssl: ssl.SSLContext | None = (
            ssl.create_default_context(cafile=ca_file) if ca_file else None
        )
        self._session = session
        self._own_session = session is None
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._ws_lock = asyncio.Lock()
        self._next_id = 1
        self.service_calls: list[dict[str, Any]] = []  # what this client sent, in order

    @classmethod
    def from_env(
        cls,
        *,
        env: Mapping[str, str] | None = None,
        env_file: Path | None = DEFAULT_ENV,
        **kw: Any,
    ) -> HassClient:
        s = connection_settings(env, env_file)
        return cls(s["HASS_URL"], s["HASS_TOKEN"], ca_file=s.get("HASS_CA_FILE"), **kw)

    def __repr__(self) -> str:  # never shows the token
        return f"HassClient({self.url!r}, service_calls={self.allow_service_calls})"

    async def __aenter__(self) -> HassClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    @property
    def session(self) -> aiohttp.ClientSession:
        if self._session is None:
            self._session = aiohttp.ClientSession(timeout=self._timeout)
        return self._session

    async def close(self) -> None:
        if self._ws is not None:
            await self._ws.close()
            self._ws = None
        if self._own_session and self._session is not None:
            await self._session.close()
            self._session = None

    # ------------------------------------------------------------------ REST

    async def _get(self, path: str, params: Mapping[str, str] | None = None) -> Any:
        headers = {"Authorization": f"Bearer {self._token}"}
        try:
            async with self.session.get(
                f"{self.url}{path}", params=params, headers=headers, ssl=self._ssl or True
            ) as resp:
                if resp.status == 401:
                    raise HassAuthError("Home Assistant refused the token (401)")
                if resp.status == 404:
                    return None
                if resp.status >= 400:
                    text = (await resp.text())[:200]
                    raise HassError(f"GET {path} failed: {resp.status} {text}")
                return await resp.json()
        except aiohttp.ClientError as err:
            raise HassError(f"cannot reach Home Assistant at {self.url}: {err}") from err
        except TimeoutError as err:
            raise HassError(f"GET {path} timed out") from err

    async def api_status(self) -> dict[str, Any]:
        return await self._get("/api/") or {}

    async def config(self) -> dict[str, Any]:
        """``/api/config``: version, time zone, units (and location, which tools drop)."""
        return await self._get("/api/config") or {}

    async def states(self) -> list[dict[str, Any]]:
        return await self._get("/api/states") or []

    async def state(self, entity_id: str) -> dict[str, Any] | None:
        return await self._get(f"/api/states/{quote(entity_id)}")

    async def states_of(self, entity_ids: Iterable[str]) -> dict[str, dict[str, Any] | None]:
        """Current state of each entity (None where it does not exist), in one request."""
        wanted = set(entity_ids)
        found = {s["entity_id"]: s for s in await self.states() if s.get("entity_id") in wanted}
        return {eid: found.get(eid) for eid in sorted(wanted)}

    async def history(
        self,
        entity_ids: Iterable[str],
        start: datetime,
        end: datetime | None = None,
        *,
        minimal: bool = True,
        no_attributes: bool = False,
        attribute_changes: bool = False,
    ) -> dict[str, list[dict[str, Any]]]:
        """State history per entity over [start, end] (the state at ``start`` included).

        ``minimal``: entries after the first carry only ``state`` and ``last_changed``.
        ``no_attributes``: no attributes at all (much smaller).
        ``attribute_changes``: also changes of attributes alone (HA leaves them out by
        default, keeping only "significant" changes)."""
        ids = sorted(set(entity_ids))
        if not ids:
            return {}
        params = {"filter_entity_id": ",".join(ids)}
        if end is not None:
            params["end_time"] = iso(end)
        if minimal:
            params["minimal_response"] = ""
        if no_attributes:
            params["no_attributes"] = ""
        if attribute_changes:
            params["significant_changes_only"] = "0"
        data = await self._get(f"/api/history/period/{quote(iso(start))}", params) or []
        out: dict[str, list[dict[str, Any]]] = {eid: [] for eid in ids}
        for rows in data:
            if not rows:
                continue
            eid = rows[0].get("entity_id")
            if eid in out:
                out[eid] = [dict(r, entity_id=eid) for r in rows]
        return out

    # ------------------------------------------------------------------ WebSocket

    def _ws_url(self) -> str:
        if self.url.startswith("https://"):
            return "wss://" + self.url.removeprefix("https://") + "/api/websocket"
        return "ws://" + self.url.removeprefix("http://") + "/api/websocket"

    async def _connect(self) -> aiohttp.ClientWebSocketResponse:
        if self._ws is not None and not self._ws.closed:
            return self._ws
        try:
            ws = await self.session.ws_connect(
                self._ws_url(), max_msg_size=WS_MAX_MSG, ssl=self._ssl or True
            )
        except aiohttp.ClientError as err:
            raise HassError(f"cannot open the WebSocket at {self.url}: {err}") from err
        try:
            hello = await self._receive(ws)
            if hello.get("type") != "auth_required":
                raise HassError(f"unexpected WebSocket greeting: {hello.get('type')}")
            await ws.send_json({"type": "auth", "access_token": self._token})
            answer = await self._receive(ws)
        except HassError:
            await ws.close()
            raise
        if answer.get("type") == "auth_invalid":
            await ws.close()
            raise HassAuthError("Home Assistant refused the token (WebSocket)")
        if answer.get("type") != "auth_ok":
            await ws.close()
            raise HassError(f"unexpected WebSocket auth answer: {answer.get('type')}")
        self._ws = ws
        return ws

    async def _receive(self, ws: aiohttp.ClientWebSocketResponse) -> dict[str, Any]:
        try:
            msg = await ws.receive(timeout=self._timeout.total)
        except TimeoutError as err:
            raise HassError("Home Assistant did not answer on the WebSocket") from err
        if msg.type is not aiohttp.WSMsgType.TEXT:
            raise HassError("the WebSocket closed")
        try:
            data = json.loads(msg.data)
        except ValueError as err:
            raise HassError("Home Assistant sent a message that is not JSON") from err
        return data if isinstance(data, dict) else {}

    async def ws_command(self, message: Mapping[str, Any]) -> Any:
        """Send one command and return its result (HassError when it fails)."""
        if message.get("type") == "call_service" and not self.allow_service_calls:
            raise ServiceCallRefused("this client was not created to call services")
        async with self._ws_lock:
            ws = await self._connect()
            msg_id = self._next_id
            self._next_id += 1
            await ws.send_json({**message, "id": msg_id})
            while True:
                try:
                    reply = await self._receive(ws)
                except HassError:
                    self._ws = None
                    raise
                if reply.get("id") != msg_id or reply.get("type") != "result":
                    continue
                if not reply.get("success"):
                    err = reply.get("error") or {}
                    raise HassError(
                        f"{message.get('type')} failed: {err.get('code')} {err.get('message')}"
                    )
                return reply.get("result")

    async def statistics_during_period(
        self,
        statistic_ids: Iterable[str],
        start: datetime,
        end: datetime,
        *,
        period: str = "hour",
        types: Iterable[str] = ("mean", "min", "max"),
    ) -> dict[str, list[dict[str, Any]]]:
        """Recorder statistics rows per id: ``start`` / ``end`` (epoch ms) and the types.
        ``period``: 5minute (kept about 10 days), hour (kept for good), day, week, month."""
        result = await self.ws_command(
            {
                "type": "recorder/statistics_during_period",
                "start_time": iso(start),
                "end_time": iso(end),
                "statistic_ids": sorted(set(statistic_ids)),
                "period": period,
                "types": list(types),
            }
        )
        return result or {}

    async def statistics_metadata(self, statistic_ids: Iterable[str]) -> list[dict[str, Any]]:
        result = await self.ws_command(
            {"type": "recorder/get_statistics_metadata", "statistic_ids": sorted(statistic_ids)}
        )
        return result or []

    async def registry_entry(self, entity_id: str) -> dict[str, Any] | None:
        """The entity registry entry (platform, unique id, disabled_by), None if absent."""
        try:
            return await self.ws_command(
                {"type": "config/entity_registry/get", "entity_id": entity_id}
            )
        except HassError as err:
            if "not_found" in str(err):
                return None
            raise

    async def schedule_helpers(self) -> list[dict[str, Any]]:
        """Every Schedule helper made in the UI, with its weekly blocks (``schedule/list``)."""
        return await self.ws_command({"type": "schedule/list"}) or []

    async def schedule_config(self, entity_id: str) -> dict[str, Any] | None:
        """The weekly config of one Schedule helper, matched through the entity registry."""
        entry = await self.registry_entry(entity_id)
        uid = (entry or {}).get("unique_id")
        object_id = entity_id.partition(".")[2]
        for item in await self.schedule_helpers():
            if item.get("id") in (uid, object_id):
                return item
        return None

    async def call_service(
        self,
        domain: str,
        service: str,
        data: Mapping[str, Any] | None = None,
        *,
        target: Mapping[str, Any] | None = None,
    ) -> Any:
        """Call a service; refused unless this client allows service calls."""
        if not self.allow_service_calls:
            raise ServiceCallRefused(
                f"refusing {domain}.{service}: this tool is read-only "
                "(only attended_test.py may call services)"
            )
        msg: dict[str, Any] = {
            "type": "call_service",
            "domain": domain,
            "service": service,
            "service_data": dict(data or {}),
        }
        if target:
            msg["target"] = dict(target)
        self.service_calls.append(
            {"domain": domain, "service": service, "data": dict(data or {}), "target": target}
        )
        return await self.ws_command(msg)
