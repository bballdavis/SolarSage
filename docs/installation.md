# Installation and upgrades

## Requirements

- Home Assistant **2026.10 or newer**, with recorder enabled and Schedule helpers available.
- Solar production, grid power, home battery power and state of charge; configure usable capacity and power limits for your equipment.
- Charger power measurement. A Tesla Wall Connector is the reference charger; SolarSage measures it and commands the vehicle, not the charger.
- [Solcast Solar](https://github.com/BJReplay/ha-solcast-solar) forecasts for today and tomorrow with `detailedForecast` attributes.
- For a Tesla: Home Assistant's Tesla Fleet integration, registered and paired for vehicle commands. API charges and account limits are your responsibility.
- For a Rivian: upstream [Home Assistant Rivian](https://github.com/bretterer/home-assistant-rivian) for telemetry, plus SolarSage's bundled Rivian login for schedule writes.

The supported design has one shared charger and a site with a home battery. Sensor mapping is configurable; the reference hardware is not a promise of compatibility with every inverter, EV, or charger. The planner does not control the inverter or refill the home battery itself.

## HACS

SolarSage is prepared as a **custom integration repository**. It is not yet listed in the HACS default catalog.

1. In HACS, open **Custom repositories**.
2. Add `https://github.com/bballdavis/SolarSage` with category **Integration**.
3. Download **SolarSage**, then restart Home Assistant.
4. Open **Settings → Devices & services → Add integration → SolarSage**.
5. Choose **Site planner**. Map the site, charger, forecast, and tariff inputs.
6. For a Rivian, add SolarSage again and choose **Rivian schedule control**. Sign in, select the vehicle, and map its telemetry. Leave dry run on.
7. On the site planner entry, use **Add car** and map its actuator entities. Add price windows if needed.

Both parts ship in the single `solar_ev_planner` integration directory. Tesla users do not need to configure Rivian or provide a Rivian account. Rivian schedule control can also be configured without the site planner.

Keep shadow mode and Rivian dry run enabled while you complete [configuration](PLANNER.md) and [commissioning](TESTING.md).

## Manual installation

Download the source ZIP from GitHub and copy **the entire** `custom_components/solar_ev_planner` directory into `/config/custom_components/`. The bundled `rivian/` and `www/` directories must come with it. Restart Home Assistant, then follow steps 4–7 above.

Do not copy `website/`, `docs/`, tests, tools, or the repository root into `custom_components`.

## Updates

Back up Home Assistant first. Update with HACS, or replace the complete integration directory, then restart. Configured mappings and planner memory live in Home Assistant's configuration storage, outside the integration code. Check the dashboard and logs after every update before resuming live control.

The internal domain and websocket routes remain `solar_ev_planner`; the card type remains `custom:solar-ev-planner-card` for compatibility. New installations use `solarsage_*` entity IDs for the site planner. Existing registered entity IDs are retained. The public name is SolarSage. Existing planner entries keep their stored mappings and entity registry records.

## Upgrading standalone Rivian Schedule Control

The earlier version used the separate `rivian_schedule_control` integration. The combined package does **not** silently move a live vehicle controller or its credentials.

1. Turn the planner's **Shadow** mode on. Do not delete the Rivian ownership journal.
2. Use the old controller's **Release** / **Restore owner's schedule** action. Confirm its control state is handed back and verify the original schedule in the Rivian app. If restoration faults, resolve it with the old controller before continuing.
3. Remove the old Rivian Schedule Control config entry. Then uninstall its old HACS package or remove `/config/custom_components/rivian_schedule_control`, and restart Home Assistant.
4. Install/update SolarSage and choose **Rivian schedule control** from its setup menu. Authenticate again; dry run starts on.
5. Check the newly created switch/current entity IDs and remap the planner, automations, and any tools that reference them. The service namespace is now `solar_ev_planner`.
6. Repeat the attended Rivian trial before enabling live planning.

SolarSage refuses to start bundled Rivian control while any old standalone Rivian Schedule Control entry remains. The vehicle-keyed ownership journal retains its original `rivian_schedule_control.*` filename so unfinished ownership is not discarded. An old entry-keyed journal still requires the documented manual recovery; do not bypass that check.

## Uninstalling

Turn on shadow mode, restore any Rivian schedules and verify the handback in the vehicle app. Remove the config entries, then uninstall through HACS or remove the integration directory and restart. Removing the planner entry deletes its stored planner settings and memory. Keep a backup if you intend to reinstall.
