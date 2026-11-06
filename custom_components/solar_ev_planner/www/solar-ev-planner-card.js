/*
 * Solar EV planner card: the planner's cockpit, in the Daylight look.
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
 * number), so permissions and the logbook work as usual.
 *
 * Mouse: click acts, right-click opens more-info. Touch: press and hold opens more-info.
 */
(() => {
  const TAG = 'solar-ev-planner-card';
  const VERSION = '0.1.0';
  if (customElements.get(TAG)) return;

  const HISTORY_EVERY = 5 * 60 * 1000;
  const BUCKET = 5 * 60 * 1000;
  const SLOT = 30 * 60 * 1000;
  const STEP = 15 * 60 * 1000;
  const STALE_AFTER = 3 * 60 * 1000;

  const MODE = {
    off: ['Planner off', 'off'], empty: ['Charger empty', 'idle'], stale: ['Car data stale', 'warn'],
    full: ['Car at its limit', 'idle'], day: ['Solar charging', 'good'], night: ['Free hours', 'night'],
    no_data: ['Cannot decide', 'bad'],
  };
  const CAUTION = [
    ['near_certain', 'Near-certain'], ['conservative', 'Conservative'],
    ['balanced', 'Balanced'], ['typical_day', 'Typical day'],
  ];
  const RIVIAN_STATE = {
    idle: 'Idle', waiting_for_vehicle: 'Waiting for the car', handed_back: 'Handed back',
    lease_active: 'In control', lease_lapsed: 'Lease lapsed', paused: 'Paused', writing: 'Writing',
    fault: 'Fault', dry_run: 'Dry run',
  };
  const PLAN_DAYS = { off: 'No plan', once: 'once', weekdays: 'weekdays', daily: 'every day', weekends: 'weekends' };

  const esc = v => String(v ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const num = v => (v === null || v === undefined || v === '' || Number.isNaN(Number(v)) ? null : Number(v));
  const f1 = v => (num(v) === null ? '—' : (Math.abs(v) < 0.05 ? 0 : v).toFixed(1));
  const f0 = v => (num(v) === null ? '—' : String(Math.round(v)));
  const ts = v => (v ? new Date(v).getTime() : null);
  const clock = t => new Date(t).toLocaleTimeString('en-US', { hour: 'numeric', minute: '2-digit' });
  const hourLabel = t => { const d = new Date(t); const h = d.getHours(); const m = d.getMinutes(); if (m) return clock(t); if (h === 0) return '12 AM'; if (h === 12) return '12 PM'; return h > 12 ? `${h - 12} PM` : `${h} AM`; };
  const shortHour = t => { const h = new Date(t).getHours(); return h === 0 ? '12' : String(h > 12 ? h - 12 : h); };
  const ago = t => { if (!t) return 'never'; const m = Math.round((Date.now() - t) / 60000); if (m < 1) return 'just now'; if (m < 60) return `${m} min ago`; const h = Math.round(m / 60); return `${h} h ago`; };
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
    plug: '<path d="M9 3v5M15 3v5M6 8h12v3a6 6 0 0 1-12 0zM12 17v4"/>',
  };
  const icon = (name, size = 16) => `<svg class="ic" width="${size}" height="${size}" viewBox="0 0 24 24" aria-hidden="true">${ICON[name] || ''}</svg>`;

  const FONT_CSS = '@font-face { font-family: "Jost"; font-style: normal; font-weight: 100 900; font-display: swap; src: url(/local/daylight-tablet/jost.woff2) format("woff2"), url(https://fonts.gstatic.com/s/jost/v20/92zatBhPNqw73oTd4jQmfxI.woff2) format("woff2"); }';

  const CSS = `:host {
  --sky-a: #07162B; --sky-b: #0D2A4A; --sky-c: #123F63; --glow: rgba(96,170,230,0.22);
  --ink: #F2F7FC; --ink-2: rgba(242,247,252,0.86); --ink-3: rgba(242,247,252,0.70); --hair: rgba(242,247,252,0.18); --hair-2: rgba(242,247,252,0.10);
  --amber: #F6A04D; --amber-2: rgba(246,160,77,0.20); --amber-band: rgba(246,160,77,0.22); --cyan: #7FD1F7; --green: #5DD39E; --red: #FF7A6B; --indigo: #9BB1FF; --indigo-band: rgba(155,177,255,0.12);
  --tesla: #E8384F; --rivian: #F7CF4A;
  --surface: rgba(255,255,255,0.07); --surface-2: rgba(255,255,255,0.13); --sheet: rgba(9,26,50,0.97);
  --font: "Jost", "Avenir Next", "Futura", "Century Gothic", system-ui, sans-serif;
  display: block; color: var(--ink); font-family: var(--font); font-size: 16px; line-height: 1.35; -webkit-font-smoothing: antialiased; color-scheme: dark; }
:host([data-theme="light"]) {
  --sky-a: #3B8ED6; --sky-b: #2468AE; --sky-c: #143F78; --glow: rgba(255,255,255,0.16);
  --ink: #FFFFFF; --ink-2: rgba(255,255,255,0.90); --ink-3: rgba(255,255,255,0.78); --hair: rgba(255,255,255,0.26); --hair-2: rgba(255,255,255,0.14);
  --amber: #FFB357; --amber-2: rgba(255,179,87,0.24); --amber-band: rgba(255,179,87,0.30); --cyan: #BFEAFF; --green: #8BEFC0; --red: #FF9A8C; --indigo: #D3DBFF; --indigo-band: rgba(211,219,255,0.16);
  --tesla: #FF4D63; --rivian: #FFD95C;
  --surface: rgba(255,255,255,0.14); --surface-2: rgba(255,255,255,0.24); --sheet: rgba(22,66,124,0.98); }
*, *::before, *::after { box-sizing: border-box; }
button { font: inherit; color: inherit; background: none; border: 0; padding: 0; margin: 0; cursor: pointer; text-align: inherit; -webkit-tap-highlight-color: transparent; touch-action: manipulation; }
button:focus-visible { outline: 2px solid var(--amber); outline-offset: 3px; border-radius: 10px; }
button:disabled { cursor: default; opacity: 0.5; }
svg.ic { fill: none; stroke: currentColor; stroke-width: 2.1; stroke-linecap: round; stroke-linejoin: round; flex: none; display: block; }
.num { font-variant-numeric: tabular-nums; font-feature-settings: "tnum"; }
.app { position: relative; container: cockpit / inline-size; padding: 26px 30px 30px; display: flex; flex-direction: column; gap: 18px; overflow: hidden;
  background: radial-gradient(900px 560px at 14% -10%, var(--glow), transparent 60%), radial-gradient(1800px 1100px at 50% 40%, var(--sky-b), var(--sky-a) 85%); }
:host([data-theme="light"]) .app { background: radial-gradient(900px 560px at 14% -10%, var(--glow), transparent 60%), linear-gradient(180deg, var(--sky-a), var(--sky-b) 58%, var(--sky-c)); }
:host([data-fill]) .app { min-height: calc(100vh - var(--header-height, 56px)); }
.lbl { font-size: 13px; font-weight: 600; letter-spacing: 0.14em; text-transform: uppercase; color: var(--ink-3); }
.panel { display: flex; flex-direction: column; gap: 12px; padding: 20px; border-radius: 22px; background: var(--surface); border: 1px solid var(--hair-2); min-width: 0; }
.ptitle { display: flex; justify-content: space-between; align-items: baseline; gap: 12px; flex-wrap: wrap; }
.ptitle h2 { margin: 0; font-size: 18px; font-weight: 500; }
.ptitle .r { font-size: 14px; color: var(--ink-2); }
.ptitle .r b { font-weight: 500; color: var(--ink); }
.muted { color: var(--ink-3); }

.top { display: flex; align-items: center; gap: 14px; flex-wrap: wrap; }
.title h1 { margin: 0; font-size: 30px; font-weight: 500; letter-spacing: 0.2px; line-height: 1.1; }
.title .sub { font-size: 14px; color: var(--ink-3); }
.title { margin-right: 6px; }
.pill { display: inline-flex; align-items: center; gap: 8px; padding: 8px 14px; border-radius: 999px; font-size: 15px; font-weight: 500; background: var(--surface); color: var(--ink-2); }
.pill.good { background: color-mix(in srgb, var(--green) 16%, transparent); color: var(--green); }
.pill.night { background: color-mix(in srgb, var(--indigo) 18%, transparent); color: var(--indigo); }
.pill.warn { background: var(--amber-2); color: var(--amber); }
.pill.bad { background: color-mix(in srgb, var(--red) 18%, transparent); color: var(--red); }
.pill.live { background: color-mix(in srgb, var(--cyan) 16%, transparent); color: var(--cyan); }
.grow { flex: 1 1 auto; }
.toggles { display: flex; gap: 10px; flex-wrap: wrap; }
.tog { display: flex; align-items: center; gap: 10px; height: 44px; padding: 0 14px 0 16px; border-radius: 14px; border: 1px solid var(--hair); background: var(--surface); font-size: 15px; }
.tog.shadow[aria-pressed="true"] { border-color: color-mix(in srgb, var(--amber) 45%, transparent); background: color-mix(in srgb, var(--amber) 12%, transparent); }
.sw { width: 36px; height: 22px; border-radius: 999px; background: var(--hair); position: relative; flex: none; transition: background 0.2s; }
.sw::after { content: ""; position: absolute; left: 3px; top: 3px; width: 16px; height: 16px; border-radius: 50%; background: var(--ink); transition: transform 0.2s; }
[aria-pressed="true"] .sw { background: var(--green); }
[aria-pressed="true"] .sw::after { transform: translateX(14px); background: var(--sky-a); }
.tog.shadow[aria-pressed="true"] .sw { background: var(--amber); }

.row1 { display: grid; grid-template-columns: minmax(300px, 360px) minmax(0, 1fr) minmax(220px, 260px); gap: 18px; }
.row2 { display: grid; grid-template-columns: minmax(0, 1.25fr) minmax(0, 1fr) minmax(0, 1fr); gap: 18px; }
@container cockpit (max-width: 1180px) {
  .row1 { grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); }
  .row1 .today { grid-column: 1 / -1; order: 3; }
  .row2 { grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); }
  .row2 .battery { grid-column: 1 / -1; }
}
@container cockpit (max-width: 720px) {
  .row1, .row2 { grid-template-columns: minmax(0, 1fr); }
  .app { padding: 18px 16px 22px; }
  .toggles { width: 100%; }
  .tog { flex: 1 1 0; justify-content: space-between; padding: 0 12px; }
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
.tile.plan { background: color-mix(in srgb, var(--amber) 12%, transparent); border-color: color-mix(in srgb, var(--amber) 35%, transparent); }
.tile.plan.live { background: color-mix(in srgb, var(--cyan) 12%, transparent); border-color: color-mix(in srgb, var(--cyan) 35%, transparent); }
.tile .k { font-size: 13px; color: var(--ink-2); }
.tile .v { font-size: 40px; font-weight: 500; line-height: 1.1; }
.tile .v small { font-size: 20px; }
.tile .u { font-size: 13px; color: var(--ink-3); }
.reason { font-size: 16px; line-height: 1.45; color: var(--ink-2); margin: 0; }
.note { font-size: 14px; line-height: 1.4; padding: 8px 12px; border-radius: 12px; background: color-mix(in srgb, var(--amber) 14%, transparent); color: var(--ink); }
.note.bad { background: color-mix(in srgb, var(--red) 18%, transparent); }
.kv { display: flex; flex-direction: column; gap: 8px; padding-top: 12px; border-top: 1px solid var(--hair-2); font-size: 15px; }
.kv > div { display: flex; justify-content: space-between; gap: 12px; }
.kv > div > span:first-child { color: var(--ink-3); flex: none; }
.kv > div > span:last-child { text-align: right; }
.good-t { color: var(--green); } .warn-t { color: var(--amber); } .bad-t { color: var(--red); } .night-t { color: var(--indigo); }
.others { display: flex; flex-direction: column; gap: 6px; padding-top: 12px; border-top: 1px solid var(--hair-2); }
.other { display: grid; grid-template-columns: auto auto 1fr; gap: 6px 10px; align-items: center; font-size: 15px; }
.other .o { grid-column: 3; text-align: right; color: var(--ink-3); font-size: 14px; }

.legend { display: flex; gap: 14px; font-size: 13px; color: var(--ink-2); flex-wrap: wrap; }
.legend span { display: inline-flex; align-items: center; gap: 6px; }
.legend i { display: inline-block; width: 14px; height: 10px; border-radius: 3px; }
.legend i.line { height: 3px; border-radius: 2px; }
.chart { position: relative; }
.chart svg { display: block; width: 100%; height: auto; overflow: visible; }
.chart text { font-family: var(--font); }
.hover { position: absolute; top: 0; pointer-events: none; padding: 8px 10px; border-radius: 12px; background: var(--sheet); border: 1px solid var(--hair); font-size: 13px; line-height: 1.45; white-space: nowrap; box-shadow: 0 8px 24px rgba(0,0,0,0.3); z-index: 2; }
.hover b { font-weight: 500; }
.hline { position: absolute; top: 0; bottom: 0; width: 1px; background: var(--ink-2); pointer-events: none; }
.stats { display: flex; gap: 8px 22px; font-size: 14px; color: var(--ink-2); flex-wrap: wrap; }
.stats b { font-weight: 500; color: var(--ink); }

.site .r { display: flex; justify-content: space-between; align-items: baseline; padding: 8px 0; border-bottom: 1px solid var(--hair-2); cursor: default; }
.site .r:last-of-type { border-bottom: 0; }
.site .r .v { font-size: 24px; }
.site .r .v small { font-size: 14px; color: var(--ink-3); }
.foot { margin-top: auto; font-size: 13px; line-height: 1.4; color: var(--ink-3); }

.seg { display: grid; grid-template-columns: repeat(var(--n, 4), minmax(0, 1fr)); gap: 4px; padding: 4px; border-radius: 12px; background: var(--surface); }
.seg button { min-height: 40px; border-radius: 9px; color: var(--ink-2); font-size: 13px; text-align: center; line-height: 1.15; padding: 4px 2px; }
.seg button small { display: block; font-size: 11px; color: var(--ink-3); }
.seg button[aria-pressed="true"] { background: var(--surface-2); color: var(--ink); font-weight: 500; }
.set { display: flex; justify-content: space-between; align-items: center; gap: 10px; font-size: 15px; }
.set > span:first-child { color: var(--ink-2); }
.stepper { display: flex; align-items: center; gap: 6px; }
.stepper button { width: 36px; height: 36px; border-radius: 10px; border: 1px solid var(--hair); text-align: center; font-size: 18px; }
.stepper b { font-weight: 500; min-width: 48px; text-align: center; }
.cap { font-size: 13px; color: var(--ink-3); }

.hbox { display: flex; flex-direction: column; gap: 4px; padding: 12px; border-radius: 14px; background: var(--surface); }
.hbox .h { display: flex; justify-content: space-between; gap: 10px; font-size: 15px; }
.hbox .d { font-size: 13px; color: var(--ink-3); }
.meter { height: 6px; border-radius: 999px; background: var(--hair-2); overflow: hidden; margin-top: 4px; }
.meter > i { display: block; height: 6px; border-radius: 999px; background: var(--tesla); }

.empty { padding: 40px 24px; text-align: center; color: var(--ink-2); font-size: 17px; }
.empty b { display: block; font-size: 22px; font-weight: 500; color: var(--ink); margin-bottom: 6px; }

.scrim { position: fixed; inset: 0; background: rgba(0,0,0,0.45); display: flex; align-items: center; justify-content: center; z-index: 10; padding: 16px; }
.dialog { max-width: 440px; width: 100%; padding: 22px; border-radius: 22px; background: var(--sheet); border: 1px solid var(--hair); display: flex; flex-direction: column; gap: 12px; }
.dialog h3 { margin: 0; font-size: 20px; font-weight: 500; }
.dialog p { margin: 0; color: var(--ink-2); line-height: 1.45; }
.dialog .btns { display: flex; justify-content: flex-end; gap: 10px; margin-top: 6px; }
.dialog .btns button { height: 44px; padding: 0 18px; border-radius: 12px; border: 1px solid var(--hair); }
.dialog .btns button.go { background: var(--amber); color: #1A0E02; border-color: var(--amber); font-weight: 500; }
@media (prefers-reduced-motion: reduce) { .sw, .sw::after { transition: none; } }
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
  const energy = (pts, t0, t1) => { let e = 0; for (let i = 0; i < pts.length; i++) { const a = Math.max(pts[i].t, t0); const b = Math.min(i + 1 < pts.length ? pts[i + 1].t : t1, t1); if (b > a) e += Math.max(0, pts[i].v) * (b - a) / 3600000; } return e; };

  const niceMax = v => { const steps = [2, 4, 6, 8, 10, 12, 16, 20, 24, 30, 40]; return steps.find(s => s >= v) || Math.ceil(v / 10) * 10; };

  // ---------------------------------------------------------------- the card

  class SolarEvPlannerCard extends HTMLElement {
    constructor() {
      super();
      this.attachShadow({ mode: 'open' });
      this._snap = null; this._hist = null; this._histAt = 0; this._error = null; this._confirm = null; this._unsub = null;
      this._bound = false;
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
    }

    disconnectedCallback() {
      clearInterval(this._tick);
      clearTimeout(this._retry);
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

    /** Entities read straight from hass (Rivian schedule control's state): re-render when they change. */
    _watchSig() {
      if (!this._snap || !this._hass) return '';
      return (this._snap.cars || []).map(c => c.control_state_entity).filter(Boolean).map(id => this._hass.states[id]?.last_updated || '').join('|');
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
      const ids = [src.pv?.entity_id, src.wall?.entity_id, ...Object.values(planned)].filter(Boolean);
      if (!ids.length) return;
      this._histBusy = true;
      try {
        const start = midnight();
        const res = await this._hass.callWS({
          type: 'history/history_during_period', start_time: new Date(start).toISOString(), end_time: new Date().toISOString(),
          entity_ids: ids, minimal_response: true, no_attributes: true, significant_changes_only: false,
        });
        const plannedPts = {};
        for (const [carId, id] of Object.entries(planned)) plannedPts[carId] = toPoints(res[id], 1);
        this._hist = {
          pv: src.pv ? toPoints(res[src.pv.entity_id], src.pv.scale) : [],
          wall: src.wall ? toPoints(res[src.wall.entity_id], src.wall.scale) : [],
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

    _call(domain, service, data) {
      return this._hass.callService(domain, service, data).catch(err => {
        this._toast = `Could not change it: ${err?.message || err}`; this._render();
        setTimeout(() => { this._toast = null; this._render(); }, 6000);
      });
    }

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

    _bind() {
      if (this._bound) return; this._bound = true;
      const root = this.shadowRoot;
      root.addEventListener('click', ev => {
        const el = ev.target.closest('[data-act]'); if (!el) return;
        const [kind, a, b] = el.getAttribute('data-act').split(':');
        if (kind === 'toggle') this._toggle(a);
        else if (kind === 'caution') this._select('caution', a);
        else if (kind === 'override') this._select('charger_car_override', a);
        else if (kind === 'step') { const [lo, hi] = el.getAttribute('data-range').split(',').map(Number); this._step(a, Number(b), lo, hi); }
        else if (kind === 'more') this._more(el.getAttribute('data-id'));
        else if (kind === 'cancel') { this._confirm = null; this._render(); }
        else if (kind === 'go') { const c = this._confirm; this._confirm = null; this._render(); c?.run(); }
      });
      root.addEventListener('contextmenu', ev => { const el = ev.target.closest('[data-more]'); if (!el) return; ev.preventDefault(); this._more(el.getAttribute('data-more')); });
      let hold = null;
      root.addEventListener('pointerdown', ev => { if (ev.pointerType !== 'touch') return; const el = ev.target.closest('[data-more]'); if (!el) return; hold = setTimeout(() => this._more(el.getAttribute('data-more')), 550); });
      ['pointerup', 'pointercancel', 'pointerleave'].forEach(n => root.addEventListener(n, () => clearTimeout(hold)));
      root.addEventListener('pointermove', ev => { if (ev.pointerType === 'touch') clearTimeout(hold); this._hover(ev); });
      root.addEventListener('pointerout', ev => { const c = ev.target.closest?.('.chart'); if (c && !c.contains(ev.relatedTarget)) this._unhover(c); });
      root.addEventListener('keydown', ev => { if (ev.key === 'Escape' && this._confirm) { this._confirm = null; this._render(); } });
    }

    _hover(ev) {
      const chart = ev.target.closest?.('.chart[data-chart]'); if (!chart) return;
      const model = this._charts?.[chart.getAttribute('data-chart')]; if (!model) return;
      const svg = chart.querySelector('svg'); const r = svg.getBoundingClientRect();
      const vx = model.vb[0] + (ev.clientX - r.left) / r.width * model.vb[2];
      if (vx < model.x0 || vx > model.x1) { this._unhover(chart); return; }
      const t = model.t0 + (vx - model.x0) / (model.x1 - model.x0) * (model.t1 - model.t0);
      const html = model.read(t); if (!html) { this._unhover(chart); return; }
      let line = chart.querySelector('.hline'); let tip = chart.querySelector('.hover');
      if (!line) { line = document.createElement('div'); line.className = 'hline'; chart.appendChild(line); tip = document.createElement('div'); tip.className = 'hover'; chart.appendChild(tip); }
      const px = ev.clientX - r.left;
      line.style.left = `${px}px`;
      tip.innerHTML = html;
      const w = tip.offsetWidth; tip.style.left = `${px + 12 + w > r.width ? px - 12 - w : px + 12}px`;
    }

    _unhover(chart) { chart.querySelectorAll('.hline, .hover').forEach(n => n.remove()); }

    // ---------------------------------------------------------------- render

    _render() {
      if (!this.shadowRoot || !this._cfg) return;
      this._bind();
      this.setAttribute('data-theme', this._theme());
      const s = this._snap;
      let body;
      if (!s) {
        body = this._error === 'not_loaded'
          ? '<div class="empty"><b>The Solar EV planner is not running</b>Add or enable it under Settings → Devices &amp; services, then this page fills in.</div>'
          : this._error ? `<div class="empty"><b>Cannot reach the planner</b>${esc(this._error)}. Trying again every 30 seconds.</div>`
            : '<div class="empty"><b>Connecting to the planner…</b></div>';
      } else {
        this._charts = {};
        body = `${this._header(s)}
          <div class="row1">${this._carPanel(s)}${this._todayPanel(s)}${this._sitePanel(s)}</div>
          <div class="row2">${this._batteryPanel(s)}${this._settingsPanel(s)}${this._healthPanel(s)}</div>`;
      }
      const toast = this._toast ? `<div class="note bad" role="status">${esc(this._toast)}</div>` : '';
      const dlg = this._confirm ? `<div class="scrim"><div class="dialog" role="dialog" aria-modal="true" aria-labelledby="dlg-t"><h3 id="dlg-t">${esc(this._confirm.title)}</h3><p>${esc(this._confirm.body)}</p><div class="btns"><button type="button" data-act="cancel">Cancel</button><button type="button" class="go" data-act="go">${esc(this._confirm.go)}</button></div></div></div>` : '';
      this.shadowRoot.innerHTML = `<style>${CSS}</style><div class="app">${toast}${body}${dlg}</div>`;
      if (this._confirm) this.shadowRoot.querySelector('.dialog .go')?.focus();
    }

    _header(s) {
      const now = Date.now();
      const date = new Date(now).toLocaleDateString('en-US', { weekday: 'short', day: 'numeric', month: 'short' });
      const [modeText, modeKind] = MODE[s.mode] || [cap1(s.mode || 'unknown'), 'idle'];
      const modeIcon = s.mode === 'night' ? 'moon' : s.mode === 'day' ? 'sun' : s.mode === 'no_data' || s.mode === 'stale' ? 'alert' : 'pause';
      let state;
      if (!s.enabled) state = `<span class="pill bad">${icon('pause')}Planner off: nothing planned or sent</span>`;
      else if (s.shadow) state = `<span class="pill warn">${icon('eye')}Shadow mode: planning only, nothing sent</span>`;
      else state = `<span class="pill live">${icon('bolt')}Live: commands go to the car</span>`;
      const tog = (key, label, on, cls = '') => `<button type="button" class="tog ${cls}" aria-pressed="${on ? 'true' : 'false'}" data-act="toggle:${key}" data-more="${esc(this._ent(key) || '')}">${label}<span class="sw" aria-hidden="true"></span></button>`;
      return `<div class="top">
        <div class="title"><h1>Solar EV</h1><div class="sub num">${esc(date)} · ${esc(clock(now))} · planned ${esc(ago(ts(s.planned_at)))}</div></div>
        <span class="pill ${modeKind}" data-more="${esc(this._ent('mode') || '')}">${icon(modeIcon)}${esc(modeText)}</span>
        ${state}
        <div class="grow"></div>
        <div class="toggles">${tog('planning', 'Planning', s.enabled)}${tog('shadow_mode', 'Shadow', s.shadow, 'shadow')}${tog('daytime_solar_charging', 'Daytime solar', !!s.settings?.daytime_solar)}</div>
      </div>`;
    }

    _carColor(c) { return c?.kind === 'tesla' ? 'var(--tesla)' : c?.kind === 'rivian' ? 'var(--rivian)' : 'var(--cyan)'; }

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
      const [h, m] = String(st.plan_time || '07:00').split(':').map(Number);
      const d = new Date(); d.setHours(h, m || 0, 0, 0);
      return `${f0(st.plan_target_pct)}% by ${clock(d.getTime())} ${PLAN_DAYS[st.plan_days] || st.plan_days}`;
    }

    _carPanel(s) {
      const c = this._focusCar(s);
      if (!c) return '<section class="panel"><div class="lbl">On the charger</div><p class="reason">No cars are set up. Add one under the planner\'s integration entry.</p></section>';
      const color = this._carColor(c);
      const site = s.site || {};
      const label = c.on_charger ? 'On the charger' : s.car_on_charger ? 'Plugged in elsewhere' : c.car_id === s.tonight_car ? 'Tonight\'s car' : 'Nobody on the charger';
      const soc = num(c.soc); const lim = num(c.limit);
      const kw = a => (num(a) === null ? '—' : f1(a * s.volts / 1000));
      const planned = c.on_charger ? c.planned_a : null;
      const wallA = num(site.wall_current);
      let planK = s.shadow ? 'Planner would send' : 'Planner sends';
      if (!s.enabled) planK = 'Planner is off';
      let wallSub = `${f1(site.wall_kw)} kW`;
      if (c.on_charger && s.shadow && wallA !== null && planned !== null) wallSub += Math.abs(wallA - planned) >= 2 ? ' · car\'s own schedule' : ' · same as the plan';
      const plannedSub = planned === null ? 'not on the charger' : planned === 0 ? 'paused' : `${kw(planned)} kW`;
      const plannedId = this._ent('planned_current', c.car_id);
      const nxt = s.next_change;
      const outlook = c.outlook;
      const oCls = outlook ? (outlook.short_kwh > 0.05 ? 'bad-t' : outlook.paid_kwh > 0.05 ? 'warn-t' : outlook.free_kwh > 0.05 && outlook.solar_kwh < 0.05 ? 'night-t' : 'good-t') : '';
      const notes = [];
      if (c.waiting) notes.push(`<div class="note">${esc(c.waiting)}</div>`);
      if (c.last_error) notes.push(`<div class="note bad">${esc(c.last_error)}</div>`);
      if (s.safety) notes.push(`<div class="note">Safety rule acting: ${esc(s.safety)}</div>`);
      const others = (s.cars || []).filter(o => o.car_id !== c.car_id).map(o => {
        const where = o.on_charger ? 'on the charger' : o.plugged ? 'plugged in' : o.home === false ? 'away' : 'home';
        return `<div class="other" data-more="${esc(this._ent('outlook', o.car_id) || '')}"><span class="dot" style="--dc:${this._carColor(o)}"></span><span>${esc(o.name)} <span class="muted num">${num(o.soc) !== null ? `${f0(o.soc)}%` : ''} · ${esc(where)}</span></span><span class="o">${esc(o.outlook?.summary || this._planText(o))}</span></div>`;
      }).join('');
      return `<section class="panel car" aria-label="${esc(label)}">
        <div class="car-h"><span class="dot" style="--dc:${color}"></span><span class="lbl">${esc(label)}</span></div>
        <div class="car-name"><div class="n">${esc(c.name)}</div><div class="s num">${soc !== null ? `${f0(soc)}%` : '—'} <span class="muted">→ limit ${lim !== null ? `${f0(lim)}%` : '—'}</span></div></div>
        <div class="bar" style="--dc:${color}" role="img" aria-label="Charge ${soc !== null ? f0(soc) : 'unknown'} percent, limit ${lim !== null ? f0(lim) : 'unknown'} percent"><i style="width:${Math.max(0, Math.min(100, soc || 0))}%"></i>${lim !== null ? `<b style="left:${Math.min(99.5, lim)}%"></b>` : ''}</div>
        <div class="tiles">
          <div class="tile plan ${s.shadow ? '' : 'live'}" data-more="${esc(plannedId || '')}"><div class="k">${esc(planK)}</div><div class="v num">${planned === null ? '—' : f0(planned)} <small>A</small></div><div class="u">${esc(plannedSub)}</div></div>
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

    _todayPanel(s) {
      const fc = (s.forecast || []).map(f => ({ t: ts(f.t), p10: f.p10, p50: f.p50, p90: f.p90 }));
      const now = Date.now();
      const today0 = midnight(now);
      // the day to show: today until its sun is down, then tomorrow
      const sunny = d0 => fc.filter(f => f.t >= d0 && f.t < d0 + 86400000 && f.p90 > 0.05);
      let day0 = today0; let lit = sunny(today0);
      if (lit.length && now > lit[lit.length - 1].t + SLOT) { const tm = sunny(today0 + 86400000); if (tm.length) { day0 = today0 + 86400000; lit = tm; } }
      const tomorrow = day0 !== today0;
      let t0 = lit.length ? lit[0].t : day0 + 6 * 3600000; let t1 = lit.length ? lit[lit.length - 1].t + SLOT : day0 + 20 * 3600000;
      t0 = Math.floor(t0 / 3600000) * 3600000; t1 = Math.ceil(t1 / 3600000) * 3600000;
      const volts = s.volts || 240;
      const h = this._hist || { pv: [], wall: [], planned: {} };
      const pvB = tomorrow ? [] : bucketMeans(h.pv, t0, Math.min(now, t1));
      const wallB = tomorrow ? [] : bucketMeans(h.wall, t0, Math.min(now, t1));
      // planned car power: the recorded plan until now, the trajectory after
      const plannedCars = Object.values(h.planned || {});
      const plannedAt = t => plannedCars.reduce((a, pts) => a + Math.max(0, holdAt(pts, t) || 0), 0) * volts / 1000;
      const plan = [];
      if (!tomorrow) for (let t = t0; t < Math.min(now, t1); t += BUCKET) plan.push({ a: t, b: Math.min(t + BUCKET, now, t1), v: plannedAt(t) });
      for (const p of s.trajectory || []) { const b = ts(p.t); const a = b - STEP; if (b <= Math.max(now, t0) || a >= t1) continue; plan.push({ a: Math.max(a, now, t0), b: Math.min(b, t1), v: p.car_kw || 0 }); }
      const yMax = niceMax(Math.max(2, ...fc.filter(f => f.t >= t0 && f.t < t1).map(f => f.p90), ...pvB.map(p => p.v), ...wallB.map(p => p.v), ...plan.map(p => p.v)) * 1.05);
      const W = 680; const H = 220; const vb = [-40, -12, 730, 262];
      const x = t => (t - t0) / (t1 - t0) * W; const y = v => H - Math.max(0, Math.min(yMax, v)) / yMax * H;
      const P = n => n.toFixed(1);
      const fcIn = fc.filter(f => f.t + SLOT > t0 && f.t < t1).map(f => ({ ...f, m: f.t + SLOT / 2 }));
      const band = fcIn.length ? `M${fcIn.map(f => `${P(x(f.m))},${P(y(f.p90))}`).join(' L')} L${[...fcIn].reverse().map(f => `${P(x(f.m))},${P(y(f.p10))}`).join(' L')} Z` : '';
      const p50 = fcIn.length ? `M${fcIn.map(f => `${P(x(f.m))},${P(y(f.p50))}`).join(' L')}` : '';
      const line = pts => (pts.length > 1 ? `M${pts.map(p => `${P(x(p.t))},${P(y(p.v))}`).join(' L')}` : '');
      let planPath = '';
      if (plan.length) {
        const parts = [`M${P(x(plan[0].a))},${H}`];
        for (const p of plan) parts.push(`L${P(x(p.a))},${P(y(p.v))} L${P(x(p.b))},${P(y(p.v))}`);
        parts.push(`L${P(x(plan[plan.length - 1].b))},${H} Z`);
        planPath = parts.join(' ');
      }
      const grid = [0.25, 0.5, 0.75, 1].map(f => `<line x1="0" y1="${P(H - f * H)}" x2="${W}" y2="${P(H - f * H)}"/>`).join('');
      const yl = [1, 0.5].map(f => `<text x="-8" y="${P(H - f * H + 4)}" text-anchor="end">${f === 1 ? `${yMax} kW` : yMax / 2}</text>`).join('');
      const span = (t1 - t0) / 3600000; const every = span > 12 ? 3 : 2;
      const xl = []; for (let t = t0; t <= t1; t += 3600000) { const hr = new Date(t).getHours(); if (t !== t0 && t !== t1 && hr % every) continue; if (t !== t0 && t !== t1 && (t - t0 < 1.5 * 3600000 || t1 - t < 1.5 * 3600000)) continue; xl.push(`<text x="${P(x(t))}" y="${H + 20}" text-anchor="${t === t0 ? 'start' : t === t1 ? 'end' : 'middle'}">${t === t0 || t === t1 ? hourLabel(t) : shortHour(t) + (hr === 12 ? ' PM' : '')}</text>`); }
      const nowMark = !tomorrow && now > t0 && now < t1 ? `<line x1="${P(x(now))}" y1="-6" x2="${P(x(now))}" y2="${H + 4}" stroke="var(--ink)" stroke-width="1.5" stroke-dasharray="2 4"/><text x="${P(x(now) + 6)}" y="4" fill="var(--ink)" font-size="12">now</text>` : '';
      const nx = s.next_change; const nxT = ts(nx?.at);
      let nextMark = '';
      if (nx && nxT > t0 && nxT < t1) {
        const cx = x(nxT); const cy = y(nx.amps * volts / 1000); const right = cx > W - 110;
        nextMark = `<circle cx="${P(cx)}" cy="${P(cy)}" r="5" fill="var(--sky-a)" stroke="var(--rivian)" stroke-width="2"/><text x="${P(right ? cx - 9 : cx + 9)}" y="${P(Math.max(12, cy - 10))}" text-anchor="${right ? 'end' : 'start'}" fill="var(--ink-2)" font-size="12">next ${esc(clock(nxT))} → ${f0(nx.amps)} A</text>`;
      }
      const carColor = this._carColor(this._focusCar(s));
      this._charts.today = {
        vb, x0: 0, x1: W, t0, t1,
        read: t => {
          const f = fcIn.find(r => t >= r.t && t < r.t + SLOT);
          const near = (pts, w = BUCKET) => pts.find(p => Math.abs(p.t - t) <= w / 2);
          const pv = near(pvB); const wl = near(wallB);
          const pl = plan.find(p => t >= p.a && t < p.b);
          const rows = [`<b>${esc(clock(t))}</b>`];
          if (f) rows.push(`Forecast ${f1(f.p50)} kW <span class="muted">(${f1(f.p10)}–${f1(f.p90)})</span>`);
          if (pv) rows.push(`Solar ${f1(pv.v)} kW`);
          if (pl) rows.push(`Planned car ${f1(pl.v)} kW <span class="muted">${f0(pl.v * 1000 / volts)} A</span>`);
          if (wl) rows.push(`Car drew ${f1(wl.v)} kW`);
          return rows.length > 1 ? rows.join('<br>') : '';
        },
      };
      const pv = s.pv || {};
      const carToday = tomorrow ? null : energy(h.wall, today0, now);
      let changes = 0;
      for (const pts of plannedCars) for (let i = 1; i < pts.length; i++) if (pts[i].t >= today0 && pts[i].v !== pts[i - 1].v) changes++;
      const range = num(pv.remaining_p10_kwh) !== null ? ` <span class="muted">(${f0(pv.remaining_p10_kwh)}–${f0(pv.remaining_p90_kwh)})</span>` : '';
      return `<section class="panel today">
        <div class="ptitle"><h2>${tomorrow ? 'Tomorrow' : 'Today'}: sun and the car</h2>
          <div class="legend"><span><i style="background:var(--amber-band)"></i>Forecast range</span><span><i class="line" style="background:var(--amber)"></i>Solar so far</span><span><i style="background:color-mix(in srgb, ${carColor} 50%, transparent)"></i>Planned car</span><span><i class="line" style="background:var(--cyan)"></i>Car drew</span></div></div>
        <div class="chart" data-chart="today">
          <svg viewBox="${vb.join(' ')}" role="img" aria-label="Solar forecast range, solar so far, the car's planned charging power and what it drew, ${esc(hourLabel(t0))} to ${esc(hourLabel(t1))}">
            <g stroke="var(--hair-2)" stroke-width="1">${grid}<line x1="0" y1="${H}" x2="${W}" y2="${H}" stroke="var(--hair)"/></g>
            <g fill="var(--ink-3)" font-size="12">${yl}${xl.join('')}</g>
            ${band ? `<path d="${band}" fill="var(--amber-band)"/>` : ''}
            ${planPath ? `<path d="${planPath}" fill="color-mix(in srgb, ${carColor} 42%, transparent)" stroke="${carColor}" stroke-width="1.5" stroke-linejoin="round"/>` : ''}
            ${p50 ? `<path d="${p50}" fill="none" stroke="var(--amber)" stroke-opacity="0.6" stroke-width="1.5" stroke-dasharray="4 4"/>` : ''}
            ${wallB.length > 1 ? `<path d="${line(wallB)}" fill="none" stroke="var(--cyan)" stroke-width="2" stroke-linejoin="round"/>` : ''}
            ${pvB.length > 1 ? `<path d="${line(pvB)}" fill="none" stroke="var(--amber)" stroke-width="2.6" stroke-linejoin="round"/>` : ''}
            ${nowMark}${nextMark}
            ${!fcIn.length ? `<text x="${W / 2}" y="${H / 2}" text-anchor="middle" fill="var(--ink-3)" font-size="14">No Solcast forecast yet</text>` : ''}
          </svg>
        </div>
        <div class="stats num">
          <span data-more="${esc(this._ent('forecast_correction') || '')}">Forecast correction <b>×${num(pv.ratio) !== null ? pv.ratio.toFixed(2) : '—'}</b></span>
          ${tomorrow ? '' : `<span>Left today <b>${f0(pv.remaining_expected_kwh)} kWh</b>${range}</span>`}
          ${tomorrow ? '' : `<span>Solar so far <b>${f0(pv.pv_today_kwh)} kWh</b></span>`}
          ${carToday !== null && this._hist ? `<span>Car today <b>${f1(carToday)} kWh</b></span>` : ''}
          ${this._hist ? `<span>Plan changes today <b>${changes}</b></span>` : ''}
        </div>
      </section>`;
    }

    _sitePanel(s) {
      const site = s.site || {};
      const c = this._focusCar(s);
      const bat = num(site.battery_kw); const grid = num(site.grid_kw);
      const batWord = bat === null ? '' : bat > 0.05 ? ' charging' : bat < -0.05 ? ' discharging' : '';
      const gridWord = grid === null ? '' : grid > 0.05 ? ' import' : grid < -0.05 ? ' export' : '';
      const src = s.sources || {};
      const row = (name, color, value, small, more) => `<div class="r" data-more="${esc(more || '')}"><span style="color:${color}">${name}</span><span class="v num">${value} <small>${small}</small></span></div>`;
      let foot = '';
      if (site.problem) foot = `<span class="bad-t">${esc(site.problem)}</span>`;
      else if (c?.on_charger && s.shadow && num(site.wall_current) !== null && Math.abs(site.wall_current - c.planned_a) >= 2) foot = `The car draws ${f0(site.wall_current)} A on its own schedule; the plan would send ${f0(c.planned_a)} A.`;
      return `<section class="panel site">
        <div class="lbl">Site now</div>
        <div>
          ${row('Solar', 'var(--amber)', f1(site.pv_kw), 'kW', src.pv?.entity_id)}
          ${row('House', 'var(--cyan)', f1(site.house_kw), 'kW')}
          ${row('Charger', this._carColor(c), f1(site.wall_kw), 'kW', src.wall?.entity_id)}
          ${row('Battery', 'var(--green)', `${f0(site.soc)}<small>%</small>`, `${bat === null ? '' : `${bat > 0 ? '+' : bat < 0 ? '−' : ''}${f1(Math.abs(bat))} kW${batWord}`}`)}
          ${row('Grid', 'var(--indigo)', f1(grid === null ? null : Math.abs(grid)), `kW${gridWord}`)}
        </div>
        ${foot ? `<div class="foot">${foot}</div>` : ''}
      </section>`;
    }

    _batteryPanel(s) {
      const tr = (s.trajectory || []).map(p => ({ t: ts(p.t), soc: p.soc, exp: p.expected, noCar: p.no_car }));
      const floor = num(s.settings?.floor_pct) ?? 30;
      if (tr.length < 2) return `<section class="panel battery"><div class="ptitle"><h2>Home battery outlook</h2></div><p class="reason">No battery path yet: the planner shows one once it can decide.</p></section>`;
      const now = Date.now();
      const t0 = Math.min(now, tr[0].t - STEP); const t1 = tr[tr.length - 1].t;
      const W = 440; const H = 150; const vb = [-34, -14, 484, 196];
      const x = t => (t - t0) / (t1 - t0) * W; const y = v => H - Math.max(0, Math.min(100, v)) / 100 * H;
      const P = n => n.toFixed(1);
      const start = { t: t0, soc: num(s.site?.soc) ?? tr[0].soc, exp: num(s.site?.soc) ?? tr[0].exp, noCar: num(s.site?.soc) ?? tr[0].noCar };
      const pts = [start, ...tr];
      const path = k => `M${pts.map(p => `${P(x(p.t))},${P(y(p[k]))}`).join(' L')}`;
      const low = tr.reduce((a, p) => (p.soc < a.soc ? p : a), tr[0]);
      const free = (s.free_windows || []).map(w => ({ a: Math.max(ts(w.start), t0), b: Math.min(ts(w.end), t1) })).filter(w => w.b > w.a);
      const freeRects = free.map((w, i) => `<rect x="${P(x(w.a))}" y="0" width="${P(x(w.b) - x(w.a))}" height="${H}" fill="var(--indigo-band)"/>${i === 0 && x(w.b) - x(w.a) > 90 ? `<text x="${P(x(w.a) + 6)}" y="14" fill="var(--indigo)" font-size="12">Free hours</text>` : ''}`).join('');
      const xl = [];
      const step = (t1 - t0) > 16 * 3600000 ? 4 : (t1 - t0) > 8 * 3600000 ? 3 : 2;
      let first = Math.ceil(t0 / 3600000) * 3600000; while (new Date(first).getHours() % step) first += 3600000;
      for (let t = first; t < t1; t += step * 3600000) if (x(t) > 40 && x(t) < W - 40) xl.push(`<text x="${P(x(t))}" y="${H + 20}" text-anchor="middle">${hourLabel(t)}</text>`);
      xl.unshift(`<text x="0" y="${H + 20}">now</text>`); xl.push(`<text x="${W}" y="${H + 20}" text-anchor="end">${hourLabel(t1)}</text>`);
      const lowX = x(low.t); const lowY = y(low.soc);
      this._charts.battery = {
        vb, x0: 0, x1: W, t0, t1,
        read: t => { const p = pts.reduce((a, q) => (Math.abs(q.t - t) < Math.abs(a.t - t) ? q : a), pts[0]); return `<b>${esc(clock(p.t))}</b><br>Battery ${f0(p.soc)}% <span class="muted">at the caution level</span><br>Expected day ${f0(p.exp)}%<br>Without the car ${f0(p.noCar)}%`; },
      };
      const minId = this._ent('battery_minimum');
      return `<section class="panel battery">
        <div class="ptitle"><h2>Home battery outlook</h2><div class="r num" data-more="${esc(minId || '')}">lowest <b>${f0(low.soc)}%</b> at ${esc(clock(low.t))}</div></div>
        <div class="legend"><span><i class="line" style="background:var(--green)"></i>At the caution level</span><span><i class="line" style="background:color-mix(in srgb, var(--green) 50%, transparent)"></i>Expected day</span><span><i class="line" style="background:var(--red)"></i>Floor</span></div>
        <div class="chart" data-chart="battery">
          <svg viewBox="${vb.join(' ')}" role="img" aria-label="Predicted home battery level until ${esc(hourLabel(t1))}; lowest ${f0(low.soc)} percent at ${esc(clock(low.t))}; floor ${f0(floor)} percent">
            ${freeRects}
            <g stroke="var(--hair-2)" stroke-width="1"><line x1="0" y1="0" x2="${W}" y2="0"/><line x1="0" y1="${y(50)}" x2="${W}" y2="${y(50)}"/><line x1="0" y1="${H}" x2="${W}" y2="${H}" stroke="var(--hair)"/></g>
            <g fill="var(--ink-3)" font-size="12"><text x="-6" y="4" text-anchor="end">100</text><text x="-6" y="${P(y(50) + 4)}" text-anchor="end">50</text>${xl.join('')}</g>
            <line x1="0" y1="${P(y(floor))}" x2="${W}" y2="${P(y(floor))}" stroke="var(--red)" stroke-width="1.5" stroke-dasharray="6 4"/>
            <text x="-6" y="${P(y(floor) + 4)}" fill="var(--red)" font-size="12" text-anchor="end">${f0(floor)}%</text>
            <path d="${path('exp')}" fill="none" stroke="var(--green)" stroke-opacity="0.5" stroke-width="2" stroke-dasharray="5 4" stroke-linejoin="round"/>
            <path d="${path('soc')}" fill="none" stroke="var(--green)" stroke-width="2.6" stroke-linejoin="round"/>
            <circle cx="${P(lowX)}" cy="${P(lowY)}" r="5" fill="var(--sky-a)" stroke="var(--green)" stroke-width="2"/>
          </svg>
        </div>
      </section>`;
    }

    _settingsPanel(s) {
      const st = s.settings || {};
      const caution = String(st.caution || '');
      const levels = s.caution_levels || {};
      const seg = CAUTION.map(([key, name]) => `<button type="button" aria-pressed="${key === caution}" data-act="caution:${key}" title="${esc(name)}">${levels[key] ? `${Math.round(levels[key] * 100)}%` : esc(name)}<small>${esc(name)}</small></button>`).join('');
      const stepper = (key, label, value, delta, lo, hi) => `<div class="set"><span data-more="${esc(this._ent(key) || '')}">${label}</span><span class="stepper"><button type="button" aria-label="Lower ${esc(label.toLowerCase())}" data-act="step:${key}:${-delta}" data-range="${lo},${hi}">−</button><b class="num">${f0(value)}%</b><button type="button" aria-label="Raise ${esc(label.toLowerCase())}" data-act="step:${key}:${delta}" data-range="${lo},${hi}">+</button></span></div>`;
      const fw = (s.free_windows || [])[0];
      const freeText = fw ? `${clock(ts(fw.start))} – ${clock(ts(fw.end))}` : 'none in the next day';
      const cars = s.cars || [];
      const override = String(st.override || 'auto');
      const ovr = cars.length > 1 ? `<div class="set" style="flex-direction:column;align-items:stretch;gap:6px"><span class="cap" data-more="${esc(this._ent('charger_car_override') || '')}">Car on the charger</span><div class="seg" style="--n:${cars.length + 1}"><button type="button" aria-pressed="${override === 'auto'}" data-act="override:auto">Auto</button>${cars.map(c => `<button type="button" aria-pressed="${override === c.car_id}" data-act="override:${esc(c.car_id)}">${esc(c.name)}</button>`).join('')}</div></div>` : '';
      return `<section class="panel settings">
        <div class="ptitle"><h2>Settings</h2></div>
        <div style="display:flex;flex-direction:column;gap:6px"><span class="cap" data-more="${esc(this._ent('caution') || '')}">Caution: how sure the battery must stay above the floor</span><div class="seg" role="group" aria-label="Caution">${seg}</div></div>
        ${stepper('battery_floor', 'Battery floor', st.floor_pct, 5, 5, 95)}
        ${stepper('battery_wanted_at_free_hours', 'Battery at free start', st.wanted_at_free_start_pct, 5, 0, 100)}
        <div class="set"><span data-more="${esc(this._ent('minimum_time_between_changes') || '')}">Changes</span><span class="num">every ${f0(st.min_change_min)} min · up to ${f0(st.max_step_a)} A</span></div>
        <div class="set"><span>Free hours</span><span class="num">${esc(freeText)}</span></div>
        ${ovr}
      </section>`;
    }

    _healthPanel(s) {
      const boxes = [];
      for (const c of (s.cars || []).filter(x => x.kind === 'rivian' && x.control_state_entity)) {
        const st = this._hass?.states[c.control_state_entity];
        if (!st) continue;
        const a = st.attributes || {};
        const label = unavailable(st) ? 'Unavailable' : (RIVIAN_STATE[st.state] || cap1(st.state));
        const cls = st.state === 'fault' || unavailable(st) ? 'bad-t' : st.state === 'dry_run' ? 'warn-t' : st.state === 'lease_active' || st.state === 'writing' ? 'good-t' : '';
        const want = a.desired_charge === true ? `${a.dry_run ? 'Would write' : 'Holds'} ${f0(a.requested_amps)} A` : a.desired_charge === false ? (a.dry_run ? 'Would pause' : 'Paused') : 'No request';
        const io = num(a.reads_last_hour) !== null ? ` · ${f0(a.reads_last_hour)} reads, ${f0(a.writes_last_hour)} writes this hour` : '';
        const fault = a.fault_message || a.last_error ? `<div class="d bad-t">${esc(a.fault_message || a.last_error)}</div>` : '';
        boxes.push(`<div class="hbox" data-more="${esc(c.control_state_entity)}"><div class="h"><span>${esc(c.name)} schedule control</span><span class="${cls}">${esc(label)}</span></div><div class="d">${esc(want)}${esc(io)}</div>${fault}</div>`);
      }
      const sp = s.spend;
      if (sp) {
        const pct = sp.cap > 0 ? Math.min(100, sp.spent / sp.cap * 100) : 0;
        const lvl = sp.level && sp.level !== 'normal' ? `<div class="d warn-t">${esc(cap1(String(sp.level).replace(/_/g, ' ')))}</div>` : '';
        boxes.push(`<div class="hbox" data-more="${esc(this._ent('tesla_api_spend') || '')}"><div class="h"><span>Tesla API this month</span><span class="num">$${Number(sp.spent).toFixed(2)} <span class="muted">of $${Number(sp.cap).toFixed(2)}</span></span></div><div class="meter"><i style="width:${pct.toFixed(1)}%"></i></div>${lvl}</div>`);
      }
      const hb = !s.enabled ? ['off', 'muted'] : s.healthy ? (s.active ? ['active', 'good-t'] : ['shadow', 'warn-t']) : ['down', 'bad-t'];
      const site = s.site || {};
      const inputs = site.problem ? [site.problem, 'bad-t'] : s.history_error ? ['house history missing', 'warn-t'] : ['all fresh', 'good-t'];
      const err = s.last_error ? `<div class="note bad">${esc(s.last_error)}</div>` : '';
      return `<section class="panel health">
        <div class="ptitle"><h2>Health</h2></div>
        ${boxes.join('')}
        <div class="set" data-more="${esc(this._ent('heartbeat') || '')}"><span>Heartbeat</span><span class="${hb[1]}">${hb[0]} <span class="muted num">· ${esc(ago(ts(s.last_success)))}</span></span></div>
        <div class="set"><span>Inputs</span><span class="${inputs[1]}">${esc(inputs[0])}</span></div>
        <div class="set"><span>Car on the charger</span><span class="muted" title="${esc(s.identification || '')}">${esc((s.cars || []).find(c => c.car_id === s.car_on_charger)?.name || 'none')}</span></div>
        ${err}
      </section>`;
    }
  }

  customElements.define(TAG, SolarEvPlannerCard);
  window.customCards = window.customCards || [];
  window.customCards.push({ type: TAG, name: 'Solar EV planner', description: 'The Solar EV planner\'s cockpit: what it plans, why, and its settings.', preview: false });
  console.info(`%c SOLAR-EV-PLANNER-CARD %c ${VERSION} `, 'background:#F6A04D;color:#07162B;font-weight:600', 'background:#07162B;color:#F2F7FC');
})();
