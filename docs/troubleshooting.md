# Troubleshooting

## SolarSage is missing after installation

Confirm `/config/custom_components/solar_ev_planner/manifest.json` exists, with the bundled `rivian/` and `www/` directories alongside it. Restart Home Assistant and look for integration load errors. Confirm the Home Assistant version meets the minimum requirement.

## The card is missing or keeps connecting

Use the exact card type `custom:solar-ev-planner-card`. The integration registers its script automatically. Reload the browser after an update and confirm the site planner entry is loaded. A Rivian-only entry does not provide a site plan. The card's resource and websocket URLs intentionally retain the `solar_ev_planner` name.

## No data, stale data, or no plan

Check solar, grid, battery, charger power and Solcast entities in Developer Tools. Unknown/unavailable readings can suspend planning. Check W versus kW, grid import positive, battery charging positive, and the forecast's `detailedForecast` attribute. Run the read-only [tools check](TOOLS.md) after mapping its example configuration to your entities.

## The car does not charge

Read the dashboard's reason and command status first. Check Planning is on, shadow mode is off only when intended, the car is plugged in and home, the vehicle is identified on the charger, and its charge limit has not been reached. Confirm the mapped charge switch and current number belong to the vehicle controller.

For Tesla, check Fleet authentication, charging state, the spending guard and command backoff. For Rivian, check dry run, control state, faults, plug/location readings, controller heartbeat, and lease renewal. Keep upstream schedule entities and competing automations disabled.

## Rivian restoration or journal faults

Follow [faults and recovery](RIVIAN_SCHEDULE_CONTROL.md#faults-and-recovery). Preserve the ownership journal. Never delete it to clear an error: it may hold the only copy of the original schedules. Verify restoration in the vehicle app before removing or replacing a controller.

## Stop commands

Turn on the planner's **Shadow** mode. For Rivian, use **Release** / **Restore owner's schedule** and verify the result. Turning Planning off is not a guarantee of immediate vehicle stop, particularly during a free-hours standing window. Use the vehicle's own controls when an immediate stop is needed. See [stopping quickly](TESTING.md#stopping-quickly).

## Report a problem

Include Home Assistant and SolarSage versions, the affected vehicle/controller, expected versus observed behavior, a timestamp and timezone, and sanitized diagnostics. Review screenshots and logs for tokens, names, VINs, coordinates, domains and account identifiers before posting. Use the repository's issue templates.
