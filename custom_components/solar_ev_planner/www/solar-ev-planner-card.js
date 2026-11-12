/*
 * SolarSage card: the planner's cockpit, in the Daylight look.
 *
 * Served and loaded by the solar_ev_planner integration (no Lovelace resource needed):
 *
 *   type: custom:solar-ev-planner-card
 *   sky: auto        # auto | day | night   (auto follows sun.sun)
 *   fill: true       # fill the window's height (for a panel view); false in a sections view
 *
 * Data: the solar_ev_planner/subscribe websocket command (a fresh snapshot after every planning
 * cycle and settings change) and the recorder's history for today's solar, what the car drew
 * and what the planner planned. Controls call the planner's own entities (switch, select,
 * number), so permissions and the logbook work as usual. The free hours are edited in their
 * Schedule helper (schedule/update, admin only), then the planner re-reads them at once.
 *
 * Mouse: click acts, right-click opens more-info. Touch: press and hold opens more-info.
 */
(() => {
  const TAG = 'solar-ev-planner-card';
  const VERSION = '0.3.0';
  if (customElements.get(TAG)) return;

  const HISTORY_EVERY = 5 * 60 * 1000;
  const BUCKET = 5 * 60 * 1000;
  const SLOT = 30 * 60 * 1000;
  const STEP = 15 * 60 * 1000;
  const HOUR = 3600000;
  const STALE_AFTER = 3 * 60 * 1000;
  const DAYS = ['monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday', 'sunday'];

  const MODE = {
    off: ['Planner off', 'grey'], empty: ['Charger empty', 'grey'], stale: ['Car data stale', 'amber'],
    full: ['Car at its limit', 'grey'], day: ['Solar charging', 'green'], night: ['Free hours', 'indigo'],
    no_data: ['Cannot decide', 'red'],
  };
  const MODE_TIP = {
    off: 'The planner is switched off: it plans nothing and sends nothing.',
    empty: 'No car is on the Wall Connector, so there is nothing to charge.',
    stale: "A car's readings are missing or too old, so the planner does not command it.",
    full: 'The car on the charger has reached its charge limit.',
    day: 'Charging from the solar surplus, kept above the home battery floor.',
    night: "In the free hours: charging until the car's plan is met.",
    no_data: 'Readings the planner needs are missing, so it cannot decide.',
  };
  const CAUTION = [
    ['near_certain', 'Near-certain'], ['conservative', 'Conservative'],
    ['balanced', 'Balanced'], ['typical_day', 'Typical day'],
  ];
  const RIVIAN_STATE = {
    idle: ['Idle', 'green'], waiting_for_vehicle: ['Waiting for the car', 'green'], handed_back: ['Handed back', 'green'],
    lease_active: ['In control', 'green'], lease_lapsed: ['Lease lapsed', 'amber'], paused: ['Paused', 'amber'],
    writing: ['Writing', 'green'], fault: ['Fault', 'red'], dry_run: ['Dry run', 'amber'],
  };
  const PLAN_DAYS = { off: 'No plan', once: 'once', weekdays: 'weekdays', daily: 'every day', weekends: 'weekends' };
  const FREE_START = 'free_hours_start';

  const esc = v => String(v ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const num = v => (v === null || v === undefined || v === '' || Number.isNaN(Number(v)) ? null : Number(v));
  const f1 = v => (num(v) === null ? '—' : (Math.abs(v) < 0.05 ? 0 : v).toFixed(1));
  const f0 = v => (num(v) === null ? '—' : String(Math.round(v)));
  const ts = v => (v ? new Date(v).getTime() : null);
  const clock = t => new Date(t).toLocaleTimeString('en-US', { hour: 'numeric', minute: '2-digit' });
  const hourLabel = t => { const d = new Date(t); const h = d.getHours(); if (d.getMinutes()) return clock(t); if (h === 0) return '12 AM'; if (h === 12) return '12 PM'; return h > 12 ? `${h - 12} PM` : `${h} AM`; };
  const shortHour = t => { const h = new Date(t).getHours(); if (h === 0) return '12 AM'; if (h === 12) return '12 PM'; return String(h > 12 ? h - 12 : h); };
  const hhmmLabel = hhmm => { const [h, m] = String(hhmm).split(':').map(Number); const d = new Date(); d.setHours(h, m || 0, 0, 0); return clock(d.getTime()); };
  const toHHMM = t => { const d = new Date(t); return `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`; };
  const ago = t => { if (!t) return 'never'; const m = Math.round((Date.now() - t) / 60000); if (m < 1) return 'just now'; if (m < 60) return `${m} min ago`; return `${Math.round(m / 60)} h ago`; };
  const midnight = (t = Date.now()) => { const d = new Date(t); d.setHours(0, 0, 0, 0); return d.getTime(); };
  const cap1 = s => (s ? s[0].toUpperCase() + s.slice(1) : s);
  const unavailable = s => !s || s.state === 'unavailable' || s.state === 'unknown';

  const ICON = {
    sun: '<circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M2 12h2M20 12h2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/>',
    moon: '<path d="M20 14.5A8 8 0 1 1 9.5 4a6.5 6.5 0 0 0 10.5 10.5z"/>',
    eye: '<path d="M2 12s3.6-7 10-7 10 7 10 7-3.6 7-10 7S2 12 2 12z"/><circle cx="12" cy="12" r="3"/>',
    bolt: '<path d="M13 2 4 14h7l-1 8 9-12h-7z"/>',
    pause: '<path d="M9 5v14M15 5v14"/>',
    alert: '<path d="M12 3 2 20h20z"/><path d="M12 10v4M12 17v.5"/>',
    plug: '<path d="M9 2v5M15 2v5M6 7h12v4a6 6 0 0 1-12 0zM12 17v5"/>',
    home: '<path d="M3 11 12 4l9 7"/><path d="M5 10v10h14V10"/>',
    grid: '<path d="M12 2 7 22M12 2l5 20M8 9h8M6.5 15h11M9 5.5h6"/>',
    battery: '<rect x="3" y="7" width="16" height="10" rx="2"/><path d="M21 10v4"/><path d="M6 10v4M9.5 10v4"/>',
    up: '<path d="M12 19V5M6 11l6-6 6 6"/>',
    down: '<path d="M12 5v14M6 13l6 6 6-6"/>',
    pulse: '<path d="M3 12h4l2-5 4 10 2-5h6"/>',
    check: '<path d="M4 12.5 9.5 18 20 6"/>',
    cloud: '<path d="M7 18h10a4 4 0 0 0 .6-7.95A6 6 0 0 0 6 11a3.5 3.5 0 0 0 1 7z"/>',
    car: '<path d="M5 16V11l2-5h10l2 5v5"/><path d="M3 16h18v3H3z"/><circle cx="7.5" cy="16" r="1"/><circle cx="16.5" cy="16" r="1"/>',
    dollar: '<path d="M12 3v18M16.5 7.5C16 6 14.3 5 12 5c-2.6 0-4.5 1.3-4.5 3.2 0 4.3 9 2.3 9 6.6 0 2-2 3.2-4.5 3.2-2.4 0-4.2-1-4.7-2.6"/>',
    sync: '<path d="M4 12a8 8 0 0 1 13.7-5.6L20 9M20 4v5h-5M20 12a8 8 0 0 1-13.7 5.6L4 15M4 20v-5h5"/>',
    edit: '<path d="M4 20h4L19 9l-4-4L4 16z"/><path d="M13.5 6.5l4 4"/>',
    gear: '<circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.5-1.1 1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z"/>',
  };
  const icon = (name, size = 16) => `<svg class="ic" width="${size}" height="${size}" viewBox="0 0 24 24" aria-hidden="true">${ICON[name] || ''}</svg>`;

  /** A legend and readout key drawn like the mark it stands for: line, dash, dot, band, area or marker. */
  const swatch = (color, kind = 'line', opacity = 1) => {
    const body = {
      line: `<line x1="1" y1="6" x2="21" y2="6" stroke="${color}" stroke-opacity="${opacity}" stroke-width="3" stroke-linecap="round"/>`,
      dash: `<line x1="1" y1="6" x2="21" y2="6" stroke="${color}" stroke-opacity="${opacity}" stroke-width="2.4" stroke-dasharray="5 3"/>`,
      dot: `<line x1="2" y1="6" x2="21" y2="6" stroke="${color}" stroke-opacity="${opacity}" stroke-width="2.6" stroke-dasharray="0.1 4.5" stroke-linecap="round"/>`,
      band: `<rect x="1" y="1" width="20" height="10" rx="2.5" fill="${color}" fill-opacity="${opacity}"/>`,
      area: `<rect x="1" y="2" width="20" height="9" rx="1.5" fill="color-mix(in srgb, ${color} 45%, transparent)"/><line x1="1" y1="2" x2="21" y2="2" stroke="${color}" stroke-width="1.6"/>`,
      marker: `<circle cx="11" cy="6" r="4.2" fill="var(--sky-a)" stroke="${color}" stroke-width="2"/>`,
    }[kind];
    return `<svg class="swl" width="22" height="12" viewBox="0 0 22 12" aria-hidden="true">${body}</svg>`;
  };
  const key = (color, kind, text, opacity) => `<span>${swatch(color, kind, opacity)}${text}</span>`;
  const title = (ic, text) => `<h2>${icon(ic, 22)}<span>${text}</span></h2>`;

  /** The spacing of the moving dots on a Home Now spoke, in pixels. */
  const FLOW_GAP = 14;

  const FONT_CSS = '@font-face { font-family: "Jost"; font-style: normal; font-weight: 100 900; font-display: swap; src: url(https://fonts.gstatic.com/s/jost/v20/92zatBhPNqw73oTd4jQmfxI.woff2) format("woff2"); }';

  const CSS = `:host {
  --sky-a: #07162B; --sky-b: #0D2A4A; --sky-c: #123F63; --glow: rgba(96,170,230,0.22);
  --ink: #F4F8FC; --ink-2: rgba(244,248,252,0.92); --ink-3: rgba(244,248,252,0.76); --hair: rgba(244,248,252,0.18); --hair-2: rgba(244,248,252,0.10);
  --surface: rgba(255,255,255,0.07); --surface-2: rgba(255,255,255,0.14); --sheet: rgba(9,26,50,0.97);
  --amber: #F6A04D; --green: #5DD39E; --red: #FF7A6B; --indigo: #9BB1FF; --cyan: #7FD1F7;
  --tesla: #E8384F; --rivian: #F7CF4A;
  --c-solar: #FFB347; --c-house: #6FD3FF; --c-grid: #B39DFF; --c-battery: #5DE0A0; --c-charger: #FF8FB1;
  --band: rgba(255,179,71,0.30); --free-band: rgba(155,177,255,0.16);
  --chip-ink: #0B1E36; --hub: #0A1D36;
  --font: "Jost", "Avenir Next", "Futura", "Century Gothic", system-ui, sans-serif;
  display: block; color: var(--ink); font-family: var(--font); font-size: 16px; line-height: 1.35; -webkit-font-smoothing: antialiased; color-scheme: dark; }
:host([data-theme="light"]) {
  --sky-a: #3B8ED6; --sky-b: #2468AE; --sky-c: #143F78; --glow: rgba(255,255,255,0.16);
  --ink: #FFFFFF; --ink-2: rgba(255,255,255,0.96); --ink-3: rgba(255,255,255,0.86); --hair: rgba(255,255,255,0.24); --hair-2: rgba(255,255,255,0.14);
  --surface: rgba(6,30,72,0.30); --surface-2: rgba(6,30,72,0.46); --sheet: rgba(14,52,104,0.98);
  --amber: #FFB357; --green: #8BEFC0; --red: #FF9A8C; --indigo: #D3DBFF; --cyan: #BFEAFF;
  --tesla: #FF4D63; --rivian: #FFD95C;
  --band: rgba(255,179,71,0.38); --free-band: rgba(211,219,255,0.18); --hub: #174E8C; }
*, *::before, *::after { box-sizing: border-box; }
button, select, input { font: inherit; color: inherit; }
button { background: none; border: 0; padding: 0; margin: 0; cursor: pointer; text-align: inherit; -webkit-tap-highlight-color: transparent; touch-action: manipulation; }
button:focus-visible, select:focus-visible, input:focus-visible { outline: 2px solid var(--amber); outline-offset: 2px; border-radius: 10px; }
button:disabled { cursor: default; opacity: 0.5; }
svg.ic { fill: none; stroke: currentColor; stroke-width: 2.1; stroke-linecap: round; stroke-linejoin: round; flex: none; display: block; }
.num { font-variant-numeric: tabular-nums; font-feature-settings: "tnum"; }
.app { position: relative; padding: 24px 28px 28px; display: flex; flex-direction: column; gap: 18px; overflow: hidden;
  background: radial-gradient(900px 560px at 14% -10%, var(--glow), transparent 60%), radial-gradient(1800px 1100px at 50% 40%, var(--sky-b), var(--sky-a) 85%); }
:host([data-theme="light"]) .app { background: radial-gradient(900px 560px at 14% -10%, var(--glow), transparent 60%), linear-gradient(180deg, var(--sky-a), var(--sky-b) 58%, var(--sky-c)); }
:host([data-fill]) .app { min-height: calc(100vh - var(--header-height, 56px)); }
.lbl { font-size: 13px; font-weight: 600; letter-spacing: 0.12em; text-transform: uppercase; color: var(--ink-2); }
.panel { display: flex; flex-direction: column; gap: 12px; padding: 18px 20px; border-radius: 22px; background: var(--surface); border: 1px solid var(--hair-2); min-width: 0; }
.ptitle { display: flex; justify-content: space-between; align-items: center; gap: 10px; flex-wrap: wrap; min-height: 32px; }
.ptitle h2 { margin: 0; display: flex; align-items: center; gap: 10px; font-size: 21px; font-weight: 500; line-height: 1.2; min-width: 0; }
.ptitle h2 .ic { color: var(--ink-2); }
.ptitle .r { font-size: 14px; color: var(--ink-2); }
.ptitle .r b { font-weight: 600; color: var(--ink); }
.muted { color: var(--ink-3); }

.top { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; }
.brand { display: flex; align-items: center; gap: 12px; min-width: 0; }
.brand-logo { flex: none; width: 54px; height: 54px; object-fit: contain; border-radius: 10px; }
.title h1 { margin: 0; font-size: 30px; font-weight: 500; line-height: 1.1; }
.title .sub { font-size: 14px; color: var(--ink-2); }
.title { margin-right: 6px; }
/* status chips: icon-only tiles in the toggles' dark glass, the border and icon in the status colour */
.pill { --on: var(--ink-3); display: inline-flex; align-items: center; justify-content: center; width: 44px; height: 44px; padding: 0; border-radius: 14px;
  border: 1.5px solid var(--on); color: var(--on);
  background: linear-gradient(180deg, color-mix(in srgb, var(--on) 16%, rgba(14,30,34,0.52)) 0%, color-mix(in srgb, var(--on) 6%, rgba(6,16,20,0.68)) 100%);
  -webkit-backdrop-filter: blur(12px); backdrop-filter: blur(12px);
  box-shadow: inset 0 1px 0 color-mix(in srgb, var(--on) 30%, transparent), 0 0 12px color-mix(in srgb, var(--on) 22%, transparent); }
.pill .ic { width: 20px; height: 20px; }
.pill .ptxt { display: none; }
.pill[aria-expanded="true"] { box-shadow: inset 0 1px 0 color-mix(in srgb, var(--on) 30%, transparent), 0 0 0 3px color-mix(in srgb, var(--on) 28%, transparent); }
.hp { position: relative; display: inline-flex; }
.tipbox { display: none; position: absolute; top: calc(100% + 8px); left: 0; z-index: 6; width: max-content; max-width: min(300px, calc(100vw - 32px)); padding: 10px 12px; border-radius: 12px; background: var(--sheet); border: 1px solid var(--hair); box-shadow: 0 8px 24px rgba(0,0,0,0.28); font-size: 14px; line-height: 1.4; color: var(--ink-2); }
/* shown on hover (mouse), keyboard focus, or a tap (open) */
.hp.open .tipbox, .hp:has(.pill:focus-visible) .tipbox { display: block; }
@media (hover: hover) { .hp:hover .tipbox { display: block; } }
.tipbox b { display: block; margin-bottom: 4px; font-weight: 600; color: var(--ink); }
.tipbox .why { display: block; margin-top: 6px; color: var(--ink-3); }
.pill.green { --on: var(--green); } .pill.indigo { --on: var(--indigo); } .pill.amber { --on: var(--amber); }
.pill.red { --on: var(--red); } .pill.cyan { --on: var(--cyan); } .pill.grey { --on: rgba(255,255,255,0.62); }
.grow { flex: 1 1 auto; }
.toggles { display: flex; gap: 10px; flex-wrap: wrap; }
/* toggles: dark glass; the border is the indicator (the accent when on, grey when off) */
.tog { --on: var(--green); display: flex; align-items: center; justify-content: center; height: 44px; padding: 0 18px; border-radius: 14px;
  border: 1.5px solid rgba(255,255,255,0.22);
  background: linear-gradient(180deg, rgba(20,30,44,0.46) 0%, rgba(8,14,24,0.62) 100%);
  -webkit-backdrop-filter: blur(12px); backdrop-filter: blur(12px);
  box-shadow: inset 0 1px 0 rgba(255,255,255,0.10), 0 2px 10px rgba(0,0,0,0.18);
  color: var(--ink-3); font-size: 15px; font-weight: 500; transition: border-color 0.2s, box-shadow 0.2s, color 0.2s, background 0.2s; }
.tog.shadow { --on: var(--amber); }
.tog[aria-pressed="true"] { border-color: var(--on); color: var(--ink); font-weight: 600;
  background: linear-gradient(180deg, color-mix(in srgb, var(--on) 16%, rgba(14,30,34,0.52)) 0%, color-mix(in srgb, var(--on) 6%, rgba(6,16,20,0.68)) 100%);
  box-shadow: inset 0 1px 0 color-mix(in srgb, var(--on) 30%, transparent), 0 0 12px color-mix(in srgb, var(--on) 22%, transparent); }

.grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 18px; container: cockpit / inline-size; }
@media (max-width: 1100px) {
  .grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .grid .today, .grid .battery { grid-column: 1 / -1; }
  .grid .car { order: 1; } .grid .site { order: 2; } .grid .today { order: 3; } .grid .battery { order: 4; } .grid .health { order: 5; } .grid .settings { order: 6; }
}
@media (max-width: 720px) {
  .grid { grid-template-columns: minmax(0, 1fr); }
  .app { padding: 18px 14px 22px; }
  .brand { flex: 1 1 0; min-width: 0; }
  .brand-logo { width: 44px; height: 44px; }
  .title { flex: 1 1 0; min-width: 0; margin-right: 0; }
  .top .grow { display: none; }
  .hp .tipbox { left: auto; right: 0; }
  .toggles { width: 100%; }
  .tog { flex: 1 1 0; padding: 0 6px; font-size: 14px; white-space: nowrap; }
}

.car-h { display: flex; align-items: center; gap: 10px; }
.dot { width: 10px; height: 10px; border-radius: 50%; background: var(--dc, var(--ink-3)); flex: none; }
.car-name { display: flex; align-items: baseline; justify-content: space-between; gap: 10px; }
.car-name .n { font-size: 28px; font-weight: 500; }
.car-name .s { font-size: 16px; color: var(--ink-2); }
.bar { height: 8px; border-radius: 999px; background: var(--hair-2); position: relative; overflow: hidden; }
.bar > i { position: absolute; left: 0; top: 0; bottom: 0; border-radius: 999px; background: var(--dc, var(--green)); }
.bar > b { position: absolute; top: 0; bottom: 0; width: 2px; background: var(--ink); }
.tiles { display: flex; gap: 12px; }
.tile { flex: 1 1 0; padding: 12px 14px; border-radius: 16px; background: var(--surface); border: 1px solid var(--hair-2); min-width: 0; }
.tile.plan { border-color: color-mix(in srgb, var(--amber) 55%, transparent); }
.tile.plan.live { border-color: color-mix(in srgb, var(--cyan) 55%, transparent); }
.tile .k { font-size: 14px; color: var(--ink-2); }
.tile .v { font-size: 38px; font-weight: 500; line-height: 1.1; }
.tile .v small { font-size: 19px; }
.tile .u { font-size: 14px; color: var(--ink-2); }
.reason { font-size: 16px; line-height: 1.45; color: var(--ink); margin: 0; }
.note { font-size: 14px; line-height: 1.4; padding: 8px 12px; border-radius: 12px; background: color-mix(in srgb, var(--amber) 22%, transparent); color: var(--ink); }
.note.bad { background: color-mix(in srgb, var(--red) 26%, transparent); }
.kv { display: flex; flex-direction: column; gap: 8px; padding-top: 12px; border-top: 1px solid var(--hair-2); font-size: 15px; }
.kv > div { display: flex; justify-content: space-between; gap: 12px; }
.kv > div > span:first-child { color: var(--ink-2); flex: none; }
.kv > div > span:last-child { text-align: right; }
.green-t { color: var(--green); } .amber-t { color: var(--amber); } .red-t { color: var(--red); } .indigo-t { color: var(--indigo); }
.others { display: flex; flex-direction: column; gap: 6px; padding-top: 12px; border-top: 1px solid var(--hair-2); }
.other { display: grid; grid-template-columns: auto auto 1fr; gap: 6px 10px; align-items: center; font-size: 15px; }
.other .o { text-align: right; color: var(--ink-2); font-size: 14px; }

.seg2 { display: inline-grid; grid-auto-flow: column; gap: 2px; padding: 3px; border-radius: 11px; background: var(--surface-2); }
.seg2 button { display: inline-flex; align-items: center; gap: 6px; height: 30px; padding: 0 12px; border-radius: 8px; font-size: 14px; color: var(--ink-2); }
.seg2 button[aria-pressed="true"] { background: var(--ink); color: var(--chip-ink); font-weight: 600; }
.legend { display: flex; gap: 6px 14px; font-size: 13px; color: var(--ink-2); flex-wrap: wrap; }
.legend span { display: inline-flex; align-items: center; gap: 6px; }
.swl { display: block; flex: none; overflow: visible; }
.hover .row { display: flex; align-items: center; gap: 8px; }
.chart { position: relative; }
.chart svg { display: block; overflow: visible; }
.chart text { font-family: var(--font); }
.hover { position: absolute; top: 0; pointer-events: none; padding: 8px 10px; border-radius: 12px; background: var(--sheet); border: 1px solid var(--hair); font-size: 13px; line-height: 1.45; white-space: nowrap; box-shadow: 0 8px 24px rgba(0,0,0,0.3); z-index: 2; }
.hover b { font-weight: 600; }
.hline { position: absolute; top: 0; bottom: 0; width: 1px; background: var(--ink-2); pointer-events: none; }
.stats { display: flex; gap: 6px 20px; font-size: 14px; color: var(--ink-2); flex-wrap: wrap; margin-top: auto; }
.stats b { font-weight: 600; color: var(--ink); }

.hub { flex: 1; display: flex; align-items: center; }
.hub svg { margin: 0 auto; }
.hub .flow { animation: flow 1s linear infinite; }
@keyframes flow { from { stroke-dashoffset: ${FLOW_GAP}; } to { stroke-dashoffset: 0; } }
.chip { width: 34px; height: 34px; border-radius: 50%; display: grid; place-items: center; background: var(--cc); color: var(--chip-ink); }
.foot { font-size: 14px; line-height: 1.4; color: var(--ink-2); margin-top: auto; }

.seg { display: grid; grid-template-columns: repeat(var(--n, 4), minmax(0, 1fr)); gap: 4px; padding: 4px; border-radius: 12px; background: var(--surface); }
.seg button { min-height: 42px; border-radius: 9px; color: var(--ink-2); font-size: 14px; text-align: center; line-height: 1.15; padding: 4px 2px; }
.seg button small { display: block; font-size: 12px; color: var(--ink-3); }
.seg button[aria-pressed="true"] { background: var(--ink); color: var(--chip-ink); font-weight: 600; }
.seg button[aria-pressed="true"] small { color: var(--chip-ink); }
.set { display: flex; justify-content: space-between; align-items: center; gap: 10px; font-size: 15px; min-height: 36px; }
.set > span:first-child { color: var(--ink-2); }
.stepper { display: flex; align-items: center; gap: 6px; }
.stepper button { width: 36px; height: 36px; border-radius: 10px; border: 1px solid var(--hair); text-align: center; font-size: 18px; }
.stepper b { font-weight: 600; min-width: 48px; text-align: center; }
.cap { font-size: 14px; color: var(--ink-2); }
.sel { height: 36px; padding: 0 10px; border-radius: 10px; border: 1px solid var(--hair); background: var(--surface); color: var(--ink); max-width: 60%; }
.sel option { color: #0B1E36; }
.btn { display: inline-flex; align-items: center; gap: 6px; height: 34px; padding: 0 12px; border-radius: 10px; border: 1px solid var(--hair); font-size: 14px; font-weight: 500; }
.btn.go { background: var(--ink); color: var(--chip-ink); border-color: var(--ink); }
.btn.icon { width: 34px; padding: 0; justify-content: center; }
.editor { display: flex; flex-direction: column; gap: 10px; padding: 12px; border-radius: 14px; background: var(--surface); border: 1px solid var(--hair); }
.editor .times { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.editor input[type="time"] { height: 38px; padding: 0 8px; border-radius: 10px; border: 1px solid var(--hair); background: var(--surface-2); color: var(--ink); color-scheme: dark; }
.editor .acts { display: flex; justify-content: flex-end; gap: 8px; }
.sep { border-top: 1px solid var(--hair-2); }

.hgrid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 10px; }
.htile { display: grid; grid-template-columns: 38px minmax(0, 1fr); gap: 4px 12px; align-items: center; padding: 12px; border-radius: 16px; background: var(--surface); border: 1px solid var(--hair-2); }
.htile .chip { width: 38px; height: 38px; grid-row: span 2; }
.htile .t { font-size: 13px; font-weight: 600; letter-spacing: 0.06em; text-transform: uppercase; color: var(--ink-2); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.htile .s { font-size: 17px; font-weight: 600; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.htile .d { grid-column: 1 / -1; font-size: 13px; color: var(--ink-2); line-height: 1.35; }
.meter { height: 6px; border-radius: 999px; background: var(--hair-2); overflow: hidden; grid-column: 1 / -1; }
.meter > i { display: block; height: 6px; border-radius: 999px; background: var(--cc); }
.st-green { --cc: var(--green); } .st-amber { --cc: var(--amber); } .st-red { --cc: var(--red); } .st-grey { --cc: rgba(255,255,255,0.55); } .st-indigo { --cc: var(--indigo); }
.htile .s.st-green, .htile .s.st-amber, .htile .s.st-red, .htile .s.st-indigo { color: var(--cc); }
.score { display: inline-flex; align-items: center; gap: 6px; height: 28px; padding: 0 10px; border-radius: 999px; font-size: 14px; font-weight: 600; background: var(--cc); color: var(--chip-ink); }

.empty { padding: 40px 24px; text-align: center; color: var(--ink-2); font-size: 17px; }
.empty b { display: block; font-size: 22px; font-weight: 500; color: var(--ink); margin-bottom: 6px; }
.scrim { position: fixed; inset: 0; background: rgba(0,0,0,0.45); display: flex; align-items: center; justify-content: center; z-index: 10; padding: 16px; }
.dialog { max-width: 440px; width: 100%; padding: 22px; border-radius: 22px; background: var(--sheet); border: 1px solid var(--hair); display: flex; flex-direction: column; gap: 12px; }
.dialog h3 { margin: 0; font-size: 20px; font-weight: 500; }
.dialog p { margin: 0; color: var(--ink-2); line-height: 1.45; }
.dialog .btns { display: flex; justify-content: flex-end; gap: 10px; margin-top: 6px; }
.dialog .btns button { height: 44px; padding: 0 18px; border-radius: 12px; border: 1px solid var(--hair); }
.dialog .btns button.go { background: var(--amber); color: #1A0E02; border-color: var(--amber); font-weight: 600; }
@media (prefers-reduced-motion: reduce) { .tog { transition: none; } .hub .flow { animation: none; } }
`;

  // ---------------------------------------------------------------- series helpers

  /** History rows {s, lu} -> [{t, v}] in kW (or A), numeric states only. */
  const toPoints = (rows, scale) => (rows || []).map(r => ({ t: (r.lu ?? r.lc ?? 0) * 1000, v: num(r.s) })).filter(p => p.v !== null).map(p => ({ t: p.t, v: p.v * scale }));

  /** Mean per BUCKET (time-weighted by step holding), for the line charts. */
  const bucketMeans = (pts, t0, t1) => {
    const out = [];
    if (!pts.length) return out;
    let i = 0; let cur = null;
    for (let b = t0; b < t1; b += BUCKET) {
      const e = Math.min(b + BUCKET, t1);
      while (i < pts.length && pts[i].t <= b) { cur = pts[i].v; i++; }
      let acc = 0; let last = b; let v = cur; let j = i;
      while (j < pts.length && pts[j].t < e) { if (v !== null) acc += v * (pts[j].t - last); last = pts[j].t; v = pts[j].v; j++; }
      if (v !== null) acc += v * (e - last);
      const known = cur !== null || (i < pts.length && pts[i].t < e);
      if (known && e > b) out.push({ t: b + (e - b) / 2, v: acc / (e - b) });
    }
    return out;
  };

  /** The value held at t (step), from sorted points. */
  const holdAt = (pts, t) => { let v = null; for (const p of pts) { if (p.t > t) break; v = p.v; } return v; };

  /** kWh from a held power series between t0 and t1. */
  const energy = (pts, t0, t1) => { let e = 0; for (let i = 0; i < pts.length; i++) { const a = Math.max(pts[i].t, t0); const b = Math.min(i + 1 < pts.length ? pts[i + 1].t : t1, t1); if (b > a) e += Math.max(0, pts[i].v) * (b - a) / HOUR; } return e; };

  /** The house load per bucket, as the planner reads it: solar + grid − battery − car. */
  const houseBuckets = (h, t0, t1) => {
    const by = pts => new Map(bucketMeans(pts || [], t0, t1).map(p => [p.t, p.v]));
    const pv = by(h.pv); const grid = by(h.grid); const bat = by(h.battery); const wall = by(h.wall);
    const out = [];
    for (const [t, g] of grid) {
      const b = bat.get(t); if (b === undefined) continue;
      out.push({ t, v: Math.max(0, (pv.get(t) || 0) + g - b - (wall.get(t) || 0)) });
    }
    return out.sort((a, b) => a.t - b.t);
  };

  /** kWh in bucket means (each a BUCKET long). */
  const bucketKwh = pts => pts.reduce((a, p) => a + Math.max(0, p.v) * BUCKET / HOUR, 0);

  /** kWh of a forecast quantile between t0 and t1 (half-hour slots, the part inside). */
  const fcKwh = (fc, t0, t1, k = 'p50') => fc.reduce((a, f) => { const o = Math.min(f.t + SLOT, t1) - Math.max(f.t, t0); return o > 0 ? a + (f[k] || 0) * o / HOUR : a; }, 0);

  /** The middle of today's daylight (from the forecast), else noon. */
  const solarNoon = (fc, day0) => {
    const lit = fc.filter(f => f.t >= day0 && f.t < day0 + 24 * HOUR && f.p90 > 0.05);
    return lit.length ? (lit[0].t + lit[lit.length - 1].t + SLOT) / 2 : day0 + 12 * HOUR;
  };

  const niceMax = v => { const steps = [2, 4, 6, 8, 10, 12, 16, 20, 24, 30, 40]; return steps.find(s => s >= v) || Math.ceil(v / 10) * 10; };

  /** Schedule blocks for a daily window (crossing midnight becomes two blocks). */
  const dailyBlocks = (start, end) => {
    const s = `${start}:00`; const e = `${end}:00`;
    if (start === end) return [{ from: '00:00:00', to: '24:00:00' }];
    if (start < end) return [{ from: s, to: e }];
    const out = [];
    if (end !== '00:00') out.push({ from: '00:00:00', to: e });
    out.push({ from: s, to: '24:00:00' });
    return out;
  };
  const sameBlocks = (a, b) => JSON.stringify((a || []).map(x => [String(x.from).slice(0, 5), String(x.to).slice(0, 5)])) === JSON.stringify((b || []).map(x => [String(x.from).slice(0, 5), String(x.to).slice(0, 5)]));

  // ---------------------------------------------------------------- the card

  class SolarEvPlannerCard extends HTMLElement {
    constructor() {
      super();
      this.attachShadow({ mode: 'open' });
      this._snap = null; this._hist = null; this._histAt = 0; this._error = null; this._confirm = null; this._unsub = null;
      this._bound = false; this._cw = {}; this._view = null; this._free = null;
    }

    setConfig(config) {
      this._cfg = { sky: 'auto', fill: true, ...(config || {}) };
      if (this._cfg.fill) this.setAttribute('data-fill', ''); else this.removeAttribute('data-fill');
      this._render();
    }

    static getStubConfig() { return { fill: false }; }
    getCardSize() { return 14; }
    getGridOptions() { return { columns: 'full', min_columns: 6 }; }

    set hass(hass) {
      const first = !this._hass;
      this._hass = hass;
      if (first || !this._unsub) this._connect();
      const theme = this._theme();
      const sig = this._watchSig();
      if (first || theme !== this._lastTheme || sig !== this._lastSig) { this._lastTheme = theme; this._lastSig = sig; this._render(); }
    }

    connectedCallback() {
      if (!document.getElementById('sevp-font')) { const st = document.createElement('style'); st.id = 'sevp-font'; st.textContent = FONT_CSS; document.head.appendChild(st); }
      this._connect();
      this._tick = setInterval(() => {
        // the planner sends every minute; silence means it was reloaded: subscribe again
        if (this._unsub && Date.now() - (this._lastMsg || 0) > STALE_AFTER) this._resubscribe();
        this._maybeHistory(); this._render();
      }, 60000);
      if (!this._ro && window.ResizeObserver) { this._ro = new ResizeObserver(() => this._measure()); }
      this._ro?.observe(this);
    }

    disconnectedCallback() {
      clearInterval(this._tick);
      clearTimeout(this._retry);
      this._ro?.disconnect();
      this._drop();
    }

    _drop() { const u = this._unsub; this._unsub = null; if (u) Promise.resolve().then(() => u()).catch(() => {}); }

    _resubscribe() { this._drop(); this._connect(); }

    _theme() {
      const sky = this._cfg?.sky || 'auto';
      if (sky === 'day') return 'light';
      if (sky === 'night') return 'dark';
      const sun = this._hass?.states['sun.sun'];
      return sun && sun.state === 'above_horizon' ? 'light' : 'dark';
    }

    /** Entities read straight from hass: re-render when they change. */
    _watchSig() {
      if (!this._snap || !this._hass) return '';
      const ids = [...(this._snap.cars || []).map(c => c.control_state_entity), this._ent('night_charging_start')].filter(Boolean);
      return ids.map(id => this._hass.states[id]?.last_updated || '').join('|');
    }

    async _connect() {
      if (this._unsub || this._connecting || !this._hass || !this.isConnected) return;
      this._connecting = true;
      try {
        this._unsub = await this._hass.connection.subscribeMessage(snap => {
          this._snap = snap; this._error = null; this._lastMsg = Date.now(); this._maybeHistory(); this._render();
        }, { type: 'solar_ev_planner/subscribe' });
        this._lastMsg = Date.now();
      } catch (err) {
        this._error = err?.code === 'not_loaded' ? 'not_loaded' : (err?.message || err?.code || 'error');
        this._unsub = null;
        this._render();
        clearTimeout(this._retry);
        this._retry = setTimeout(() => this._connect(), 30000);
      } finally { this._connecting = false; }
    }

    _maybeHistory(force = false) {
      if (!this._snap || !this._hass || this._histBusy) return;
      if (!force && Date.now() - this._histAt < HISTORY_EVERY) return;
      this._loadHistory();
    }

    async _loadHistory() {
      const s = this._snap;
      const src = s.sources || {};
      const planned = {};
      for (const [carId, ents] of Object.entries(s.entities?.cars || {})) if (ents.planned_current) planned[carId] = ents.planned_current;
      const ids = [src.pv?.entity_id, src.wall?.entity_id, src.grid?.entity_id, src.battery?.entity_id, src.soc?.entity_id, ...Object.values(planned)].filter(Boolean);
      if (!ids.length) return;
      this._histBusy = true;
      try {
        // from yesterday evening, so the night view has the night so far
        const start = midnight() - 6 * HOUR;
        const res = await this._hass.callWS({
          type: 'history/history_during_period', start_time: new Date(start).toISOString(), end_time: new Date().toISOString(),
          entity_ids: ids, minimal_response: true, no_attributes: true, significant_changes_only: false,
        });
        const plannedPts = {};
        for (const [carId, id] of Object.entries(planned)) plannedPts[carId] = toPoints(res[id], 1);
        this._hist = {
          pv: src.pv ? toPoints(res[src.pv.entity_id], src.pv.scale) : [],
          wall: src.wall ? toPoints(res[src.wall.entity_id], src.wall.scale) : [],
          grid: src.grid ? toPoints(res[src.grid.entity_id], src.grid.scale) : [],
          battery: src.battery ? toPoints(res[src.battery.entity_id], src.battery.scale) : [],
          soc: src.soc ? toPoints(res[src.soc.entity_id], src.soc.scale) : [],
          planned: plannedPts,
        };
        this._histAt = Date.now();
        this._render();
      } catch (err) {
        this._histAt = Date.now(); // try again at the next interval
      } finally { this._histBusy = false; }
    }

    // ---------------------------------------------------------------- actions

    _ent(key, carId) { const e = this._snap?.entities; if (!e) return null; return carId ? e.cars?.[carId]?.[key] : e.planner?.[key]; }

    _fail(err) {
      this._toast = `Could not change it: ${err?.message || err}`; this._render();
      clearTimeout(this._toastT); this._toastT = setTimeout(() => { this._toast = null; this._render(); }, 6000);
    }

    _call(domain, service, data) { return this._hass.callService(domain, service, data).catch(err => this._fail(err)); }

    _toggle(key) {
      const id = this._ent(key); if (!id) return;
      const s = this._snap;
      const on = key === 'planning' ? s.enabled : key === 'shadow_mode' ? s.shadow : !!s.settings?.daytime_solar;
      const run = () => this._call('switch', on ? 'turn_off' : 'turn_on', { entity_id: id });
      if (key === 'shadow_mode' && on && s.enabled) {
        this._confirm = { title: 'Start sending commands?', body: 'With shadow mode off, the planner sets the charging current on the car at the Wall Connector: the Tesla through Tesla Fleet, the Rivian through its schedule control. Turn shadow mode back on to stop at any time.', go: 'Send commands', run };
        this._render(); return;
      }
      if (key === 'planning' && on && !s.shadow) {
        this._confirm = { title: 'Turn the planner off?', body: 'It stops sending commands. The heartbeat goes down, so Rivian schedule control parks its charging window and each car goes back to its own schedule.', go: 'Turn off', run };
        this._render(); return;
      }
      run();
    }

    _step(key, delta, lo, hi) {
      const id = this._ent(key); if (!id) return;
      const st = this._hass.states[id];
      const cur = num(st?.state); if (cur === null) return;
      const v = Math.max(lo, Math.min(hi, Math.round((cur + delta) / Math.abs(delta)) * Math.abs(delta)));
      if (v !== cur) this._call('number', 'set_value', { entity_id: id, value: v });
    }

    _select(key, option) { const id = this._ent(key); if (id) this._call('select', 'select_option', { entity_id: id, option }); }

    _more(id) { if (!id) return; const t = Date.now(); if (this._lastMore && t - this._lastMore < 600) return; this._lastMore = t; this.dispatchEvent(new CustomEvent('hass-more-info', { bubbles: true, composed: true, detail: { entityId: id } })); }

    // the free hours, edited in their Schedule helper (every day the same)
    async _freeOpen() {
      const fw = (this._snap?.free_windows || [])[0];
      this._free = { start: fw ? toHHMM(ts(fw.start)) : '20:00', end: fw ? toHHMM(ts(fw.end)) : '06:00', busy: true, error: null, note: null };
      this._render();
      try {
        const item = await this._freeItem();
        const days = DAYS.map(d => item[d] || []);
        const first = days.find(d => d.length) || [];
        // the window as painted: a block ending at 24:00 starts it, a block from 00:00 ends it
        const late = first.find(b => String(b.to).startsWith('24:00'));
        const early = first.find(b => String(b.from).startsWith('00:00') && !String(b.to).startsWith('24:00'));
        if (late || early || first.length === 1) {
          this._free.start = String((late || first[0]).from).slice(0, 5);
          this._free.end = String((early || first[0]).to).slice(0, 5);
        }
        if (!days.every(d => sameBlocks(d, days[0]))) this._free.note = 'The days differ now; saving sets every day to these hours.';
        this._free.item = item;
      } catch (err) {
        this._free.error = String(err?.message || err);
      }
      this._free.busy = false;
      this._render();
    }

    async _freeItem() {
      const id = this._snap?.free_hours_entity;
      if (!id) throw new Error('The planner has no free-hours schedule; set one under its Configure.');
      const reg = await this._hass.callWS({ type: 'config/entity_registry/get', entity_id: id });
      const list = await this._hass.callWS({ type: 'schedule/list' });
      const item = (list || []).find(x => x.id === reg.unique_id);
      if (!item) throw new Error(`${id} is not a Schedule helper made in the UI; edit it where it is defined.`);
      return item;
    }

    async _freeSave() {
      const f = this._free; if (!f || f.busy) return;
      if (!/^\d\d:\d\d$/.test(f.start) || !/^\d\d:\d\d$/.test(f.end)) { f.error = 'Enter both times.'; this._render(); return; }
      f.busy = true; f.error = null; this._render();
      try {
        const item = f.item || await this._freeItem();
        const blocks = dailyBlocks(f.start, f.end);
        const data = { type: 'schedule/update', schedule_id: item.id, name: item.name };
        if (item.icon) data.icon = item.icon;
        for (const d of DAYS) data[d] = blocks;
        await this._hass.callWS(data);
        await this._hass.callWS({ type: 'solar_ev_planner/refresh_schedules' });
        this._free = null;
      } catch (err) {
        f.busy = false; f.error = String(err?.message || err);
      }
      this._render();
    }

    _bind() {
      if (this._bound) return; this._bound = true;
      const root = this.shadowRoot;
      root.addEventListener('click', ev => {
        const el = ev.target.closest('[data-act]');
        const [kind, a, b] = el ? el.getAttribute('data-act').split(':') : [];
        if (this._tip && kind !== 'tip' && !ev.target.closest('.tipbox')) { this._tip = null; this._render(); }
        if (!el) return;
        if (kind === 'tip') {
          // a mouse shows the tooltip on hover; a tap (no hover) opens and closes it
          if (!window.matchMedia?.('(hover: hover)').matches) { this._tip = this._tip === a ? null : a; this._render(); }
        }
        else if (kind === 'toggle') this._toggle(a);
        else if (kind === 'caution') this._select('caution', a);
        else if (kind === 'override') this._select('charger_car_override', a);
        else if (kind === 'step') { const [lo, hi] = el.getAttribute('data-range').split(',').map(Number); this._step(a, Number(b), lo, hi); }
        else if (kind === 'view') { this._view = a; this._render(); }
        else if (kind === 'free-edit') this._freeOpen();
        else if (kind === 'free-cancel') { this._free = null; this._render(); }
        else if (kind === 'free-save') this._freeSave();
        else if (kind === 'cancel') { this._confirm = null; this._render(); }
        else if (kind === 'go') { const c = this._confirm; this._confirm = null; this._render(); c?.run(); }
      });
      root.addEventListener('change', ev => {
        const el = ev.target;
        if (el.matches?.('select[data-select]')) { const key = el.getAttribute('data-select'); const v = el.value; el.blur(); this._select(key, v); }
      });
      root.addEventListener('input', ev => {
        const el = ev.target;
        if (el.matches?.('input[data-free]') && this._free) this._free[el.getAttribute('data-free')] = el.value;
      });
      root.addEventListener('focusout', () => { if (this._deferred) setTimeout(() => { if (this._deferred && !this._editing()) this._render(); }, 0); });
      root.addEventListener('contextmenu', ev => { const el = ev.target.closest('[data-more]'); if (!el || !el.getAttribute('data-more')) return; ev.preventDefault(); this._more(el.getAttribute('data-more')); });
      let hold = null;
      root.addEventListener('pointerdown', ev => { if (ev.pointerType !== 'touch') return; const el = ev.target.closest('[data-more]'); if (!el || !el.getAttribute('data-more')) return; hold = setTimeout(() => this._more(el.getAttribute('data-more')), 550); });
      ['pointerup', 'pointercancel', 'pointerleave'].forEach(n => root.addEventListener(n, () => clearTimeout(hold)));
      root.addEventListener('pointermove', ev => { if (ev.pointerType === 'touch') clearTimeout(hold); this._hover(ev); });
      root.addEventListener('pointerout', ev => { const c = ev.target.closest?.('.chart'); if (c && !c.contains(ev.relatedTarget)) this._unhover(c); });
      root.addEventListener('keydown', ev => {
        if (ev.key !== 'Escape') return;
        if (this._confirm) { this._confirm = null; this._render(); } else if (this._tip) { this._tip = null; this._render(); }
      });
    }

    _editing() { const ae = this.shadowRoot?.activeElement; return !!ae && (ae.tagName === 'INPUT' || ae.tagName === 'SELECT'); }

    _hover(ev) {
      const chart = ev.target.closest?.('.chart[data-chart]'); if (!chart) return;
      const model = this._charts?.[chart.getAttribute('data-chart')]; if (!model) return;
      const r = chart.getBoundingClientRect();
      const x = ev.clientX - r.left;
      if (x < model.x0 || x > model.x1) { this._unhover(chart); return; }
      const t = model.t0 + (x - model.x0) / (model.x1 - model.x0) * (model.t1 - model.t0);
      const html = model.read(t); if (!html) { this._unhover(chart); return; }
      let line = chart.querySelector('.hline'); let tip = chart.querySelector('.hover');
      if (!line) { line = document.createElement('div'); line.className = 'hline'; chart.appendChild(line); tip = document.createElement('div'); tip.className = 'hover'; chart.appendChild(tip); }
      line.style.left = `${x}px`;
      tip.innerHTML = html;
      const w = tip.offsetWidth; tip.style.left = `${x + 12 + w > r.width ? x - 12 - w : x + 12}px`;
    }

    _unhover(chart) { chart.querySelectorAll('.hline, .hover').forEach(n => n.remove()); }

    /** Charts are drawn at their real pixel width (fixed height), so text never scales. */
    _measure() {
      if (!this.shadowRoot) return;
      let changed = false;
      this.shadowRoot.querySelectorAll('.chart[data-chart]').forEach(el => {
        const key = el.getAttribute('data-chart'); const w = Math.round(el.clientWidth);
        if (w > 0 && Math.abs(w - (this._cw[key] || 0)) > 2) { this._cw[key] = w; changed = true; }
      });
      if (changed) { cancelAnimationFrame(this._raf); this._raf = requestAnimationFrame(() => this._render()); }
    }

    _frame(key, height, left = 40) {
      const W = this._cw[key] || 480;
      const L = left; const R = 12; const T = 14; const B = 28;
      return { W, H: height, L, R, T, B, pw: Math.max(40, W - L - R), ph: height - T - B };
    }

    // ---------------------------------------------------------------- render

    _render() {
      if (!this.shadowRoot || !this._cfg) return;
      if (this._editing()) { this._deferred = true; return; }
      this._deferred = false;
      this._bind();
      this.setAttribute('data-theme', this._theme());
      const s = this._snap;
      let body;
      if (!s) {
        body = this._error === 'not_loaded'
          ? '<div class="empty"><b>SolarSage is not running</b>Add or enable it under Settings → Devices &amp; services, then this page fills in.</div>'
          : this._error ? `<div class="empty"><b>Cannot reach the planner</b>${esc(this._error)}. Trying again every 30 seconds.</div>`
            : '<div class="empty"><b>Connecting to the planner…</b></div>';
      } else {
        this._charts = {};
        body = `${this._header(s)}
          <div class="grid">
            ${this._carPanel(s)}${this._chartPanel(s)}${this._sitePanel(s)}
            ${this._batteryPanel(s)}${this._healthPanel(s)}${this._settingsPanel(s)}
          </div>`;
      }
      const toast = this._toast ? `<div class="note bad" role="status">${esc(this._toast)}</div>` : '';
      const dlg = this._confirm ? `<div class="scrim"><div class="dialog" role="dialog" aria-modal="true" aria-labelledby="dlg-t"><h3 id="dlg-t">${esc(this._confirm.title)}</h3><p>${esc(this._confirm.body)}</p><div class="btns"><button type="button" data-act="cancel">Cancel</button><button type="button" class="go" data-act="go">${esc(this._confirm.go)}</button></div></div></div>` : '';
      this.shadowRoot.innerHTML = `<style>${CSS}</style><div class="app">${toast}${body}${dlg}</div>`;
      if (this._confirm) this.shadowRoot.querySelector('.dialog .go')?.focus();
      this._measure();
    }

    _header(s) {
      const now = Date.now();
      const date = new Date(now).toLocaleDateString('en-US', { weekday: 'short', day: 'numeric', month: 'short' });
      const [modeText, modeKind] = MODE[s.mode] || [cap1(s.mode || 'unknown'), 'grey'];
      const modeIcon = s.mode === 'night' ? 'moon' : s.mode === 'day' ? 'sun' : s.mode === 'no_data' || s.mode === 'stale' ? 'alert' : 'pause';
      const [stKind, stIcon, stText, stTip] = !s.enabled
        ? ['red', 'pause', 'Planner off: nothing planned or sent', 'Nothing is planned or sent to the cars. Turn Planning on to start again.']
        : s.shadow
          ? ['amber', 'eye', 'Shadow mode: planning only, nothing sent', 'The planner makes and logs every decision, but sends nothing to the cars. Turn Shadow off to go live.']
          : ['cyan', 'bolt', 'Live: commands go to the car', "The planner's commands go to the cars: charge on or off, and the current."];
      // a status pill: icon and text, icon only on a phone; a tap shows what it means
      const pill = (key, kind, ic, text, tip, more) => {
        const open = this._tip === key;
        return `<span class="hp${open ? ' open' : ''}"><button type="button" class="pill ${kind}" data-act="tip:${key}" aria-expanded="${open}" aria-label="${esc(text)}" data-more="${esc(more || '')}">${icon(ic)}<span class="ptxt">${esc(text)}</span></button><span class="tipbox" role="tooltip"><b>${esc(text)}</b>${tip}</span></span>`;
      };
      const modeTip = `${esc(MODE_TIP[s.mode] || '')}${s.reason ? `<span class="why">Now: ${esc(s.reason)}</span>` : ''}`;
      const tog = (key, label, on, cls = '') => `<button type="button" class="tog ${cls}" aria-pressed="${on ? 'true' : 'false'}" data-act="toggle:${key}" data-more="${esc(this._ent(key) || '')}">${label}</button>`;
      return `<div class="top">
        <div class="brand"><img class="brand-logo" src="/solar_ev_planner/solarsage-logo.png" alt="" width="54" height="54"><div class="title"><h1>SolarSage</h1><div class="sub num">${esc(date)} · ${esc(clock(now))} · planned ${esc(ago(ts(s.planned_at)))}</div></div></div>
        ${pill('mode', modeKind, modeIcon, modeText, modeTip, this._ent('mode'))}
        ${pill('state', stKind, stIcon, stText, esc(stTip), this._ent(s.enabled ? 'shadow_mode' : 'planning'))}
        <div class="grow"></div>
        <div class="toggles">${tog('planning', 'Planning', s.enabled)}${tog('shadow_mode', 'Shadow', s.shadow, 'shadow')}${tog('daytime_solar_charging', 'Daytime solar', !!s.settings?.daytime_solar)}</div>
      </div>`;
    }

    _carColor(c) { return c?.kind === 'tesla' ? 'var(--tesla)' : c?.kind === 'rivian' ? 'var(--rivian)' : 'var(--c-charger)'; }

    _focusCar(s) {
      const cars = s.cars || [];
      return cars.find(c => c.on_charger) || cars.find(c => c.car_id === s.tonight_car) || cars.find(c => c.plugged) || cars[0] || null;
    }

    _planText(c) {
      const st = c.settings || {};
      if (!st.plan_days || st.plan_days === 'off') {
        const lim = num(st.limit_pct) ? st.limit_pct : c.limit;
        return `None · charges to ${lim != null ? `${f0(lim)}%` : 'its limit'}`;
      }
      return `${f0(st.plan_target_pct)}% by ${hhmmLabel(st.plan_time || '07:00')} ${PLAN_DAYS[st.plan_days] || st.plan_days}`;
    }

    _carPanel(s) {
      const c = this._focusCar(s);
      if (!c) {
        return `<section class="panel car"><div class="ptitle">${title('plug', 'On the Charger')}</div>
          <p class="reason">No cars are set up yet.</p>
          <p class="cap">Add one under Settings → Devices &amp; services → SolarSage → <b>Add car</b>. The Rivian needs Rivian schedule control set up first; the Tesla needs Tesla Fleet.</p></section>`;
      }
      const color = this._carColor(c);
      const site = s.site || {};
      const label = c.on_charger ? 'On the Charger' : s.car_on_charger ? 'Plugged In Elsewhere' : c.car_id === s.tonight_car ? 'Tonight\'s Car' : 'Nobody on the Charger';
      const soc = num(c.soc); const lim = num(c.limit);
      const kw = a => (num(a) === null ? '—' : f1(a * s.volts / 1000));
      const planned = c.on_charger ? c.planned_a : null;
      const wallA = num(site.wall_current);
      let planK = s.shadow ? 'Planner would send' : 'Planner sends';
      if (!s.enabled) planK = 'Planner is off';
      let wallSub = `${f1(site.wall_kw)} kW`;
      if (c.on_charger && s.shadow && wallA !== null && planned !== null) wallSub += Math.abs(wallA - planned) >= 2 ? ' · car\'s own schedule' : ' · same as the plan';
      const plannedSub = planned === null ? 'not on the charger' : planned === 0 ? 'paused' : `${kw(planned)} kW`;
      const nxt = s.next_change;
      const outlook = c.outlook;
      const oCls = outlook ? (outlook.short_kwh > 0.05 ? 'red-t' : outlook.paid_kwh > 0.05 ? 'amber-t' : outlook.free_kwh > 0.05 && outlook.solar_kwh < 0.05 ? 'indigo-t' : 'green-t') : '';
      const notes = [];
      if (c.waiting) notes.push(`<div class="note">${esc(c.waiting)}</div>`);
      if (c.last_error) notes.push(`<div class="note bad">${esc(c.last_error)}</div>`);
      if (s.safety) notes.push(`<div class="note">Safety rule acting: ${esc(s.safety)}</div>`);
      const others = (s.cars || []).filter(o => o.car_id !== c.car_id).map(o => {
        const where = o.on_charger ? 'on the charger' : o.plugged ? 'plugged in' : o.home === false ? 'away' : 'home';
        return `<div class="other" data-more="${esc(this._ent('outlook', o.car_id) || '')}"><span class="dot" style="--dc:${this._carColor(o)}"></span><span>${esc(o.name)} <span class="muted num">${num(o.soc) !== null ? `${f0(o.soc)}%` : ''} · ${esc(where)}</span></span><span class="o">${esc(o.outlook?.summary || this._planText(o))}</span></div>`;
      }).join('');
      return `<section class="panel car" aria-label="${esc(label)}">
        <div class="ptitle">${title('plug', esc(label))}</div>
        <div class="car-name"><div class="n" style="display:flex;align-items:center;gap:10px"><span class="dot" style="--dc:${color}"></span>${esc(c.name)}</div><div class="s num">${soc !== null ? `${f0(soc)}%` : '—'} <span class="muted">→ limit ${lim !== null ? `${f0(lim)}%` : '—'}</span></div></div>
        <div class="bar" style="--dc:${color}" role="img" aria-label="Charge ${soc !== null ? f0(soc) : 'unknown'} percent, limit ${lim !== null ? f0(lim) : 'unknown'} percent"><i style="width:${Math.max(0, Math.min(100, soc || 0))}%"></i>${lim !== null ? `<b style="left:${Math.min(99.5, lim)}%"></b>` : ''}</div>
        <div class="tiles">
          <div class="tile plan ${s.shadow ? '' : 'live'}" data-more="${esc(this._ent('planned_current', c.car_id) || '')}"><div class="k">${esc(planK)}</div><div class="v num">${planned === null ? '—' : f0(planned)} <small>A</small></div><div class="u">${esc(plannedSub)}</div></div>
          <div class="tile" data-more="${esc((s.sources?.wall_current || s.sources?.wall)?.entity_id || '')}"><div class="k">Wall Connector reads</div><div class="v num">${wallA === null ? '—' : f0(wallA)} <small>A</small></div><div class="u">${esc(wallSub)}</div></div>
        </div>
        <p class="reason" data-more="${esc(this._ent('reason') || '')}">${esc(s.reason || '')}</p>
        ${notes.join('')}
        <div class="kv">
          <div data-more="${esc(this._ent('next_change') || '')}"><span>Next change</span><span class="num" title="${esc(nxt?.why || '')}">${nxt ? `${esc(clock(ts(nxt.at)))} → ${f0(nxt.amps)} A` : 'None planned'}</span></div>
          <div data-more="${esc(this._ent('outlook', c.car_id) || '')}"><span>Outlook</span><span class="${oCls}">${esc(cap1(outlook?.summary || '—'))}</span></div>
          <div data-more="${esc(this._ent('plan_target', c.car_id) || '')}"><span>Plan</span><span>${esc(this._planText(c))}</span></div>
        </div>
        ${others ? `<div class="others">${others}</div>` : ''}
      </section>`;
    }

    /** The planned car power: the recorded plan until now, the trajectory after. */
    _planSteps(s, t0, t1) {
      const now = Date.now(); const volts = s.volts || 240;
      const plannedCars = Object.values(this._hist?.planned || {});
      const plannedAt = t => plannedCars.reduce((a, pts) => a + Math.max(0, holdAt(pts, t) || 0), 0) * volts / 1000;
      const plan = [];
      if (this._hist) for (let t = t0; t < Math.min(now, t1); t += BUCKET) plan.push({ a: t, b: Math.min(t + BUCKET, now, t1), v: plannedAt(t) });
      for (const p of s.trajectory || []) { const b = ts(p.t); const a = b - STEP; if (b <= Math.max(now, t0) || a >= t1) continue; plan.push({ a: Math.max(a, now, t0), b: Math.min(b, t1), v: p.car_kw || 0 }); }
      return plan;
    }

    _stepPath(plan, x, y, base) {
      if (!plan.length) return '';
      const P = n => n.toFixed(1);
      const parts = [`M${P(x(plan[0].a))},${P(base)}`];
      let end = plan[0].a;
      for (const p of plan) {
        if (p.a > end + 1000) parts.push(`L${P(x(end))},${P(base)} L${P(x(p.a))},${P(base)}`); // a gap: down to zero
        parts.push(`L${P(x(p.a))},${P(y(p.v))} L${P(x(p.b))},${P(y(p.v))}`);
        end = p.b;
      }
      parts.push(`L${P(x(plan[plan.length - 1].b))},${P(base)} Z`);
      return parts.join(' ');
    }

    _axisX(t0, t1, x, yBase, minGap = 56) {
      const out = []; const span = (t1 - t0) / HOUR;
      const every = span > 16 ? 4 : span > 9 ? 2 : 1;
      let last = -1e9;
      for (let t = Math.ceil(t0 / HOUR) * HOUR; t <= t1; t += HOUR) {
        if (new Date(t).getHours() % every) continue;
        const px = x(t); if (px - last < minGap) continue;
        out.push(`<text x="${px.toFixed(1)}" y="${yBase + 20}" text-anchor="middle">${shortHour(t)}</text>`); last = px;
      }
      return out.join('');
    }

    _chartPanel(s) {
      const now = Date.now();
      const sunUp = this._hass?.states['sun.sun']?.state === 'above_horizon';
      const view = this._view || (sunUp ? 'sun' : 'night');
      const seg = `<div class="seg2" role="group" aria-label="Chart"><button type="button" aria-pressed="${view === 'sun'}" data-act="view:sun">${icon('sun', 14)}Sun</button><button type="button" aria-pressed="${view === 'night'}" data-act="view:night">${icon('moon', 14)}Night</button></div>`;
      return view === 'night' ? this._nightPanel(s, seg, now) : this._sunPanel(s, seg, now);
    }

    /**
     * Today, actuals first: what the sun made, what the house used and what the car drew, with
     * the forecast behind them for reference. After now the line is the planner's own expectation
     * (Solcast corrected by today's production), which is what it plans on.
     */
    _sunPanel(s, seg, now) {
      const fc = (s.forecast || []).map(f => ({ t: ts(f.t), p10: f.p10, p50: f.p50, p90: f.p90 }));
      const today0 = midnight(now);
      const lit = fc.filter(f => f.t >= today0 && f.t < today0 + 24 * HOUR && f.p90 > 0.05);
      let t0 = lit.length ? lit[0].t : today0 + 6 * HOUR; let t1 = lit.length ? lit[lit.length - 1].t + SLOT : today0 + 20 * HOUR;
      t0 = Math.floor(t0 / HOUR) * HOUR; t1 = Math.ceil(t1 / HOUR) * HOUR;
      const volts = s.volts || 240;
      const h = this._hist || { pv: [], wall: [], grid: [], battery: [], planned: {} };
      const upto = Math.min(now, t1);
      const pvB = bucketMeans(h.pv, t0, upto);
      const wallB = bucketMeans(h.wall, t0, upto);
      const houseB = houseBuckets(h, t0, upto);
      // the planner's expected solar from now on (15-min steps, each ending at p.t)
      const exp = (s.trajectory || []).map(p => ({ t: ts(p.t) - STEP / 2, v: num(p.pv_kw) })).filter(p => p.v !== null && p.t > now && p.t < t1);
      const expLine = exp.length ? [{ t: now, v: pvB.length ? pvB[pvB.length - 1].v : exp[0].v }, ...exp] : [];
      const plan = this._planSteps(s, Math.max(now, t0), t1);
      const yMax = niceMax(Math.max(2, ...fc.filter(f => f.t >= t0 && f.t < t1).map(f => f.p90), ...pvB.map(p => p.v), ...houseB.map(p => p.v), ...wallB.map(p => p.v), ...exp.map(p => p.v), ...plan.map(p => p.v)) * 1.05);
      const F = this._frame('today', 260);
      const x = t => F.L + (t - t0) / (t1 - t0) * F.pw; const y = v => F.T + F.ph - Math.max(0, Math.min(yMax, v)) / yMax * F.ph;
      const base = F.T + F.ph; const P = n => n.toFixed(1);
      const fcIn = fc.filter(f => f.t + SLOT > t0 && f.t < t1).map(f => ({ ...f, m: f.t + SLOT / 2 }));
      const band = fcIn.length ? `M${fcIn.map(f => `${P(x(f.m))},${P(y(f.p90))}`).join(' L')} L${[...fcIn].reverse().map(f => `${P(x(f.m))},${P(y(f.p10))}`).join(' L')} Z` : '';
      const p50 = fcIn.length ? `M${fcIn.map(f => `${P(x(f.m))},${P(y(f.p50))}`).join(' L')}` : '';
      const line = pts => (pts.length > 1 ? `M${pts.map(p => `${P(x(p.t))},${P(y(p.v))}`).join(' L')}` : '');
      const area = pts => (pts.length > 1 ? `${line(pts)} L${P(x(pts[pts.length - 1].t))},${P(base)} L${P(x(pts[0].t))},${P(base)} Z` : '');
      const grid = [0.5, 1].map(f => `<line x1="${F.L}" y1="${P(base - f * F.ph)}" x2="${F.L + F.pw}" y2="${P(base - f * F.ph)}"/>`).join('');
      const yl = [1, 0.5].map(f => `<text x="${F.L - 8}" y="${P(base - f * F.ph + 4)}" text-anchor="end">${f === 1 ? `${yMax} kW` : yMax / 2}</text>`).join('');
      const nowMark = now > t0 && now < t1 ? `<line x1="${P(x(now))}" y1="${F.T - 6}" x2="${P(x(now))}" y2="${base}" stroke="var(--ink)" stroke-width="1.5" stroke-dasharray="2 4"/><text x="${P(x(now) + 6)}" y="${F.T + 2}" fill="var(--ink)" font-size="12">now</text>` : '';
      // the charger's own colour here: the Rivian's yellow is too close to the sun's orange
      const carColor = 'var(--c-charger)';
      this._charts.today = {
        x0: F.L, x1: F.L + F.pw, t0, t1,
        read: t => {
          const f = fcIn.find(r => t >= r.t && t < r.t + SLOT);
          const near = pts => pts.find(p => Math.abs(p.t - t) <= BUCKET / 2);
          const pv = near(pvB); const hs = near(houseB); const wl = near(wallB);
          const ex = exp.find(p => Math.abs(p.t - t) <= STEP / 2); const pl = plan.find(p => t >= p.a && t < p.b);
          const rows = [`<b>${esc(clock(t))}</b>`];
          if (pv) rows.push(`<span class="row">${swatch('var(--c-solar)')}Solar produced ${f1(pv.v)} kW</span>`);
          if (hs) rows.push(`<span class="row">${swatch('var(--c-house)')}House use ${f1(hs.v)} kW</span>`);
          if (wl) rows.push(`<span class="row">${swatch(carColor)}Car charging ${f1(wl.v)} kW</span>`);
          if (ex) rows.push(`<span class="row">${swatch('var(--c-solar)', 'dash')}Solar expected ${f1(ex.v)} kW</span>`);
          if (pl) rows.push(`<span class="row">${swatch(carColor, 'area')}Planned car ${f1(pl.v)} kW <span class="muted">${f0(pl.v * 1000 / volts)} A</span></span>`);
          if (f) rows.push(`<span class="row">${swatch('var(--ink-3)', 'dot')}Solcast ${f1(f.p50)} kW <span class="muted">(${f1(f.p10)}–${f1(f.p90)})</span></span>`);
          return rows.length > 1 ? rows.join('') : '';
        },
      };
      const pv = s.pv || {};
      const have = !!this._hist;
      const solarSoFar = num(pv.pv_today_kwh) ?? (have ? bucketKwh(pvB) : null);
      const fcSoFar = fcKwh(fc, today0, now);
      const houseSoFar = have && houseB.length ? bucketKwh(houseB) : null;
      const carToday = have ? energy(h.wall, today0, now) : null;
      const vs = solarSoFar !== null && fcSoFar > 0.5 ? solarSoFar / fcSoFar - 1 : null;
      const vsTxt = vs === null ? '' : ` <span class="${vs < -0.1 ? 'amber-t' : vs > 0.1 ? 'green-t' : 'muted'}">${vs >= 0 ? '+' : '−'}${f0(Math.abs(vs) * 100)}% vs Solcast</span>`;
      const ratio = num(pv.ratio);
      let check = '';
      if (vs !== null && Math.abs(vs) >= 0.1) {
        check = `Solar is running ${f0(Math.abs(vs) * 100)}% ${vs < 0 ? 'under' : 'over'} the forecast. The planner plans the rest of today at ×${ratio !== null ? ratio.toFixed(2) : '—'} of Solcast and re-plans every 15 minutes from the live battery level${vs < 0 ? '; if the car starts pulling from the grid or the battery at its floor, it is cut back within minutes' : ''}.`;
      } else if (vs !== null) check = 'Solar is tracking the forecast; the planner works from the live battery level and re-plans every 15 minutes.';
      return `<section class="panel today">
        <div class="ptitle">${title('sun', 'Today: Solar, House &amp; Car')}${seg}</div>
        <div class="legend">${key('var(--c-solar)', 'area', 'Solar produced')}${key('var(--c-house)', 'line', 'House use')}${key(carColor, 'line', 'Car charging')}${key('var(--c-solar)', 'dash', 'Solar expected (adjusted)')}${key(carColor, 'area', 'Planned car')}${key('var(--ink-3)', 'dot', 'Solcast forecast')}</div>
        <div class="chart" data-chart="today">
          <svg width="${F.W}" height="${F.H}" viewBox="0 0 ${F.W} ${F.H}" role="img" aria-label="Today's solar produced, house use and car charging, with the adjusted expectation for the rest of the day and the Solcast forecast, ${esc(hourLabel(t0))} to ${esc(hourLabel(t1))}">
            <g stroke="var(--hair-2)" stroke-width="1">${grid}<line x1="${F.L}" y1="${base}" x2="${F.L + F.pw}" y2="${base}" stroke="var(--hair)"/></g>
            <g fill="var(--ink-2)" font-size="12">${yl}${this._axisX(t0, t1, x, base)}</g>
            ${band ? `<path d="${band}" fill="var(--ink)" fill-opacity="0.06"/>` : ''}
            ${p50 ? `<path d="${p50}" fill="none" stroke="var(--ink-3)" stroke-width="1.6" stroke-dasharray="0.1 4" stroke-linecap="round"/>` : ''}
            ${pvB.length > 1 ? `<path d="${area(pvB)}" fill="var(--c-solar)" fill-opacity="0.28"/><path d="${line(pvB)}" fill="none" stroke="var(--c-solar)" stroke-width="2.6" stroke-linejoin="round"/>` : ''}
            ${plan.length ? `<path d="${this._stepPath(plan, x, y, base)}" fill="color-mix(in srgb, ${carColor} 35%, transparent)" stroke="${carColor}" stroke-width="1.2" stroke-linejoin="round"/>` : ''}
            ${expLine.length > 1 ? `<path d="${line(expLine)}" fill="none" stroke="var(--c-solar)" stroke-width="2.2" stroke-dasharray="6 4" stroke-linejoin="round"/>` : ''}
            ${houseB.length > 1 ? `<path d="${line(houseB)}" fill="none" stroke="var(--c-house)" stroke-width="2.2" stroke-linejoin="round"/>` : ''}
            ${wallB.length > 1 ? `<path d="${line(wallB)}" fill="none" stroke="${carColor}" stroke-width="2" stroke-linejoin="round"/>` : ''}
            ${nowMark}
            ${!fcIn.length && !pvB.length ? `<text x="${F.L + F.pw / 2}" y="${F.T + F.ph / 2}" text-anchor="middle" fill="var(--ink-2)" font-size="14">No solar readings or forecast yet</text>` : ''}
          </svg>
        </div>
        <div class="stats num">
          <span data-more="${esc(s.sources?.pv?.entity_id || '')}">Solar so far <b>${solarSoFar !== null ? `${f1(solarSoFar)} kWh` : '—'}</b>${vsTxt}</span>
          ${houseSoFar !== null ? `<span>House so far <b>${f1(houseSoFar)} kWh</b></span>` : ''}
          ${carToday !== null ? `<span>Car today <b>${f1(carToday)} kWh</b></span>` : ''}
          <span data-more="${esc(this._ent('forecast_correction') || '')}">Rest of today <b>${f0(pv.remaining_expected_kwh)} kWh</b>${num(pv.remaining_p50_kwh) !== null ? ` <span class="muted">(Solcast ${f0(pv.remaining_p50_kwh)})</span>` : ''}</span>
        </div>
        ${check ? `<p class="cap">${esc(check)}</p>` : ''}
      </section>`;
    }

    /** When the car starts in a free window: the night charging start inside it, else its start. */
    _nightStart(s, a, b) {
      const v = s.settings?.night_start;
      if (!v || v === FREE_START) return a;
      const [h, m] = String(v).split(':').map(Number);
      const d = new Date(a); d.setHours(h, m || 0, 0, 0);
      let at = d.getTime(); if (at < a) at += 24 * HOUR;
      return at < b ? at : a;
    }

    _nightPanel(s, seg, now) {
      const fw = (s.free_windows || [])[0];
      const fa = fw ? ts(fw.start) : null; const fb = fw ? ts(fw.end) : null;
      // the evening before the free hours (two hours of it), through to an hour after them
      const t0 = Math.floor(((fa ? fa - 2 * HOUR : now)) / HOUR) * HOUR;
      const t1 = Math.ceil(((fb ? fb + HOUR : now + 12 * HOUR)) / HOUR) * HOUR;
      const volts = s.volts || 240;
      const h = this._hist || { wall: [], planned: {} };
      const wallB = bucketMeans(h.wall, t0, Math.min(now, t1));
      const c = this._focusCar(s); const carColor = this._carColor(c);
      const fullKw = (c?.max_a || 48) * volts / 1000;
      const start = fw ? this._nightStart(s, fa, fb) : null;
      let plan = this._planSteps(s, t0, t1);
      // By day the planner's path covers the day only: draw tonight as it plans it, the car's
      // free-hours energy at full current from the start time.
      const inFree = p => fw && p.b > fa && p.a < fb;
      if (fw && c && !plan.some(p => inFree(p) && p.v > 0.1)) {
        const kwh = num(c.outlook?.free_kwh) || 0;
        const from = Math.max(start, now);
        const until = Math.min(fb, from + kwh / fullKw * HOUR);
        plan = plan.filter(p => !inFree(p));
        if (until > from) plan.push({ a: from, b: until, v: fullKw });
        plan.sort((p, q) => p.a - q.a);
      }
      const maxKw = fullKw;
      const yMax = niceMax(Math.max(2, maxKw, ...plan.map(p => p.v), ...wallB.map(p => p.v)) * 1.05);
      const F = this._frame('night', 250);
      const x = t => F.L + (t - t0) / (t1 - t0) * F.pw; const y = v => F.T + F.ph - Math.max(0, Math.min(yMax, v)) / yMax * F.ph;
      const base = F.T + F.ph; const P = n => n.toFixed(1);
      const windows = (s.free_windows || []).map(w => ({ a: Math.max(ts(w.start), t0), b: Math.min(ts(w.end), t1) })).filter(w => w.b > w.a);
      const freeRects = windows.map(w => `<rect x="${P(x(w.a))}" y="${F.T}" width="${P(x(w.b) - x(w.a))}" height="${F.ph}" fill="var(--free-band)"/>`).join('');
      const freeLabel = windows[0] && x(windows[0].b) - x(windows[0].a) > 80 ? `<text x="${P(x(windows[0].a) + 8)}" y="${F.T + 16}" fill="var(--indigo)" font-size="12" font-weight="600">Free hours</text>` : '';
      const startMark = start && start > t0 && start < t1 && start !== fa ? `<line x1="${P(x(start))}" y1="${F.T}" x2="${P(x(start))}" y2="${base}" stroke="${carColor}" stroke-width="2" stroke-dasharray="5 4"/><text x="${P(x(start) + 6)}" y="${F.T + 34}" fill="var(--ink)" font-size="12">car starts ${esc(clock(start))}</text>` : '';
      const nowMark = now > t0 && now < t1 ? `<line x1="${P(x(now))}" y1="${F.T - 6}" x2="${P(x(now))}" y2="${base}" stroke="var(--ink)" stroke-width="1.5" stroke-dasharray="2 4"/><text x="${P(x(now) + 6)}" y="${F.T + 2}" fill="var(--ink)" font-size="12">now</text>` : '';
      const grid = [0.5, 1].map(f => `<line x1="${F.L}" y1="${P(base - f * F.ph)}" x2="${F.L + F.pw}" y2="${P(base - f * F.ph)}"/>`).join('');
      const yl = [1, 0.5].map(f => `<text x="${F.L - 8}" y="${P(base - f * F.ph + 4)}" text-anchor="end">${f === 1 ? `${yMax} kW` : yMax / 2}</text>`).join('');
      const line = pts => (pts.length > 1 ? `M${pts.map(p => `${P(x(p.t))},${P(y(p.v))}`).join(' L')}` : '');
      const isFree = t => windows.some(w => t >= w.a && t < w.b);
      this._charts.night = {
        x0: F.L, x1: F.L + F.pw, t0, t1,
        read: t => {
          const pl = plan.find(p => t >= p.a && t < p.b); const wl = wallB.find(p => Math.abs(p.t - t) <= BUCKET / 2);
          const rows = [`<b>${esc(clock(t))}</b> <span class="muted">${isFree(t) ? 'free hours' : 'paid'}</span>`];
          if (pl) rows.push(`<span class="row">${swatch(carColor, 'area')}Planned car ${f1(pl.v)} kW <span class="muted">${f0(pl.v * 1000 / volts)} A</span></span>`);
          if (wl) rows.push(`<span class="row">${swatch(carColor)}Car charging ${f1(wl.v)} kW</span>`);
          return rows.join('');
        },
      };
      // what the plan puts in the car tonight, and anything it would charge in paid hours after
      // the sun has gone (charging on the sun is the point; on paid grid power it is not)
      const lastSun = (s.forecast || []).filter(f => f.p50 > 0.05 && ts(f.t) < (fa || now)).reduce((m, f) => Math.max(m, ts(f.t) + SLOT), 0);
      const dark = t => t >= lastSun;
      let freeKwh = 0; let paidKwh = 0;
      for (const p of plan) { if (p.b <= now) continue; const mid = (p.a + p.b) / 2; const e = p.v * (p.b - Math.max(p.a, now)) / HOUR; if (isFree(mid)) freeKwh += e; else if (dark(mid)) paidKwh += e; }
      const drewPaid = wallB.filter(p => !isFree(p.t) && dark(p.t) && p.v > 0.3).length * BUCKET / HOUR;
      return `<section class="panel today">
        <div class="ptitle">${title('moon', 'Tonight: Free Hours')}${seg}</div>
        <div class="legend">${key('var(--indigo)', 'band', 'Free hours', 0.4)}${key(carColor, 'area', 'Planned car')}${key(carColor, 'line', 'Car charging')}${key(carColor, 'dash', 'Car starts')}</div>
        <div class="chart" data-chart="night">
          <svg width="${F.W}" height="${F.H}" viewBox="0 0 ${F.W} ${F.H}" role="img" aria-label="Tonight: the free hours, when the car starts and its planned charging power">
            ${freeRects}
            <g stroke="var(--hair-2)" stroke-width="1">${grid}<line x1="${F.L}" y1="${base}" x2="${F.L + F.pw}" y2="${base}" stroke="var(--hair)"/></g>
            <g fill="var(--ink-2)" font-size="12">${yl}${this._axisX(t0, t1, x, base)}</g>
            ${plan.length ? `<path d="${this._stepPath(plan, x, y, base)}" fill="color-mix(in srgb, ${carColor} 45%, transparent)" stroke="${carColor}" stroke-width="1.5" stroke-linejoin="round"/>` : ''}
            ${wallB.length > 1 ? `<path d="${line(wallB)}" fill="none" stroke="${carColor}" stroke-width="2" stroke-linejoin="round"/>` : ''}
            ${freeLabel}${startMark}${nowMark}
          </svg>
        </div>
        <div class="stats num">
          <span>Free hours <b>${fw ? `${esc(clock(fa))}–${esc(clock(fb))}` : 'none ahead'}</b></span>
          ${!fw && s.free_hours_problem ? `<span class="amber-t">${esc(s.free_hours_problem)}</span>` : ''}
          <span>Car starts <b>${start ? esc(clock(start)) : '—'}</b></span>
          <span>Planned tonight <b>${f1(freeKwh)} kWh</b></span>
          <span>${paidKwh > 0.1 ? `<b class="amber-t">${f1(paidKwh)} kWh in paid hours</b>` : 'Paid hours: <b class="green-t">car paused</b>'}</span>
          ${drewPaid > 0.2 ? `<span class="amber-t">The car drew power in paid hours (about ${f1(drewPaid)} h)</span>` : ''}
        </div>
      </section>`;
    }

    /**
     * Home Now as a flow hub: the house in the middle with a spoke each to the sun, the grid, the
     * battery and the charger. Dots run along a spoke the way the power flows, faster with more of
     * it. Drawn at its real pixel width, so the text never scales.
     */
    _sitePanel(s) {
      const site = s.site || {};
      const c = this._focusCar(s);
      const now = Date.now();
      const src = s.sources || {};
      const pv = num(site.pv_kw); const grid = num(site.grid_kw); const bat = num(site.battery_kw);
      const wall = num(site.wall_kw); const soc = num(site.soc);
      const car = c?.on_charger ? c : null;
      const carSoc = car ? num(car.soc) : null;
      const freeNow = (s.free_windows || []).some(w => ts(w.start) <= now && now < ts(w.end));
      const floor = num(s.settings?.floor_pct);
      const live = v => v !== null && Math.abs(v) >= 0.05;
      const W = Math.max(260, Math.min(460, this._cw.site || 380)); const H = 308;
      const cx = W / 2; const cy = 156; const R = 28; const RH = 36; const side = 48;
      const at = { pv: [cx, 42], grid: [side, cy], bat: [W - side, cy], car: [cx, H - 38], house: [cx, cy] };
      const P = n => n.toFixed(1);
      // a spoke runs centre to centre and the discs sit on top, so dots slide out from under one
      // node and in under the next; `back` turns the flow round
      const spoke = (a, b, kw, color, back = false) => {
        const [p, q] = back ? [b, a] : [a, b];
        const d = `M${P(p[0])},${P(p[1])} L${P(q[0])},${P(q[1])}`;
        if (!live(kw)) return `<path d="${d}" stroke="var(--hair)" stroke-width="2"/>`;
        const dur = FLOW_GAP / (14 + 40 * Math.min(1, Math.abs(kw) / 10));
        return `<path d="${d}" stroke="${color}" stroke-opacity="0.32" stroke-width="2"/>
          <path class="flow" d="${d}" stroke="${color}" stroke-width="5" stroke-linecap="round" stroke-dasharray="0.1 ${FLOW_GAP - 0.1}" style="animation-duration:${dur.toFixed(2)}s;animation-delay:-${((now / 1000) % dur).toFixed(2)}s"/>`;
      };
      const ico = (name, [x, y], size, color) => `<svg class="ic" x="${P(x - size / 2)}" y="${P(y - size / 2)}" width="${size}" height="${size}" viewBox="0 0 24 24" style="color:${color}" aria-hidden="true">${ICON[name]}</svg>`;
      // a lit node is a solid chip like the rest of the card; an idle one is a dim well
      const chip = (xy, r, color, name, on, more = '') => `<g data-more="${esc(more || '')}"><circle cx="${P(xy[0])}" cy="${P(xy[1])}" r="${r}" fill="${on ? color : `color-mix(in srgb, ${color} 24%, var(--hub))`}"/>${ico(name, xy, 22, on ? 'var(--chip-ink)' : color)}</g>`;
      // the two things that hold charge are discs whose own border is the charge: it fills
      // clockwise from the top, with the icon and the percent inside
      const gauge = ([x, y], color, pct, name, more = '') => {
        const sw = 5; const r = R - sw / 2; const C = 2 * Math.PI * r;
        const fill = P(C * Math.max(0, Math.min(100, pct)) / 100);
        return `<g data-more="${esc(more || '')}"><circle cx="${P(x)}" cy="${P(y)}" r="${R}" fill="color-mix(in srgb, ${color} 20%, var(--hub))"/>
          <circle cx="${P(x)}" cy="${P(y)}" r="${r}" fill="none" stroke="color-mix(in srgb, ${color} 30%, var(--hub))" stroke-width="${sw}"/>
          <circle cx="${P(x)}" cy="${P(y)}" r="${r}" fill="none" stroke="${color}" stroke-width="${sw}" stroke-linecap="round" stroke-dasharray="${fill} ${P(C)}" transform="rotate(-90 ${P(x)} ${P(y)})"/>
          ${ico(name, [x, y - 8], 17, color)}
          <text x="${P(x)}" y="${P(y + 14)}" text-anchor="middle" class="num" fill="var(--ink)" font-size="14" font-weight="600">${f0(pct)}<tspan font-size="10" font-weight="500">%</tspan></text></g>`;
      };
      const kw = (x, y, v, anchor) => `<text x="${P(x)}" y="${P(y)}" text-anchor="${anchor}" class="num" fill="var(--ink)" font-size="20" font-weight="500">${f1(v === null ? null : Math.abs(v))}<tspan dx="3" font-size="13" font-weight="400" fill="var(--ink-2)">kW</tspan></text>`;
      const word = (x, y, t, anchor, fill = 'var(--ink-2)', weight = 400) => `<text x="${P(x)}" y="${P(y)}" text-anchor="${anchor}" fill="${fill}" font-size="13" font-weight="${weight}">${esc(t)}</text>`;
      const gridWord = !live(grid) ? ['idle', 'var(--ink-3)'] : grid < 0 ? ['exporting', 'var(--green)'] : freeNow ? ['free power', 'var(--indigo)'] : ['importing', 'var(--amber)'];
      const batWord = !live(bat) ? ['holding', 'var(--ink-3)'] : bat > 0 ? ['charging', 'var(--green)'] : ['discharging', 'var(--amber)'];
      const batColor = soc !== null && floor !== null && soc <= floor ? 'var(--red)' : 'var(--c-battery)';
      const carColor = 'var(--c-charger)';
      const carName = car ? (car.name.length > 16 ? `${car.name.slice(0, 15)}…` : car.name) : 'Charger';
      const sx = cx + R + 12; // the sun and the car carry their reading beside them, clear of the spokes
      const label = `Solar ${f1(pv)} kW, house ${f1(site.house_kw)} kW, grid ${gridWord[0]} ${f1(grid === null ? null : Math.abs(grid))} kW, battery ${f0(soc)}% ${batWord[0]} ${f1(bat === null ? null : Math.abs(bat))} kW, ${carName} ${f1(wall)} kW`;
      let foot = '';
      if (site.problem) foot = `<span class="red-t">${esc(site.problem)}</span>`;
      else if (c?.on_charger && s.shadow && num(site.wall_current) !== null && Math.abs(site.wall_current - c.planned_a) >= 2) foot = `The car draws ${f0(site.wall_current)} A on its own schedule; the plan would send ${f0(c.planned_a)} A.`;
      return `<section class="panel site">
        <div class="ptitle">${title('home', 'Home Now')}</div>
        <div class="chart hub" data-chart="site">
          <svg width="${W}" height="${H}" viewBox="0 0 ${W} ${H}" role="img" aria-label="${esc(label)}">
            <g fill="none">
              ${spoke(at.pv, at.house, pv, 'var(--c-solar)')}
              ${spoke(at.grid, at.house, grid, 'var(--c-grid)', grid < 0)}
              ${spoke(at.house, at.bat, bat, batColor, bat < 0)}
              ${spoke(at.house, at.car, wall, carColor)}
            </g>
            ${chip(at.pv, R, 'var(--c-solar)', 'sun', live(pv), src.pv?.entity_id)}
            ${chip(at.grid, R, 'var(--c-grid)', 'grid', live(grid), src.grid?.entity_id)}
            ${soc !== null ? gauge(at.bat, batColor, soc, 'battery', src.soc?.entity_id) : chip(at.bat, R, batColor, 'battery', live(bat), src.battery?.entity_id)}
            ${carSoc !== null ? gauge(at.car, carColor, carSoc, 'car', src.wall?.entity_id) : chip(at.car, R, carColor, car ? 'car' : 'plug', live(wall), src.wall?.entity_id)}
            <circle cx="${P(cx)}" cy="${cy}" r="${RH}" fill="var(--c-house)"/>
            ${ico('home', [cx, cy - 10], 22, 'var(--chip-ink)')}
            <text x="${P(cx)}" y="${cy + 19}" text-anchor="middle" class="num" fill="var(--chip-ink)" font-size="16" font-weight="600">${f1(site.house_kw)}<tspan dx="2" font-size="12" font-weight="500">kW</tspan></text>
            ${word(sx, at.pv[1] - 6, 'Solar', 'start', 'var(--ink-2)', 500)}${kw(sx, at.pv[1] + 15, pv, 'start')}
            ${word(sx, at.car[1] - 6, carName, 'start', 'var(--ink-2)', 500)}${kw(sx, at.car[1] + 15, wall, 'start')}
            ${word(at.grid[0], cy - R - 11, 'Grid', 'middle', 'var(--ink-2)', 500)}${kw(at.grid[0], cy + R + 25, grid, 'middle')}${word(at.grid[0], cy + R + 43, gridWord[0], 'middle', gridWord[1])}
            ${word(at.bat[0], cy - R - 11, 'Battery', 'middle', 'var(--ink-2)', 500)}${kw(at.bat[0], cy + R + 25, bat, 'middle')}${word(at.bat[0], cy + R + 43, batWord[0], 'middle', batWord[1])}
          </svg>
        </div>
        ${foot ? `<div class="foot">${foot}</div>` : ''}
      </section>`;
    }

    /**
     * Today and tonight: 24 hours centred on the middle of the daylight, so the solar day sits in
     * the middle. The measured level up to now, the planner's paths after it.
     */
    _batteryPanel(s) {
      const now = Date.now();
      const fc = (s.forecast || []).map(f => ({ t: ts(f.t), p90: f.p90 }));
      let noon = solarNoon(fc, midnight(now));
      if (now - noon > 12 * HOUR) noon += 24 * HOUR; else if (noon - now > 12 * HOUR) noon -= 24 * HOUR;
      const t0 = noon - 12 * HOUR; const t1 = noon + 12 * HOUR;
      const floor = num(s.settings?.floor_pct) ?? 30;
      const q = num(s.caution_quantile);
      const qTxt = q !== null ? `${Math.round(q * 100)}%` : '';
      const soc0 = num(s.site?.soc);
      const actual = bucketMeans(this._hist?.soc || [], t0, Math.min(now, t1));
      if (soc0 !== null && now > t0 && now < t1) actual.push({ t: now, v: soc0 });
      const tr = (s.trajectory || []).map(p => ({ t: ts(p.t), soc: num(p.soc), exp: num(p.expected), noCar: num(p.no_car) })).filter(p => p.t > now && p.t <= t1 && p.soc !== null);
      const start = soc0 ?? tr[0]?.soc;
      const ahead = tr.length && start != null ? [{ t: now, soc: start, exp: start, noCar: start }, ...tr] : [];
      if (!actual.length && !ahead.length) return `<section class="panel battery"><div class="ptitle">${title('battery', 'Battery Outlook')}</div><p class="reason">No battery readings or path yet: the planner shows one once it can decide.</p></section>`;
      const F = this._frame('battery', 230, 44);
      const x = t => F.L + (t - t0) / (t1 - t0) * F.pw; const y = v => F.T + F.ph - Math.max(0, Math.min(100, v)) / 100 * F.ph;
      const base = F.T + F.ph; const P = n => n.toFixed(1);
      const path = (pts, k) => (pts.length > 1 ? `M${pts.map(p => `${P(x(p.t))},${P(y(p[k]))}`).join(' L')}` : '');
      const low = tr.length ? tr.reduce((a, p) => (p.soc < a.soc ? p : a), tr[0]) : null;
      // daylight from the forecast (the sun's hours), and the free hours, as bands
      const lit = fc.filter(f => f.t + SLOT > t0 && f.t < t1 && f.p90 > 0.05);
      const sun = lit.length ? { a: Math.max(t0, lit[0].t), b: Math.min(t1, lit[lit.length - 1].t + SLOT) } : null;
      const free = [...(s.free_windows_past || []), ...(s.free_windows || [])].map(w => ({ a: Math.max(ts(w.start), t0), b: Math.min(ts(w.end), t1) })).filter(w => w.b > w.a);
      const sunRect = sun ? `<rect x="${P(x(sun.a))}" y="${F.T}" width="${P(x(sun.b) - x(sun.a))}" height="${F.ph}" fill="var(--c-solar)" fill-opacity="0.08"/><text x="${P((x(sun.a) + x(sun.b)) / 2)}" y="${P(base - 8)}" fill="var(--c-solar)" font-size="12" font-weight="600" text-anchor="middle">Daylight</text>` : '';
      const freeRects = free.map(w => `<rect x="${P(x(w.a))}" y="${F.T}" width="${P(x(w.b) - x(w.a))}" height="${F.ph}" fill="var(--free-band)"/>${x(w.b) - x(w.a) > 70 ? `<text x="${P(x(w.a) + 8)}" y="${F.T + 16}" fill="var(--indigo)" font-size="12" font-weight="600">Free hours</text>` : ''}`).join('');
      const nowMark = now > t0 && now < t1 ? `<line x1="${P(x(now))}" y1="${F.T - 6}" x2="${P(x(now))}" y2="${base}" stroke="var(--ink)" stroke-width="1.5" stroke-dasharray="2 4"/><text x="${P(x(now) + 6)}" y="${F.T + 2}" fill="var(--ink)" font-size="12">now</text>` : '';
      const actualPts = actual.map(p => ({ t: p.t, v: p.v }));
      this._charts.battery = {
        x0: F.L, x1: F.L + F.pw, t0, t1,
        read: t => {
          const freeNow = free.some(w => t >= w.a && t < w.b);
          const head = `<b>${esc(clock(t))}</b>${freeNow ? ' <span class="muted">free hours</span>' : ''}`;
          if (t <= now) {
            const a = actualPts.length ? actualPts.reduce((m, p) => (Math.abs(p.t - t) < Math.abs(m.t - t) ? p : m), actualPts[0]) : null;
            return a && Math.abs(a.t - t) <= BUCKET ? `${head}<span class="row">${swatch('var(--c-battery)')}Battery level ${f0(a.v)}%</span>` : '';
          }
          if (!ahead.length) return '';
          const p = ahead.reduce((m, r) => (Math.abs(r.t - t) < Math.abs(m.t - t) ? r : m), ahead[0]);
          return head
            + `<span class="row">${swatch('var(--c-battery)', 'dash')}Expected ${f0(p.exp)}%</span>`
            + `<span class="row">${swatch('var(--amber)', 'dash')}Cautious${qTxt ? ` (${qTxt})` : ''} ${f0(p.soc)}%</span>`
            + `<span class="row">${swatch('var(--c-house)', 'dot')}Cautious, car off ${f0(p.noCar)}%</span>`
            + `<span class="row">${swatch('var(--red)', 'dash')}Floor ${f0(floor)}%</span>`;
        },
      };
      const right = low ? `<div class="r num" data-more="${esc(this._ent('battery_minimum') || '')}">lowest ahead <b>${f0(low.soc)}%</b> at ${esc(clock(low.t))}</div>`
        : soc0 !== null ? `<div class="r num">now <b>${f0(soc0)}%</b></div>` : '';
      return `<section class="panel battery">
        <div class="ptitle">${title('battery', 'Battery Outlook')}${right}</div>
        <div class="legend">${key('var(--c-battery)', 'line', 'Battery level')}${key('var(--c-battery)', 'dash', 'Expected')}${key('var(--amber)', 'dash', `Cautious${qTxt ? ` (${qTxt})` : ''}`)}${key('var(--c-house)', 'dot', 'Cautious, car off')}${key('var(--red)', 'dash', 'Floor')}${key('var(--c-battery)', 'marker', 'Lowest ahead')}</div>
        <div class="chart" data-chart="battery">
          <svg width="${F.W}" height="${F.H}" viewBox="0 0 ${F.W} ${F.H}" role="img" aria-label="Home battery from ${esc(hourLabel(t0))} to ${esc(hourLabel(t1))}: the measured level so far and the planner's paths after now${low ? `; lowest ${f0(low.soc)} percent at ${esc(clock(low.t))}` : ''}; floor ${f0(floor)} percent">
            ${sunRect}${freeRects}
            <g stroke="var(--hair-2)" stroke-width="1"><line x1="${F.L}" y1="${P(y(100))}" x2="${F.L + F.pw}" y2="${P(y(100))}"/><line x1="${F.L}" y1="${P(y(50))}" x2="${F.L + F.pw}" y2="${P(y(50))}"/><line x1="${F.L}" y1="${base}" x2="${F.L + F.pw}" y2="${base}" stroke="var(--hair)"/></g>
            <g fill="var(--ink-2)" font-size="12"><text x="${F.L - 8}" y="${P(y(100) + 4)}" text-anchor="end">100%</text><text x="${F.L - 8}" y="${P(y(50) + 4)}" text-anchor="end">50</text>${this._axisX(t0, t1, x, base)}</g>
            <line x1="${F.L}" y1="${P(y(floor))}" x2="${F.L + F.pw}" y2="${P(y(floor))}" stroke="var(--red)" stroke-width="1.5" stroke-dasharray="6 4"/>
            <text x="${F.L - 8}" y="${P(y(floor) + 4)}" fill="var(--red)" font-size="12" text-anchor="end">${f0(floor)}</text>
            ${ahead.length ? `<path d="${path(ahead, 'noCar')}" fill="none" stroke="var(--c-house)" stroke-width="2.4" stroke-dasharray="0.1 5" stroke-linecap="round" stroke-linejoin="round"/>
            <path d="${path(ahead, 'soc')}" fill="none" stroke="var(--amber)" stroke-width="2" stroke-dasharray="3 4" stroke-linejoin="round"/>
            <path d="${path(ahead, 'exp')}" fill="none" stroke="var(--c-battery)" stroke-width="2.2" stroke-dasharray="7 4" stroke-linejoin="round"/>` : ''}
            ${actualPts.length > 1 ? `<path d="${path(actualPts, 'v')}" fill="none" stroke="var(--c-battery)" stroke-width="3" stroke-linejoin="round" stroke-linecap="round"/>` : ''}
            ${nowMark}
            ${low ? `<circle cx="${P(x(low.t))}" cy="${P(y(low.soc))}" r="5" fill="var(--sky-a)" stroke="var(--amber)" stroke-width="2"/>` : ''}
          </svg>
        </div>
      </section>`;
    }

    _healthPanel(s) {
      const tiles = [];
      const tile = (st, ic, title, value, detail = '', more = '', extra = '') => tiles.push({ st, html: `<div class="htile st-${st}" data-more="${esc(more)}"><span class="chip">${icon(ic, 20)}</span><span class="t">${esc(title)}</span><span class="s st-${st}">${esc(value)}</span>${detail ? `<span class="d">${detail}</span>` : ''}${extra}</div>` });
      const hb = !s.enabled ? ['grey', 'Off'] : s.healthy ? (s.active ? ['green', 'Live'] : ['amber', 'Shadow']) : ['red', 'Down'];
      tile(hb[0], 'pulse', 'Planner', hb[1], `decided ${esc(ago(ts(s.last_success)))}${s.last_error ? ` · <span class="red-t">${esc(s.last_error)}</span>` : ''}`, this._ent('heartbeat'));
      const site = s.site || {};
      if (site.problem) tile('red', 'alert', 'Inputs', 'Missing', esc(site.problem));
      else if (s.history_error) tile('amber', 'alert', 'Inputs', 'No history', 'House use history not read yet; a flat profile meanwhile');
      else tile('green', 'check', 'Inputs', 'Fresh', 'Solar, grid, battery, charger');
      const age = num(s.pv?.forecast_age_h);
      if (!(s.forecast || []).length) tile('red', 'cloud', 'Forecast', 'Missing', 'No Solcast half hours');
      else tile(age !== null && age > 12 ? 'red' : age !== null && age > 4 ? 'amber' : 'green', 'cloud', 'Forecast', age !== null ? `${f1(age)} h old` : 'Solcast', `correction ×${num(s.pv?.ratio) !== null ? s.pv.ratio.toFixed(2) : '—'}`, this._ent('forecast_correction'));
      const onCar = (s.cars || []).find(c => c.car_id === s.car_on_charger);
      tile(onCar ? 'green' : 'indigo', 'car', 'Charger', onCar ? onCar.name : 'Empty', site.wall_connected ? 'a car is connected' : 'no car connected', this._ent('car_on_charger'));
      const rivians = (s.cars || []).filter(c => c.kind === 'rivian');
      for (const c of rivians) {
        const st = c.control_state_entity ? this._hass?.states[c.control_state_entity] : null;
        if (!st) { tile('grey', 'sync', `${c.name} control`, 'Not found', 'Rivian schedule control', c.control_state_entity); continue; }
        const a = st.attributes || {};
        const [label, cls] = unavailable(st) ? ['Unavailable', 'red'] : (RIVIAN_STATE[st.state] || [cap1(st.state), 'grey']);
        const want = a.desired_charge === true ? `${a.dry_run ? 'would write' : 'holds'} ${f0(a.requested_amps)} A` : a.desired_charge === false ? (a.dry_run ? 'would pause' : 'paused') : 'no request';
        const io = num(a.reads_last_hour) !== null ? ` · ${f0(a.writes_last_hour)} writes/h` : '';
        const fault = a.fault_message || a.last_error ? ` · <span class="red-t">${esc(a.fault_message || a.last_error)}</span>` : '';
        tile(cls, 'sync', `${c.name} control`, label, `${esc(want)}${esc(io)}${fault}`, c.control_state_entity);
      }
      const setup = s.setup || {};
      const ctl = setup.rivian_controllers || [];
      if (setup.legacy_rivian) tile('red', 'sync', 'Rivian control', 'Old version', 'The separate Rivian Schedule Control integration is still installed. Hand the schedule back, remove it and restart; SolarSage will not start its own while it remains.');
      else if (!rivians.length && ctl.length) {
        const bad = ctl.find(e => e.state !== 'loaded');
        tile(bad ? 'red' : 'amber', 'sync', 'Rivian control', bad ? cap1(String(bad.state).replace(/_/g, ' ')) : 'Not in the plan', bad ? `${esc(bad.title)} did not start; open SolarSage under Devices &amp; services` : `${esc(ctl[0].title)} is running; add the car to the planner: SolarSage → Add car`);
      } else if (!rivians.length) tile('grey', 'sync', 'Rivian control', 'Not set up', 'SolarSage → Add entry → Rivian schedule control, then Add car on the site planner');
      if (num(setup.planner_entries) > 1) tile('amber', 'alert', 'Setup', 'Two planners', `${f0(setup.planner_entries)} SolarSage site planner entries exist; this card shows the one with cars. Remove the empty one.`);
      const fwin = (s.free_windows || [])[0];
      if (s.free_hours_problem) tile('amber', 'moon', 'Free hours', 'Not read', esc(s.free_hours_problem), s.free_hours_entity);
      else if (fwin) tile('green', 'moon', 'Free hours', `${hourLabel(ts(fwin.start))}–${hourLabel(ts(fwin.end))}`, 'from the schedule helper', s.free_hours_entity);
      const sp = s.spend;
      if (sp) {
        const pct = sp.cap > 0 ? Math.min(100, sp.spent / sp.cap * 100) : 0;
        const st = sp.level === 'stopped' ? 'red' : sp.level === 'essential_only' ? 'amber' : 'green';
        tile(st, 'dollar', 'Tesla API', `$${Number(sp.spent).toFixed(2)}`, `of $${Number(sp.cap).toFixed(2)} this month`, this._ent('tesla_api_spend'), `<span class="meter"><i style="width:${pct.toFixed(1)}%"></i></span>`);
      } else tile('grey', 'dollar', 'Tesla API', 'No Tesla', 'Add the Tesla after Tesla Fleet');
      const ok = tiles.filter(t => t.st === 'green').length;
      const all = tiles.filter(t => t.st !== 'grey').length;
      const bad = tiles.some(t => t.st === 'red');
      return `<section class="panel health">
        <div class="ptitle">${title('pulse', 'Health')}<span class="score st-${bad ? 'red' : ok === all ? 'green' : 'amber'}">${icon(bad ? 'alert' : 'check', 14)}${ok} of ${all} OK</span></div>
        <div class="hgrid">${tiles.map(t => t.html).join('')}</div>
      </section>`;
    }

    _settingsPanel(s) {
      const st = s.settings || {};
      const caution = String(st.caution || '');
      const levels = s.caution_levels || {};
      const segBtns = CAUTION.map(([key, name]) => `<button type="button" aria-pressed="${key === caution}" data-act="caution:${key}" title="${esc(name)}">${levels[key] ? `${Math.round(levels[key] * 100)}%` : esc(name)}<small>${esc(name)}</small></button>`).join('');
      const stepper = (key, label, value, delta, lo, hi) => `<div class="set"><span data-more="${esc(this._ent(key) || '')}">${label}</span><span class="stepper"><button type="button" aria-label="Lower ${esc(label.toLowerCase())}" data-act="step:${key}:${-delta}" data-range="${lo},${hi}">−</button><b class="num">${f0(value)}%</b><button type="button" aria-label="Raise ${esc(label.toLowerCase())}" data-act="step:${key}:${delta}" data-range="${lo},${hi}">+</button></span></div>`;
      const fw = (s.free_windows || [])[0];
      const freeText = fw ? `${clock(ts(fw.start))} – ${clock(ts(fw.end))}` : s.free_hours_entity ? 'none in the next day' : 'not set';
      const f = this._free;
      const freeBlock = f
        ? `<div class="editor" aria-label="Edit the free hours">
            <div class="times"><span class="cap">Every day from</span><input type="time" data-free="start" value="${esc(f.start)}" ${f.busy ? 'disabled' : ''} aria-label="Free hours start"><span class="cap">to</span><input type="time" data-free="end" value="${esc(f.end)}" ${f.busy ? 'disabled' : ''} aria-label="Free hours end"></div>
            ${f.note ? `<div class="cap amber-t">${esc(f.note)}</div>` : ''}
            ${f.error ? `<div class="cap red-t">${esc(f.error)}</div>` : ''}
            <div class="acts"><button type="button" class="btn" data-act="free-cancel">Cancel</button><button type="button" class="btn go" data-act="free-save" ${f.busy ? 'disabled' : ''}>${f.busy ? 'Saving…' : 'Save'}</button></div>
          </div>`
        : `<div class="set"><span data-more="${esc(s.free_hours_entity || '')}">Free hours</span><span style="display:flex;align-items:center;gap:8px"><span class="num${s.free_hours_problem ? ' amber-t' : ''}">${esc(freeText)}</span>${s.free_hours_entity ? `<button type="button" class="btn icon" data-act="free-edit" aria-label="Edit the free hours" title="Edit the free hours">${icon('edit', 16)}</button>` : ''}</span></div>${s.free_hours_problem ? `<div class="cap amber-t">${esc(s.free_hours_problem)}</div>` : ''}`;
      const nsId = this._ent('night_charging_start');
      const nsOptions = this._hass?.states[nsId]?.attributes?.options || [];
      const nsValue = String(st.night_start || FREE_START);
      const night = nsId ? `<div class="set"><span data-more="${esc(nsId)}">Night charging starts</span><select class="sel" data-select="night_charging_start" aria-label="Night charging start">${nsOptions.map(o => `<option value="${esc(o)}" ${o === nsValue ? 'selected' : ''}>${o === FREE_START ? 'With the free hours' : esc(hhmmLabel(o))}</option>`).join('')}</select></div>` : '';
      const cars = s.cars || [];
      const override = String(st.override || 'auto');
      const ovr = cars.length > 1 ? `<div style="display:flex;flex-direction:column;gap:6px"><span class="cap" data-more="${esc(this._ent('charger_car_override') || '')}">Car on the charger</span><div class="seg" style="--n:${cars.length + 1}"><button type="button" aria-pressed="${override === 'auto'}" data-act="override:auto">Auto</button>${cars.map(c => `<button type="button" aria-pressed="${override === c.car_id}" data-act="override:${esc(c.car_id)}">${esc(c.name)}</button>`).join('')}</div></div>` : '';
      return `<section class="panel settings">
        <div class="ptitle">${title('gear', 'Settings')}</div>
        <div style="display:flex;flex-direction:column;gap:6px"><span class="cap" data-more="${esc(this._ent('caution') || '')}">Caution: how sure the battery must stay above the floor</span><div class="seg" role="group" aria-label="Caution">${segBtns}</div></div>
        ${stepper('battery_floor', 'Battery floor', st.floor_pct, 5, 5, 95)}
        ${stepper('battery_wanted_at_free_hours', 'Battery at free start', st.wanted_at_free_start_pct, 5, 0, 100)}
        <div class="sep"></div>
        ${freeBlock}
        ${night}
        <div class="set"><span data-more="${esc(this._ent('minimum_time_between_changes') || '')}">Changes</span><span class="num">every ${f0(st.min_change_min)} min · up to ${f0(st.max_step_a)} A</span></div>
        ${ovr}
      </section>`;
    }
  }

  customElements.define(TAG, SolarEvPlannerCard);
  window.customCards = window.customCards || [];
  window.customCards.push({ type: TAG, name: 'SolarSage', description: 'The SolarSage\'s cockpit: what it plans, why, and its settings.', preview: false });
  console.info(`%c SOLAR-EV-PLANNER-CARD %c ${VERSION} `, 'background:#F6A04D;color:#07162B;font-weight:600', 'background:#07162B;color:#F2F7FC');
})();
