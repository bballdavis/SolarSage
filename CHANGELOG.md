# Changelog

## Unreleased

- Dashboard: the Sun chart is now **Today: Solar, House & Car**. It is built on actuals (solar produced, house use, car charging), with the planner's adjusted expectation after now and Solcast behind it for reference. It compares solar so far with the forecast and says how the planner handles the gap.
- Dashboard: **Battery Outlook** covers today and tonight only, 24 hours centred on the daylight. It shows the measured battery level so far and clearer curve names: Expected, Cautious, Cautious with the car off, Floor, Lowest ahead.
- Dashboard: the car's plan and what it really drew no longer share a colour. The plan keeps the car's colour, measured charging is a white line on both Today and Tonight, and Tonight's start marker is green and only listed when it is drawn.
- Dashboard: **Home Now** is the Daylight energy cluster instead of a list: rings for solar, the house and the car on one shared scale around the battery level and its charge or use, a bar each side for the grid (in or out) and the battery (use or charge), the grid in words, and the four readings underneath.
- Dashboard: Health says what is missing instead of "Not set up": a Rivian controller whose car is not in the planner, the old standalone Rivian integration, an unmapped or unreadable free-hours schedule, or a second planner entry. Panel titles use title case.
- Car power can come from a current sensor (A), counted at the configured voltage. On US split-phase wiring the Wall Connector's total power reads about 28% high, which pushed the house use down to zero while the car charged; its vehicle current is the better choice there.
- The websocket snapshot adds grid, battery-power and battery-level sources, the free hours already passed today, why there are no free hours, and what is set up. With two planner entries the card follows the configured one.

## 0.2.0 — Unreleased

- Rename the public project, setup and dashboard header to SolarSage, using the supplied transparent artwork.
- Bundle Rivian schedule control in the same Home Assistant integration and HACS package as the site planner. The SolarSage setup menu offers both entry types.
- Keep existing planner registry IDs, the integration domain, dashboard card tag, websocket routes, and vehicle-keyed Rivian journals stable. New site entities use the SolarSage name.
- Move Rivian services to `solar_ev_planner`. Existing standalone installations must restore and remove the old controller before configuring bundled control; see the upgrade guide.
- Add a light Docusaurus documentation site built from `docs/`, GitHub Pages deployment, CI checks, release packaging and local integration branding.
- Add the MIT license, contribution and security guidance, synthetic dashboard screenshots, and generic public configuration examples.
- Remove the tracked local workspace file and ignore local configuration, recordings and generated files.

This release remains experimental. Automated tests use simulated Home Assistant and vehicle APIs. Real-system commissioning and marketplace validation are separate release gates.
