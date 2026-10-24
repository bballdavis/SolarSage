# Solar EV charging ecosystem survey (October 2026)

Research date: October 24, 2026. This is the evidence base for the [architecture decision](SOLAR_CHARGING_ARCHITECTURE.md).

- **Method.** Upstream projects were shallow-cloned at the commits below and read directly. Web searches covered pricing, community reports, and release news.
- **No live contact.** Nothing contacted a vehicle, charger, account, or the Home Assistant instance.
- **Confidence labels.**
  - **High** means the source code or official document was read.
  - **Medium** means a credible secondary source.
  - **Low** means inference.
- **Accepted versus physical.** Software accepting a command is always kept separate from a vehicle physically changing current.

## Snapshot of sources

| Project | Commit | Head date | Notes |
| --- | --- | --- | --- |
| [evcc-io/evcc](https://github.com/evcc-io/evcc) | `bf5c276` | 2026-10-23 | Latest release tag 0.318.0; HA add-on 0.317.2 |
| [evcc-io/docs](https://github.com/evcc-io/docs) | `3a6a782` | 2026-10-24 | |
| [marq24/ha-evcc](https://github.com/marq24/ha-evcc) | `c974aa1` | 2026-10-19 | HACS integration `evcc_intg`; no evcc integration exists in HA core |
| [bretterer/home-assistant-rivian](https://github.com/bretterer/home-assistant-rivian) | `088fd22` | 2026-10-23 | Release 2.0.0; requires `rivian-python-client` 2.3.1 |
| [bretterer/rivian-python-client](https://github.com/bretterer/rivian-python-client) | `67ba96f` | 2026-10-19 | PyPI 2.3.1 |
| [rchav/rivian_solar_charge_ha](https://github.com/rchav/rivian_solar_charge_ha) | `0c4e6ec` | 2026-06-18 | |
| [ostap-korkuna/rivian-charging-automation](https://github.com/ostap-korkuna/rivian-charging-automation) | `c3cd9e1` | 2026-01-24 | |
| [home-assistant/core](https://github.com/home-assistant/core) | `3902d41` | 2026-10-24 | Sparse: Tesla family, Wall Connector, OpenEVSE |
| [alandtse/tesla](https://github.com/alandtse/tesla) | `87010a3` | 2026-10-22 | v3.28.0 |
| [yoziru/esphome-tesla-ble](https://github.com/yoziru/esphome-tesla-ble) | `a720276` | 2026-10-19 | |
| [yoziru/ha-tesla-ble](https://github.com/yoziru/ha-tesla-ble) | `57da60d` | 2026-10-19 | First release 2026-10-12 |
| [wimaha/TeslaBleHttpProxy](https://github.com/wimaha/TeslaBleHttpProxy) | `2ab9416` | 2026-10-19 | 2.5.0 |
| [zany92/tesla-loadpilot](https://github.com/zany92/tesla-loadpilot) | `5e83b93` | 2026-08-31 | 1.3.1 |
| [PVi1/esphome-twc-control](https://github.com/PVi1/esphome-twc-control) | `7ae2336` | 2026-09-14 | |
| [flashg1/SolarCharger](https://github.com/flashg1/SolarCharger) | `52bec46` | 2026-10-18 | 0.13.4; replaces the deprecated evSolarCharger blueprint |
| [pkuehnel/TeslaSolarCharger](https://github.com/pkuehnel/TeslaSolarCharger) | `e342e39` | 2026-10-19 | Proprietary licence |
| [InventoCasa/ha-advanced-blueprints](https://github.com/InventoCasa/ha-advanced-blueprints) | — | 2024-06-14 | PV Excess Control blueprint; inactive |

## evcc

| Finding | Evidence | Conf. |
| --- | --- | --- |
| The `twc3` charger reads `/api/1/vitals` for status (A/B/C from `vehicle_connected` and `contactor_closed`) and metering. It forwards `Enable` to the active vehicle's `ChargeController` and `MaxCurrent` to its `CurrentController`. No vehicle-type check exists in code. | [`charger/twc3.go#L100-L163`](https://github.com/evcc-io/evcc/blob/bf5c276cdb395443fda4601b99151c0a962d0db4/charger/twc3.go#L100-L163) | High |
| If the vehicle lacks current control, `MaxCurrent` is ignored in "now" mode and errors in any other mode. Smart mode calls `MaxCurrent` before `Enable`, so a car without a current setter never starts in smart mode on a TWC3. | [`twc3.go#L135-L151`](https://github.com/evcc-io/evcc/blob/bf5c276cdb395443fda4601b99151c0a962d0db4/charger/twc3.go#L135-L151), [`core/loadpoint.go#L1028-L1055`](https://github.com/evcc-io/evcc/blob/bf5c276cdb395443fda4601b99151c0a962d0db4/core/loadpoint.go#L1028-L1055) | High |
| The `homeassistant` vehicle provides start/stop (one `switch`, or two `script`s), `setMaxCurrent` (`number`/`input_number`), status mapping, SoC, limit SoC (read-only), and wake-up. It does not provide a current getter. | [`vehicle/homeassistant.go#L26-L123`](https://github.com/evcc-io/evcc/blob/bf5c276cdb395443fda4601b99151c0a962d0db4/vehicle/homeassistant.go#L26-L123), [template](https://github.com/evcc-io/evcc/blob/bf5c276cdb395443fda4601b99151c0a962d0db4/templates/definition/vehicle/homeassistant.yaml) | High |
| Running as the HA add-on, evcc authenticates with the supervisor token at `uri: http://supervisor/core`. The long-lived `token:` key is deprecated in favor of an OAuth flow. | `util/homeassistant/{supervisor,config}.go` at the pinned commit | High |
| The `homeassistant` meter template supports grid, PV, battery, and charge roles. A leading `-` on the entity inverts the sign. Battery adds SoC, capacity, and optional mode scripts. | [meter template](https://github.com/evcc-io/evcc/blob/bf5c276cdb395443fda4601b99151c0a962d0db4/templates/definition/meter/homeassistant.yaml) | High |
| The `homeassistant` solar tariff reads the HA Energy-dashboard forecast (Solcast, Forecast.Solar) hourly, so it costs no extra Solcast API calls. In free evcc forecasts are displayed only. The sponsor-only, experimental optimizer is the decision consumer. | [tariff template](https://github.com/evcc-io/evcc/blob/bf5c276cdb395443fda4601b99151c0a962d0db4/templates/definition/tariff/homeassistant.yaml), [optimizer docs](https://github.com/evcc-io/docs/blob/3a6a78295a35287ea9cbe4a12b035b58076c7f8b/src/content/docs/en/features/optimizer.mdx) | High |
| Smart mode uses a 30 s control interval, a 1 min enable delay, and a 3 min disable delay by default. It targets `offered + (−sitePower)/(230 × phases)` and sends whole amps when the charger lacks milliamp control. `sitePower = grid + battery + residual − aux − flexible`. | [`core/loadpoint.go#L1816-L1986`](https://github.com/evcc-io/evcc/blob/bf5c276cdb395443fda4601b99151c0a962d0db4/core/loadpoint.go#L1816-L1986), [`core/site.go#L1240`](https://github.com/evcc-io/evcc/blob/bf5c276cdb395443fda4601b99151c0a962d0db4/core/site.go#L1240) | High |
| Battery: below `prioritySoc`, battery charging is removed from the surplus. Above `bufferSoc`, discharge may continue a session. Above `bufferStartSoc`, a session may start from the battery. `batterySupported()` holds the minimum current through clouds. | [`core/site.go#L1193-L1240`](https://github.com/evcc-io/evcc/blob/bf5c276cdb395443fda4601b99151c0a962d0db4/core/site.go#L1193-L1240), [battery docs](https://github.com/evcc-io/docs/blob/3a6a78295a35287ea9cbe4a12b035b58076c7f8b/src/content/docs/en/features/battery.mdx) | High |
| Vehicle detection: with no default vehicle, evcc polls every configured vehicle's status for 10 minutes after plug-in. It "gives up" when more than one matches. Location is not used. A pinned default vehicle disables detection. | [`core/coordinator/coordinator.go#L132-L179`](https://github.com/evcc-io/evcc/blob/bf5c276cdb395443fda4601b99151c0a962d0db4/core/coordinator/coordinator.go#L132-L179), [`core/loadpoint_vehicle.go#L19-L22`](https://github.com/evcc-io/evcc/blob/bf5c276cdb395443fda4601b99151c0a962d0db4/core/loadpoint_vehicle.go#L19-L22) | High |
| Per-vehicle `onIdentify` settings (mode, min/max current). Effective minimum = max(vehicle, loadpoint); effective maximum = min(loadpoint, vehicle, charger). Loadpoint min/max/phases in YAML are ignored; set them in the UI or API. | [`core/loadpoint.go#L114-L118`](https://github.com/evcc-io/evcc/blob/bf5c276cdb395443fda4601b99151c0a962d0db4/core/loadpoint.go#L114-L118), `core/loadpoint_effective.go` | High |
| A charger without known phases defaults to three phases until it measures otherwise. The nominal voltage is a fixed 230 V. | [`core/loadpoint.go#L315-L324`](https://github.com/evcc-io/evcc/blob/bf5c276cdb395443fda4601b99151c0a962d0db4/core/loadpoint.go#L315-L324), [`util/grid/grid.go#L10`](https://github.com/evcc-io/evcc/blob/bf5c276cdb395443fda4601b99151c0a962d0db4/util/grid/grid.go#L10) | High |
| Split-phase TWC3: current = `currentA + currentB`, power = `IA·VA + IB·VB`. Whether US firmware reports full current on both legs (which would double-count) is not established. | [`charger/twc3.go#L191-L218`](https://github.com/evcc-io/evcc/blob/bf5c276cdb395443fda4601b99151c0a962d0db4/charger/twc3.go#L191-L218) | High (code) / unverified (firmware) |
| An `unavailable` grid entity is not treated as an error, so the previous value is kept. | [`core/site.go#L1050-L1057`](https://github.com/evcc-io/evcc/blob/bf5c276cdb395443fda4601b99151c0a962d0db4/core/site.go#L1050-L1057) | High |
| No sponsor token is needed for `twc3`, `tesla`, `tesla-ble`, `openevse`, or `homeassistant` devices. Generic OCPP and many commercial chargers need one ($4/month or $150 lifetime via GitHub Sponsors). | [`ocpp.yaml#L50`](https://github.com/evcc-io/evcc/blob/bf5c276cdb395443fda4601b99151c0a962d0db4/templates/definition/charger/ocpp.yaml#L50), [sponsorship docs](https://github.com/evcc-io/docs/blob/3a6a78295a35287ea9cbe4a12b035b58076c7f8b/src/content/docs/en/sponsorship.mdx) | High |
| `ha-evcc` (HACS) exposes:<ul><li>selects: mode, always-charge, vehicle assignment, min/max current, priority/buffer SoC;</li><li>numbers: thresholds, delays, residual power;</li><li>switches;</li><li>plan services.</li></ul>Local push over evcc's websocket. | [`ha-evcc` const.py](https://github.com/marq24/ha-evcc/blob/c974aa17bcd93695b8cea57d0b6517c11f2994c3/custom_components/evcc_intg/const.py) | High |
| evcc 0.316 (September 2026) renamed modes to `off`/`smart`/`now`; `minpv` maps to smart with "always charge". | [blog](https://github.com/evcc-io/docs/blob/3a6a78295a35287ea9cbe4a12b035b58076c7f8b/src/content/docs/en/blog/2026/09/25/highlights-smart-battery-solar-share.mdx) | High |
| evcc has no Rivian vehicle; `grep -ri rivian` finds nothing. | evcc source at `bf5c276` | High |

## Tesla

### Control paths

| Option | Transport and cost | Maturity | Fit |
| --- | --- | --- | --- |
| **HA core Teslemetry with Bluetooth vehicle sub-entry** | BLE first, per-command cloud fallback through `VehicleRouter`; streamed telemetry. About US$30/year, and BLE commands use no credits. | Core integration, platinum quality scale. The BLE path shipped in HA 2026.10. | **Primary** |
| `esphome-tesla-ble` on a dedicated ESP32 | Local BLE, $0 | Since July 2024, actively tagged | Fallback actuator, or a $0 primary |
| HA core `tesla_fleet` (own developer account) | Fleet API against Tesla's monthly credit | Core | Cloud fallback in a $0 stack |
| `ha-tesla-ble` (HACS, HA Bluetooth stack) | Local BLE, $0 | First release 2026-10-12 | Watch |
| TeslaBleHttpProxy with evcc `tesla-ble` | Local BLE, $0; no authentication on the proxy | evcc-recommended, since 2024 | Best if evcc talks to the car directly |
| `tesla_custom` (installed today) | Owner API data. Commands need the external Tesla HTTP Proxy add-on, which its author marked unmaintained in January 2025. | Integration active (v3.28.0, 2026-10-22) | **Retire** for unattended control |
| TeslaSolarCharger | Proprietary, paid activation | Active | Rejected (see orchestrators) |

Evidence:

- **Teslemetry BLE routing:** [`teslemetry/__init__.py#L327-L376`](https://github.com/home-assistant/core/blob/3902d41667731e9a2cb911d73b023b859ba5b299/homeassistant/components/teslemetry/__init__.py#L327-L376), plus the [HA 2026.10 release notes](https://rc.home-assistant.io/blog/2026/10/07/release-202610/).
- **`tesla_custom` dependencies:** [`tesla_custom` README](https://github.com/alandtse/tesla/blob/87010a33df0c6119c4134838dbd7253b6af5edeb/README.md); proxy status from the `llamafilm/tesla-http-proxy-addon` README.
- **BLE projects:** each repository at the pinned commit.
- **Prices:** subscription and pricing figures in the table are medium confidence; recheck them before subscribing.

### Behavior relevant to a solar loop

- **Amps change live without restarting.** evcc, TeslaSolarCharger, and evSolarCharger all modulate `set_charging_amps` mid-session (high for software).
- **The 5 A boundary needs a second command.** Both `teslajsonpy` and TeslaSolarCharger resend when crossing 5 A. Below 5 A the reported current reads about 1 A high and efficiency is poor, so use a floor of 5–6 A and stop below it (high for code, medium for vehicle behavior).
- **Start takes about 11–16 s** from switch-on to ramp (evSolarCharger measurements, medium).
- **Charging amps are sticky.** A later manual or app start resumes at the last amps the controller set. Restore a normal value at session end (medium; [Charge HQ](https://chargehq.net/kb/advanced-settings)).
- **A BLE link held open keeps the car awake.** Integrations disconnect when idle (high).

### Fleet API cost model

The model covers the Tesla only, on the user's own developer account. Rates are medium confidence, triangulated from [Tesla billing docs](https://developer.tesla.com/docs/fleet-api/billing-and-limits), [notateslaapp](https://notateslaapp.com/news/2415/tesla-announces-api-pricing-third-party-service-costs-expected-to-rise), and [evcc discussion #17501](https://github.com/evcc-io/evcc/discussions/17501):

- commands: 1,000 per US$1;
- data requests: 500 per $1;
- wakes: 50 per $1;
- monthly credit: $10;
- limits: 30 commands/min and 3 wakes/min.

Model assumptions: 7 sunny hours a day, 30 days, about 4 start/stops a day, and 10-minute polling.

| Amp-update cadence | Gross / month | After $10 credit |
| --- | --- | --- |
| Every 5 min | about $6.70 | $0 |
| Every 2 min | about $10.50 | about $0.50 |
| Every 1 min | about $16.80 | about $6.80 |
| Every 1 min plus 1-min `vehicle_data` feedback polling | about $39.50 | about $29.50 |

Never use cloud `vehicle_data` as the fast feedback signal. Use TWC3 vitals, which evcc reads locally, or streamed data. Over BLE the command cost is zero.

## Rivian

| Finding | Evidence | Conf. |
| --- | --- | --- |
| Remote vehicle commands need a phone key paired over Bluetooth, which "isn't supported yet for Gen2 R1T and R1S vehicles (2025 model year and later)". | [`README.md#L59-L61`](https://github.com/bretterer/home-assistant-rivian/blob/088fd22cb0a82c9c8752c5b8e483096738166891/README.md#L59-L61) | High |
| `charging_enabled` (START/STOP_CHARGING) exists in 2.x but only for phone-key vehicles. It is available when `remoteChargingAvailable` is true or charging is active. | [`switch.py#L34-L50`](https://github.com/bretterer/home-assistant-rivian/blob/088fd22cb0a82c9c8752c5b8e483096738166891/custom_components/rivian/switch.py#L34-L50) | High |
| Schedule amperage `number` (8–48 A, step 2), schedule-enabled `switch`, and schedule start/end `time` entities are created for all vehicles. | [`number.py#L46-L80`](https://github.com/bretterer/home-assistant-rivian/blob/088fd22cb0a82c9c8752c5b8e483096738166891/custom_components/rivian/number.py#L46-L80), [`switch.py#L76-L106`](https://github.com/bretterer/home-assistant-rivian/blob/088fd22cb0a82c9c8752c5b8e483096738166891/custom_components/rivian/switch.py#L76-L106), [`time.py`](https://github.com/bretterer/home-assistant-rivian/blob/088fd22cb0a82c9c8752c5b8e483096738166891/custom_components/rivian/time.py) | High |
| Schedule writes merge the change into the first schedule and send `[updated_schedule]` alone. The local cache keeps the other schedules. If no schedule exists, an all-day default at HA's home coordinates is created. | [`coordinator.py#L415-L453`](https://github.com/bretterer/home-assistant-rivian/blob/088fd22cb0a82c9c8752c5b8e483096738166891/custom_components/rivian/coordinator.py#L415-L453) | High (code); server list-replacement semantics unverified |
| The GraphQL operations are `getVehicleChargingSchedules` (`weekDays`, `startTime`, `duration`, `location`, `amperage`, `enabled`) and `setChargingSchedules(vehicleId, [InputChargingSchedule!]!)` returning `success`. | [`rivian.py#L473-L505`](https://github.com/bretterer/rivian-python-client/blob/67ba96fc16bc810b88f9ddf86fcf5955a494d5b0/src/rivian/rivian.py#L473-L505) | High |
| `send_vehicle_operation` writes Parallax RVMs "Unlike `send_vehicle_command` … does not need an enrolled phone". It is used today only for climate-hold settings. The protobufs show a writable-looking `ScheduleTimeWindow` with amps and a `SocSlider` charge limit. No evidence shows charging RVM writes are accepted. | [`rivian.py#L765-L830`](https://github.com/bretterer/rivian-python-client/blob/67ba96fc16bc810b88f9ddf86fcf5955a494d5b0/src/rivian/rivian.py#L765-L830), [`parallax/charging.py`](https://github.com/bretterer/rivian-python-client/blob/67ba96fc16bc810b88f9ddf86fcf5955a494d5b0/src/rivian/parallax/charging.py) | High (code) / research lead |
| `rchav`:<ul><li><b>Charge:</b> an all-day, all-week schedule at 8–48 A, written every 5 minutes.</li><li><b>Stop:</b> keeps the schedule enabled and moves a 1-hour window about 12 h ahead, re-pushed before it arrives. `enabled: false` "does NOT pause charging".</li></ul>It replaces the whole list with one schedule and never restores it. | [`rivian_client.py#L228-L289`](https://github.com/rchav/rivian_solar_charge_ha/blob/0c4e6ec9d48a576e9a8dcdf2f6ac775867af433b/custom_components/rivian_solar_charging/rivian_client.py#L228-L289) | High (code) / medium (behavior) |
| `ostap-korkuna` uses the same schedule technique, "tested on R1S" (generation not stated). | [README](https://github.com/ostap-korkuna/rivian-charging-automation/blob/c3cd9e1f69b12dc5233e49cfb9b478a56210fe90/README.md) | Medium |
| An owner reports that app schedule amperage adjusts rates "while charging at home … mid charge". Another reports a bad-request error from the HA slider. | [Upstream issue #244](https://github.com/bretterer/home-assistant-rivian/issues/244) | Medium/low |
| Rivian's own smart charging (with WeaveGrid) shifts charging to cheaper or cleaner **grid** periods; no home-solar following was found. Charging states include `smart_charging_active`/`smart_charging_paused`. | [RivianTrackr](https://riviantrackr.com/news/rivian-joins-ionna-and-launches-smart-charging-to-make-charging-smarter-and-simpler/), Parallax `charging.proto` | Medium |
| A 2025 R1S has a CCS1/J1772 inlet and ships with a NACS DC adapter. AC charging on a NACS EVSE needs a separate NACS-to-J1772 adapter. | [Lectron guide](https://ev-lectron.com/blogs/blog/when-will-rivian-have-nacs), [owner thread](https://www.rivianforums.com/forum/threads/nacs-to-j1772-adapter-recommendations.54645/post-928486) | Medium |

## Charger side

| Option | Findings | Conf. |
| --- | --- | --- |
| TWC3 local API | Read-only vitals, lifetime, version and Wi-Fi status. evcc's template: "cannot be controlled directly. Control is via the vehicle." | High |
| Meter emulation: `tesla-loadpilot` | Emulates a Neurio meter on the TWC3 RS485 bus, so any car obeys. Production on one French three-phase site, firmware 26.18 and 26.26.1 ([README#L11](https://github.com/zany92/tesla-loadpilot/blob/5e83b936d055fa0ff375052f4592a28731dd6cb2/README.md#L11)). Limits:<ul><li>single-phase support is "theoretical" ([#L194](https://github.com/zany92/tesla-loadpilot/blob/5e83b936d055fa0ff375052f4592a28731dd6cb2/README.md#L194));</li><li>the charger may distrust the meter "sometimes for hours" ([#L57](https://github.com/zany92/tesla-loadpilot/blob/5e83b936d055fa0ff375052f4592a28731dd6cb2/README.md#L57));</li><li>an absent meter falls back to a documented 6 A cap ([#L167](https://github.com/zany92/tesla-loadpilot/blob/5e83b936d055fa0ff375052f4592a28731dd6cb2/README.md#L167));</li><li>firmware 26.2+ gates the meter menu behind installer credentials ([#L113](https://github.com/zany92/tesla-loadpilot/blob/5e83b936d055fa0ff375052f4592a28731dd6cb2/README.md#L113)).</li></ul> | High |
| Meter emulation: `esphome-twc-control` | Same principle. The README describes the charger reacting only about 1.1 A above its own limit. | High |
| OpenEVSE | HA core integration: `charge_rate` number, `override_state` select, `solar_pv_divert`, `current_shaper` and `manual_override` switches ([`openevse/`](https://github.com/home-assistant/core/tree/3902d41667731e9a2cb911d73b023b859ba5b299/homeassistant/components/openevse)). evcc `openevse` template (claims API, firmware 4.1.9+), sponsor-free. NACS 48 A versions listed at about $599 (kit) and about $679 (assembled) ([store](https://store.openevse.com/products/advanced-series-48a-level-2-nacs-kit-bundle)). | High (code) / medium (price) |
| OCPP chargers (Grizzl-E and others) | Work with evcc's OCPP server, which needs a sponsor token. HA has a HACS OCPP integration. Do not run two OCPP masters. | High |

## Orchestrators

| Project | Fit for Tesla + Gen 2 Rivian on one TWC3 |
| --- | --- |
| **evcc** | Best fit. Mature policy, vehicle-delegating TWC3 driver, HA-entity vehicles, battery logic, plans, UI, HA integration. |
| **SolarCharger** (flashg1, HA integration 0.13.4) | Strongest HA-native alternative. Chargers for Tesla (`tesla_custom`, Fleet, Teslemetry, Tessie, ESP BLE, MQTT BLE), OCPP, several other brands, and a `user_custom` charger for generic entities. Allocates surplus by device priority from one net-power sensor. Single maintainer; README status "work in progress". No Rivian adapter. |
| **TeslaSolarCharger** | Rejected:<ul><li>proprietary licence with paid activation ([LICENSE](https://github.com/pkuehnel/TeslaSolarCharger/blob/e342e39d820812568aea3954d2875a4eb4125bd1/LICENSE));</li><li>non-Tesla cars only through an OCPP station ([`CarType.cs`](https://github.com/pkuehnel/TeslaSolarCharger/blob/e342e39d820812568aea3954d2875a4eb4125bd1/TeslaSolarCharger/Shared/Enums/CarType.cs));</li><li>no TWC3 driver.</li></ul> |
| **rchav / ostap-korkuna** | Rivian-only schedule controllers; useful as references for the schedule technique. |
| **evSolarCharger** (blueprint) | Deprecated by its author in favor of SolarCharger. |
| **PV Excess Control** blueprint | Last commit June 2024. |
| **solar_optimizer**, **EMHASS**, **Predbat** | Not evaluated in depth; they are device or battery optimizers rather than EV loadpoint controllers for a shared, vehicle-commanded charger. |

## Energy site

- **No native evcc template fits.** The Deye/Sunsynk templates are three-phase Modbus. Use evcc's `homeassistant` meter template over the existing Sol-Ark MQTT entities, with `-` on battery power.
- **Battery control in evcc is optional.** Active battery modes (hold or charge) would need HA scripts that write Sol-Ark settings. The `prioritySoc`/`bufferSoc`/`bufferStartSoc` logic should be enough to start.
- **Solcast.** Feed it to evcc through the HA Energy-dashboard forecast (`homeassistant` tariff). Do not use evcc's own `solcast` template, which would share the hobbyist 10-call/day quota with the HA integration.
- **TWC3 telemetry.** HA's `tesla_wall_connector` integration showed fetch timeouts and stale states in September. evcc polls the vitals itself with a 10 s HTTP timeout. Wi-Fi problems would still affect both, so check the TWC3's network link during commissioning.

## Limits of this survey

- Most vendor websites (Tesla developer docs, evcc docs site, HA docs site) were unreachable from the research sandbox. Their source repositories and search results were used instead. Prices are "as listed in search results" and need rechecking before purchase.
- **Vehicle behavior is unmeasured.** No live behavior of this Tesla, this R1S, or this TWC3 was observed. All vehicle-behavior claims above come from other owners' projects and reports.
- Two upstream points need confirming on this installation:
  - **Schedule list semantics.** Whether `setChargingSchedules` replaces the whole schedule list.
  - **TWC3 split-phase readings.** What the TWC3 split-phase per-leg currents report.
