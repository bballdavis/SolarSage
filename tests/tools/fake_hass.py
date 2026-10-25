"""A fake Home Assistant for the tool tests: REST and WebSocket on an aiohttp test server.

It serves ``/api/``, ``/api/config``, ``/api/states``, ``/api/history/period`` and the
WebSocket commands the tools use (auth, recorder statistics and metadata, the entity
registry, ``schedule/list``, ``call_service``). Every request and service call is logged so
tests can prove a tool stayed read-only. Nothing here reaches a real system.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from aiohttp import WSMsgType, web

TOKEN = "test-long-lived-token"


def ts(t: datetime) -> str:
    return t.astimezone(UTC).isoformat()


class FakeHass:
    """In-memory HA state. ``on_service(domain, service, data, target)`` may change states."""

    def __init__(self, token: str = TOKEN, time_zone: str = "America/Los_Angeles") -> None:
        self.token = token
        self.config = {
            "version": "2026.10.3",
            "time_zone": time_zone,
            "unit_system": {"length": "mi", "temperature": "°F"},
            # sensitive fields a real instance returns; the tools must drop them
            "latitude": 32.87336,
            "longitude": -117.22743,
            "location_name": "Test Home",
        }
        self.states: dict[str, dict[str, Any]] = {}
        self.history: dict[str, list[dict[str, Any]]] = {}
        self.statistics: dict[str, dict[str, list[dict[str, Any]]]] = {"5minute": {}, "hour": {}}
        self.stat_units: dict[str, str] = {}
        self.registry: dict[str, dict[str, Any]] = {}
        self.schedules: list[dict[str, Any]] = []
        self.requests: list[str] = []
        self.ws_messages: list[dict[str, Any]] = []
        self.service_calls: list[dict[str, Any]] = []
        self.on_service: Callable[[str, str, dict, dict | None], None] | None = None
        self.on_poll: Callable[[], None] | None = None  # runs before states are served
        self.clock: Callable[[], datetime] = lambda: datetime.now(UTC)

    # ------------------------------------------------------------------ state helpers

    def set_state(
        self,
        entity_id: str,
        state: Any,
        attributes: dict[str, Any] | None = None,
        *,
        when: datetime | None = None,
        record: bool = True,
    ) -> None:
        """Set a state; an unchanged state keeps its old last_changed (as HA does)."""
        when = when or self.clock()
        old = self.states.get(entity_id)
        attrs = dict(attributes if attributes is not None else (old or {}).get("attributes", {}))
        changed = old is None or old["state"] != str(state)
        new = {
            "entity_id": entity_id,
            "state": str(state),
            "attributes": attrs,
            "last_changed": ts(when) if changed else old["last_changed"],
            "last_updated": ts(when)
            if changed or attrs != old["attributes"]
            else old["last_updated"],
            "last_reported": ts(when) if changed else old.get("last_reported", old["last_changed"]),
            "context": {"id": "01TEST", "parent_id": None, "user_id": "test-user-id"},
        }
        self.states[entity_id] = new
        if record and (changed or attrs != (old or {}).get("attributes")):
            self.history.setdefault(entity_id, []).append(dict(new))

    def remove(self, entity_id: str) -> None:
        self.states.pop(entity_id, None)

    def register(self, entity_id: str, platform: str, unique_id: str | None = None) -> None:
        self.registry[entity_id] = {
            "entity_id": entity_id,
            "platform": platform,
            "unique_id": unique_id or entity_id.partition(".")[2],
            "disabled_by": None,
        }

    # ------------------------------------------------------------------ the app

    def app(self) -> web.Application:
        app = web.Application()
        app.router.add_get("/api/", self._api)
        app.router.add_get("/api/config", self._config)
        app.router.add_get("/api/states", self._states)
        app.router.add_get("/api/states/{entity_id}", self._state)
        app.router.add_get("/api/history/period/{start}", self._history)
        app.router.add_post("/api/services/{domain}/{service}", self._rest_service)
        app.router.add_get("/api/websocket", self._websocket)
        return app

    def _authorized(self, request: web.Request) -> bool:
        self.requests.append(f"{request.method} {request.path}")
        return request.headers.get("Authorization") == f"Bearer {self.token}"

    async def _api(self, request: web.Request) -> web.Response:
        if not self._authorized(request):
            return web.Response(status=401)
        return web.json_response({"message": "API running."})

    async def _config(self, request: web.Request) -> web.Response:
        if not self._authorized(request):
            return web.Response(status=401)
        return web.json_response(self.config)

    async def _states(self, request: web.Request) -> web.Response:
        if not self._authorized(request):
            return web.Response(status=401)
        if self.on_poll is not None:
            self.on_poll()
        return web.json_response(list(self.states.values()))

    async def _state(self, request: web.Request) -> web.Response:
        if not self._authorized(request):
            return web.Response(status=401)
        st = self.states.get(request.match_info["entity_id"])
        if st is None:
            return web.json_response({"message": "Entity not found."}, status=404)
        return web.json_response(st)

    async def _history(self, request: web.Request) -> web.Response:
        if not self._authorized(request):
            return web.Response(status=401)
        start = datetime.fromisoformat(request.match_info["start"])
        q = request.query
        end = datetime.fromisoformat(q["end_time"]) if "end_time" in q else self.clock()
        minimal = "minimal_response" in q
        no_attr = "no_attributes" in q
        significant = q.get("significant_changes_only", "1") != "0"
        out = []
        for eid in q.get("filter_entity_id", "").split(","):
            rows = self.history.get(eid, [])
            before = [r for r in rows if datetime.fromisoformat(r["last_updated"]) <= start]
            inside = [r for r in rows if start < datetime.fromisoformat(r["last_updated"]) <= end]
            series = []
            if before:  # the state at the start, stamped with the start (as HA does)
                first = dict(before[-1], last_changed=ts(start), last_updated=ts(start))
                series.append(first)
            # by default HA keeps only changes of the state itself
            series += [
                dict(r) for r in inside if not significant or r["last_changed"] == r["last_updated"]
            ]
            if not series:
                continue
            shaped = []
            for i, r in enumerate(series):
                r.pop("context", None)
                if no_attr:
                    r.pop("attributes", None)
                if minimal and 0 < i < len(series) - 1:
                    r = {"state": r["state"], "last_changed": r["last_changed"]}
                shaped.append(r)
            out.append(shaped)
        return web.json_response(out)

    async def _rest_service(self, request: web.Request) -> web.Response:
        self._authorized(request)
        return web.json_response({"message": "REST service calls are not used"}, status=405)

    # ------------------------------------------------------------------ WebSocket

    async def _websocket(self, request: web.Request) -> web.WebSocketResponse:
        self.requests.append("GET /api/websocket")
        ws = web.WebSocketResponse(max_msg_size=0)
        await ws.prepare(request)
        await ws.send_json({"type": "auth_required", "ha_version": self.config["version"]})
        msg = await ws.receive()
        if msg.type is not WSMsgType.TEXT:
            await ws.close()
            return ws
        auth = msg.json()
        if auth.get("type") != "auth" or auth.get("access_token") != self.token:
            await ws.send_json({"type": "auth_invalid", "message": "Invalid access token"})
            await ws.close()
            return ws
        await ws.send_json({"type": "auth_ok", "ha_version": self.config["version"]})
        async for msg in ws:
            if msg.type is not WSMsgType.TEXT:
                break
            cmd = msg.json()
            self.ws_messages.append(cmd)
            # an unrelated event first: clients must wait for their own result
            await ws.send_json({"id": 0, "type": "event", "event": {"event_type": "noise"}})
            try:
                result = self._command(cmd)
            except LookupError as err:
                await ws.send_json(
                    {
                        "id": cmd["id"],
                        "type": "result",
                        "success": False,
                        "error": {"code": str(err.args[0]), "message": str(err.args[-1])},
                    }
                )
                continue
            await ws.send_json(
                {"id": cmd["id"], "type": "result", "success": True, "result": result}
            )
        return ws

    def _command(self, cmd: dict[str, Any]) -> Any:
        kind = cmd.get("type")
        if kind == "recorder/statistics_during_period":
            start = datetime.fromisoformat(cmd["start_time"]).timestamp() * 1000
            end = datetime.fromisoformat(cmd["end_time"]).timestamp() * 1000
            table = self.statistics.get(cmd.get("period", "hour"), {})
            types = set(cmd.get("types") or ("mean", "min", "max"))
            out = {}
            for sid in cmd["statistic_ids"]:
                rows = [
                    {k: v for k, v in r.items() if k in ("start", "end") or k in types}
                    for r in table.get(sid, [])
                    if start <= r["start"] < end
                ]
                if rows:
                    out[sid] = rows
            return out
        if kind == "recorder/get_statistics_metadata":
            return [
                {"statistic_id": sid, "statistics_unit_of_measurement": unit, "has_mean": True}
                for sid, unit in self.stat_units.items()
                if sid in cmd.get("statistic_ids", ())
            ]
        if kind == "config/entity_registry/get":
            entry = self.registry.get(cmd["entity_id"])
            if entry is None:
                raise LookupError("not_found", "Entity not found")
            return entry
        if kind == "schedule/list":
            return self.schedules
        if kind == "call_service":
            call = {
                "domain": cmd["domain"],
                "service": cmd["service"],
                "data": cmd.get("service_data", {}),
                "target": cmd.get("target"),
            }
            self.service_calls.append(call)
            if self.on_service is not None:
                self.on_service(call["domain"], call["service"], call["data"], call["target"])
            return {"context": {"id": "01CALL"}, "response": None}
        raise LookupError("unknown_command", f"Unknown command {kind}")
