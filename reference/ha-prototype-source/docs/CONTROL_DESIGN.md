# Solar charging control contract

This document describes the hardening work following the initial 1.6.0 prototype.
Software tests do not certify a vehicle, a charger, or its installed firmware.

## Capability boundary

The Home Assistant Tesla Wall Connector integration supplies local measurements,
including power and connection status. It does not expose a writable charge limit
or a start/stop control. Its default polling interval is 30 seconds. This controller
cannot respond to a cloud before its input integrations report the resulting drop.

Tesla charging uses the mapped vehicle amperage and charge switch. Rivian current
uses the unofficial schedule API; physical start/stop additionally requires an
actual supported paired vehicle switch. Generic EVSE switch adapters are not
implemented; all vehicle bindings must belong to the same HA registry device.
A schedule write/readback is not physical
proof that the car stopped. Upstream currently documents Gen 2 remote control as
unsupported because secure pairing has not been implemented for that hardware.
Without a supported stop path, Rivian operation is limited to shadow decisions.

Sources checked September 19, 2026:

- [Home Assistant Tesla Wall Connector](https://www.home-assistant.io/integrations/tesla_wall_connector/)
- [Upstream Rivian remote control](https://github.com/bretterer/home-assistant-rivian#remote-vehicle-control)
- [Forecast.Solar integration](https://www.home-assistant.io/integrations/forecast_solar/)

## Two independent decisions

Live power determines permitted current. Optional forecasts determine whether
starting a new session is worthwhile after reserving energy for the house. A good
forecast can never raise the current ceiling imposed by measured power.

With grid import positive and battery charging positive:

```text
available watts = measured EV watts - grid watts
                  - max(0, -battery watts) - safety margin
```

Each sample must have usable units, a finite value, and a sufficiently recent
report. Missing SOC, charge-limit, connection, home, or blocker data is not zero,
false, or permission to charge. Only one eligible vehicle may be selected at the
home charger. Cleanup uses the stored identity of the controlled session.

## Cloud response

- Evaluate every 10 seconds using the newest available inputs.
- Require 120 seconds of continuous surplus before starting by default.
- Smooth rising surplus over 60 seconds. Falling surplus immediately bounds the
  target; the filter must not preserve a high current during a power drop.
- Increase at 2 A per elapsed minute, independent of evaluation frequency.
  Tesla uses 1 A increments and Rivian uses 2 A increments.
- If less than 8 A is supported, reduce to the minimum while evaluating a short
  deficit. A brief bridge is explicitly bounded by time, energy, and hard power
  limits; it is not a guarantee of zero grid import.
- Default deficit duration is 30 seconds and energy allowance is 10 Wh. Hard
  grid import above 1,500 W or battery discharge above 1,000 W stops immediately.
- After stopping, wait at least 180 seconds before a restart, and require the
  surplus entry condition again. Safety stops must not bypass this cooldown.
- Battery reserve, stale data, ambiguous identity, charge completion, and a
  selected blocker take precedence over smoothing and forecast advice.

Settings describe policy at the observed measurement cadence. Network latency and
cloud API execution add delay; electrical protection remains the EVSE's job.

## Forecast policy

Use existing energy forecast entities from Forecast.Solar, Solcast, or another HA
provider. Map remaining-today energy and next-hour energy, in Wh or kWh. This
integration does not request extra forecast API updates or use weather icons as
irradiance measurements. Forecast reports older than two hours are not current.

```text
battery reservation = usable battery kWh * max(0, target SOC - actual SOC) / 100
EV forecast budget = max(0, remaining forecast * confidence
                           - battery reservation - household reservation)
minimum session kWh = 8 A * measured voltage * minimum session minutes / 60,000
```

The default confidence factor is 0.75. Protection mode requires enough daily
budget and next-hour energy for the minimum session. These are conservative
entry checks, not a claim that household demand or clouds can be predicted.

| Mode | Effect |
| --- | --- |
| Off | Measured surplus alone governs charging. |
| Advice | Show forecast and energy reservations; do not block starts. |
| Protect | Reject new sessions if the budget is insufficient or the forecast is invalid/stale. |

An existing session continues only while live conditions support it. An expired
forecast does not cause repeated stop/start cycles during an otherwise valid
session.

## Physical control and recovery

A commissioning acknowledgement is distinct from a physical stop test. The test
starts with an actively charging vehicle at home, commands OFF, and observes both
the switch and wall power. It does not restart charging. Already-idle readings
cannot prove the stop command worked. Proof is bound to the mapped control and
vehicle identity; changing that binding requires another test.

Persist ownership before side effects. A successful service call means the request
was accepted, not that charging occurred. Observe charging and stop feedback, and
display requested current separately from observed power/current.
Current estimated from wall watts divided by voltage is labelled as an estimate;
it is not a vehicle current-limit register readback. Use correctly configured
single/split-phase measurements; the power model does not support three phases.

For an owned Rivian session, stopping and releasing follows this order:

1. Command physical OFF through the stored control binding.
2. Observe fresh OFF and low wall power on distinct reports.
3. Restore the saved complete schedule only if the current schedule is still owned.
4. Reassert OFF and verify charging stayed stopped.
5. Clear the journal only after recovery finishes.

An external schedule edit must never be overwritten. A failed stop or failed
restoration remains visible with its recovery journal. Restart, disable, reload,
and shadow-mode transitions must recover existing ownership before new starts.
Disabling the feature in options must not orphan an unfinished journal.

No cloud-only controller can issue a stop while Home Assistant or the network is
down. The Rivian ten-minute schedule is a recurring weekly schedule, not a
server-enforced expiring lease; it can recur if recovery never runs. This is a
known limitation, not a fail-safe guarantee. A local controllable EVSE is the
stronger control boundary when available.

## Operator interface

The Solar charging sidebar page uses the Home Assistant theme and live entity
state. It separates requested target, observed power, command feedback, forecast
budget, cloud hold/cooldown, capability gaps, and cleanup faults. Commands use HA
services and preserve their errors for the operator. The source includes a
clearly labelled simulated preview for layout and interaction tests.

The configuration menu separates energy setup, vehicle bindings, forecast policy,
response tuning, and the original Rivian account settings. It keeps the existing
stored option keys, so grouping the form does not reset configuration.

## Acceptance evidence

Automated checks must include elapsed-time ramp equivalence, bad numbers before
division, brief and repeated clouds, cooldown, stale forecasts, absent control,
ignored service calls, changing bindings, stop-before-restore ordering, and
persisted cleanup after restart. A rendered preview verifies UI behavior, not a
live HA installation. Physical commissioning and live solar traces remain a
separate installation step on the actual hardware.
