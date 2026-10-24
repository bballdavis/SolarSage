# Rivian Charging Schedule Test Harness

## Purpose

`tools/test_charging_schedule.py` is a standalone CLI harness for validating Rivian charging schedule behavior outside Home Assistant. It is designed to prove one of these outcomes:

- We can authenticate, read schedule data, and change schedule amperage.
- We can authenticate and read schedule data, but schedule writes fail with clear API errors.
- We can authenticate, but schedule data is unavailable.
- Authentication itself fails and the missing/required step is clear.

## First-time Login

Run:

```bash
python tools/test_charging_schedule.py login
```

The login flow follows the same sequence used in this integration's config flow:

1. `create_csrf_token`
2. `authenticate(email, password)`
3. `validate_otp(email, otp)` if MFA/OTP is required

You will be prompted for:

- Rivian email
- Rivian password
- MFA/OTP code (only when required)

Expected summary output includes:

- `Login started`
- `Email: ...`
- `MFA required: yes/no`
- `Token saved: yes/no`
- `Credential cache: .rivian-test-auth.json`

## Token Storage

Tokens are loaded in this order:

1. Local cache file `.rivian-test-auth.json`
2. Environment variables:
   - `RIVIAN_ACCESS_TOKEN`
   - `RIVIAN_REFRESH_TOKEN`
   - `RIVIAN_USER_SESSION_TOKEN`
3. Interactive login (`login` command)

The cache file format is:

```json
{
  "access_token": "...",
  "refresh_token": "...",
  "user_session_token": "...",
  "created_at": "2026-05-30T00:00:00Z",
  "updated_at": "2026-05-30T00:00:00Z"
}
```

## List Vehicles

```bash
python tools/test_charging_schedule.py whoami
python tools/test_charging_schedule.py vehicles
```

Vehicle selection is available by:

- `--vehicle-id <id>`
- `--vin <vin>`
- `--vehicle-index <n>` (default `0`)

## Read Charging Schedule

```bash
python tools/test_charging_schedule.py get-schedule --vehicle-index 0
```

Optional:

- `--home-only` to avoid non-home schedule targets
- `--raw` to save redacted request/response payloads
- `--debug` for extra local diagnostics

## Dry-run Amperage Changes

Dry-run is the default behavior.

```bash
python tools/test_charging_schedule.py set-amps --vehicle-index 0 --amps 16 --dry-run
```

Safety behavior:

- Requested amps are clamped to `8-48A`.
- If schedule max amps appears lower than requested, the command stops unless `--force` is used.
- Writes are not applied unless both apply flags are present.

## Apply Amperage Changes

Apply requires both flags:

```bash
python tools/test_charging_schedule.py set-amps \
  --vehicle-index 0 \
  --amps 16 \
  --apply \
  --i-understand-this-may-change-my-vehicle-settings
```

Before apply, the harness:

1. Reads current schedule.
2. Saves `schedule-before-*.json`.
3. Builds a payload preserving existing schedule fields and changing only one amperage-like field.
4. Attempts mutation.
5. Saves `mutation-response-*.json`.
6. Re-reads schedule and saves `schedule-after-*.json`.
7. Prints verification result.

## Restore From Backup

Dry-run preview:

```bash
python tools/test_charging_schedule.py restore \
  --vehicle-index 0 \
  --from-file tmp/rivian-schedule-tests/schedule-before-<stamp>.json
```

Apply restore:

```bash
python tools/test_charging_schedule.py restore \
  --vehicle-index 0 \
  --from-file tmp/rivian-schedule-tests/schedule-before-<stamp>.json \
  --apply \
  --i-understand-this-may-change-my-vehicle-settings
```

## Output and Logs to Capture

Capture these for troubleshooting:

- CLI stdout summary
- `tmp/rivian-schedule-tests/schedule-before-*.json`
- `tmp/rivian-schedule-tests/mutation-response-*.json`
- `tmp/rivian-schedule-tests/schedule-after-*.json`
- Optional `raw-read-*.json` and `dry-run-payload-*.json` when `--raw` is used

## Known Risks and Limits

- `rivian-python-client` currently has no dedicated high-level charging schedule read/write method.
- This harness uses an `ExperimentalChargingScheduleClient` with multiple guessed GraphQL operation shapes.
- Reads or writes can fail if Rivian's schema differs from these guesses.
- Write operations are intentionally conservative and stop when multiple schedule targets are ambiguous.

## If MFA or Auth Fails

Use this sequence:

1. Run `python tools/test_charging_schedule.py login` and complete OTP carefully.
2. If existing cached/env tokens fail, run `python tools/test_charging_schedule.py refresh-token`.
3. If auth still fails, collect the redacted error output and identify whether failure is:
   - invalid credentials
   - expired/invalid OTP
   - unauthenticated token state
   - transport/API error

## Recommended Safe Test Flow

```bash
python tools/test_charging_schedule.py login
python tools/test_charging_schedule.py whoami
python tools/test_charging_schedule.py vehicles
python tools/test_charging_schedule.py get-schedule --vehicle-index 0
python tools/test_charging_schedule.py set-amps --vehicle-index 0 --amps 16 --dry-run
python tools/test_charging_schedule.py set-amps --vehicle-index 0 --amps 16 --apply --i-understand-this-may-change-my-vehicle-settings
python tools/test_charging_schedule.py get-schedule --vehicle-index 0
```
