"""
Build script for GitHub Pages site.

Builds per-region pages for Canada (BoC) and the United States (FRED), plus a
small landing page at site/index.html. Data strategy:

  - CA: data/boc_policy_rate.csv, data/commercial_prime_rate.csv (BoC Valet API)
  - US: data/us_fed_funds_rate.csv, data/us_prime_rate.csv (FRED fredgraph CSV)
  - Each region has its own events file: data/events.json, data/us_events.json.

On each build, only new records since the last stored date are fetched
(incremental update). Output:

  - site/index.html                  — landing page
  - site/ca/index.html               — Canadian tracker
  - site/ca/data/rates.json          — CA rates payload
  - site/ca/data/events.json         — CA events (copy)
  - site/us/index.html               — US tracker
  - site/us/data/rates.json          — US rates payload
  - site/us/data/events.json         — US events (copy)
"""

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

from fred_rates import FREDRateFetcher
from historical_rates import HistoricalRateFetcher

# URL of the deployed Cloudflare Worker that accepts subscribe form POSTs.
# Update this after `wrangler deploy` if the workers.dev subdomain differs.
SUBSCRIBE_PROXY_URL = "https://mortgage-rates-subscribe-proxy.usraelwar.workers.dev"


# ---------------------------------------------------------------------------
# Region page template — uses __TOKEN__ placeholders for per-region strings.
# ---------------------------------------------------------------------------
REGION_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en" data-theme="dark">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>__TITLE__</title>
  <link rel="stylesheet" href="https://k1monfared.github.io/site_kit/css/base.css">
  <script src="https://k1monfared.github.io/site_kit/js/theme.js"></script>
  <script src="https://cdn.jsdelivr.net/npm/echarts@5/dist/echarts.min.js"></script>
  <style>
    :root {
      --card-bg: #16213e;
      --card-border: #30363d;
      --muted: #8b949e;
      --accent-policy: #58a6ff;
      --accent-target: #ffb86c;
      --accent-prime:  #d2a8ff;
      --chart-axis: #8b949e;
      --chart-grid: #30363d;
      --tooltip-bg: #16213e;
      --btn-hover-bg: #1f2a44;
    }
    [data-theme="light"] {
      --card-bg: #ffffff;
      --card-border: #e1e4e8;
      --muted: #6a737d;
      --accent-policy: #2E86AB;
      --accent-target: #C25500;
      --accent-prime:  #A23B72;
      --chart-axis: #6a737d;
      --chart-grid: #e1e4e8;
      --tooltip-bg: #ffffff;
      --btn-hover-bg: #f0f0f0;
    }

    *, *::before, *::after { box-sizing: border-box; }
    body {
      margin: 0; padding: 20px;
      background: var(--bg); color: var(--text);
      transition: background .2s, color .2s;
    }
    h1 { text-align: center; color: var(--text); margin: 0 0 20px; font-size: 1.6rem; }
    .stats {
      display: flex; gap: 14px; justify-content: center;
      margin-bottom: 16px; flex-wrap: wrap;
    }
    .stat-box {
      background: var(--card-bg); border: 1px solid var(--card-border); border-radius: 10px;
      padding: 14px 28px; text-align: center; min-width: 160px; flex: 1 1 160px; max-width: 260px;
      box-shadow: 0 1px 4px rgba(0,0,0,.18);
    }
    .stat-box .label { font-size: 12px; color: var(--muted); margin-bottom: 4px; text-transform: uppercase; letter-spacing: .5px; }
    .stat-box .value { font-size: 30px; font-weight: 700; color: var(--text); }
    .stat-box .as-of { font-size: 11px; color: var(--muted); margin-top: 3px; opacity: .75; }
    .controls {
      display: flex; gap: 8px; justify-content: center; align-items: center;
      flex-wrap: wrap; margin-bottom: 12px;
    }
    .btn {
      padding: 9px 18px; border: 1px solid var(--card-border); border-radius: 6px;
      background: var(--card-bg); cursor: pointer; font-size: 14px; color: var(--text);
      min-height: 42px; touch-action: manipulation;
      transition: background .15s, border-color .15s, color .15s;
    }
    .btn:hover { background: var(--btn-hover-bg); border-color: var(--muted); }
    .btn.active { background: var(--link); color: var(--bg); border-color: var(--link); }
    .btn:disabled { opacity: .55; cursor: default; }
    #refreshStatus { font-size: 12px; color: var(--muted); }
    .chart-wrap {
      background: var(--card-bg); border: 1px solid var(--card-border); border-radius: 10px;
      padding: 8px; box-shadow: 0 1px 4px rgba(0,0,0,.18);
    }
    #chart { width: 100%; height: 560px; }
    .footer {
      text-align: center; font-size: 11px; color: var(--muted); margin-top: 14px; opacity: .85;
    }
    .footer a { color: var(--link); text-decoration: none; }
    .footer a:hover { color: var(--link-hover); text-decoration: underline; }
    #error { color: #e5534b; text-align: center; padding: 20px; display: none; }
    .description {
      max-width: 780px; margin: 14px auto 0; padding: 0 4px;
      font-size: 13px; line-height: 1.6; color: var(--muted); text-align: left;
    }
    .description strong { color: var(--text); }

    .subscribe {
      max-width: 520px; margin: 24px auto 0; padding: 14px 16px;
      background: var(--card-bg); border: 1px solid var(--card-border);
      border-radius: 10px; text-align: center;
    }
    .subscribe h2 {
      margin: 0 0 4px; font-size: 0.95rem; font-weight: 600; color: var(--text);
    }
    .subscribe p {
      margin: 0 0 10px; font-size: 12px; color: var(--muted); line-height: 1.5;
    }
    .subscribe-form {
      display: flex; flex-direction: column; gap: 10px; align-items: center;
    }
    .subscribe-row {
      display: flex; gap: 8px; justify-content: center; flex-wrap: wrap; width: 100%;
    }
    .subscribe-form input[type="email"] {
      flex: 1 1 220px; min-width: 180px; max-width: 300px;
      padding: 9px 12px; font-size: 14px;
      background: var(--bg); color: var(--text);
      border: 1px solid var(--card-border); border-radius: 6px;
    }
    .subscribe-form input[type="email"]:focus {
      outline: none; border-color: var(--link);
    }
    .subscribe-form button {
      padding: 9px 18px; font-size: 14px;
      background: var(--link); color: #fff;
      border: 1px solid var(--link); border-radius: 6px;
      cursor: pointer; font-weight: 500;
      transition: opacity .15s;
    }
    .subscribe-form button:hover { opacity: .88; }
    .subscribe-choices {
      display: flex; gap: 16px; justify-content: center; flex-wrap: wrap;
      font-size: 13px; color: var(--muted);
    }
    .subscribe-choices label {
      display: inline-flex; align-items: center; gap: 6px; cursor: pointer;
    }
    .subscribe-choices input[type="checkbox"] {
      width: 15px; height: 15px; accent-color: var(--link); cursor: pointer;
    }

    #theme-toggle {
      position: fixed; top: 12px; right: 12px;
      width: 36px; height: 36px; border-radius: 50%;
      background: var(--card-bg); border: 1px solid var(--card-border);
      color: var(--text); cursor: pointer; font-size: 16px; line-height: 1;
      display: flex; align-items: center; justify-content: center;
      box-shadow: 0 1px 4px rgba(0,0,0,.2); z-index: 100;
      transition: background .15s, border-color .15s;
    }
    #theme-toggle:hover { background: var(--btn-hover-bg); border-color: var(--muted); }

    .region-nav {
      position: fixed; top: 16px; right: 60px;
      display: flex; gap: 8px; z-index: 100;
    }
    .flag-link {
      display: block; line-height: 0;
      border-radius: 3px; overflow: hidden;
      border: 1px solid var(--card-border);
      box-shadow: 0 1px 3px rgba(0,0,0,.25);
      transition: opacity .15s, transform .15s, border-color .15s;
    }
    .flag-link img { width: 36px; height: auto; display: block; }
    .flag-link.active { opacity: .45; pointer-events: none; cursor: default; }
    .flag-link:not(.active):hover { transform: translateY(-1px); border-color: var(--muted); }

    @media (max-width: 640px) {
      body { padding: 12px 10px; }
      h1 { font-size: 1.25rem; margin-bottom: 14px; padding-right: 44px; }
      .stats { gap: 10px; }
      .stat-box { padding: 10px 14px; min-width: 130px; }
      .stat-box .value { font-size: 24px; }
      .btn { font-size: 13px; padding: 9px 14px; }
      #chart { height: 360px; }
      #theme-toggle { top: 8px; right: 8px; width: 32px; height: 32px; font-size: 14px; }
      .region-nav { top: 12px; right: 48px; gap: 4px; }
      .flag-link img { width: 28px; }
    }
  </style>
</head>
<body>
  <nav class="region-nav" aria-label="Region">
    <a href="../ca/" class="flag-link __CA_ACTIVE__" aria-label="Canada" title="Canadian rates"><img src="https://flagcdn.com/ca.svg" alt="CA" width="36"></a>
    <a href="../us/" class="flag-link __US_ACTIVE__" aria-label="United States" title="US rates"><img src="https://flagcdn.com/us.svg" alt="US" width="36"></a>
  </nav>
  <button id="theme-toggle" aria-label="Toggle theme"><span class="theme-icon"></span></button>

  <h1>__TITLE__</h1>

  <div class="stats">
    <div class="stat-box">
      <div class="label">__LABEL_POLICY__</div>
      <div class="value" id="policyRate">–</div>
      <div class="as-of" id="policyDate"></div>
    </div>
    <div class="stat-box">
      <div class="label">__LABEL_PRIME__</div>
      <div class="value" id="primeRate">–</div>
      <div class="as-of" id="primeDate"></div>
    </div>
  </div>

  <div class="controls">
    <button class="btn" id="toggleEvents">Show Historical Events</button>
    <button class="btn" id="resetZoom">Reset Zoom</button>
  </div>

  <div class="chart-wrap">
    <div id="chart"></div>
  </div>

  <p id="error">Failed to load chart data. Please try refreshing.</p>

  <div class="description">
    <p>The <strong>__LABEL_POLICY__</strong> is the short-term benchmark rate
    set by the central bank; it steers borrowing costs across the economy.
    The <strong>__LABEL_PRIME__</strong> is the benchmark lending rate used
    by major commercial banks, historically running a few percentage points
    above the policy rate. Hover over the chart to read exact values for any
    date. Zoom in to under three years to see individual announcement dots
    on the line.</p>
  </div>

  <div class="subscribe">
    <h2>Get notified when prime rates change</h2>
    <p>One short email when a rate actually moves. Two separate lists — Canada and the US — pick either or both.</p>
    <form class="subscribe-form" action="__SUBSCRIBE_URL__" method="POST">
      <div class="subscribe-choices">
        <label>
          <input type="checkbox" name="lists" value="ca" __CA_CHECKED__>
          Canadian prime rate
        </label>
        <label>
          <input type="checkbox" name="lists" value="us" __US_CHECKED__>
          US prime rate
        </label>
      </div>
      <div class="subscribe-row">
        <input type="email" name="email" required placeholder="you@example.com" autocomplete="email">
        <button type="submit">Subscribe</button>
      </div>
    </form>
  </div>

  <div class="footer">
    Data source: <a href="__SOURCE_URL__" target="_blank" rel="external noopener">__SOURCE_NAME__</a>
    &mdash; Built __BUILD_TIME__
    &mdash; <a href="https://github.com/k1monfared/mortgage_rate_tracker" target="_blank" rel="external noopener">GitHub</a>
    &mdash; <a href="https://k1monfared.github.io/sponsor.html" rel="external noopener">Sponsor</a>
  </div>

  <script>
  // ── Module-level state ─────────────────────────────────────────────────
  var rates, events, chart, eventsVisible = false, dotsVisible = false;
  var THREE_YEARS_MS = 3 * 365.25 * 24 * 3600 * 1000;

  // ── Region config (substituted by build_site.py) ───────────────────────
  var SLUG         = '__SLUG__';
  var LABEL_POLICY = '__LABEL_POLICY__';
  var LABEL_PRIME  = '__LABEL_PRIME__';
  var LABEL_TARGET = '__LABEL_TARGET__';
  var STATE_KEY    = 'tracker.state.' + SLUG;

  // Ordered list of series present on this chart. Populated once rates.json
  // loads, since the target series is US-only. Everything downstream
  // (legend, chart series, tooltip, theme recolor, events markArea, dots)
  // iterates over this list.
  var SERIES_DEFS = [];

  // ── Theme palette (reads live CSS variables so it tracks the toggle) ───
  function chartPalette() {
    var cs = getComputedStyle(document.documentElement);
    function v(name, fallback) {
      var x = cs.getPropertyValue(name);
      return (x && x.trim()) || fallback;
    }
    return {
      bg:       v('--card-bg',        '#16213e'),
      text:     v('--text',           '#e0e0e0'),
      muted:    v('--muted',          '#8b949e'),
      axis:     v('--chart-axis',     '#8b949e'),
      grid:     v('--chart-grid',     '#30363d'),
      tooltip:  v('--tooltip-bg',     '#16213e'),
      border:   v('--card-border',    '#30363d'),
      link:     v('--link',           '#58a6ff'),
      policy:   v('--accent-policy',  '#58a6ff'),
      target:   v('--accent-target',  '#ffb86c'),
      prime:    v('--accent-prime',   '#d2a8ff'),
    };
  }

  function buildSeriesDefs(pal) {
    // Display order: policy (central bank), target (US only), prime (commercial).
    // Lines are drawn in this order; the tooltip sorts by value so its top-to-
    // bottom order always matches the visual vertical stacking on the chart.
    var defs = [
      { key: 'policy', label: LABEL_POLICY, color: pal.policy },
    ];
    if (rates && rates.target && LABEL_TARGET) {
      defs.push({ key: 'target', label: LABEL_TARGET, color: pal.target });
    }
    defs.push({ key: 'prime', label: LABEL_PRIME, color: pal.prime });
    return defs;
  }

  function themedOption(p) {
    return {
      backgroundColor: 'transparent',
      textStyle: { color: p.text },
      tooltip: {
        backgroundColor: p.tooltip,
        borderColor: p.border,
        textStyle: { color: p.text },
        axisPointer: { crossStyle: { color: p.muted } },
      },
      legend: { textStyle: { color: p.text } },
      xAxis: {
        axisLine:  { lineStyle: { color: p.axis } },
        axisTick:  { lineStyle: { color: p.axis } },
        axisLabel: { color: p.text },
      },
      yAxis: {
        axisLine:  { lineStyle: { color: p.axis } },
        axisTick:  { lineStyle: { color: p.axis } },
        axisLabel: { color: p.text },
        splitLine: { lineStyle: { color: p.grid } },
      },
      dataZoom: [
        {},
        { borderColor: p.border, fillerColor: 'rgba(128,128,128,0.12)',
          handleStyle: { color: p.link }, textStyle: { color: p.muted } },
      ],
      series: SERIES_DEFS.map(function(def) {
        return {
          lineStyle: { color: def.color },
          itemStyle: { color: def.color },
          markPoint: { label: { color: def.color, backgroundColor: p.bg, borderColor: p.border } },
        };
      }),
    };
  }

  // ── Helpers ────────────────────────────────────────────────────────────
  function nextDay(dateStr) {
    var d = new Date(dateStr + 'T12:00:00Z');
    d.setUTCDate(d.getUTCDate() + 1);
    return d.toISOString().slice(0, 10);
  }

  function today() {
    return new Date().toISOString().slice(0, 10);
  }

  function mpFormatter(p) {
    var val  = Array.isArray(p.value) ? p.value[1] : p.value;
    var date = '';
    if (p.data && p.data.coord && p.data.coord[0]) {
      date = '\n' + new Date(p.data.coord[0]).toISOString().slice(0, 10);
    }
    return p.name + ': ' + Number(val).toFixed(2) + '%' + date;
  }

  function buildMarkArea() {
    var p = chartPalette();
    return {
      silent: true,
      label: {
        show: true, position: 'insideTop', distance: 6,
        fontSize: 10, color: p.text, rotate: 90, overflow: 'truncate',
      },
      data: events.regions.map(function(r) {
        return [
          { name: r.label, xAxis: r.start, itemStyle: { color: r.color } },
          { xAxis: r.end },
        ];
      }),
    };
  }

  var emptyMarkArea = { data: [] };

  function findStepRate(series, ts) {
    // Dates in rates.json are "YYYY-MM-DD"; JS Date() parses these as UTC
    // midnight, matching how ECharts places the point on the x-axis. Using
    // the same epoch on both sides ensures that on a change date we return
    // the NEW rate (matching the step line), not the previous one.
    var lo = 0, hi = series.length - 1, result = null;
    while (lo <= hi) {
      var mid = (lo + hi) >> 1;
      var midTs = new Date(series[mid].date).getTime();
      if (midTs <= ts) { result = series[mid].rate; lo = mid + 1; }
      else              { hi = mid - 1; }
    }
    return result;
  }

  function tooltipFormatter(params) {
    if (!params || !params.length) return '';
    var ts   = params[0].axisValue;
    var date = new Date(ts).toISOString().slice(0, 10);
    var items = SERIES_DEFS.map(function(def) {
      return { label: def.label, color: def.color, rate: findStepRate(rates[def.key], ts) };
    }).filter(function(it) { return it.rate !== null; });
    // Sort by rate descending so the tooltip lists series top-to-bottom in
    // the same vertical order the lines stack on the chart at this x.
    items.sort(function(a, b) { return b.rate - a.rate; });
    var html = '<strong>' + date + '</strong><br>';
    items.forEach(function(it) {
      html += '<span style="color:' + it.color + '">&#9679;</span> ' + it.label
           + ': <strong>' + it.rate.toFixed(2) + '%</strong><br>';
    });
    return html;
  }

  // ── Chart update helpers ───────────────────────────────────────────────
  function updateStatBoxes() {
    var meta = rates.meta;
    document.getElementById('policyRate').textContent = meta.policy_current.toFixed(2) + '%';
    document.getElementById('policyDate').textContent = 'as of ' + meta.policy_current_date;
    document.getElementById('primeRate').textContent  = meta.prime_current.toFixed(2) + '%';
    document.getElementById('primeDate').textContent  = 'as of ' + meta.prime_current_date;
  }

  function updateChartSeries() {
    chart.setOption({
      series: SERIES_DEFS.map(function(def) {
        return { data: rates[def.key].map(function(d) { return [d.date, d.rate]; }) };
      }),
    });
  }

  // ── Per-region state persistence ───────────────────────────────────────
  function loadState() {
    try {
      var raw = localStorage.getItem(STATE_KEY);
      return raw ? JSON.parse(raw) : null;
    } catch (e) {
      localStorage.removeItem(STATE_KEY);
      return null;
    }
  }
  var saveStateTimer = null;
  function saveState(patch) {
    clearTimeout(saveStateTimer);
    saveStateTimer = setTimeout(function() {
      var current = loadState() || {};
      for (var k in patch) current[k] = patch[k];
      try { localStorage.setItem(STATE_KEY, JSON.stringify(current)); } catch (e) {}
    }, 250);
  }

  // ── Init ───────────────────────────────────────────────────────────────
  (async function init() {
    try {
      var results = await Promise.all([
        fetch('data/rates.json').then(function(r)  { if (!r.ok) throw new Error(r.status); return r.json(); }),
        fetch('data/events.json').then(function(r) { if (!r.ok) throw new Error(r.status); return r.json(); }),
      ]);
      rates  = results[0];
      events = results[1];
    } catch (e) {
      document.getElementById('error').style.display = 'block';
      console.error('Data load failed:', e);
      return;
    }

    updateStatBoxes();

    var pal = chartPalette();
    SERIES_DEFS = buildSeriesDefs(pal);

    // Default x-axis window: past 18 months
    var defaultStart = (function() {
      var d = new Date();
      d.setMonth(d.getMonth() - 18);
      return d.toISOString().slice(0, 10);
    })();

    function makeMarkPoint(color) {
      var p = chartPalette();
      var labelBase = {
        show: true,
        fontSize: 11,
        fontWeight: 'bold',
        color: color,
        formatter: mpFormatter,
        backgroundColor: p.bg,
        borderColor: p.border,
        borderWidth: 1,
        padding: [2, 5],
        borderRadius: 3,
      };
      return {
        data: [
          {
            type: 'max', name: 'Max',
            symbol: 'circle', symbolSize: 5, itemStyle: { color: color },
            label: Object.assign({}, labelBase, { position: 'top', offset: [0, -6] }),
          },
          {
            type: 'min', name: 'Min',
            symbol: 'circle', symbolSize: 5, itemStyle: { color: color },
            label: Object.assign({}, labelBase, { position: 'bottom', offset: [0, 6] }),
          },
        ],
      };
    }

    chart = echarts.init(document.getElementById('chart'));

    chart.setOption({
      backgroundColor: 'transparent',
      textStyle: { color: pal.text },
      tooltip: {
        trigger: 'axis',
        backgroundColor: pal.tooltip,
        borderColor: pal.border,
        textStyle: { color: pal.text },
        axisPointer: { type: 'cross', crossStyle: { color: pal.muted } },
        formatter: tooltipFormatter,
      },
      legend: {
        data: SERIES_DEFS.map(function(d) { return d.label; }),
        top: 8, itemGap: 24,
        textStyle: { color: pal.text },
      },
      grid: { left: 8, right: 8, top: 48, bottom: 65, containLabel: true },
      xAxis: {
        type: 'time',
        boundaryGap: false,
        axisLine:  { lineStyle: { color: pal.axis } },
        axisTick:  { lineStyle: { color: pal.axis } },
        axisLabel: { fontSize: 13, color: pal.text },
        splitLine: { show: false },
      },
      yAxis: {
        type: 'value',
        min: 0,
        max: function(value) {
          return value.max > 0 ? Math.ceil(value.max * 1.12) : 25;
        },
        axisLabel: { formatter: '{value}%', fontSize: 13, color: pal.text },
        axisTick:  { lineStyle: { color: pal.axis } },
        splitLine: { lineStyle: { color: pal.grid } },
        axisLine:  { show: true, lineStyle: { color: pal.axis } },
      },
      dataZoom: [
        {
          type: 'inside',
          xAxisIndex: [0],
          filterMode: 'filter',
          startValue: defaultStart,
          zoomOnMouseWheel: true,
          moveOnMouseWheel: false,
          moveOnMouseMove: true,
          moveOnTouchMove: true,
        },
        {
          type: 'slider',
          xAxisIndex: [0],
          filterMode: 'filter',
          startValue: defaultStart,
          bottom: 8, height: 22,
          borderColor: pal.border,
          fillerColor: 'rgba(128,128,128,0.12)',
          handleStyle: { color: pal.link },
          textStyle: { color: pal.muted },
        },
      ],
      series: SERIES_DEFS.map(function(def) {
        return {
          name: def.label,
          type: 'line', step: 'end',
          // Initial data is the raw series. updateDots() runs on init and on
          // every zoom change, and rewrites this array with synthetic edge
          // points at the window boundaries (see buildWindowedSeriesData).
          data: rates[def.key].map(function(d) { return [d.date, d.rate]; }),
          lineStyle: { color: def.color, width: 2 },
          itemStyle: { color: def.color },
          symbol: 'none',
          markPoint: makeMarkPoint(def.color),
          markArea: emptyMarkArea,
        };
      }),
    });

    // Events toggle — markArea lives on the first series only (one band per
    // region); the other series always get the empty markArea placeholder.
    function setEventsVisible(v) {
      eventsVisible = v;
      chart.setOption({
        series: SERIES_DEFS.map(function(_, i) {
          if (i === 0) return { markArea: eventsVisible ? buildMarkArea() : emptyMarkArea };
          return { markArea: emptyMarkArea };
        }),
      });
      var btn = document.getElementById('toggleEvents');
      btn.textContent = eventsVisible ? 'Hide Historical Events' : 'Show Historical Events';
      btn.classList.toggle('active', eventsVisible);
      saveState({ eventsVisible: eventsVisible });
    }
    document.getElementById('toggleEvents').addEventListener('click', function() {
      setEventsVisible(!eventsVisible);
    });

    // Reset Zoom button
    document.getElementById('resetZoom').addEventListener('click', function() {
      var start = new Date();
      start.setMonth(start.getMonth() - 18);
      var startMs = start.getTime();
      var endMs   = Date.now();
      chart.dispatchAction({ type: 'dataZoom', dataZoomIndex: 0, startValue: startMs, endValue: endMs });
      chart.dispatchAction({ type: 'dataZoom', dataZoomIndex: 1, startValue: startMs, endValue: endMs });
    });

    // Show dots only when zoomed inside a 3-year window
    function applyDotsForRange(rangeMs) {
      var shouldShow = rangeMs > 0 && rangeMs < THREE_YEARS_MS;
      if (shouldShow === dotsVisible) return;
      dotsVisible = shouldShow;
      chart.setOption({
        series: SERIES_DEFS.map(function() {
          return { symbol: dotsVisible ? 'circle' : 'none', symbolSize: 5 };
        }),
      });
    }
    function toMs(v, fallback) {
      if (v == null) return fallback;
      if (typeof v === 'number') return v;
      var t = new Date(v).getTime();
      return isNaN(t) ? fallback : t;
    }
    // Build a series data array for the visible window [xMinMs, xMaxMs].
    //   - Line stops at the last real observation globally: if the series has
    //     no data after xMax, no right edge extension is added.
    //   - If data exists before the window, a synthetic point at xMin carries
    //     the last-known rate in from the left, so the step line enters at
    //     the correct level.
    //   - If data exists after xMax, a synthetic point at xMax extends the
    //     step line to the right edge at the most recent in-window value.
    // Synthetic edge points carry `symbol: 'none'` so the dots-on toggle
    // never marks them as observations.
    function buildWindowedSeriesData(raw, xMinMs, xMaxMs) {
      if (!raw || raw.length === 0) return [];
      var data = raw.map(function(d) { return [d.date, d.rate]; });
      var prevPoint = null, hasNext = false, lastRateInWindow = null;
      for (var i = 0; i < raw.length; i++) {
        var ts = new Date(raw[i].date).getTime();
        if (ts < xMinMs) {
          prevPoint = raw[i];
        } else if (ts > xMaxMs) {
          hasNext = true;
          break;
        } else {
          lastRateInWindow = raw[i].rate;
        }
      }
      // Use the window's numeric timestamps directly for the synthetic edges,
      // NOT a truncated "YYYY-MM-DD" string. Truncation pushes the point back
      // to UTC midnight, which can land a few hours before xMinMs and get
      // filtered out when the user has zoomed to a mid-day boundary.
      if (prevPoint) {
        data.unshift({ value: [xMinMs, prevPoint.rate], symbol: 'none' });
      }
      if (hasNext) {
        var r = lastRateInWindow != null ? lastRateInWindow
              : (prevPoint ? prevPoint.rate : null);
        if (r != null) {
          data.push({ value: [xMaxMs, r], symbol: 'none' });
        }
      }
      return data;
    }
    function applyWindowedSeries(xMinMs, xMaxMs) {
      chart.setOption({
        series: SERIES_DEFS.map(function(def) {
          return { data: buildWindowedSeriesData(rates[def.key], xMinMs, xMaxMs) };
        }),
      });
    }
    function updateDots() {
      var opt = chart.getOption();
      var dz  = opt.dataZoom && opt.dataZoom[0];
      if (!dz) return;
      var start = toMs(dz.startValue, 0);
      var end   = toMs(dz.endValue,   Date.now());
      applyWindowedSeries(start, end);
      applyDotsForRange(end - start);
      saveState({ zoomStartMs: start, zoomEndMs: end });
    }
    chart.on('datazoom', updateDots);

    // Initial render doesn't emit a datazoom event, so evaluate the default
    // window (defaultStart → today) directly to set edges and dot visibility.
    (function initWindow() {
      var startMs = toMs(defaultStart, 0);
      var endMs   = Date.now();
      applyWindowedSeries(startMs, endMs);
      applyDotsForRange(endMs - startMs);
    })();

    // Restore saved per-region state, if any.
    var saved = loadState();
    if (saved) {
      if (typeof saved.zoomStartMs === 'number' && typeof saved.zoomEndMs === 'number'
          && saved.zoomEndMs > saved.zoomStartMs) {
        chart.dispatchAction({ type: 'dataZoom', dataZoomIndex: 0,
                               startValue: saved.zoomStartMs, endValue: saved.zoomEndMs });
        chart.dispatchAction({ type: 'dataZoom', dataZoomIndex: 1,
                               startValue: saved.zoomStartMs, endValue: saved.zoomEndMs });
        applyDotsForRange(saved.zoomEndMs - saved.zoomStartMs);
      }
      if (saved.eventsVisible === true) {
        setEventsVisible(true);
      }
    }

    // Theme recolor: merge-apply a palette-derived option when data-theme flips.
    function applyThemeToChart() {
      if (!chart) return;
      var p = chartPalette();
      // Refresh the color cache on each series def so tooltip items pick up
      // the new theme colors too.
      SERIES_DEFS = SERIES_DEFS.map(function(d) {
        return { key: d.key, label: d.label, color: p[d.key] };
      });
      chart.setOption(themedOption(p), false);
      chart.setOption({
        series: SERIES_DEFS.map(function(def) {
          return { markPoint: makeMarkPoint(def.color) };
        }),
      }, false);
      if (eventsVisible) {
        chart.setOption({
          series: SERIES_DEFS.map(function(_, i) {
            return { markArea: i === 0 ? buildMarkArea() : emptyMarkArea };
          }),
        }, false);
      }
    }
    new MutationObserver(applyThemeToChart).observe(
      document.documentElement,
      { attributes: true, attributeFilter: ['data-theme'] }
    );

    window.addEventListener('resize', function() { chart.resize(); });
  })();
  </script>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# Landing page template — tiny two-card grid, reuses site_kit palette.
# ---------------------------------------------------------------------------
LANDING_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en" data-theme="dark">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Interest Rate Tracker</title>
  <link rel="stylesheet" href="https://k1monfared.github.io/site_kit/css/base.css">
  <script src="https://k1monfared.github.io/site_kit/js/theme.js"></script>
  <style>
    :root {
      --card-bg: #16213e;
      --card-border: #30363d;
      --muted: #8b949e;
      --btn-hover-bg: #1f2a44;
    }
    [data-theme="light"] {
      --card-bg: #ffffff;
      --card-border: #e1e4e8;
      --muted: #6a737d;
      --btn-hover-bg: #f0f0f0;
    }

    *, *::before, *::after { box-sizing: border-box; }
    body {
      margin: 0; padding: 40px 20px;
      background: var(--bg); color: var(--text);
      transition: background .2s, color .2s;
      min-height: 100vh;
      display: flex; flex-direction: column;
    }
    h1 { text-align: center; margin: 0 0 8px; font-size: 1.8rem; }
    .tagline {
      text-align: center; color: var(--muted); font-size: 14px;
      max-width: 560px; margin: 0 auto 32px;
    }
    .cards {
      display: grid; gap: 20px;
      grid-template-columns: repeat(auto-fit, minmax(260px, 1fr));
      max-width: 780px; width: 100%; margin: 0 auto;
    }
    a.card, a.card:link, a.card:visited, a.card:hover, a.card:focus, a.card:active {
      color: var(--text);
      text-decoration: none;
    }
    .card {
      display: block;
      background: var(--card-bg); border: 1px solid var(--card-border);
      border-radius: 12px; padding: 24px;
      transition: transform .15s, border-color .15s, background .15s;
      box-shadow: 0 1px 4px rgba(0,0,0,.18);
    }
    .card:hover {
      transform: translateY(-2px);
      border-color: var(--link);
      background: var(--btn-hover-bg);
    }
    .card .region {
      font-size: 13px; color: var(--muted); text-transform: uppercase;
      letter-spacing: .6px; margin-bottom: 4px;
    }
    .card .headline {
      font-size: 1.2rem; font-weight: 600; margin-bottom: 16px;
    }
    .rate-row {
      display: flex; justify-content: space-between; align-items: baseline;
      margin-bottom: 8px;
    }
    .rate-row .name { color: var(--muted); font-size: 13px; }
    .rate-row .val  { font-weight: 700; font-size: 18px; }
    .rate-row .date { color: var(--muted); font-size: 11px; margin-left: 8px; }
    .card .cta {
      margin-top: 12px; color: var(--link); font-size: 13px;
    }
    .footer {
      margin-top: auto; padding-top: 32px;
      text-align: center; font-size: 11px; color: var(--muted); opacity: .85;
    }
    .footer a { color: var(--link); text-decoration: none; }
    .footer a:hover { text-decoration: underline; }

    #theme-toggle {
      position: fixed; top: 12px; right: 12px;
      width: 36px; height: 36px; border-radius: 50%;
      background: var(--card-bg); border: 1px solid var(--card-border);
      color: var(--text); cursor: pointer; font-size: 16px; line-height: 1;
      display: flex; align-items: center; justify-content: center;
      box-shadow: 0 1px 4px rgba(0,0,0,.2); z-index: 100;
      transition: background .15s, border-color .15s;
    }

    .region-nav {
      position: fixed; top: 16px; right: 60px;
      display: flex; gap: 8px; z-index: 100;
    }
    .flag-link {
      display: block; line-height: 0;
      border-radius: 3px; overflow: hidden;
      border: 1px solid var(--card-border);
      box-shadow: 0 1px 3px rgba(0,0,0,.25);
      transition: transform .15s, border-color .15s;
    }
    .flag-link img { width: 36px; height: auto; display: block; }
    .flag-link:hover { transform: translateY(-1px); border-color: var(--muted); }

    @media (max-width: 640px) {
      body { padding: 24px 14px; }
      h1 { font-size: 1.4rem; padding-right: 44px; text-align: left; }
      .tagline { text-align: left; margin-bottom: 24px; }
      #theme-toggle { top: 8px; right: 8px; width: 32px; height: 32px; font-size: 14px; }
      .region-nav { top: 12px; right: 48px; gap: 4px; }
      .flag-link img { width: 28px; }
    }
  </style>
</head>
<body>
  <nav class="region-nav" aria-label="Region">
    <a href="ca/" class="flag-link" aria-label="Canada" title="Canadian rates"><img src="https://flagcdn.com/ca.svg" alt="CA" width="36"></a>
    <a href="us/" class="flag-link" aria-label="United States" title="US rates"><img src="https://flagcdn.com/us.svg" alt="US" width="36"></a>
  </nav>
  <button id="theme-toggle" aria-label="Toggle theme"><span class="theme-icon"></span></button>

  <h1>Interest Rate Tracker</h1>
  <p class="tagline">Live charts of central-bank and commercial prime rates. Pick a region.</p>

  <div class="cards">
    <a class="card" href="ca/">
      <div class="region">🇨🇦 Canada</div>
      <div class="headline">Bank of Canada</div>
      <div class="rate-row">
        <span class="name">BoC Policy Rate</span>
        <span><span class="val" id="ca-policy">–</span><span class="date" id="ca-policy-date"></span></span>
      </div>
      <div class="rate-row">
        <span class="name">Commercial Prime</span>
        <span><span class="val" id="ca-prime">–</span><span class="date" id="ca-prime-date"></span></span>
      </div>
      <div class="cta">View chart →</div>
    </a>
    <a class="card" href="us/">
      <div class="region">🇺🇸 United States</div>
      <div class="headline">Federal Reserve</div>
      <div class="rate-row">
        <span class="name">Fed Funds</span>
        <span><span class="val" id="us-policy">–</span><span class="date" id="us-policy-date"></span></span>
      </div>
      <div class="rate-row">
        <span class="name">US Bank Prime</span>
        <span><span class="val" id="us-prime">–</span><span class="date" id="us-prime-date"></span></span>
      </div>
      <div class="cta">View chart →</div>
    </a>
  </div>

  <div class="footer">
    Built __BUILD_TIME__
    &mdash; <a href="https://github.com/k1monfared/mortgage_rate_tracker" target="_blank" rel="external noopener">GitHub</a>
    &mdash; <a href="https://k1monfared.github.io/sponsor.html" rel="external noopener">Sponsor</a>
  </div>

  <script>
  function fillCard(slug, data) {
    var m = data && data.meta;
    if (!m) return;
    document.getElementById(slug + '-policy').textContent       = m.policy_current.toFixed(2) + '%';
    document.getElementById(slug + '-policy-date').textContent  = ' ' + m.policy_current_date;
    document.getElementById(slug + '-prime').textContent        = m.prime_current.toFixed(2) + '%';
    document.getElementById(slug + '-prime-date').textContent   = ' ' + m.prime_current_date;
  }
  ['ca', 'us'].forEach(function(slug) {
    fetch(slug + '/data/rates.json')
      .then(function(r) { if (!r.ok) throw new Error(r.status); return r.json(); })
      .then(function(data) { fillCard(slug, data); })
      .catch(function(e) { console.warn('Failed to load ' + slug + ' rates:', e); });
  });
  </script>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# Small static pages used by the subscribe flow (confirmation-sent, subscribed,
# blocked). Rendered once with shared styling; body/heading are substituted.
# ---------------------------------------------------------------------------
STATIC_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en" data-theme="dark">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>__TITLE__ &middot; Mortgage Rate Tracker</title>
  <link rel="stylesheet" href="https://k1monfared.github.io/site_kit/css/base.css">
  <script src="https://k1monfared.github.io/site_kit/js/theme.js"></script>
  <style>
    :root {
      --card-bg: #16213e;
      --card-border: #30363d;
      --muted: #8b949e;
    }
    [data-theme="light"] {
      --card-bg: #ffffff;
      --card-border: #e1e4e8;
      --muted: #6a737d;
    }
    body {
      margin: 0; padding: 40px 20px;
      background: var(--bg); color: var(--text);
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      min-height: 100vh;
      display: flex; flex-direction: column;
      transition: background .2s, color .2s;
    }
    .box {
      background: var(--card-bg); border: 1px solid var(--card-border);
      border-radius: 12px; padding: 32px;
      max-width: 520px; margin: 40px auto 0;
      box-shadow: 0 1px 4px rgba(0,0,0,.18);
    }
    h1 { margin: 0 0 12px; font-size: 1.4rem; }
    p { margin: 0 0 12px; line-height: 1.6; color: var(--muted); }
    p.lede { color: var(--text); }
    a { color: var(--link); }
    a:hover { color: var(--link-hover); }
    .footer {
      margin-top: auto; padding-top: 32px;
      text-align: center; font-size: 11px; color: var(--muted); opacity: .85;
    }
    .footer a { color: var(--link); text-decoration: none; }
    .footer a:hover { text-decoration: underline; }
  </style>
</head>
<body>
  <div class="box">
    <h1>__HEADING__</h1>
    __BODY__
    <p><a href="/mortgage_rate_tracker/">&larr; Back to the tracker</a></p>
  </div>
  <div class="footer">
    <a href="https://github.com/k1monfared/mortgage_rate_tracker" target="_blank" rel="external noopener">GitHub</a>
    &middot; <a href="https://k1monfared.github.io/sponsor.html" rel="external noopener">Sponsor</a>
  </div>
</body>
</html>
"""


STATIC_PAGES = {
    "confirmation-sent": {
        "title":   "Check your email",
        "heading": "Almost there",
        "body": (
            "<p class='lede'>We just sent you a confirmation email. "
            "Click the link inside to activate your subscription.</p>"
            "<p>The link is valid for 24 hours. If you don't see the email, "
            "check your spam folder.</p>"
        ),
    },
    "subscribed": {
        "title":   "Subscribed",
        "heading": "You're subscribed",
        "body": (
            "<p class='lede'>We'll email you only when the rate actually changes.</p>"
            "<p>Every email includes a one-click unsubscribe link.</p>"
        ),
    },
    "blocked": {
        "title":   "Address blocked",
        "heading": "We won't contact you again",
        "body": (
            "<p class='lede'>This address has been blocked from subscribing.</p>"
            "<p>Even if someone tries to subscribe you again later, we will not send "
            "any email to this address. Sorry for the hassle.</p>"
        ),
    },
    "unsubscribed": {
        "title":   "Unsubscribed",
        "heading": "You're unsubscribed",
        "body": (
            "<p class='lede'>We won't email this address again.</p>"
            "<p>If you change your mind, you can resubscribe from the tracker "
            "page at any time.</p>"
        ),
    },
}


def render_static(page_id: str) -> str:
    cfg = STATIC_PAGES[page_id]
    html = STATIC_TEMPLATE
    for token, value in [
        ("__TITLE__",   cfg["title"]),
        ("__HEADING__", cfg["heading"]),
        ("__BODY__",    cfg["body"]),
    ]:
        html = html.replace(token, value)
    return html


# ---------------------------------------------------------------------------
# Region configuration
# ---------------------------------------------------------------------------
REGIONS = [
    {
        "slug": "ca",
        "title": "Canadian Interest Rate Tracker",
        "fetcher": HistoricalRateFetcher(),
        "labels": {"policy": "BoC Policy Rate",
                   "prime":  "Commercial Prime Rate"},
        "source_name": "Bank of Canada Valet API",
        "source_url":  "https://www.bankofcanada.ca/valet/docs",
        "events_file": Path("data/events.json"),
        "meetings_file": None,
    },
    {
        "slug": "us",
        "title": "US Interest Rate Tracker",
        "fetcher": FREDRateFetcher(),
        "labels": {"policy": "Fed Funds Rate",
                   "target": "Fed Target (Upper)",
                   "prime":  "US Bank Prime Rate"},
        "source_name": "Federal Reserve Economic Data (FRED)",
        "source_url":  "https://fred.stlouisfed.org/",
        "events_file": Path("data/us_events.json"),
        "meetings_file": Path("data/us_fomc_meetings.json"),
    },
]


def _load_meeting_dates(meetings_file):
    """Return a set of YYYY-MM-DD strings from a meetings JSON file, or None
    if the file is missing / unreadable."""
    if meetings_file is None or not meetings_file.exists():
        return None
    try:
        with open(meetings_file) as f:
            return set(json.load(f).get("dates", []))
    except (OSError, json.JSONDecodeError) as e:
        print(f"  Warning: could not read {meetings_file}: {e}")
        return None


def _compress_series(df, meeting_dates=None, min_delta=0.0):
    """Keep only rows that carry information for the step chart:

    - the first row (establishes the starting value),
    - the last row (extends the line to 'today'),
    - every rate transition where |rate - prev_kept| > min_delta,
    - every row whose date is in `meeting_dates` (shows a dot on days the
      central bank met, even when the rate didn't change).

    `min_delta` lets series like the Fed Funds effective rate (which wiggles
    daily by a basis point or two) drop noise-level changes while still
    capturing every announcement-day dot.
    """
    if df is None or df.empty:
        return df
    rows = df.reset_index(drop=True)
    keep = [False] * len(rows)
    keep[0] = True
    keep[-1] = True
    prev_kept = float(rows.iloc[0]["rate"])
    for i in range(1, len(rows) - 1):
        rate = float(rows.iloc[i]["rate"])
        date_str = rows.iloc[i]["date"].strftime("%Y-%m-%d")
        is_transition = abs(rate - prev_kept) > min_delta
        is_meeting = meeting_dates is not None and date_str in meeting_dates
        if is_transition or is_meeting:
            keep[i] = True
            prev_kept = rate
    return rows[keep].reset_index(drop=True)


# Per-series compression tuning. Fed Funds effective rate has daily float
# noise (~1bp); the others are piecewise-constant by construction.
_SERIES_MIN_DELTA = {
    "policy": 0.01,
    "target": 0.0,
    "prime":  0.0,
}


def _series_to_json(df):
    return [
        {"date": row["date"].strftime("%Y-%m-%d"), "rate": float(row["rate"])}
        for _, row in df.iterrows()
    ]


def write_rates_json(cfg, out_path, build_time):
    """Serialize a region's rates + metadata to JSON.

    Always writes `policy` and `prime` arrays. If the region's fetcher also
    exposes a `target` series (currently US only, FOMC target upper bound),
    that array is written too along with `target_current` meta fields.

    Daily series are compressed to transitions + central-bank-meeting days
    (when a meetings file is configured for the region), so the step chart
    renders with dots on rate-change days AND on announcement days where the
    rate was held, without 10k+ daily points weighing down the payload.
    """
    fetcher  = cfg["fetcher"]
    policy_df = fetcher.load_rate_data("policy")
    prime_df  = fetcher.load_rate_data("prime")
    if policy_df is None or prime_df is None or policy_df.empty or prime_df.empty:
        raise RuntimeError(f"Missing data for region {cfg['slug']}")

    latest_policy = policy_df.iloc[-1]
    latest_prime  = prime_df.iloc[-1]

    # Only compress regions with daily data (flagged by having a meetings_file).
    # Monthly/weekly series (e.g. Canada) pass through as-is so the chart keeps
    # a dot on every reported data point.
    if cfg.get("meetings_file"):
        meeting_dates = _load_meeting_dates(cfg["meetings_file"])
        policy_compressed = _compress_series(policy_df, meeting_dates,
                                             _SERIES_MIN_DELTA["policy"])
        prime_compressed  = _compress_series(prime_df,  meeting_dates,
                                             _SERIES_MIN_DELTA["prime"])
    else:
        meeting_dates = None
        policy_compressed = policy_df
        prime_compressed  = prime_df

    payload = {
        "policy": _series_to_json(policy_compressed),
        "prime":  _series_to_json(prime_compressed),
        "meta": {
            "policy_current":      float(latest_policy["rate"]),
            "policy_current_date": latest_policy["date"].strftime("%Y-%m-%d"),
            "prime_current":       float(latest_prime["rate"]),
            "prime_current_date":  latest_prime["date"].strftime("%Y-%m-%d"),
            "built": build_time,
            "labels": cfg["labels"],
        },
    }

    has_target = "target" in getattr(fetcher, "SERIES", {})
    if has_target:
        target_df = fetcher.load_rate_data("target")
        if target_df is not None and not target_df.empty:
            if cfg.get("meetings_file"):
                target_compressed = _compress_series(target_df, meeting_dates,
                                                     _SERIES_MIN_DELTA["target"])
            else:
                target_compressed = target_df
            payload["target"] = _series_to_json(target_compressed)
            latest_target = target_df.iloc[-1]
            payload["meta"]["target_current"]      = float(latest_target["rate"])
            payload["meta"]["target_current_date"] = latest_target["date"].strftime("%Y-%m-%d")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w") as f:
        json.dump(payload, f, separators=(",", ":"))
    counts = f"{len(payload['policy'])} policy, {len(payload['prime'])} prime"
    if "target" in payload:
        counts += f", {len(payload['target'])} target"
    print(f"Wrote {out_path} ({counts} records)")


def render_region(cfg, build_time):
    slug = cfg["slug"]
    substitutions = {
        "__TITLE__":          cfg["title"],
        "__LABEL_POLICY__":   cfg["labels"]["policy"],
        "__LABEL_PRIME__":    cfg["labels"]["prime"],
        "__LABEL_TARGET__":   cfg["labels"].get("target", ""),
        "__SOURCE_NAME__":    cfg["source_name"],
        "__SOURCE_URL__":     cfg["source_url"],
        "__SLUG__":           slug,
        "__CA_ACTIVE__":      "active" if slug == "ca" else "",
        "__US_ACTIVE__":      "active" if slug == "us" else "",
        "__CA_CHECKED__":     "checked" if slug == "ca" else "",
        "__US_CHECKED__":     "checked" if slug == "us" else "",
        "__BUILD_TIME__":     build_time,
        "__SUBSCRIBE_URL__":  SUBSCRIBE_PROXY_URL,
    }
    html = REGION_TEMPLATE
    for token, value in substitutions.items():
        html = html.replace(token, value)
    return html


def render_landing(build_time):
    return LANDING_TEMPLATE.replace("__BUILD_TIME__", build_time)


def build_site():
    site_dir = Path("site")
    site_dir.mkdir(exist_ok=True)
    build_time = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    for cfg in REGIONS:
        slug = cfg["slug"]
        print(f"\n{'=' * 70}\nBuilding region: {slug}\n{'=' * 70}")
        fetcher = cfg["fetcher"]
        for series_key in getattr(fetcher, "SERIES", {}).keys() or ("policy", "prime"):
            fetcher.update_incremental(series_key)

        out_dir = site_dir / slug
        out_dir.mkdir(parents=True, exist_ok=True)
        data_out = out_dir / "data"
        data_out.mkdir(exist_ok=True)

        write_rates_json(cfg, data_out / "rates.json", build_time)

        events_src = cfg["events_file"]
        if events_src.exists():
            shutil.copy(events_src, data_out / "events.json")
            print(f"Copied {events_src} → {data_out / 'events.json'}")
        else:
            print(f"WARNING: {events_src} not found — writing empty events file")
            with open(data_out / "events.json", "w") as f:
                json.dump({"regions": []}, f)

        html = render_region(cfg, build_time)
        (out_dir / "index.html").write_text(html, encoding="utf-8")
        print(f"Built {out_dir / 'index.html'}")

    (site_dir / "index.html").write_text(render_landing(build_time), encoding="utf-8")
    print(f"Built landing page: {site_dir / 'index.html'}")

    for page_id in STATIC_PAGES:
        page_dir = site_dir / page_id
        page_dir.mkdir(parents=True, exist_ok=True)
        (page_dir / "index.html").write_text(render_static(page_id), encoding="utf-8")
        print(f"Built static page: {page_dir / 'index.html'}")


if __name__ == "__main__":
    build_site()
