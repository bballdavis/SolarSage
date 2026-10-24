# 1.7.0b2 commissioning-beta evidence

For the September 25 exploratory replay, live Home Assistant inventory, and
hardware commissioning gates, see [Exploratory test plan](EXPLORATORY_TEST_PLAN.md).
That update passed 130 local tests on each pinned Home Assistant version after
two additional replay tests and retains live vehicle and charger control as
unproved. The results below are the September 19 baseline.

Verified locally on September 19, 2026 (America/Chicago). The deployed HA MCP
was used read-only to inspect real entity units, timestamps, histories and the
Savant dashboard configuration. No configuration writes or physical commands
were sent to Home Assistant, a vehicle, or a charger. No live control capability
is certified.

## Automated checks

| Runtime | Result |
| --- | --- |
| Python 3.13, Home Assistant 2025.1.4 | 105 passed |
| Python 3.14, Home Assistant 2026.9.3 | 105 passed |
| Node frontend contract checks | Passed |
| Ruff, Python compilation, JavaScript syntax, translation JSON, diff whitespace | Passed |

The HA runs use `rivian-python-client[ble]==2.0.0`, pytest and pyserial in isolated
environments. Dependency deprecation warnings remain (7 on the minimum version,
5 on the current version). CI is configured for the same two HA/Python pairs;
these results are local results, not a claim that remote CI has completed.

Coverage includes malformed/future/stale inputs, elapsed-time ramps, brief clouds,
cooldown after physical stops, forecast policy, reserve and manual safety gates,
in-place adjustments, schedule ownership and restoration order, no-op OFF commands,
changed connection during feedback, paused restart recovery, retained legacy
journals, and grouped configuration/panel lifecycle.

Beta 2 adds strict Solcast estimate selection, Wh/kWh normalization, independent
provider/derived-entity freshness, aware timestamp validation, missing physical
input rejection, unit/age diagnostics, and first-run UI registration without
creating a charging controller. Frontend checks cover exact controller selection,
unavailable retained state, missing native forms, validation errors, retained form
data, and canceled asynchronous setup responses. Form tests verify current and
default values survive menu navigation, false/zero values stay intact, menu
metadata is not saved, optional cleared fields stay omitted, and normal HA updates
do not rebuild an open form. Native label/error/selector callback contracts and
light/dark primary-button contrast have dedicated regression assertions.

The native-HA tests instantiate actual Home Assistant state, service and storage
objects locally. One registers an OFF service that accepts the call but does not
change device state, then verifies the failure and journal through a new Store.
Other coordinator tests use controlled sensor/service doubles. These are not
network, vehicle firmware, or fully running HA frontend tests.

## Interface evidence

The local preview is `tools/preview-panel.html`; every action is simulated.
Desktop (1280 x 1000) and phone (390 x 844) were rendered and independently reviewed.
Checks cover the expanded mobile layout without horizontal overflow, pause/enable,
manual-mode action transitions, retaining expanded diagnostics after updates,
disabled dry-run actions, stale/empty states, and visible service failure messages.
The existing Savant card's compact spacing, rounded surfaces, subtle gradients,
and HA theme variables guided the interface. The new card was rendered at desktop
and phone widths in light and dark themes. Setup uses a real browser modal with
Escape dismissal; its form and options API are simulated in this preview.

The actual installed HA frontend, native `ha-form` selectors, and live config-entry
reload have not been exercised. The UI falls back to Settings when `ha-form` is
unavailable, rather than submitting blank options. These local checks are not a
claim that the card has been installed or verified on the live dashboard.

Local screenshots are retained under `output/playwright/` and intentionally
excluded from the integration package. An independent safety review accepted the
source as a commissioning beta after the identified defects were corrected.
The beta-2 backend and UI received separate independent reviews. The final UI
review accepted the corrected form contracts, first-run lifecycle, and preview
captures with no remaining material fixes. This acceptance does not cover native
HA frontend or hardware commissioning. Terra review and Luna design-documentation
roles were used because the skill's dedicated role profiles were unavailable.

## Read-only installation findings

See [ENTITY_CONTRACT.md](ENTITY_CONTRACT.md) for exact mappings. Solcast next-hour
energy is Wh, remaining-today energy is kWh, and both expose estimate/P10/P90
attributes in their native units. Provider freshness comes from the API-last-polled
timestamp state, not an hourly sensor recalculation. The inspected Rivian device
has no switch/select physical charge control. The Wall Connector is telemetry-only;
its unchanged zero power/current states can be much older than its voltage state.
Those are commissioning blockers, not grounds to bypass freshness or stop gates.

The September 26, 2026 read-only HA MCP diagnostic confirmed that the
`tesla_wall_connector` integration entry is loaded, but the HA system log
contained 12 coordinator fetch timeouts; the latest was `2026-09-25 11:52:43
CDT`. Wall current/power were last reported at approximately 11:53 CDT. H1 is
therefore still blocked by a source connectivity/update issue. No connector
address was collected or retained. This diagnostic is separate from the
September 19 automated baseline of 105 passed on each pinned runtime and the
latest worker full runs of 130 passed on each pinned runtime.

## Required before unattended use

1. Confirm the exact EVSE, Rivian generation, supported paired control path,
   entity identities, power signs, units, electrical phases and update cadence.
2. Observe representative shadow-mode decisions, including clouds and missing data.
3. With automation paused, prove OFF from a real active charging session using
   the physical commissioning action. Check both the car and wall measurements.
4. Separately run the unplugged Rivian schedule round trip and check restoration.
5. Supervise the first live start/current changes/stop; test input loss and restart.

The Tesla Wall Connector integration is measurement-only. Upstream Gen 2 Rivian
paired remote control is unsupported. A successful schedule API test cannot
remove either limitation. A total host/network failure cannot be turned into a
guaranteed physical stop by this software. See [the full contract](CONTROL_DESIGN.md).
