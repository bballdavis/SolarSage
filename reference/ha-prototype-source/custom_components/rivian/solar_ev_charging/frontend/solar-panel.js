/* Native Home Assistant solar-charging panel and Lovelace card. No network or third-party assets. */
const HASS_PATH = "/config/integrations/integration/rivian";
const CARD_TYPE = "custom:solar-charging-card";
const text = (value, fallback = "—") =>
  value === undefined || value === null || value === ""
    ? fallback
    : String(value);
const escape = (value) =>
  text(value, "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
const human = (value, fallback = "Not reported") => {
  const label = text(value, fallback).replaceAll("_", " ");
  return label.charAt(0).toUpperCase() + label.slice(1);
};
const finite = (value) =>
  value === null ||
  value === undefined ||
  value === "" ||
  typeof value === "boolean"
    ? undefined
    : Number.isFinite(Number(value))
      ? Number(value)
      : undefined;
const power = (value) =>
  finite(value) === undefined
    ? "—"
    : `${Math.round(finite(value)).toLocaleString()} W`;
const amps = (value) =>
  finite(value) === undefined
    ? "—"
    : `${Math.round(finite(value) * 10) / 10} A`;
const energy = (value, unit = "Wh") =>
  finite(value) === undefined
    ? "—"
    : `${Math.round(finite(value) * 100) / 100} ${unit}`;

function controllers(hass) {
  return Object.entries(hass?.states || {})
    .filter(([, state]) => state?.attributes?.solar_controller === true)
    .map(([entity_id, state]) => ({
      entity_id,
      ...state,
      attributes: state.attributes || {},
    }));
}
function controllerFor(hass, entityId, allowDefault = false) {
  const all = controllers(hass);
  if (!entityId) return allowDefault ? all[0] || null : null;
  return all.find((item) => item.entity_id === entityId) || null;
}
function modelFor(controller) {
  if (!controller) return null;
  const status = controller.attributes.status || {},
    controls = controller.attributes.controls || {},
    forecast = status.forecast || controller.attributes.forecast || {};
  const available =
    controller.state !== "unavailable" && controller.state !== "unknown";
  return {
    controller,
    status,
    controls,
    forecast,
    available,
    enabled:
      available &&
      (status.enabled !== undefined
        ? Boolean(status.enabled)
        : controller.state === "on"),
    dryRun: Boolean(status.dry_run),
    fault: status.fault,
    stale: Boolean(
      status.input_stale ||
        status.stale ||
        /stale/i.test(text(status.reason, "")),
    ),
    pending: Boolean(
      status.pending_cleanup || /pending/i.test(text(status.control_phase, "")),
    ),
  };
}
function metric(label, value, note) {
  return `<article class="metric"><span>${escape(label)}</span><strong>${escape(value)}</strong><small>${escape(note)}</small></article>`;
}
function forecastEnergy(forecast, key) {
  if (finite(forecast[`${key}_wh`]) !== undefined)
    return energy(forecast[`${key}_wh`], "Wh");
  if (finite(forecast[`${key}_kwh`]) !== undefined)
    return energy(forecast[`${key}_kwh`], "kWh");
  return "—";
}
function initialFormData(flow) {
  const initial = {};
  for (const field of flow?.data_schema || []) {
    if (!field?.name) continue;
    for (const property of ["default", "suggested_value", "value"]) {
      if (Object.hasOwn(field, property) && field[property] !== undefined)
        initial[field.name] = field[property];
    }
  }
  if (flow?.data && typeof flow.data === "object")
    Object.assign(initial, flow.data);
  return initial;
}
function sameFormValidation(previous, next) {
  return (
    previous?.type === "form" &&
    next?.type === "form" &&
    previous.step_id === next.step_id &&
    next.errors &&
    Object.keys(next.errors).length > 0
  );
}
function inputDiagnostics(status) {
  const inputs = status.inputs?.measurements;
  if (!Array.isArray(inputs) || !inputs.length) return "";
  return `<details class="diagnostics"><summary>Input freshness and units</summary><div class="input-list">${inputs.map((input) => `<div class="input ${escape(input.status || "not_configured")}"><strong>${escape(input.label || input.entity_id || "Input")}</strong><span>${escape(input.entity_id || "Not configured")}</span><small>${escape(human(input.status || "not_configured"))}${input.unit ? ` · ${escape(input.unit)}` : ""}${finite(input.age_seconds) !== undefined ? ` · ${escape(`${Math.round(finite(input.age_seconds))}s old`)}` : ""}${finite(input.max_age_seconds) !== undefined ? ` / ${escape(`${Math.round(finite(input.max_age_seconds))}s max`)}` : ""}</small></div>`).join("")}</div></details>`;
}

/** Shared view: both the retained sidebar and card use the same rendered status and command bindings. */
function overview(model, compact = false) {
  const {
    controller,
    status,
    controls,
    forecast,
    stale,
    pending,
    enabled,
    dryRun,
    fault,
    available,
  } = model;
  const cooldown = finite(status.restart_remaining_seconds),
    unavailableStop = !controls.commission_stop;
  const readiness = !available
    ? "unavailable"
    : fault
      ? "fault"
      : pending
        ? "pending"
        : stale
          ? "stale"
          : dryRun
            ? "dry-run"
            : enabled
              ? "enabled"
              : "paused";
  const disabled = (key, extra = false) =>
    !available || !controls[key] || extra ? "disabled" : "";
  return `<section class="summary ${readiness}"><div><h2>${escape(controller.attributes.entry_title || controller.entity_id)}</h2><p><strong>${escape(human(status.state || controller.state, "Unknown"))}.</strong> ${escape(human(status.reason, "No blocking reason reported"))}</p></div><span class="state-chip ${readiness}">${escape(readiness === "enabled" ? "Automatic enabled" : human(readiness))}</span></section>
    ${!available ? '<p class="callout warning" role="status"><strong>Controller is unavailable.</strong> Retained attributes are not treated as live telemetry or controls.</p>' : ""}${fault ? `<p class="callout error" role="alert"><strong>Controller fault.</strong> ${escape(human(fault))}</p>` : ""}${pending ? '<p class="callout warning" role="status"><strong>Recovery is pending.</strong> Ownership is preserved until cleanup is confirmed.</p>' : ""}${stale ? '<p class="callout warning" role="status"><strong>Feedback is stale.</strong> New charging is blocked until current readings return.</p>' : ""}${dryRun ? '<p class="callout neutral" role="status"><strong>Dry-run is active.</strong> New charge and test commands are unavailable.</p>' : ""}
    <section class="metric-grid" aria-label="Charging telemetry">${metric("Target current", amps(status.target_amps), "Requested vehicle current")}${metric("Available solar", power(status.available_watts), "Live controller estimate")}${metric("Wall power", power(status.observed_power_w), "Read-only wall telemetry")}${metric("Observed current", amps(status.observed_amps), "Wall power ÷ voltage")}${metric("Vehicle", status.active_vehicle ? human(status.active_vehicle) : "No vehicle selected", status.vehicle_mode ? `Mode: ${human(status.vehicle_mode)}` : "Connection status")}</section>
    <section class="operation"><div class="section-head"><h3>Control</h3><button class="quiet" data-setup="true" ${!controller.attributes.entry_id || !model.admin ? "disabled" : ""}>Setup</button></div><div class="actions"><button class="primary" data-action="enabled" data-entity="${escape(controls.enabled)}" data-service="${enabled ? "turn_off" : "turn_on"}" ${disabled("enabled")}>${enabled ? "Pause automation" : "Enable automation"}</button><button data-action="charge_now" data-entity="${escape(controls.charge_now)}" ${disabled("charge_now", dryRun || stale || pending)}>Charge now</button>${status.manual_override ? `<button data-action="resume" data-entity="${escape(controls.resume)}" ${disabled("resume", pending)}>End manual override</button>` : ""}<button data-action="safe_release" data-entity="${escape(controls.safe_release)}" ${disabled("safe_release")}>Stop and release control</button></div><p class="hint">${cooldown && cooldown > 0 ? `<strong>Restart cooldown:</strong> ${escape(`${Math.ceil(cooldown)} seconds remaining.`)} ` : ""}${escape(human(status.reason, "No blocking reason reported"))}</p></section>
    <section class="forecast"><div class="section-head"><h3>Forecast advice</h3><span>${escape(human(forecast.status, "Not available"))}</span></div><p>${escape(human(forecast.reason, "No forecast explanation reported"))}</p><dl><div><dt>Next hour</dt><dd>${escape(forecastEnergy(forecast, "next_hour"))}</dd></div><div><dt>Remaining today</dt><dd>${escape(forecastEnergy(forecast, "remaining"))}</dd></div><div><dt>EV budget</dt><dd>${escape(forecastEnergy(forecast, "ev_budget"))}</dd></div>${finite(forecast.source_age_seconds) !== undefined ? `<div><dt>Forecast age</dt><dd>${escape(`${Math.round(finite(forecast.source_age_seconds) / 60)} min`)}</dd></div>` : ""}</dl></section>
    ${compact ? "" : `<details class="diagnostics"><summary>Readiness and commissioning</summary><div class="diagnostic-content"><p><strong>Readiness:</strong> ${escape(human(status.capability_reason, "No capability explanation reported"))}</p><p>Wall telemetry is read-only. Vehicle commands use the configured Rivian command API; a schedule write/readback is not physical proof.</p>${unavailableStop ? '<p class="callout warning"><strong>Physical stop is unavailable.</strong> No paired vehicle stop control is configured, so this test cannot run.</p>' : ""}<div class="diagnostic-grid"><span>Control phase<strong>${escape(human(status.control_phase, "Idle"))}</strong></span><span>Acknowledged<strong>${status.command_acknowledged === true ? "Yes" : status.command_acknowledged === false ? "No" : "—"}</strong></span><span>Last feedback<strong>${escape(text(status.last_feedback_at))}</strong></span><span>Deficit bridge<strong>${escape(energy(status.transient_deficit_wh))}</strong></span></div><div class="actions"><button data-action="probe" data-entity="${escape(controls.probe)}" ${disabled("probe", dryRun || pending)}>Test schedule control</button><button class="danger" data-confirm-stop="true" ${!controls.commission_stop || dryRun || pending || !model.admin ? "disabled" : ""}>Test physical stop</button><button data-action="clear_fault" data-entity="${escape(controls.clear_fault)}" ${disabled("clear_fault")}>Clear recovered fault</button></div><p class="hint">Tests are scoped to ${escape(controller.attributes.entry_title || controller.entity_id)}. They never enable automation or commission a vehicle by saving settings.</p></div></details>${inputDiagnostics(status)}`}`;
}

class SolarChargingSurface extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._hass = null;
    this._busy = new Set();
    this._error = "";
    this._setup = null;
    this._confirmStop = false;
    this._focusKey = "";
    customElements.whenDefined("ha-form").then(() => {
      if (this.isConnected && this._setup?.flow?.type === "form") this.render();
    });
  }
  set hass(value) {
    this._hass = value;
    const form = this.shadowRoot?.querySelector("#options-form");
    if (this._setup?.flow?.type === "form" && form) {
      form.hass = value;
      return;
    }
    this.render();
  }
  get hass() {
    return this._hass;
  }
  connectedCallback() {
    this.render();
  }
  selectedController() {
    return "";
  }
  allowDefault() {
    return false;
  }
  viewModel() {
    const model = modelFor(
      controllerFor(this._hass, this.selectedController(), this.allowDefault()),
    );
    if (model) model.admin = this.isAdmin();
    return model;
  }
  isAdmin() {
    return this._hass?.user?.is_admin === true;
  }
  captureFocus() {
    const active = this.shadowRoot?.activeElement;
    if (!active) return;
    if (active.id) this._focusKey = `#${active.id}`;
    else if (active.dataset?.action)
      this._focusKey = `[data-action="${active.dataset.action}"]`;
    else if (active.hasAttribute("data-setup")) this._focusKey = "[data-setup]";
  }
  async callControl(key, entityId, service = "press") {
    const model = this.viewModel(),
      expected = key === "enabled" ? "switch" : "button";
    if (!model || !model.available || !entityId || this._busy.has(key)) return;
    if (key === "commission_stop" && !this.isAdmin()) {
      this._error =
        "Only Home Assistant administrators can run the physical stop test.";
      this.render();
      return;
    }
    if (
      model.controls[key] !== entityId ||
      entityId.split(".")[0] !== expected
    ) {
      this._error =
        "The configured controller did not expose a valid control for that action.";
      this.render();
      return;
    }
    this._busy.add(key);
    this._error = "";
    this.render();
    try {
      await this._hass.callService(
        expected,
        expected === "switch" ? service : "press",
        {},
        { entity_id: entityId },
      );
    } catch (error) {
      this._error =
        error?.message || "Home Assistant could not complete that action.";
    } finally {
      this._busy.delete(key);
      this.render();
    }
  }
  async beginOptions() {
    const model = this.viewModel();
    if (!this.isAdmin()) {
      this._error =
        "Only Home Assistant administrators can change solar charging setup.";
      this.render();
      return;
    }
    const setup = { loading: true };
    this._setup = setup;
    this.render();
    try {
      const entryId =
        model?.controller.attributes.entry_id ||
        (await this.findRivianEntryId());
      if (this._setup !== setup) return;
      if (!entryId)
        throw new Error(
          "No single Rivian config entry could be identified for setup. Open the Rivian integration to select one.",
        );
      const flow = await this._hass.callApi(
        "POST",
        "config/config_entries/options/flow",
        { handler: entryId },
      );
      if (this._setup !== setup) return;
      setup.entryId = entryId;
      setup.flow = flow;
      if (flow?.type === "form") setup.formData = initialFormData(flow);
      setup.loading = false;
    } catch (error) {
      if (this._setup !== setup) return;
      setup.error =
        error?.message ||
        "Home Assistant could not start the native options flow.";
      setup.loading = false;
    }
    if (this._setup === setup) this.render();
  }
  async findRivianEntryId() {
    const entries = await this._hass.callApi(
      "GET",
      "config/config_entries/entry?domain=rivian",
    );
    return Array.isArray(entries) && entries.length === 1
      ? entries[0].entry_id
      : null;
  }
  localizeFlowError(code) {
    const key = `component.rivian.options.error.${code}`;
    const localized = this._hass?.localize?.(key);
    return localized && localized !== key ? localized : human(code);
  }
  localizeFlowField(stepId, field) {
    const key = `component.rivian.options.step.${stepId}.data.${field.name}`;
    const localized = this._hass?.localize?.(key);
    return localized && localized !== key ? localized : human(field.name);
  }
  localizeFlowHelper(stepId, field) {
    const key = `component.rivian.options.step.${stepId}.data_description.${field.name}`;
    const localized = this._hass?.localize?.(key);
    return localized && localized !== key ? localized : "";
  }
  flowDescription(flow) {
    const key = `component.rivian.options.step.${flow?.step_id}.description`;
    const localized = this._hass?.localize?.(
      key,
      flow?.description_placeholders,
    );
    if (localized && localized !== key) return localized;
    return typeof flow?.description === "string" ? flow.description : "";
  }
  localizeFlowMenu(stepId, option, fallback) {
    const key = `component.rivian.options.step.${stepId}.menu_options.${option}`;
    const localized = this._hass?.localize?.(key);
    return localized && localized !== key ? localized : fallback;
  }
  async advanceOptions(data) {
    const setup = this._setup;
    if (!setup?.flow?.flow_id || setup.submitting) return;
    const previous = setup.flow;
    const flowId = previous.flow_id;
    const menuNavigation = previous.type === "menu";
    const submitted = data && typeof data === "object" ? data : {};
    if (!menuNavigation) setup.formData = submitted;
    setup.submitting = true;
    setup.error = "";
    this.render();
    try {
      const result = await this._hass.callApi(
        "POST",
        `config/config_entries/options/flow/${flowId}`,
        submitted,
      );
      if (this._setup !== setup) return;
      if (result.type === "create_entry" || result.type === "abort")
        this._setup = null;
      else {
        setup.flow = result;
        setup.formData =
          result?.type === "form"
            ? sameFormValidation(previous, result)
              ? submitted
              : initialFormData(result)
            : undefined;
        setup.submitting = false;
      }
    } catch (error) {
      if (this._setup !== setup) return;
      setup.error =
        error?.message ||
        "Home Assistant could not save this options-flow step.";
      setup.submitting = false;
    }
    if (this._setup === setup || this._setup === null) this.render();
  }
  setupMarkup(model) {
    if (!this._setup) return "";
    const flow = this._setup.flow,
      title =
        model?.controller?.attributes?.entry_title ||
        this.selectedController() ||
        "Rivian integration";
    const menuCopy = {
      solar_setup: "Solar setup (keep solar charging disabled while mapping)",
      solar_vehicles: "Vehicle controls and readiness",
      solar_forecast: "Forecast guidance",
      solar_tuning: "Safety tuning",
    };
    const error = flow?.errors?.base || this._setup.error;
    const errorText = error ? this.localizeFlowError(error) : "";
    const description = flow?.type === "form" ? this.flowDescription(flow) : "";
    if (flow?.type === "menu")
      return `<dialog class="setup"><form><header><h2>Configure ${escape(title)}</h2><button type="button" class="quiet" data-cancel-setup="true">Close</button></header><p>Changes are validated and persisted only by Home Assistant’s native options flow. Map measurements and vehicles first. Keep solar charging disabled in Solar setup until all required controls are present; enable it last.</p><div class="flow-menu">${(flow.menu_options || []).map((option) => `<button type="button" data-flow-menu="${escape(option)}">${escape(this.localizeFlowMenu(flow.step_id, option, menuCopy[option] || human(option)))}</button>`).join("")}</div>${error ? `<p class="callout error" role="alert">${escape(errorText)}</p>` : ""}</form></dialog>`;
    if (flow?.type === "form")
      return customElements.get("ha-form")
        ? `<dialog class="setup"><form><header><h2>Configure ${escape(title)}</h2><button type="button" class="quiet" data-cancel-setup="true">Close</button></header><p>Use this guided Home Assistant form to map measurements and controls. Saving never runs commissioning. Enable the feature last; automatic switching remains separately paused, dry-run, and safety guarded.</p>${description ? `<p class="flow-description">${escape(description)}</p>` : ""}<ha-form id="options-form"></ha-form><footer><button type="button" class="primary" data-submit-flow="true" ${this._setup.submitting ? "disabled" : ""}>${this._setup.submitting ? "Saving…" : "Save step"}</button><button type="button" data-cancel-setup="true">Cancel</button></footer>${error ? `<p class="callout error" role="alert">${escape(errorText)}</p>` : ""}</form></dialog>`
        : `<dialog class="setup"><form><header><h2>Configure ${escape(title)}</h2><button type="button" class="quiet" data-cancel-setup="true">Close</button></header><p>Home Assistant’s native form control is not loaded in this view, so this page will not submit a blank configuration.</p><p><a href="${HASS_PATH}">Open the Rivian integration in Settings</a> to continue the native options flow.</p></form></dialog>`;
    return `<dialog class="setup"><p>${this._setup.loading ? "Loading native setup…" : escape(this._setup.error || "Setup is unavailable.")}</p><button type="button" data-cancel-setup="true">Close</button></dialog>`;
  }
  confirmationMarkup(model) {
    if (!this._confirmStop) return "";
    const title =
      model?.controller?.attributes?.entry_title || this.selectedController();
    return `<dialog class="confirm"><form><h2>Test physical stop control?</h2><p>This sends <strong>Stop charging now</strong> to ${escape(title)}. It does not restart charging and it does not enable automation.</p><div class="actions"><button type="button" class="danger" data-confirm-action="commission_stop" data-entity="${escape(model.controls.commission_stop)}">Stop charging now</button><button type="button" data-cancel-confirm="true">Cancel</button></div><p class="hint">Press Escape to cancel.</p></form></dialog>`;
  }
  bind(root, model) {
    root
      .querySelectorAll("[data-action]")
      .forEach((button) =>
        button.addEventListener("click", () =>
          this.callControl(
            button.dataset.action,
            button.dataset.entity,
            button.dataset.service || "press",
          ),
        ),
      );
    root
      .querySelector("[data-setup]")
      ?.addEventListener("click", () => this.beginOptions());
    root.querySelector("[data-confirm-stop]")?.addEventListener("click", () => {
      this._confirmStop = true;
      this.render();
    });
    root
      .querySelector("[data-cancel-confirm]")
      ?.addEventListener("click", () => {
        this._confirmStop = false;
        this.render();
      });
    root
      .querySelector("[data-confirm-action]")
      ?.addEventListener("click", (event) => {
        event.preventDefault();
        this._confirmStop = false;
        this.callControl("commission_stop", event.currentTarget.dataset.entity);
      });
    root.querySelectorAll("[data-cancel-setup]").forEach((button) =>
      button.addEventListener("click", (event) => {
        event.preventDefault();
        this._setup = null;
        this.render();
      }),
    );
    root
      .querySelectorAll("[data-flow-menu]")
      .forEach((button) =>
        button.addEventListener("click", () =>
          this.advanceOptions({ next_step_id: button.dataset.flowMenu }),
        ),
      );
    root.querySelectorAll("dialog").forEach((dialog) =>
      dialog.addEventListener("cancel", (event) => {
        event.preventDefault();
        if (dialog.classList.contains("confirm")) this._confirmStop = false;
        else this._setup = null;
        this.render();
      }),
    );
    root.querySelectorAll("dialog").forEach((dialog) => {
      if (!dialog.open) dialog.showModal();
    });
    const form = root.querySelector("#options-form"),
      setup = this._setup,
      flow = setup?.flow;
    if (form && setup && flow) {
      form.hass = this._hass;
      form.context = {
        handler: flow.handler || setup.entryId,
        domain: "rivian",
      };
      form.computeLabel = (field) =>
        this.localizeFlowField(flow.step_id, field);
      form.computeHelper = (field) =>
        this.localizeFlowHelper(flow.step_id, field);
      form.computeError = (error) => this.localizeFlowError(error);
      form.localizeValue = (key) => {
        const localized = this._hass?.localize?.(key);
        return localized && localized !== key
          ? localized
          : human(String(key).split(".").at(-1));
      };
      form.schema = flow.data_schema || [];
      form.data = setup.formData ?? initialFormData(flow);
      form.error = flow.errors || {};
      form.addEventListener("value-changed", (event) => {
        if (this._setup === setup && setup.flow === flow)
          setup.formData = event.detail.value;
      });
      root
        .querySelector("[data-submit-flow]")
        ?.addEventListener("click", (event) => {
          event.preventDefault();
          const setup = this._setup;
          if (setup) this.advanceOptions(setup.formData ?? form.data ?? {});
        });
    }
    if (this._focusKey) {
      root.querySelector(this._focusKey)?.focus();
      this._focusKey = "";
    }
  }
  styles(compact = false) {
    return `:host{display:block;color:var(--primary-text-color);font-family:var(--paper-font-body1_-_font-family,Roboto,sans-serif);--surface:var(--ha-card-background,var(--card-background-color));--soft:var(--secondary-background-color);--line:color-mix(in srgb,var(--divider-color) 64%,transparent);--radius:12px;--gap:12px;--pad:16px;--shadow:0 8px 22px rgb(15 23 42 / .09)}*{box-sizing:border-box}button,select{font:inherit}button{min-height:40px;padding:0 13px;border:1px solid var(--line);border-radius:9px;background:var(--surface);color:var(--primary-text-color);cursor:pointer}button:hover:not(:disabled){border-color:var(--primary-color);background:color-mix(in srgb,var(--primary-color) 8%,var(--surface))}button:focus-visible,a:focus-visible,summary:focus-visible{outline:3px solid var(--focus-color,#03a9f4);outline-offset:2px}button:disabled{opacity:.52;cursor:not-allowed}.primary{background:var(--primary-color);border-color:var(--primary-color);color:var(--text-primary-color,#fff)}.danger{color:var(--error-color,#b3261e);border-color:color-mix(in srgb,var(--error-color,#b3261e) 62%,transparent)}.quiet{min-height:32px;padding:0 10px;background:transparent}.summary,.operation,.forecast,.diagnostics,.metric,.empty{background:linear-gradient(160deg,color-mix(in srgb,var(--surface) 96%,var(--primary-color)),var(--surface));border-radius:var(--radius);box-shadow:var(--shadow)}.summary{padding:var(--pad);display:flex;justify-content:space-between;gap:var(--gap);align-items:flex-start}.summary h2{margin:0 0 7px;font-size:${compact ? "20px" : "24px"};line-height:1.15}.summary p,.forecast p,.hint{margin:0;color:var(--secondary-text-color);line-height:1.45}.metric>span,dt{display:block;color:var(--secondary-text-color);font-size:12px;font-weight:650;letter-spacing:.03em;text-transform:uppercase}.state-chip{flex:none;border-radius:999px;padding:7px 10px;font-size:12px;font-weight:650;background:var(--soft)}.state-chip.enabled{color:var(--success-color,#2e7d32);background:color-mix(in srgb,var(--success-color,#2e7d32) 16%,transparent)}.state-chip.fault{color:var(--error-color,#b3261e);background:color-mix(in srgb,var(--error-color,#b3261e) 12%,transparent)}.state-chip.stale,.state-chip.pending{color:var(--warning-color,#a35c00);background:color-mix(in srgb,var(--warning-color,#a35c00) 14%,transparent)}.callout{margin:var(--gap) 0 0;padding:12px;border-radius:9px;line-height:1.4}.callout.error{background:color-mix(in srgb,var(--error-color,#b3261e) 11%,transparent)}.callout.warning{background:color-mix(in srgb,var(--warning-color,#a35c00) 12%,transparent)}.callout.neutral{background:var(--soft)}.metric-grid{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:var(--gap);margin-top:var(--gap)}.metric{padding:var(--pad);min-width:0}.metric strong{display:block;margin:9px 0 5px;font-size:${compact ? "19px" : "23px"};overflow-wrap:anywhere}.metric small{color:var(--secondary-text-color);line-height:1.35}.operation,.forecast,.diagnostics{padding:var(--pad);margin-top:var(--gap)}.section-head{display:flex;align-items:center;justify-content:space-between;gap:var(--gap);margin-bottom:10px}.section-head h3{font-size:16px;margin:0}.actions{display:flex;flex-wrap:wrap;gap:10px}.hint{margin-top:12px}.forecast .section-head span{font-size:12px;color:var(--secondary-text-color)}dl{display:flex;flex-wrap:wrap;gap:20px;margin:14px 0 0}dd{margin:3px 0 0;font-weight:650}.diagnostics summary{cursor:pointer;font-weight:650}.diagnostic-content{display:grid;gap:12px;padding-top:14px}.diagnostic-content p{margin:0;line-height:1.45;color:var(--secondary-text-color)}.diagnostic-grid{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:10px}.diagnostic-grid span{color:var(--secondary-text-color);font-size:12px}.diagnostic-grid strong{display:block;margin-top:4px;color:var(--primary-text-color);font-size:14px}.input-list{display:grid;gap:8px;padding-top:12px}.input{display:grid;grid-template-columns:minmax(100px,1fr) minmax(120px,2fr) minmax(120px,1fr);gap:8px;padding:8px;border-radius:8px;background:var(--soft);font-size:13px}.input span,.input small{color:var(--secondary-text-color);overflow-wrap:anywhere}.input.stale,.input.invalid,.input.unavailable{background:color-mix(in srgb,var(--warning-color,#a35c00) 12%,transparent)}dialog{border:0;border-radius:12px;background:var(--ha-card-background,var(--card-background-color));color:var(--primary-text-color);box-shadow:0 18px 48px rgb(15 23 42 / .25);max-width:min(640px,calc(100vw - 24px));padding:0}dialog::backdrop{background:rgb(15 23 42 / .4)}dialog form,dialog>p{margin:0;padding:20px}dialog header{display:flex;justify-content:space-between;gap:12px;align-items:start;margin-bottom:12px}dialog h2{margin:0;font-size:20px}dialog p{line-height:1.45}.setup ha-form{display:block;margin-top:16px}.setup footer{display:flex;flex-wrap:wrap;gap:10px;margin-top:16px}.flow-menu{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px;margin-top:16px}.flow-menu button{text-align:left;height:auto;min-height:58px}@media(max-width:900px){.metric-grid{grid-template-columns:repeat(3,minmax(0,1fr))}.diagnostic-grid{grid-template-columns:repeat(2,minmax(0,1fr))}}@media(max-width:600px){.summary{flex-direction:column}.metric-grid{grid-template-columns:repeat(2,minmax(0,1fr))}.metric-grid .metric:last-child{grid-column:1/-1}.actions button{flex:1 1 160px}.flow-menu{grid-template-columns:1fr}.input{grid-template-columns:1fr}}@media(max-width:390px){:host{--pad:12px;--gap:8px}.metric strong{font-size:18px}.actions button{flex-basis:100%}}`;
  }
}
class SolarChargingPanel extends SolarChargingSurface {
  constructor() {
    super();
    this._selected = "";
  }
  allowDefault() {
    return true;
  }
  selectedController() {
    const all = controllers(this._hass);
    if (!all.some((item) => item.entity_id === this._selected))
      this._selected = all[0]?.entity_id || "";
    return this._selected;
  }
  render() {
    if (!this.shadowRoot) return;
    this.captureFocus();
    const model = this.viewModel(),
      all = controllers(this._hass);
    const picker =
      all.length > 1
        ? `<label class="picker">Controller<select id="controller">${all.map((item) => `<option value="${escape(item.entity_id)}" ${item.entity_id === this._selected ? "selected" : ""}>${escape(item.attributes.entry_title || item.entity_id)}</option>`).join("")}</select></label>`
        : "";
    this.shadowRoot.innerHTML = `<style>${this.styles()}</style><main><header class="page-header"><div><h1>Solar charging</h1><p>Measured surplus, vehicle commands, and forecast advice in one place.</p></div>${picker}</header>${this._error ? `<p class="callout error" role="alert">${escape(this._error)}</p>` : ""}${model ? overview(model) : `<section class="empty"><h2>No controller found</h2><p>Configure solar EV charging in the Rivian integration, then return here.</p><p><a href="${HASS_PATH}">Open Rivian integration</a></p></section>`}${this.setupMarkup(model)}${model ? this.confirmationMarkup(model) : ""}</main>`;
    const main = this.shadowRoot.querySelector("main");
    this.bind(main, model);
    main.querySelector("#controller")?.addEventListener("change", (event) => {
      this._setup = null;
      this._selected = event.target.value;
      this.render();
    });
  }
  styles() {
    return `${super.styles()}main{max-width:1080px;margin:0 auto;padding:28px clamp(16px,4vw,40px) 48px}.primary{color:var(--primary-background-color,var(--text-primary-color,#fff))}.primary:hover:not(:disabled){background:var(--primary-color);color:var(--primary-background-color,var(--text-primary-color,#fff))}.flow-description{color:var(--secondary-text-color)}.page-header{display:flex;justify-content:space-between;gap:20px;align-items:flex-end;margin-bottom:20px}.page-header h1{font-size:30px;margin:0 0 6px}.page-header p{margin:0;color:var(--secondary-text-color)}.picker{display:grid;gap:6px;min-width:220px;color:var(--secondary-text-color);font-size:13px}.picker select{min-height:40px;border:1px solid var(--line);border-radius:9px;background:var(--surface);color:var(--primary-text-color);padding:0 10px}@media(max-width:600px){main{padding:20px 12px 36px}.page-header{display:block}.picker{margin-top:14px}}`;
  }
}
class SolarChargingCard extends SolarChargingSurface {
  constructor() {
    super();
    this._config = {};
  }
  setConfig(config) {
    if (!config || config.type !== CARD_TYPE)
      throw new Error(`Expected ${CARD_TYPE} card configuration.`);
    this._setup = null;
    this._config = config;
    this.render();
  }
  getCardSize() {
    return 5;
  }
  getGridOptions() {
    return { columns: 12, min_columns: 6, rows: 5, min_rows: 4 };
  }
  selectedController() {
    return this._config.controller || "";
  }
  render() {
    if (!this.shadowRoot) return;
    this.captureFocus();
    const model = this.viewModel(),
      configured = Boolean(this.selectedController());
    const setupAccess = this.isAdmin()
      ? '<button data-setup="true">Open guided setup</button>'
      : '<p class="hint">An administrator can open guided setup for this integration.</p>';
    this.shadowRoot.innerHTML = `<style>${this.styles(true)}</style><ha-card><div class="card-shell">${this._error ? `<p class="callout error" role="alert">${escape(this._error)}</p>` : ""}${!configured ? `<section class="empty"><h2>Select a solar controller</h2><p>Edit this card and choose the controller sensor marked for solar charging. This card never guesses a different controller.</p>${setupAccess}</section>` : !model ? '<section class="empty"><h2>Configured controller is unavailable</h2><p>The selected controller is not exposing its solar-controller marker. Check the integration before changing the card.</p></section>' : overview(model, true)}${this.setupMarkup(model)}${model ? this.confirmationMarkup(model) : ""}</div></ha-card>`;
    this.bind(this.shadowRoot, model);
  }
  styles(compact) {
    return `${super.styles(compact)}ha-card{display:block;padding:16px;background:var(--surface)}.primary{color:var(--primary-background-color,var(--text-primary-color,#fff))}.primary:hover:not(:disabled){background:var(--primary-color);color:var(--primary-background-color,var(--text-primary-color,#fff))}.flow-description{color:var(--secondary-text-color)}.card-shell{min-width:0}.empty button{margin-top:14px}`;
  }
  static getConfigElement() {
    return document.createElement("solar-charging-card-editor");
  }
  static getStubConfig() {
    return { type: CARD_TYPE };
  }
}
class SolarChargingCardEditor extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._config = { type: CARD_TYPE };
    this._hass = null;
  }
  set hass(value) {
    this._hass = value;
    this.render();
  }
  setConfig(config) {
    this._config = { type: CARD_TYPE, ...config };
    this.render();
  }
  render() {
    if (!this.shadowRoot) return;
    this.shadowRoot.innerHTML = `<style>:host{display:block;padding:12px;font-family:var(--paper-font-body1_-_font-family,Roboto,sans-serif);color:var(--primary-text-color)}p{margin:8px 0 0;color:var(--secondary-text-color);line-height:1.4}</style><ha-form id="card-form"></ha-form><p>Only this integration’s marked controller sensors are accepted at runtime. Selecting one does not enable automation or commission a vehicle.</p>`;
    const form = this.shadowRoot.querySelector("#card-form");
    if (!form) return;
    form.hass = this._hass;
    form.schema = [
      {
        name: "controller",
        required: true,
        selector: {
          entity: {
            filter: [
              {
                domain: "sensor",
                integration: "rivian",
                attributes: { solar_controller: true },
              },
            ],
          },
        },
      },
    ];
    form.data = this._config;
    form.computeLabel = () => "Solar controller";
    form.computeHelper = () =>
      "Choose the controller sensor created by Solar EV Charging.";
    form.addEventListener("value-changed", (event) => {
      this._config = { ...this._config, ...event.detail.value };
      this.dispatchEvent(
        new CustomEvent("config-changed", {
          detail: { config: this._config },
          bubbles: true,
          composed: true,
        }),
      );
    });
  }
}
if (!customElements.get("solar-charging-panel"))
  customElements.define("solar-charging-panel", SolarChargingPanel);
if (!customElements.get("solar-charging-card"))
  customElements.define("solar-charging-card", SolarChargingCard);
if (!customElements.get("solar-charging-card-editor"))
  customElements.define("solar-charging-card-editor", SolarChargingCardEditor);
window.customCards = window.customCards || [];
if (!window.customCards.some((card) => card.type === CARD_TYPE))
  window.customCards.push({
    type: CARD_TYPE,
    name: "Solar charging",
    description:
      "Measured solar charging status and controlled actions for one configured controller.",
    preview: true,
  });
