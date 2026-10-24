#!/usr/bin/env python3
"""Standalone Rivian charging schedule/amperage test harness.

This script is intentionally outside Home Assistant and reuses the same auth
sequence as the integration:
1) create_csrf_token
2) authenticate
3) validate_otp (if MFA is required)

It can then attempt schedule read/write operations using experimental GraphQL
queries against the authenticated Rivian session.
"""

from __future__ import annotations

import argparse
import asyncio
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
import getpass
import json
import os
from pathlib import Path
from typing import Any

from rivian import Rivian
from rivian.exceptions import RivianApiException, RivianUnauthenticated

AUTH_CACHE_FILE = Path(".rivian-test-auth.json")
OUTPUT_DIR = Path("tmp") / "rivian-schedule-tests"

GRAPHQL_BASEPATH = "https://rivian.com/api/gql"
GRAPHQL_GATEWAY = GRAPHQL_BASEPATH + "/gateway/graphql"
GRAPHQL_CHARGING = GRAPHQL_BASEPATH + "/chrg/user/graphql"

BASE_HEADERS = {
    "User-Agent": "RivianApp/707 CFNetwork/1237 Darwin/20.4.0",
    "Accept": "application/json",
    "Content-Type": "application/json",
    "Apollographql-Client-Name": "com.rivian.ios.consumer-apollo-ios",
}

TOKEN_KEYS = (
    "access_token",
    "refresh_token",
    "user_session_token",
)

SENSITIVE_KEYWORDS = (
    "token",
    "password",
    "secret",
    "authorization",
    "cookie",
)

AMP_KEYS = (
    "amperage",
    "amps",
    "currentamps",
    "maxamps",
    "chargecurrent",
)


class HarnessError(Exception):
    """Harness-specific error."""


@dataclass
class Context:
    """Runtime context for commands."""

    debug: bool
    raw: bool
    redact: bool


@dataclass
class AuthSource:
    """Resolved auth source and token payload."""

    source: str
    tokens: dict[str, str]


@dataclass
class SelectedVehicle:
    """Vehicle chosen by id, vin, or index."""

    user_vehicle_id: str
    name: str
    vin: str
    model: str
    raw: dict[str, Any]


@dataclass
class ScheduleReadResult:
    """Schedule query result and parsed candidates."""

    vehicle: SelectedVehicle
    operation_name: str
    response_status: int
    response_json: dict[str, Any]
    candidates: list[dict[str, Any]]


def utc_now_iso() -> str:
    """Return current UTC timestamp in ISO format."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def stamp_for_filename() -> str:
    """Timestamp suitable for filenames."""
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def ensure_output_dir() -> None:
    """Ensure artifact output directory exists."""
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


def mask_token(value: Any) -> Any:
    """Mask tokens, keeping first 6 and last 4 characters."""
    if not isinstance(value, str):
        return value
    if len(value) <= 10:
        return "*" * len(value)
    return f"{value[:6]}...{value[-4:]}"


def redact_json(data: Any) -> Any:
    """Recursively redact sensitive fields from JSON-like structures."""
    if isinstance(data, dict):
        output: dict[str, Any] = {}
        for key, value in data.items():
            lowered = key.lower()
            if any(keyword in lowered for keyword in SENSITIVE_KEYWORDS):
                output[key] = mask_token(value)
            else:
                output[key] = redact_json(value)
        return output
    if isinstance(data, list):
        return [redact_json(item) for item in data]
    return data


def save_json(path: Path, payload: Any, *, redact: bool = True) -> None:
    """Save JSON payload to disk with optional redaction."""
    path.parent.mkdir(parents=True, exist_ok=True)
    data_to_save = redact_json(payload) if redact else payload
    path.write_text(json.dumps(data_to_save, indent=2, sort_keys=True), encoding="utf-8")


def print_json(payload: Any, *, redact: bool = True) -> None:
    """Print JSON payload to stdout."""
    data_to_print = redact_json(payload) if redact else payload
    print(json.dumps(data_to_print, indent=2, sort_keys=True))


def load_auth_cache() -> dict[str, Any] | None:
    """Load auth cache from disk."""
    if not AUTH_CACHE_FILE.exists():
        return None
    try:
        return json.loads(AUTH_CACHE_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise HarnessError(f"Invalid auth cache JSON in {AUTH_CACHE_FILE}") from exc


def save_auth_cache(tokens: dict[str, str], email: str | None = None) -> None:
    """Persist auth cache."""
    now = utc_now_iso()
    existing = load_auth_cache() or {}
    created_at = existing.get("created_at", now)
    payload = {
        "access_token": tokens.get("access_token", ""),
        "refresh_token": tokens.get("refresh_token", ""),
        "user_session_token": tokens.get("user_session_token", ""),
        "created_at": created_at,
        "updated_at": now,
    }
    if email:
        payload["email"] = email
    AUTH_CACHE_FILE.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def resolve_cached_or_env_tokens() -> AuthSource:
    """Resolve credentials in required order: cache then env."""
    cache = load_auth_cache()
    if cache and all(cache.get(k) for k in TOKEN_KEYS):
        return AuthSource(
            source="cache",
            tokens={
                "access_token": str(cache["access_token"]),
                "refresh_token": str(cache["refresh_token"]),
                "user_session_token": str(cache["user_session_token"]),
            },
        )

    env_tokens = {
        "access_token": os.getenv("RIVIAN_ACCESS_TOKEN", "").strip(),
        "refresh_token": os.getenv("RIVIAN_REFRESH_TOKEN", "").strip(),
        "user_session_token": os.getenv("RIVIAN_USER_SESSION_TOKEN", "").strip(),
    }
    if all(env_tokens.values()):
        return AuthSource(source="env", tokens=env_tokens)

    raise HarnessError(
        "No valid credentials found. Run `python tools/test_charging_schedule.py login` "
        "or set all required environment variables."
    )


def vehicle_label(vehicle: SelectedVehicle) -> str:
    """Build a concise vehicle label."""
    return f"{vehicle.name} ({vehicle.vin[-6:]}) id={mask_token(vehicle.user_vehicle_id)}"


def flatten_vehicle(user_vehicle: dict[str, Any]) -> SelectedVehicle:
    """Normalize user vehicle entry into a selected vehicle dataclass."""
    inner = user_vehicle.get("vehicle", {})
    model = f"{inner.get('modelYear', '')} {inner.get('make', '')} {inner.get('model', '')}".strip()
    return SelectedVehicle(
        user_vehicle_id=user_vehicle.get("id", ""),
        name=user_vehicle.get("name", "Unnamed Vehicle"),
        vin=user_vehicle.get("vin") or inner.get("vin", ""),
        model=model or inner.get("model", "unknown"),
        raw=user_vehicle,
    )


def find_schedule_candidates(data: Any, path: str = "data") -> list[dict[str, Any]]:
    """Recursively locate schedule-like objects in a response payload."""
    candidates: list[dict[str, Any]] = []
    if isinstance(data, dict):
        for key, value in data.items():
            child_path = f"{path}.{key}"
            lowered = key.lower()
            if "schedule" in lowered:
                candidates.append({"path": child_path, "value": value})
            candidates.extend(find_schedule_candidates(value, child_path))
    elif isinstance(data, list):
        for index, item in enumerate(data):
            candidates.extend(find_schedule_candidates(item, f"{path}[{index}]"))
    return candidates


def find_amp_targets(data: Any, path: str = "root") -> list[dict[str, Any]]:
    """Find dict nodes that contain amperage-like keys."""
    targets: list[dict[str, Any]] = []
    if isinstance(data, dict):
        lowered_keys = {str(k).lower() for k in data}
        if any(any(amp_key in key for amp_key in AMP_KEYS) for key in lowered_keys):
            targets.append({"path": path, "value": data})
        for key, value in data.items():
            targets.extend(find_amp_targets(value, f"{path}.{key}"))
    elif isinstance(data, list):
        for index, item in enumerate(data):
            targets.extend(find_amp_targets(item, f"{path}[{index}]"))
    return targets


def get_amp_field_name(node: dict[str, Any]) -> str | None:
    """Return the preferred amperage field name from a schedule dict."""
    ordered = ["amperage", "amps", "currentAmps", "maxAmps", "chargeCurrent"]
    keys_by_lower = {str(key).lower(): key for key in node}
    for field in ordered:
        if field.lower() in keys_by_lower:
            return str(keys_by_lower[field.lower()])
    return None


def maybe_get_max_amp(node: dict[str, Any]) -> int | None:
    """Attempt to read a max amperage field from the current node."""
    candidates = ["maxAmps", "maxAmperage", "maximumAmps", "chargerMaxAmps"]
    for key in candidates:
        if key in node:
            value = node.get(key)
            if isinstance(value, (int, float)):
                return int(value)
    return None


def clamp_requested_amps(amps: int) -> int:
    """Clamp amps to standard safe range."""
    return max(8, min(48, amps))


class ExperimentalChargingScheduleClient:
    """Experimental GraphQL access for schedule read/write attempts.

    The upstream client does not currently expose a high-level charging schedule API,
    so this class isolates guessed GraphQL operation shapes in one place.
    """

    def __init__(self, rivian: Rivian, ctx: Context) -> None:
        self._rivian = rivian
        self._ctx = ctx

    async def _graphql(
        self,
        *,
        url: str,
        body: dict[str, Any],
        include_csrf: bool,
        include_app_session: bool,
        include_user_session: bool,
    ) -> tuple[int, dict[str, Any]]:
        headers = dict(BASE_HEADERS)
        csrf = getattr(self._rivian, "_csrf_token", "")
        app_sess = getattr(self._rivian, "_app_session_token", "")
        user_sess = getattr(self._rivian, "_user_session_token", "")

        if include_csrf and csrf:
            headers["Csrf-Token"] = csrf
        if include_app_session and app_sess:
            headers["A-Sess"] = app_sess
        if include_user_session and user_sess:
            headers["U-Sess"] = user_sess

        query_fn = getattr(self._rivian, "_Rivian__graphql_query", None)
        if callable(query_fn):
            response = await query_fn(headers, url, body)
        else:
            session = getattr(self._rivian, "_session", None)
            if session is None:
                raise HarnessError("No active HTTP session available on Rivian client")
            response = await session.request("POST", url, json=body, headers=headers)

        response_json = await response.json()
        return response.status, response_json

    async def read_schedule(self, vehicle: SelectedVehicle) -> tuple[str, int, dict[str, Any], list[dict[str, Any]]]:
        """Try multiple read operations and return first one with schedule candidates."""
        attempts = [
            {
                "operationName": "GetChargingSchedule",
                "url": GRAPHQL_CHARGING,
                "include_csrf": False,
                "include_app_session": False,
                "include_user_session": True,
                "query": (
                    "query GetChargingSchedule($vehicleId: ID!) {"
                    " getChargingSchedule(vehicleId: $vehicleId) { __typename id name enabled locationName days startTime endTime targetSoc amperage amps currentAmps maxAmps schedules locations entries }"
                    "}"
                ),
                "variables": {"vehicleId": vehicle.user_vehicle_id},
            },
            {
                "operationName": "GetVehicleChargingSchedule",
                "url": GRAPHQL_GATEWAY,
                "include_csrf": False,
                "include_app_session": True,
                "include_user_session": True,
                "query": (
                    "query GetVehicleChargingSchedule($vehicleId: String!) {"
                    " getVehicle(id: $vehicleId) { __typename id vin chargingSchedule chargingSchedules }"
                    "}"
                ),
                "variables": {"vehicleId": vehicle.user_vehicle_id},
            },
            {
                "operationName": "GetVehicleStateSchedule",
                "url": GRAPHQL_GATEWAY,
                "include_csrf": False,
                "include_app_session": True,
                "include_user_session": True,
                "query": (
                    "query GetVehicleStateSchedule($vehicleID: String!) {"
                    " vehicleState(id: $vehicleID) { __typename chargingSchedule chargingSchedules }"
                    "}"
                ),
                "variables": {"vehicleID": vehicle.user_vehicle_id},
            },
        ]

        fallback_result: tuple[str, int, dict[str, Any], list[dict[str, Any]]] | None = None

        for attempt in attempts:
            body = {
                "operationName": attempt["operationName"],
                "query": attempt["query"],
                "variables": attempt["variables"],
            }
            status, payload = await self._graphql(
                url=str(attempt["url"]),
                body=body,
                include_csrf=bool(attempt["include_csrf"]),
                include_app_session=bool(attempt["include_app_session"]),
                include_user_session=bool(attempt["include_user_session"]),
            )

            if self._ctx.raw:
                ensure_output_dir()
                out_file = OUTPUT_DIR / f"raw-read-{attempt['operationName']}-{stamp_for_filename()}.json"
                save_json(
                    out_file,
                    {
                        "request": body,
                        "response_status": status,
                        "response_json": payload,
                    },
                    redact=self._ctx.redact,
                )

            candidates = find_schedule_candidates(payload)
            if candidates:
                return str(attempt["operationName"]), status, payload, candidates
            if fallback_result is None:
                fallback_result = (str(attempt["operationName"]), status, payload, candidates)

        if fallback_result is None:
            raise HarnessError("No schedule read attempts were executed")
        return fallback_result

    async def apply_schedule_mutation(
        self,
        *,
        vehicle: SelectedVehicle,
        schedule_payload: Any,
    ) -> dict[str, Any]:
        """Attempt multiple guessed mutation shapes; return first successful response."""
        attempts = [
            {
                "operationName": "SetChargingSchedule",
                "url": GRAPHQL_CHARGING,
                "query": (
                    "mutation SetChargingSchedule($vehicleId: ID!, $schedule: ChargingScheduleInput!) {"
                    " setChargingSchedule(vehicleId: $vehicleId, schedule: $schedule) { __typename id success message }"
                    "}"
                ),
                "variables": {
                    "vehicleId": vehicle.user_vehicle_id,
                    "schedule": schedule_payload,
                },
            },
            {
                "operationName": "UpdateChargingSchedule",
                "url": GRAPHQL_CHARGING,
                "query": (
                    "mutation UpdateChargingSchedule($attrs: UpdateChargingScheduleInput!) {"
                    " updateChargingSchedule(attrs: $attrs) { __typename id success message }"
                    "}"
                ),
                "variables": {
                    "attrs": {
                        "vehicleId": vehicle.user_vehicle_id,
                        "schedule": schedule_payload,
                    }
                },
            },
            {
                "operationName": "SetChargingScheduleV2",
                "url": GRAPHQL_GATEWAY,
                "query": (
                    "mutation SetChargingScheduleV2($input: SetChargingScheduleInput!) {"
                    " setChargingSchedule(input: $input) { __typename id success message }"
                    "}"
                ),
                "variables": {
                    "input": {
                        "vehicleId": vehicle.user_vehicle_id,
                        "schedule": schedule_payload,
                    }
                },
            },
        ]

        all_attempts: list[dict[str, Any]] = []

        for attempt in attempts:
            body = {
                "operationName": attempt["operationName"],
                "query": attempt["query"],
                "variables": attempt["variables"],
            }
            status, payload = await self._graphql(
                url=str(attempt["url"]),
                body=body,
                include_csrf=True,
                include_app_session=True,
                include_user_session=True,
            )
            attempt_result = {
                "operationName": attempt["operationName"],
                "status": status,
                "request": body,
                "response": payload,
            }
            all_attempts.append(attempt_result)

            has_errors = bool(payload.get("errors")) if isinstance(payload, dict) else True
            data_present = bool(payload.get("data")) if isinstance(payload, dict) else False
            if status == 200 and data_present and not has_errors:
                return {
                    "success": True,
                    "selected_attempt": attempt_result,
                    "all_attempts": all_attempts,
                }

        return {
            "success": False,
            "selected_attempt": all_attempts[-1] if all_attempts else None,
            "all_attempts": all_attempts,
        }


async def interactive_login(ctx: Context) -> tuple[dict[str, str], str, bool]:
    """Run first-time login with optional OTP prompt."""
    email = input("Rivian email: ").strip()
    password = getpass.getpass("Rivian password: ")

    async with Rivian(request_timeout=30) as rivian:
        print("Login started")
        print(f"Email: {email}")
        await rivian.create_csrf_token()
        await rivian.authenticate(email, password)

        otp_needed = bool(getattr(rivian, "_otp_needed", False))
        print(f"MFA required: {'yes' if otp_needed else 'no'}")

        if otp_needed:
            otp = input("MFA/OTP code: ").strip()
            await rivian.validate_otp(email, otp)

        tokens = {
            "access_token": str(getattr(rivian, "_access_token", "")),
            "refresh_token": str(getattr(rivian, "_refresh_token", "")),
            "user_session_token": str(getattr(rivian, "_user_session_token", "")),
        }

    if not all(tokens.values()):
        raise HarnessError("Login completed but token fields were not populated")

    return tokens, email, otp_needed


async def build_client_from_source(auth_source: AuthSource) -> Rivian:
    """Create Rivian client from token source and bootstrap CSRF/app session."""
    rivian = Rivian(
        request_timeout=30,
        access_token=auth_source.tokens.get("access_token", ""),
        refresh_token=auth_source.tokens.get("refresh_token", ""),
        user_session_token=auth_source.tokens.get("user_session_token", ""),
    )
    await rivian.create_csrf_token()
    return rivian


async def fetch_current_user(rivian: Rivian) -> dict[str, Any]:
    """Fetch current user payload."""
    response = await rivian.get_user_information(include_phones=True)
    payload = await response.json()
    return payload.get("data", {}).get("currentUser", {})


def select_vehicle_from_args(user: dict[str, Any], args: argparse.Namespace) -> SelectedVehicle:
    """Select vehicle by id/vin/index."""
    vehicles = [flatten_vehicle(v) for v in user.get("vehicles", [])]
    if not vehicles:
        raise HarnessError("No vehicles found for this account")

    if args.vehicle_id:
        for vehicle in vehicles:
            if vehicle.user_vehicle_id == args.vehicle_id:
                return vehicle
        raise HarnessError(f"Vehicle ID not found: {args.vehicle_id}")

    if args.vin:
        wanted = args.vin.lower()
        for vehicle in vehicles:
            if vehicle.vin.lower() == wanted:
                return vehicle
        raise HarnessError(f"VIN not found: {args.vin}")

    index = int(args.vehicle_index if args.vehicle_index is not None else 0)
    if index < 0 or index >= len(vehicles):
        raise HarnessError(f"Vehicle index out of range: {index}")
    return vehicles[index]


def summarize_schedule_node(node: Any) -> dict[str, Any]:
    """Build a concise schedule summary from a schedule-like object."""
    if not isinstance(node, dict):
        return {"type": str(type(node)), "preview": str(node)[:120]}

    day_fields = ["days", "weekDays", "activeDays"]
    time_start_fields = ["startTime", "timeStart", "windowStart"]
    time_end_fields = ["endTime", "timeEnd", "windowEnd"]
    target_soc_fields = ["targetSoc", "targetSOC", "socTarget", "targetChargeLimit"]

    amp_field = get_amp_field_name(node)

    def first_value(fields: list[str]) -> Any:
        for field in fields:
            if field in node:
                return node[field]
        return None

    return {
        "schedule_or_location_name": node.get("name") or node.get("locationName") or node.get("label"),
        "enabled": node.get("enabled"),
        "days": first_value(day_fields),
        "time_window": {
            "start": first_value(time_start_fields),
            "end": first_value(time_end_fields),
        },
        "amperage": node.get(amp_field) if amp_field else None,
        "target_soc": first_value(target_soc_fields),
    }


def print_schedule_summary(summary: dict[str, Any]) -> None:
    """Print schedule summary in requested concise format."""
    print("Current schedule summary:")
    print(f"- Schedule/location name: {summary.get('schedule_or_location_name')}")
    print(f"- Enabled: {summary.get('enabled')}")
    print(f"- Days: {summary.get('days')}")
    time_window = summary.get("time_window") or {}
    print(f"- Time window: {time_window.get('start')} -> {time_window.get('end')}")
    print(f"- Amperage: {summary.get('amperage')}")
    print(f"- Target SOC: {summary.get('target_soc')}")


def pick_schedule_for_update(candidates: list[dict[str, Any]], home_only: bool) -> dict[str, Any]:
    """Pick a single schedule candidate to mutate safely."""
    if not candidates:
        raise HarnessError("No schedule candidates found")

    filtered = candidates
    if home_only:
        home_filtered = []
        for candidate in candidates:
            path = str(candidate.get("path", "")).lower()
            value = candidate.get("value")
            text_blob = json.dumps(value, default=str).lower() if isinstance(value, (dict, list)) else str(value).lower()
            if "home" in path or "home" in text_blob:
                home_filtered.append(candidate)
        if home_filtered:
            filtered = home_filtered
        else:
            raise HarnessError(
                "--home-only requested but no schedule candidate appears to be home-related"
            )

    amp_candidates = [candidate for candidate in filtered if find_amp_targets(candidate.get("value"))]
    if len(amp_candidates) == 1:
        return amp_candidates[0]
    if len(amp_candidates) > 1:
        raise HarnessError(
            "Multiple schedule candidates with amperage fields found. "
            "Refusing to guess; use --raw and inspect outputs first."
        )

    if len(filtered) == 1:
        return filtered[0]

    raise HarnessError(
        "Multiple schedule candidates found and home cannot be identified safely. "
        "Use --raw to inspect schedule summary and narrow selection criteria."
    )


async def cmd_login(args: argparse.Namespace) -> int:
    """Handle login command."""
    ctx = Context(debug=args.debug, raw=args.raw, redact=args.redact)
    try:
        tokens, email, _otp_needed = await interactive_login(ctx)
        save_auth_cache(tokens, email=email)
        print("Token saved: yes")
        print(f"Credential cache: {AUTH_CACHE_FILE}")
        if ctx.debug:
            print("Token preview:")
            print(f"- access_token: {mask_token(tokens['access_token'])}")
            print(f"- refresh_token: {mask_token(tokens['refresh_token'])}")
            print(f"- user_session_token: {mask_token(tokens['user_session_token'])}")
        return 0
    except Exception as exc:  # pylint: disable=broad-except
        print(f"Login failed: {exc}")
        return 1


async def cmd_refresh_token(args: argparse.Namespace) -> int:
    """Handle refresh-token command."""
    try:
        source = resolve_cached_or_env_tokens()
    except HarnessError as exc:
        print(exc)
        print("Falling back to interactive login...")
        source = None

    if source is not None:
        try:
            async with await build_client_from_source(source) as rivian:
                user = await fetch_current_user(rivian)
                if not user:
                    raise HarnessError("Current user response is empty")
                tokens = {
                    "access_token": str(getattr(rivian, "_access_token", "")) or source.tokens["access_token"],
                    "refresh_token": str(getattr(rivian, "_refresh_token", "")) or source.tokens["refresh_token"],
                    "user_session_token": str(getattr(rivian, "_user_session_token", "")) or source.tokens["user_session_token"],
                }
                save_auth_cache(tokens, email=user.get("email"))
                print("Token refresh flow complete")
                print(f"Credential source: {source.source}")
                print("Token saved: yes")
                print(f"Credential cache: {AUTH_CACHE_FILE}")
                return 0
        except Exception as exc:  # pylint: disable=broad-except
            print(f"Token refresh via existing tokens failed: {exc}")
            print("Falling back to interactive login...")

    try:
        tokens, email, otp_needed = await interactive_login(Context(debug=args.debug, raw=args.raw, redact=args.redact))
        save_auth_cache(tokens, email=email)
        print("Token refresh flow complete")
        print("Credential source: interactive login")
        print(f"MFA required: {'yes' if otp_needed else 'no'}")
        print("Token saved: yes")
        print(f"Credential cache: {AUTH_CACHE_FILE}")
        return 0
    except Exception as exc:  # pylint: disable=broad-except
        print(f"Refresh-token failed: {exc}")
        return 1


async def with_authenticated_client(args: argparse.Namespace) -> tuple[Rivian, AuthSource, dict[str, Any]]:
    """Build authenticated client and fetch user payload."""
    source = resolve_cached_or_env_tokens()
    rivian = await build_client_from_source(source)

    try:
        user = await fetch_current_user(rivian)
    except RivianUnauthenticated as exc:
        await rivian.close()
        raise HarnessError(
            "Authentication failed with cached/env tokens. "
            "Run `python tools/test_charging_schedule.py refresh-token` or `login`."
        ) from exc
    except Exception:
        await rivian.close()
        raise

    return rivian, source, user


async def cmd_whoami(args: argparse.Namespace) -> int:
    """Handle whoami command."""
    try:
        rivian, source, user = await with_authenticated_client(args)
        try:
            print("Whoami:")
            print(f"Credential source: {source.source}")
            print(f"User ID: {mask_token(user.get('id'))}")
            print(f"Email: {user.get('email')}")
            print(f"Vehicles: {len(user.get('vehicles', []))}")
            channels = user.get("registrationChannels") or []
            print(f"MFA channels configured: {'yes' if channels else 'no'}")
            return 0
        finally:
            await rivian.close()
    except Exception as exc:  # pylint: disable=broad-except
        print(f"whoami failed: {exc}")
        return 1


async def cmd_vehicles(args: argparse.Namespace) -> int:
    """Handle vehicles command."""
    try:
        rivian, source, user = await with_authenticated_client(args)
        try:
            vehicles = [flatten_vehicle(v) for v in user.get("vehicles", [])]
            print(f"Credential source: {source.source}")
            print("Vehicles found:")
            if not vehicles:
                print("(none)")
                return 0
            for index, vehicle in enumerate(vehicles, start=1):
                print(f"{index}. Name: {vehicle.name}")
                print(f"   VIN: ...{vehicle.vin[-4:] if vehicle.vin else 'unknown'}")
                print(f"   Vehicle ID: {mask_token(vehicle.user_vehicle_id)}")
                print(f"   Model/generation: {vehicle.model}")
            return 0
        finally:
            await rivian.close()
    except Exception as exc:  # pylint: disable=broad-except
        print(f"vehicles failed: {exc}")
        return 1


async def read_schedule_for_args(
    args: argparse.Namespace,
) -> tuple[Rivian, AuthSource, ScheduleReadResult]:
    """Shared helper for schedule fetch commands."""
    ctx = Context(debug=args.debug, raw=args.raw, redact=args.redact)
    rivian, source, user = await with_authenticated_client(args)
    vehicle = select_vehicle_from_args(user, args)
    schedule_client = ExperimentalChargingScheduleClient(rivian, ctx)
    operation_name, status, response_json, candidates = await schedule_client.read_schedule(vehicle)
    return rivian, source, ScheduleReadResult(
        vehicle=vehicle,
        operation_name=operation_name,
        response_status=status,
        response_json=response_json,
        candidates=candidates,
    )


async def cmd_get_schedule(args: argparse.Namespace) -> int:
    """Handle get-schedule command."""
    try:
        rivian, source, result = await read_schedule_for_args(args)
        try:
            print(f"Credential source: {source.source}")
            print(f"Vehicle: {vehicle_label(result.vehicle)}")
            print(f"Schedule query operation: {result.operation_name}")
            print(f"HTTP status: {result.response_status}")
            print(f"Schedule candidate count: {len(result.candidates)}")

            if not result.candidates:
                errors = result.response_json.get("errors") if isinstance(result.response_json, dict) else None
                print("Current schedule summary:")
                print("- Schedule/location name: unavailable")
                print("- Enabled: unavailable")
                print("- Days: unavailable")
                print("- Time window: unavailable")
                print("- Amperage: unavailable")
                print("- Target SOC: unavailable")
                if errors:
                    print("GraphQL errors:")
                    print_json(errors, redact=args.redact)
                return 0

            selected = pick_schedule_for_update(result.candidates, home_only=args.home_only)
            summary = summarize_schedule_node(selected.get("value"))
            print_schedule_summary(summary)

            if args.raw:
                ensure_output_dir()
                file_path = OUTPUT_DIR / f"schedule-read-{stamp_for_filename()}.json"
                save_json(
                    file_path,
                    {
                        "vehicle": {
                            "id": result.vehicle.user_vehicle_id,
                            "vin": result.vehicle.vin,
                            "name": result.vehicle.name,
                        },
                        "operation": result.operation_name,
                        "response_status": result.response_status,
                        "response_json": result.response_json,
                        "selected_candidate": selected,
                    },
                    redact=args.redact,
                )
                print(f"Saved raw output: {file_path}")
            return 0
        finally:
            await rivian.close()
    except Exception as exc:  # pylint: disable=broad-except
        print(f"get-schedule failed: {exc}")
        return 1


def build_mutated_schedule(
    selected_schedule: dict[str, Any],
    requested_amps: int,
    force: bool,
) -> tuple[dict[str, Any], str, Any, Any]:
    """Return mutated schedule copy with one amperage field changed."""
    working = deepcopy(selected_schedule)
    targets = find_amp_targets(working)
    if not targets:
        raise HarnessError("No amperage-like fields found in selected schedule payload")
    if len(targets) > 1:
        raise HarnessError(
            "Multiple amperage targets found in selected payload. "
            "Refusing to modify multiple locations in one operation."
        )

    node = targets[0]["value"]
    if not isinstance(node, dict):
        raise HarnessError("Amp target is not a mutable dictionary")

    amp_field = get_amp_field_name(node)
    if not amp_field:
        raise HarnessError("Could not determine amperage field in selected schedule payload")

    old_amps = node.get(amp_field)
    max_amp = maybe_get_max_amp(node)
    if max_amp is not None and requested_amps > max_amp and not force:
        raise HarnessError(
            f"Requested amps ({requested_amps}) exceeds current max ({max_amp}). "
            "Use --force to override."
        )

    node[amp_field] = requested_amps
    new_amps = node.get(amp_field)
    return working, amp_field, old_amps, new_amps


async def cmd_set_amps(args: argparse.Namespace) -> int:
    """Handle set-amps command."""
    mode = "apply" if args.apply else "dry-run"
    if args.apply and not args.i_understand_this_may_change_my_vehicle_settings:
        print("Refusing to apply changes without explicit acknowledgement flag")
        return 1

    requested_amps = clamp_requested_amps(int(args.amps))
    if requested_amps != int(args.amps):
        print(
            f"Requested amperage {args.amps} clamped to safe range value {requested_amps} (8-48A)"
        )

    ctx = Context(debug=args.debug, raw=args.raw, redact=args.redact)
    ensure_output_dir()

    try:
        rivian, source, result = await read_schedule_for_args(args)
        try:
            selected = pick_schedule_for_update(result.candidates, home_only=args.home_only)
            schedule_value = selected.get("value")
            if not isinstance(schedule_value, dict):
                raise HarnessError("Selected schedule candidate is not a dict payload")

            mutated_schedule, amp_field, old_amps, _new_amps = build_mutated_schedule(
                schedule_value,
                requested_amps=requested_amps,
                force=bool(args.force),
            )

            before_file = OUTPUT_DIR / f"schedule-before-{stamp_for_filename()}.json"
            save_json(before_file, schedule_value, redact=args.redact)

            schedule_client = ExperimentalChargingScheduleClient(rivian, ctx)
            mutation_attempted = False
            mutation_result: dict[str, Any] = {
                "success": False,
                "selected_attempt": None,
                "all_attempts": [],
            }

            if args.apply:
                mutation_attempted = True
                mutation_result = await schedule_client.apply_schedule_mutation(
                    vehicle=result.vehicle,
                    schedule_payload=mutated_schedule,
                )
                mutation_file = OUTPUT_DIR / f"mutation-response-{stamp_for_filename()}.json"
                save_json(mutation_file, mutation_result, redact=args.redact)

            if not args.apply and args.raw:
                dry_run_file = OUTPUT_DIR / f"dry-run-payload-{stamp_for_filename()}.json"
                save_json(
                    dry_run_file,
                    {
                        "vehicle_id": result.vehicle.user_vehicle_id,
                        "amp_field": amp_field,
                        "old_amps": old_amps,
                        "requested_amps": requested_amps,
                        "mutated_schedule": mutated_schedule,
                    },
                    redact=args.redact,
                )

            operation_name_after, status_after, response_after, candidates_after = await schedule_client.read_schedule(
                result.vehicle
            )
            selected_after = pick_schedule_for_update(candidates_after, home_only=args.home_only) if candidates_after else None
            after_schedule = selected_after.get("value") if selected_after else None
            after_file = OUTPUT_DIR / f"schedule-after-{stamp_for_filename()}.json"
            save_json(after_file, after_schedule, redact=args.redact)

            new_amps_verified = None
            if isinstance(after_schedule, dict):
                amp_targets_after = find_amp_targets(after_schedule)
                if len(amp_targets_after) == 1 and isinstance(amp_targets_after[0]["value"], dict):
                    after_node = amp_targets_after[0]["value"]
                    after_amp_field = get_amp_field_name(after_node)
                    if after_amp_field:
                        new_amps_verified = after_node.get(after_amp_field)

            gql_errors: Any = None
            if mutation_result.get("selected_attempt"):
                gql_errors = mutation_result["selected_attempt"].get("response", {}).get("errors")

            verification = "unchanged"
            if new_amps_verified == requested_amps:
                verification = "amperage changed"
            elif new_amps_verified is None:
                verification = "unable to verify"

            print(f"Credential source: {source.source}")
            print(f"Mutation attempted: {'yes' if mutation_attempted else 'no'}")
            print(f"Mode: {mode}")
            print(f"Vehicle: {vehicle_label(result.vehicle)}")
            print(f"Old amps: {old_amps}")
            print(f"Requested amps: {requested_amps}")
            print(f"New amps: {new_amps_verified}")
            print("GraphQL errors:")
            print_json(gql_errors, redact=args.redact) if gql_errors else print("null")
            print(f"Verification result: {verification}")
            print(f"Before snapshot: {before_file}")
            print(f"After snapshot: {after_file}")
            if args.apply:
                print("Mutation apply was requested")
                if mutation_result.get("success"):
                    print("Mutation success: yes")
                else:
                    print("Mutation success: no")
            return 0
        finally:
            await rivian.close()
    except Exception as exc:  # pylint: disable=broad-except
        print(f"set-amps failed: {exc}")
        return 1


async def cmd_restore(args: argparse.Namespace) -> int:
    """Handle restore command."""
    ctx = Context(debug=args.debug, raw=args.raw, redact=args.redact)

    backup_path = Path(args.from_file)
    if not backup_path.exists():
        print(f"Restore source file not found: {backup_path}")
        return 1

    try:
        restore_payload = json.loads(backup_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        print(f"Restore source is invalid JSON: {exc}")
        return 1

    if not args.apply or not args.i_understand_this_may_change_my_vehicle_settings:
        print("Restore defaults to dry-run. To apply, provide both:")
        print("--apply")
        print("--i-understand-this-may-change-my-vehicle-settings")
        print("Restore payload preview:")
        print_json(restore_payload, redact=args.redact)
        return 0

    try:
        rivian, source, user = await with_authenticated_client(args)
        try:
            vehicle = select_vehicle_from_args(user, args)
            schedule_client = ExperimentalChargingScheduleClient(rivian, ctx)
            mutation_result = await schedule_client.apply_schedule_mutation(
                vehicle=vehicle,
                schedule_payload=restore_payload,
            )
            ensure_output_dir()
            out_file = OUTPUT_DIR / f"restore-mutation-response-{stamp_for_filename()}.json"
            save_json(out_file, mutation_result, redact=args.redact)
            print(f"Credential source: {source.source}")
            print(f"Vehicle: {vehicle_label(vehicle)}")
            print("Mutation attempted: yes")
            print("Mode: apply")
            print(f"Restore input: {backup_path}")
            print(f"Mutation response file: {out_file}")
            print(f"Mutation success: {'yes' if mutation_result.get('success') else 'no'}")
            return 0
        finally:
            await rivian.close()
    except Exception as exc:  # pylint: disable=broad-except
        print(f"restore failed: {exc}")
        return 1


def add_vehicle_flags(parser: argparse.ArgumentParser) -> None:
    """Attach common vehicle selector flags."""
    parser.add_argument("--vin", help="Select vehicle by exact VIN")
    parser.add_argument("--vehicle-id", help="Select vehicle by Rivian user vehicle ID")
    parser.add_argument("--vehicle-index", type=int, default=0, help="Select vehicle by index from vehicles list")


def build_parser() -> argparse.ArgumentParser:
    """Build CLI parser."""
    parser = argparse.ArgumentParser(
        description="Standalone Rivian charging schedule/amperage test harness"
    )
    parser.add_argument("--debug", action="store_true", help="Enable extra debug output")
    parser.add_argument("--raw", action="store_true", help="Persist raw request/response JSON artifacts")
    parser.add_argument("--redact", action="store_true", default=True, help="Redact sensitive fields in logs and saved JSON")
    parser.add_argument("--no-redact", dest="redact", action="store_false", help="Disable redaction (not recommended)")

    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("login", help="Run first-time interactive login and save tokens")
    subparsers.add_parser("refresh-token", help="Refresh auth by validating existing tokens or re-running interactive login")
    subparsers.add_parser("whoami", help="Show current authenticated user")
    subparsers.add_parser("vehicles", help="List vehicles for current account")

    get_schedule = subparsers.add_parser("get-schedule", help="Read charging schedule")
    add_vehicle_flags(get_schedule)
    get_schedule.add_argument("--home-only", action="store_true", help="Restrict selection to schedule entries likely associated with home")

    set_amps = subparsers.add_parser("set-amps", help="Dry-run or apply schedule amperage change")
    add_vehicle_flags(set_amps)
    set_amps.add_argument("--amps", required=True, type=int, help="Requested amperage")
    set_amps.add_argument("--dry-run", action="store_true", help="Explicitly mark operation as dry-run")
    set_amps.add_argument("--apply", action="store_true", help="Apply mutation (requires acknowledgement flag)")
    set_amps.add_argument(
        "--i-understand-this-may-change-my-vehicle-settings",
        action="store_true",
        help="Required acknowledgement for applied mutations",
    )
    set_amps.add_argument("--home-only", action="store_true", help="Restrict schedule mutation target to home schedule")
    set_amps.add_argument("--force", action="store_true", help="Allow requested amps above reported schedule max")

    restore = subparsers.add_parser("restore", help="Restore schedule from saved JSON file")
    add_vehicle_flags(restore)
    restore.add_argument("--from-file", required=True, help="Path to saved schedule JSON")
    restore.add_argument("--apply", action="store_true", help="Apply restore mutation")
    restore.add_argument(
        "--i-understand-this-may-change-my-vehicle-settings",
        action="store_true",
        help="Required acknowledgement for applied restore",
    )

    return parser


async def run_command(args: argparse.Namespace) -> int:
    """Dispatch command handlers."""
    if args.command == "login":
        return await cmd_login(args)
    if args.command == "refresh-token":
        return await cmd_refresh_token(args)
    if args.command == "whoami":
        return await cmd_whoami(args)
    if args.command == "vehicles":
        return await cmd_vehicles(args)
    if args.command == "get-schedule":
        return await cmd_get_schedule(args)
    if args.command == "set-amps":
        return await cmd_set_amps(args)
    if args.command == "restore":
        return await cmd_restore(args)
    raise HarnessError(f"Unsupported command: {args.command}")


def main() -> int:
    """Main entry point."""
    parser = build_parser()
    args = parser.parse_args()
    try:
        return asyncio.run(run_command(args))
    except KeyboardInterrupt:
        print("Interrupted")
        return 130
    except RivianApiException as exc:
        print(f"Rivian API error: {exc}")
        return 1
    except HarnessError as exc:
        print(str(exc))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
