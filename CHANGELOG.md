# Changelog

## 0.2.0 — Unreleased

- Rename the public project, setup and dashboard header to SolarSage, using the supplied transparent artwork.
- Bundle Rivian schedule control in the same Home Assistant integration and HACS package as the site planner. The SolarSage setup menu offers both entry types.
- Keep existing planner registry IDs, the integration domain, dashboard card tag, websocket routes, and vehicle-keyed Rivian journals stable. New site entities use the SolarSage name.
- Move Rivian services to `solar_ev_planner`. Existing standalone installations must restore and remove the old controller before configuring bundled control; see the upgrade guide.
- Add a light Docusaurus documentation site built from `docs/`, GitHub Pages deployment, CI checks, release packaging and local integration branding.
- Add the MIT license, contribution and security guidance, synthetic dashboard screenshots, and generic public configuration examples.
- Remove the tracked local workspace file and ignore local configuration, recordings and generated files.

This release remains experimental. Automated tests use simulated Home Assistant and vehicle APIs. Real-system commissioning and marketplace validation are separate release gates.
