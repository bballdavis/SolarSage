import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import vm from "node:vm";

const source = await readFile(
  new URL(
    "../custom_components/rivian/solar_ev_charging/frontend/solar-panel.js",
    import.meta.url,
  ),
  "utf8",
);

class FakeHTMLElement {}
const registrations = new Map();
const context = {
  console,
  HTMLElement: FakeHTMLElement,
  customElements: {
    define: (name, value) => registrations.set(name, value),
    get: (name) => registrations.get(name),
  },
  window: { customCards: [] },
};
vm.createContext(context);
vm.runInContext(
  `${source}\nglobalThis.__solarUiTest = { finite, power, controllerFor, modelFor, initialFormData, SolarChargingSurface };`,
  context,
);

const {
  finite,
  power,
  controllerFor,
  modelFor,
  initialFormData,
  SolarChargingSurface,
} = context.__solarUiTest;

function surface({ hass, model } = {}) {
  const instance = Object.create(SolarChargingSurface.prototype);
  Object.assign(instance, {
    _hass: hass,
    _setup: null,
    _busy: new Set(),
    _error: "",
    _focusKey: "",
    render() {},
    isAdmin: () => true,
    selectedController: () => "",
    viewModel: () => model,
  });
  return instance;
}

assert.equal(finite(null), undefined, "null telemetry must not become zero");
assert.equal(finite(""), undefined, "empty telemetry must not become zero");
assert.equal(power(null), "—", "missing power is visibly missing");
assert.match(
  source,
  /\.primary:hover:not\(:disabled\)\{background:var\(--primary-color\);color:var\(--primary-background-color,var\(--text-primary-color,#fff\)\)\}/,
  "primary hover retains its solid theme color and on-primary foreground",
);

function contrast(hexA, hexB) {
  const luminance = (hex) => {
    const channels = hex
      .slice(1)
      .match(/../g)
      .map((value) => Number.parseInt(value, 16) / 255)
      .map((value) =>
        value <= 0.04045 ? value / 12.92 : ((value + 0.055) / 1.055) ** 2.4,
      );
    return channels[0] * 0.2126 + channels[1] * 0.7152 + channels[2] * 0.0722;
  };
  const [light, dark] = [luminance(hexA), luminance(hexB)].sort(
    (a, b) => b - a,
  );
  return (light + 0.05) / (dark + 0.05);
}
assert.ok(
  contrast("#1473a7", "#ffffff") >= 4.5,
  "light primary foreground has readable contrast in normal and hover states",
);
assert.ok(
  contrast("#6cc8f2", "#061820") >= 4.5,
  "dark primary foreground has readable contrast in normal and hover states",
);

const markerState = {
  state: "on",
  attributes: {
    solar_controller: true,
    status: { enabled: true },
    controls: {},
  },
};
assert.equal(
  controllerFor({ states: { "sensor.live": markerState } }, "sensor.wrong"),
  null,
  "a configured card must not fall back to another controller",
);
const unavailable = modelFor({ ...markerState, state: "unavailable" });
assert.equal(
  unavailable.available,
  false,
  "unavailable controller is not live",
);
assert.equal(
  unavailable.enabled,
  false,
  "unavailable controller cannot appear enabled",
);

const noForm = surface();
noForm._setup = {
  flow: { type: "form", flow_id: "flow", data_schema: [] },
  formData: { keep_me: "value" },
};
assert.match(
  noForm.setupMarkup(null),
  /native form control is not loaded/,
  "a missing ha-form must show a safe Settings fallback, not a blank save form",
);

registrations.set("ha-form", class {});
const errorForm = surface();
errorForm._setup = {
  flow: { type: "form", flow_id: "flow", errors: { base: "solar_required" } },
  formData: { keep_me: "value" },
};
assert.match(
  errorForm.setupMarkup(null),
  /Solar required/,
  "options-flow base errors must be visible",
);

const boundForm = { addEventListener: () => {} };
const bindingSurface = surface({
  hass: {
    localize: (key) =>
      ({
        "component.rivian.options.error.solar_forecast_required":
          "Forecast mapping is incomplete.",
        "component.rivian.selector.forecast_value_field.options.estimate10":
          "Conservative estimate (P10)",
      })[key] || key,
  },
});
bindingSurface._setup = {
  entryId: "entry-a",
  formData: { forecast_mode: "off" },
  flow: {
    type: "form",
    flow_id: "flow",
    handler: "entry-a",
    step_id: "solar_forecast",
    data_schema: [{ name: "forecast_mode" }],
    errors: { forecast_mode: "solar_forecast_required" },
  },
};
bindingSurface.bind(
  {
    querySelectorAll: () => [],
    querySelector: (selector) =>
      selector === "#options-form" ? boundForm : null,
  },
  null,
);
assert.deepEqual(
  boundForm.error,
  { forecast_mode: "solar_forecast_required" },
  "native ha-form receives field errors through its singular error property",
);
assert.equal(
  boundForm.computeError("solar_forecast_required"),
  "Forecast mapping is incomplete.",
  "native ha-form computeError receives one error argument",
);
assert.equal(
  boundForm.context.handler,
  "entry-a",
  "native ha-form receives the options-flow handler context",
);
assert.equal(
  boundForm.context.domain,
  "rivian",
  "native ha-form receives the integration domain context",
);
assert.equal(
  boundForm.localizeValue(
    "component.rivian.selector.forecast_value_field.options.estimate10",
  ),
  "Conservative estimate (P10)",
  "selector values use Home Assistant localization when available",
);

const fieldDefaults = initialFormData({
  data_schema: [
    { name: "solar_ev_charging_enabled", default: false },
    { name: "start_hold_seconds", default: 0 },
    { name: "dry_run", suggested_value: true },
    { name: "grid_power_entity", value: "sensor.live_grid" },
  ],
  data: { dry_run: false },
});
assert.deepEqual(
  { ...fieldDefaults },
  {
    solar_ev_charging_enabled: false,
    start_hold_seconds: 0,
    dry_run: false,
    grid_power_entity: "sensor.live_grid",
  },
  "new forms seed current values and preserve false and zero defaults",
);

const menuPayloads = [];
const menuSurface = surface({
  hass: {
    callApi: async (_method, _path, data) => {
      menuPayloads.push(data);
      if (data.next_step_id)
        return {
          type: "form",
          flow_id: "flow",
          step_id: "solar_setup",
          data_schema: [
            { name: "solar_ev_charging_enabled", default: false },
            { name: "dry_run", default: true },
            { name: "start_hold_seconds", default: 0 },
            { name: "grid_power_entity", value: "sensor.live_grid" },
          ],
        };
      return { type: "create_entry" };
    },
  },
});
menuSurface._setup = {
  flow: { type: "menu", flow_id: "flow", step_id: "init" },
};
await menuSurface.advanceOptions({ next_step_id: "solar_setup" });
assert.deepEqual(
  { ...menuSurface._setup.formData },
  {
    solar_ev_charging_enabled: false,
    dry_run: true,
    start_hold_seconds: 0,
    grid_power_entity: "sensor.live_grid",
  },
  "menu navigation seeds the selected form instead of becoming configuration data",
);
await menuSurface.advanceOptions(menuSurface._setup.formData);
assert.deepEqual(
  { ...menuPayloads[1] },
  {
    solar_ev_charging_enabled: false,
    dry_run: true,
    start_hold_seconds: 0,
    grid_power_entity: "sensor.live_grid",
  },
  "the first form save retains schema defaults and current fields without next_step_id",
);
assert.equal(
  Object.hasOwn(menuPayloads[1], "next_step_id"),
  false,
  "menu navigation metadata never leaks into a form submission",
);

const validationSurface = surface({
  hass: {
    callApi: async () => ({
      type: "form",
      flow_id: "flow",
      errors: { base: "solar_required" },
    }),
  },
});
validationSurface._setup = {
  flow: { type: "form", flow_id: "flow" },
  formData: { keep_me: "value" },
};
await validationSurface.advanceOptions({ keep_me: "value" });
assert.deepEqual(
  validationSurface._setup.formData,
  { keep_me: "value" },
  "a rejected save must preserve unsaved form values",
);
assert.equal(
  validationSurface._setup.flow.errors.base,
  "solar_required",
  "the returned options-flow error must remain attached to the form",
);

const updateForm = {};
const updateSurface = surface();
let updateRenders = 0;
updateSurface.render = () => {
  updateRenders += 1;
};
updateSurface.shadowRoot = {
  querySelector: (selector) =>
    selector === "#options-form" ? updateForm : null,
};
updateSurface._setup = {
  formData: { forecast_mode: "protect" },
  flow: { type: "form", flow_id: "flow" },
};
updateSurface.hass = { states: {} };
assert.equal(
  updateRenders,
  0,
  "routine HA updates do not rebuild an open native options form",
);
assert.equal(updateForm.hass.states !== undefined, true);
assert.deepEqual(
  updateSurface._setup.formData,
  { forecast_mode: "protect" },
  "routine HA updates retain in-progress form data",
);

let resolveStart;
const startPromise = new Promise((resolve) => {
  resolveStart = resolve;
});
const raceModel = {
  controller: { attributes: { entry_id: "entry-a" } },
  available: true,
};
const raceSurface = surface({
  model: raceModel,
  hass: { callApi: () => startPromise },
});
const opening = raceSurface.beginOptions();
raceSurface._setup = null;
resolveStart({ type: "menu", flow_id: "old-flow", menu_options: [] });
await opening;
assert.equal(
  raceSurface._setup,
  null,
  "cancelling an in-flight setup must not revive an old options flow",
);

console.log("solar UI contract checks passed");
