"""The replay as one self-contained HTML page: inline SVG, inline CSS and script, no assets.

Per day, three charts share the time axis (one y-scale each, never two on one chart):

- power (kW): actual solar, the forecast in force (p10-p90 band, p50 line), house, grid
  (+ import, - export) and the car's actual draw;
- home battery (%): actual, the expected and cautious levels the planner predicted
  ``horizon`` hours earlier, the what-if level (closed-loop replays) and the floor;
- car current (A): the planner's current, the Wall Connector's measured current, and a dot at
  each change.

Free hours are shaded. Hovering (or focusing a chart and using the arrow keys) shows every
value at that time with the planner's reason; each day lists its changes with their reasons,
and a table view of all steps opens on demand. Colours: the reference categorical palette's
first four slots, validated for colour-vision deficiency, with dark-mode steps.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import datetime, time
import html
import json
import math
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from tools.replay import Result, Row

W = 960
LEFT, RIGHT = 48, 16
PANELS = (("power", 200), ("battery", 130), ("amps", 110))
GAP, TOP, AXIS = 30, 18, 26
SERIES = {
    "power": (
        ("pv_kw", "Solar", 1),
        ("pv_p50_kw", "Forecast p50 (p10-p90 band)", 1),
        ("house_kw", "House", 2),
        ("grid_kw", "Grid (+ import)", 3),
        ("grid_whatif_kw", "Grid, what-if", 3),
        ("car_kw", "Car (actual)", 4),
    ),
    "battery": (
        ("soc", "Actual", 1),
        ("soc_pred", "Expected, predicted {h} h earlier", 2),
        ("soc_pred_caution", "Cautious, predicted {h} h earlier", 3),
        ("soc_whatif", "What-if (planner in control)", 4),
    ),
    "amps": (
        ("planned_a", "Planner current", 1),
        ("wall_a", "Wall Connector (measured)", 2),
    ),
}
TITLES = {
    "power": "Power, kW",
    "battery": "Home battery, %",
    "amps": "Car current, A",
}
DASHED = {"pv_p50_kw", "soc_pred", "soc_pred_caution", "grid_whatif_kw"}
WHATIF = {"soc_whatif", "grid_whatif_kw"}  # drawn only in closed-loop replays

CSS = """
.viz-root {
  color-scheme: light;
  --page: #f9f9f7; --surface-1: #fcfcfb;
  --text-primary: #0b0b0b; --text-secondary: #52514e; --text-muted: #898781;
  --grid: #e1e0d9; --axis: #c3c2b7; --border: rgba(11,11,11,0.10);
  --shade: rgba(42,120,214,0.07);
  --series-1: #2a78d6; --series-2: #eb6834; --series-3: #1baf7a; --series-4: #eda100;
  --critical: #d03b3b;
}
@media (prefers-color-scheme: dark) {
  :root:where(:not([data-theme="light"])) .viz-root {
    color-scheme: dark;
    --page: #0d0d0d; --surface-1: #1a1a19;
    --text-primary: #f0efec; --text-secondary: #c3c2b7; --text-muted: #898781;
    --grid: #2c2c2a; --axis: #383835; --border: rgba(255,255,255,0.10);
    --shade: rgba(57,135,229,0.10);
    --series-1: #3987e5; --series-2: #d95926; --series-3: #199e70; --series-4: #c98500;
  }
}
:root[data-theme="dark"] .viz-root {
  color-scheme: dark;
  --page: #0d0d0d; --surface-1: #1a1a19;
  --text-primary: #f0efec; --text-secondary: #c3c2b7; --text-muted: #898781;
  --grid: #2c2c2a; --axis: #383835; --border: rgba(255,255,255,0.10);
  --shade: rgba(57,135,229,0.10);
  --series-1: #3987e5; --series-2: #d95926; --series-3: #199e70; --series-4: #c98500;
}
* { box-sizing: border-box; }
body { margin: 0; background: #f9f9f7; }
@media (prefers-color-scheme: dark) { body { background: #0d0d0d; } }
.viz-root {
  background: var(--page); color: var(--text-primary);
  font: 14px/1.45 system-ui, -apple-system, "Segoe UI", sans-serif;
  padding: 24px 16px 48px; min-height: 100vh;
}
main { max-width: 1000px; margin: 0 auto; }
h1 { font-size: 22px; margin: 0 0 4px; font-weight: 600; }
h2 { font-size: 16px; margin: 0 0 8px; font-weight: 600; }
.sub, .muted { color: var(--text-secondary); }
.card {
  background: var(--surface-1); border: 1px solid var(--border); border-radius: 10px;
  padding: 16px; margin: 16px 0;
}
table { border-collapse: collapse; width: 100%; font-size: 13px; }
th, td { text-align: left; padding: 4px 8px; border-bottom: 1px solid var(--grid);
  vertical-align: top; }
th { color: var(--text-secondary); font-weight: 600; }
td.num, th.num { text-align: right; font-variant-numeric: tabular-nums; white-space: nowrap; }
.scroll { overflow-x: auto; }
.legend { display: flex; flex-wrap: wrap; gap: 4px 16px; margin: 4px 0 8px;
  color: var(--text-secondary); font-size: 12px; }
.legend span { display: inline-flex; align-items: center; gap: 6px; }
.key { display: inline-block; width: 18px; height: 0; border-top: 2px solid; }
.key.dash { border-top-style: dashed; }
.key.band { height: 10px; border: 0; }
.key.dot { width: 8px; height: 8px; border-radius: 50%; border: 0;
  background: var(--series-1); }
.chart-wrap { position: relative; }
svg.chart { display: block; width: 100%; height: auto; outline: none; }
svg.chart:focus-visible { box-shadow: 0 0 0 2px var(--series-1); border-radius: 4px; }
svg text { fill: var(--text-muted); font-size: 11px; font-variant-numeric: tabular-nums; }
svg .title { fill: var(--text-secondary); font-size: 12px; font-weight: 600; }
.gridline { stroke: var(--grid); stroke-width: 1; }
.baseline { stroke: var(--axis); stroke-width: 1; }
.floor { stroke: var(--critical); stroke-width: 1; }
.free { fill: var(--shade); }
.line { fill: none; stroke-width: 2; stroke-linejoin: round; stroke-linecap: round; }
.line.dash { stroke-dasharray: 5 4; stroke-width: 1.5; }
.band { stroke: none; opacity: 0.12; }
.s1 { stroke: var(--series-1); } .s2 { stroke: var(--series-2); }
.s3 { stroke: var(--series-3); } .s4 { stroke: var(--series-4); }
.f1 { fill: var(--series-1); } .f2 { fill: var(--series-2); }
.f3 { fill: var(--series-3); } .f4 { fill: var(--series-4); }
.k1 { border-color: var(--series-1); } .k2 { border-color: var(--series-2); }
.k3 { border-color: var(--series-3); } .k4 { border-color: var(--series-4); }
.b1 { background: var(--series-1); opacity: .25; } .bshade { background: var(--shade); }
.kfloor { border-color: var(--critical); }
.dot { stroke: var(--surface-1); stroke-width: 2; }
.cross { stroke: var(--text-muted); stroke-width: 1; }
.tip {
  position: absolute; pointer-events: none; z-index: 2; min-width: 220px; max-width: 320px;
  background: var(--surface-1); border: 1px solid var(--border); border-radius: 8px;
  padding: 8px 10px; font-size: 12px; box-shadow: 0 4px 16px rgba(0,0,0,.12);
}
.tip .t { font-weight: 600; margin-bottom: 4px; }
.tip .r { display: flex; align-items: center; gap: 6px; }
.tip .r b { min-width: 52px; text-align: right; font-variant-numeric: tabular-nums; }
.tip .why { margin-top: 6px; color: var(--text-secondary); }
details summary { cursor: pointer; color: var(--text-secondary); margin-top: 8px; }
ul.notes { margin: 8px 0 0; padding-left: 18px; color: var(--text-secondary); }
"""

SCRIPT = r"""
(function () {
  const fmt = (v, d) => (v === null || v === undefined) ? '-' : Number(v).toFixed(d);
  document.querySelectorAll('section.day').forEach(function (sec) {
    const data = JSON.parse(sec.querySelector('script.day-data').textContent);
    const svg = sec.querySelector('svg.chart');
    const tip = sec.querySelector('.tip');
    const cross = svg.querySelector('.cross');
    const wrap = sec.querySelector('.chart-wrap');
    const x0 = data.x0, dx = data.dx, rows = data.rows, keys = data.keys;
    let idx = -1;
    function row(label, value, unit, cls, dash) {
      const r = document.createElement('div'); r.className = 'r';
      const k = document.createElement('span');
      k.className = 'key ' + (cls || '') + (dash ? ' dash' : '');
      const b = document.createElement('b'); b.textContent = value;
      const l = document.createElement('span'); l.textContent = unit + ' ' + label;
      r.append(k, b, l); return r;
    }
    function show(i, px) {
      if (!rows.length) return;
      idx = Math.max(0, Math.min(rows.length - 1, i));
      const r = rows[idx];
      const x = x0 + r.i * dx;
      cross.setAttribute('x1', x); cross.setAttribute('x2', x);
      cross.style.display = '';
      tip.replaceChildren();
      const head = document.createElement('div'); head.className = 't';
      head.textContent = r.time + '  ' + r.mode + (r.car ? ' (' + r.car + ')' : '');
      tip.append(head);
      keys.forEach(function (k) {
        if (r[k.key] === null || r[k.key] === undefined) return;
        tip.append(row(k.label, fmt(r[k.key], k.d), k.unit, 'k' + k.slot, k.dash));
      });
      if (r.p10 !== null) tip.append(row('forecast p10-p90 (' + r.src + ')',
        fmt(r.p10, 1) + '-' + fmt(r.p90, 1), 'kW', 'k1', true));
      const why = document.createElement('div'); why.className = 'why';
      why.textContent = r.reason + (r.safety ? ' [safety: ' + r.safety + ']' : '');
      tip.append(why);
      tip.hidden = false;
      const box = wrap.getBoundingClientRect();
      const left = px !== undefined ? px : (x / data.w) * box.width;
      const w = tip.offsetWidth;
      tip.style.left = Math.max(0, Math.min(box.width - w, left + 14)) + 'px';
      tip.style.top = '8px';
    }
    function hide() { tip.hidden = true; cross.style.display = 'none'; }
    svg.addEventListener('pointermove', function (ev) {
      const p = svg.createSVGPoint(); p.x = ev.clientX; p.y = ev.clientY;
      const q = p.matrixTransform(svg.getScreenCTM().inverse());
      const target = Math.round((q.x - x0) / dx);
      let best = 0, bd = Infinity;
      rows.forEach(function (r, j) {
        const d = Math.abs(r.i - target);
        if (d < bd) { bd = d; best = j; }
      });
      show(best, ev.clientX - wrap.getBoundingClientRect().left);
    });
    svg.addEventListener('pointerleave', hide);
    svg.addEventListener('blur', hide);
    svg.addEventListener('keydown', function (ev) {
      if (ev.key === 'ArrowRight') { show(idx + 1); ev.preventDefault(); }
      else if (ev.key === 'ArrowLeft') {
        show(idx < 0 ? rows.length - 1 : idx - 1);
        ev.preventDefault();
      }
      else if (ev.key === 'Escape') { hide(); }
    });
    const det = sec.querySelector('details.steps');
    det.addEventListener('toggle', function () {
      if (!det.open || det.dataset.done) return;
      det.dataset.done = '1';
      const tb = det.querySelector('tbody');
      rows.forEach(function (r) {
        const tr = document.createElement('tr');
        const cells = [r.time, r.mode, r.car, fmt(r.planned_a, 0), fmt(r.pv_kw, 2),
          fmt(r.house_kw, 2), fmt(r.grid_kw, 2), fmt(r.car_kw, 2), fmt(r.soc, 0),
          fmt(r.soc_pred, 0), r.src, r.reason];
        cells.forEach(function (c, j) {
          const td = document.createElement('td'); td.textContent = c;
          if (j >= 3 && j <= 9) td.className = 'num';
          tr.append(td);
        });
        tb.append(tr);
      });
    });
  });
})();
"""


def esc(x: Any) -> str:
    return html.escape(str(x), quote=True)


def nice_ceil(x: float) -> float:
    if x <= 0:
        return 1.0
    mag = 10 ** math.floor(math.log10(x))
    for m in (1, 1.2, 1.5, 2, 2.5, 3, 4, 5, 6, 8, 10):
        if x <= m * mag:
            return m * mag
    return 10 * mag


def nice_step(x: float) -> float:
    """1, 2 or 5 times a power of ten, at least x."""
    if x <= 0:
        return 1.0
    mag = 10 ** math.floor(math.log10(x))
    return next((m * mag for m in (1, 2, 5) if x <= m * mag), 10 * mag)


def ticks(lo: float, hi: float, n: int = 4) -> list[float]:
    step = nice_step((hi - lo) / n)
    start = math.ceil(lo / step) * step
    out, v = [], start
    while v <= hi + 1e-9:
        out.append(round(v, 6))
        v += step
    return out


def _path(xs: Sequence[float], ys: Sequence[float | None], step: bool = False) -> str:
    """An SVG path through the points, broken where a value is missing."""
    parts, pen = [], False
    last_y = None
    for x, y in zip(xs, ys, strict=True):
        if y is None:
            pen = False
            continue
        if not pen:
            parts.append(f"M{x:.1f},{y:.1f}")
            pen = True
        elif step and last_y is not None:
            parts.append(f"H{x:.1f}V{y:.1f}")
        else:
            parts.append(f"L{x:.1f},{y:.1f}")
        last_y = y
    return "".join(parts)


def _band(xs: Sequence[float], lo: Sequence[float | None], hi: Sequence[float | None]) -> str:
    """Closed areas between two series, one per unbroken run."""
    out, run = [], []
    for x, a, b in zip(xs, lo, hi, strict=True):
        if a is None or b is None:
            if run:
                out.append(run)
            run = []
        else:
            run.append((x, a, b))
    if run:
        out.append(run)
    d = ""
    for r in out:
        top = "L".join(f"{x:.1f},{b:.1f}" for x, _, b in r)
        bottom = "L".join(f"{x:.1f},{a:.1f}" for x, a, _ in reversed(r))
        d += f"M{top}L{bottom}Z"
    return d


def day_svg(rows: Sequence[Row], day0: datetime, result: Result, step_min: int) -> str:
    """The three panels of one day, as one SVG."""
    span = 24 * 60
    x0, x1 = LEFT, W - RIGHT
    dx = (x1 - x0) / (span / step_min)
    height = TOP + sum(h for _, h in PANELS) + GAP * (len(PANELS) - 1) + AXIS

    def xi(t: datetime) -> float:
        return x0 + (t - day0).total_seconds() / 60 / step_min * dx

    xs = [xi(r.t) for r in rows]
    out = [
        f'<svg class="chart" viewBox="0 0 {W} {height}" role="img" tabindex="0" '
        f'aria-label="Power, home battery and car current on {day0:%A %d %B}">'
    ]
    # free-hours shading over all panels
    free_runs: list[tuple[float, float]] = []
    run_start = None
    for x, free in zip(xs, [r.price is not None and r.price <= 0 for r in rows], strict=True):
        if free and run_start is None:
            run_start = x
        if not free and run_start is not None:
            free_runs.append((run_start, x))
            run_start = None
    if run_start is not None:
        free_runs.append((run_start, xs[-1] + dx))
    y = TOP
    for name, h in PANELS:
        for a, b in free_runs:
            out.append(f'<rect class="free" x="{a:.1f}" y="{y}" width="{b - a:.1f}" height="{h}"/>')
        out.append(_panel(name, rows, xs, y, h, result))
        y += h + GAP
    # hour axis
    axis_y = y - GAP + 14
    for hh in range(0, 25, 3):
        x = x0 + hh * 60 / step_min * dx
        anchor = "start" if hh == 0 else "end" if hh == 24 else "middle"
        out.append(f'<text x="{x:.1f}" y="{axis_y + 4}" text-anchor="{anchor}">{hh:02d}:00</text>')
    out.append(
        f'<line class="cross" x1="0" x2="0" y1="{TOP}" y2="{y - GAP}" style="display:none"/>'
    )
    out.append("</svg>")
    return "".join(out)


def _panel(
    name: str, rows: Sequence[Row], xs: Sequence[float], y0: float, h: float, result: Result
) -> str:
    out = [f'<text class="title" x="{LEFT}" y="{y0 - 6}">{esc(TITLES[name])}</text>']
    get: Callable[[Row, str], float | None] = lambda r, k: getattr(r, k)  # noqa: E731
    if name == "power":
        keys = [k for k, _, _ in SERIES["power"]] + ["pv_p10_kw", "pv_p90_kw"]
        vals = [v for r in rows for k in keys if (v := get(r, k)) is not None]
        lo = min([0.0, *vals])
        hi = max([1.0, *vals])
        lo = -nice_ceil(-lo) if lo < 0 else 0.0
        hi = nice_ceil(hi)
    elif name == "battery":
        lo, hi = 0.0, 100.0
    else:
        vals = [v for r in rows for k in ("planned_a", "wall_a") if (v := get(r, k)) is not None]
        lo, hi = 0.0, max(48.0, nice_ceil(max(vals, default=48.0)))

    def ys(v: float | None) -> float | None:
        if v is None:
            return None
        v = min(hi, max(lo, v))
        return y0 + h - (v - lo) / (hi - lo) * h

    for tv in ticks(lo, hi):
        yy = ys(tv)
        cls = "baseline" if tv == 0 else "gridline"
        out.append(
            f'<line class="{cls}" x1="{LEFT}" x2="{W - RIGHT}" y1="{yy:.1f}" y2="{yy:.1f}"/>'
        )
        label = f"{tv:g}"
        out.append(f'<text x="{LEFT - 6}" y="{yy + 4:.1f}" text-anchor="end">{label}</text>')
    if name == "power":
        band = _band(
            xs, [ys(get(r, "pv_p10_kw")) for r in rows], [ys(get(r, "pv_p90_kw")) for r in rows]
        )
        if band:
            out.append(f'<path class="band f1" d="{band}"/>')
    if name == "battery":
        fy = ys(result.floor_pct)
        out.append(
            f'<line class="floor" x1="{LEFT}" x2="{W - RIGHT}" y1="{fy:.1f}" y2="{fy:.1f}"/>'
        )
        out.append(
            f'<text x="{W - RIGHT}" y="{fy - 4:.1f}" text-anchor="end">'
            f"floor {result.floor_pct:g}%</text>"
        )
    for key, _, slot in SERIES[name]:
        if key in WHATIF and not result.whatif:
            continue
        pts = [ys(get(r, key)) for r in rows]
        if all(p is None for p in pts):
            continue
        d = _path(xs, pts, step=(name == "amps"))
        dash = " dash" if key in DASHED else ""
        out.append(f'<path class="line s{slot}{dash}" d="{d}"/>')
    if name == "amps":
        changed = {c.t for c in result.changes}
        for x, r in zip(xs, rows, strict=True):
            if r.t in changed:
                yy = ys(r.planned_a)
                out.append(
                    f'<circle class="dot f1" cx="{x:.1f}" cy="{yy:.1f}" r="4">'
                    f"<title>{r.t:%H:%M} {esc(r.planned_a)} A: {esc(r.reason)}</title></circle>"
                )
    return "".join(out)


def _legend(name: str, result: Result) -> str:
    items = []
    for key, label, slot in SERIES[name]:
        if key in WHATIF and not result.whatif:
            continue
        cls = f"key k{slot}" + (" dash" if key in DASHED else "")
        text = label.format(h=f"{result.horizon_h:g}")
        items.append(f'<span><i class="{cls}"></i>{esc(text)}</span>')
    if name == "power":
        items.insert(2, '<span><i class="key band b1"></i>Forecast p10-p90</span>')
    if name == "battery":
        items.append(f'<span><i class="key kfloor"></i>Floor {result.floor_pct:g}%</span>')
    if name == "amps":
        items.append('<span><i class="key dot"></i>A change (hover for the reason)</span>')
    return f'<div class="legend"><b>{esc(TITLES[name])}</b>{"".join(items)}</div>'


def _day_json(rows: Sequence[Row], day0: datetime, step_min: int, horizon_h: float) -> str:
    keys = []
    for name, unit in (("power", "kW"), ("battery", "%"), ("amps", "A")):
        for key, label, slot in SERIES[name]:
            if key == "pv_p50_kw":
                continue
            d = 1 if name == "battery" else 0 if name == "amps" else 2
            keys.append(
                {
                    "key": key,
                    "label": label.format(h=f"{horizon_h:g}"),
                    "unit": unit,
                    "slot": slot,
                    "d": d,
                    "dash": key in DASHED,
                }
            )
    data = {
        "x0": LEFT,
        "dx": (W - LEFT - RIGHT) / (24 * 60 / step_min),
        "w": W,
        "keys": keys,
        "rows": [
            {
                "i": round((r.t - day0).total_seconds() / 60 / step_min),
                "time": f"{r.t:%a %H:%M}",
                "mode": r.mode,
                "car": r.car,
                "reason": r.reason,
                "safety": r.safety,
                "src": r.forecast_source,
                "p10": r.pv_p10_kw,
                "p90": r.pv_p90_kw,
                **{k["key"]: getattr(r, k["key"]) for k in keys},
            }
            for r in rows
        ],
    }
    # keep "</script>" out of the embedded JSON
    return json.dumps(data, separators=(",", ":")).replace("</", "<\\/")


def render_html(result: Result) -> str:
    """The whole page."""
    rows = result.rows
    if not rows:
        return "<!doctype html><title>Planner replay</title><p>No steps.</p>"
    step_min = (round((rows[1].t - rows[0].t).total_seconds() / 60) if len(rows) > 1 else 5) or 5
    first, last = rows[0].t, rows[-1].t
    mode = "what-if (the car obeys the planner)" if result.whatif else "shadow (open loop)"
    s = result.settings
    head = (
        f"{first:%a %d %b %Y %H:%M} to {last:%a %d %b %H:%M}, every {step_min} min. "
        f"Mode: {mode}. Caution {s.get('caution')}, floor {s.get('floor_pct')}%, "
        f"wanted at free start {s.get('wanted_at_free_start_pct')}%, night window "
        f"{s.get('night_start')}-{s.get('night_end')}, at most one change per "
        f"{s.get('min_change_min')} min of up to {s.get('max_step_a')} A."
    )
    parts = [
        "<!doctype html>",
        '<html lang="en"><head><meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        "<title>Planner replay</title>",
        f"<style>{CSS}</style></head>",
        '<body><div class="viz-root"><main>',
        "<h1>Planner replay</h1>",
        f'<p class="sub">{esc(head)}</p>',
        _summary(result),
    ]
    by_day: dict[Any, list[Row]] = {}
    for r in rows:
        by_day.setdefault(r.t.date(), []).append(r)
    for d, day_rows in by_day.items():
        day0 = datetime.combine(d, time(0))
        changes = [c for c in result.changes if c.t.date() == d]
        parts.append(f'<section class="day card" aria-label="{d:%A %d %B}">')
        parts.append(f"<h2>{d:%A %d %B %Y}</h2>")
        src = sorted({r.forecast_source for r in day_rows})
        parts.append(f'<p class="muted">Forecast source: {esc(", ".join(src))}.</p>')
        parts.append("".join(_legend(n, result) for n, _ in PANELS))
        parts.append('<div class="chart-wrap">')
        parts.append(day_svg(day_rows, day0, result, step_min))
        parts.append('<div class="tip" hidden></div></div>')
        parts.append(_changes_table(changes))
        parts.append(
            '<details class="steps"><summary>Table view: every step</summary>'
            '<div class="scroll"><table><thead><tr><th>Time</th><th>Mode</th><th>Car</th>'
            '<th class="num">Planner A</th><th class="num">Solar kW</th>'
            '<th class="num">House kW</th><th class="num">Grid kW</th><th class="num">Car kW</th>'
            '<th class="num">Battery %</th><th class="num">Predicted %</th><th>Forecast</th>'
            "<th>Reason</th></tr></thead><tbody></tbody></table></div></details>"
        )
        parts.append(
            f'<script type="application/json" class="day-data">'
            f"{_day_json(day_rows, day0, step_min, result.horizon_h)}</script>"
        )
        parts.append("</section>")
    parts.append(f"<script>{SCRIPT}</script></main></div></body></html>")
    return "\n".join(parts)


def _summary(result: Result) -> str:
    cols = [
        ("day", "Day", ""),
        ("changes", "Changes", "num"),
        ("planned_car_kwh", "Planner car kWh", "num"),
        ("actual_car_kwh", "Actual car kWh", "num"),
        ("paid_import_kwh", "Paid import kWh", "num"),
        ("paid_import_cost", "Paid import $", "num"),
        ("min_soc", "Min battery %", "num"),
        ("safety_steps", "Safety steps", "num"),
    ]
    if result.whatif:
        cols += [
            ("whatif_paid_import_kwh", "What-if paid kWh", "num"),
            ("whatif_paid_import_cost", "What-if paid $", "num"),
            ("whatif_min_soc", "What-if min %", "num"),
        ]
    head = "".join(f'<th class="{c}">{esc(t)}</th>' for _, t, c in cols)
    body = ""
    for d in result.days:
        body += (
            "<tr>"
            + "".join(
                f'<td class="{c}">{esc("-" if d.get(k) is None else d.get(k))}</td>'
                for k, _, c in cols
            )
            + "</tr>"
        )
    notes = "".join(f"<li>{esc(n)}</li>" for n in result.notes)
    srcs = ", ".join(f"{k}: {v} steps" for k, v in sorted(result.sources.items()))
    explain = (
        "Planner car kWh is the planner's current over the steps it would charge (day and "
        "night modes); actual car kWh is what the Wall Connector measured. Paid import is the "
        "real grid import at a price above zero."
    )
    if result.whatif:
        explain += (
            " What-if columns come from the closed loop: the real solar and house load with "
            "the planner's car current, through a simple Sol-Ark battery model."
        )
    return (
        '<section class="card"><h2>Summary</h2><div class="scroll"><table>'
        f"<thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>"
        f'<p class="muted">{esc(explain)} Forecast sources: {esc(srcs)}.</p>'
        + (f'<ul class="notes">{notes}</ul>' if notes else "")
        + "</section>"
    )


def _changes_table(changes: Sequence[Any]) -> str:
    if not changes:
        return '<p class="muted">The planner would not have changed the current this day.</p>'
    rows = "".join(
        f"<tr><td>{c.t:%H:%M}</td><td>{esc(c.car)}</td>"
        f'<td class="num">{c.from_a} &rarr; {c.to_a} A</td><td>{esc(c.mode)}</td>'
        f"<td>{esc(c.reason)}</td></tr>"
        for c in changes
    )
    return (
        f"<details open><summary>Current changes and reasons ({len(changes)})</summary>"
        '<div class="scroll"><table><thead><tr><th>Time</th><th>Car</th>'
        '<th class="num">Current</th><th>Mode</th><th>Reason</th></tr></thead>'
        f"<tbody>{rows}</tbody></table></div></details>"
    )
