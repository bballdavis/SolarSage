# Solar charging dashboard card

The integration sidebar remains available at **Solar charging**. To add the compact card to a dashboard, ensure Home Assistant has loaded the integration frontend, then add this static module as a Lovelace resource if needed:

```text
/rivian_solar_static/solar-panel.js?v=1.7.0b2
```

Choose **Solar charging** in the card picker, or use YAML:

```yaml
type: custom:solar-charging-card
controller: sensor.your_solar_controller
```

The editor chooses a controller sensor marked by this integration. The card never substitutes another controller when the configured one is absent or stale.

**Setup** opens Home Assistant's native Rivian options flow for that entry. It does not write raw options. Map measurements, vehicles, forecasts, and safety tuning while solar charging remains disabled; enable the feature last. Saving never runs commissioning; automatic switching remains separately paused, dry-run, and safety guarded. Only administrators can open setup or physical tests; the backend still authorizes each service call.

The sidebar and static card resource are registered even before the solar feature is configured. If Home Assistant has not loaded its native `ha-form` control in the current view, Setup provides a Settings link instead of submitting an empty form. The local preview uses a simulated form; native frontend behavior still needs verification in the installed HA version.

Wall telemetry is read-only. Vehicle commands use the configured Rivian command path. A missing paired stop entity means physical-stop commissioning is unavailable, not ready. The scoped stop confirmation never restarts charging and Escape cancels it.
