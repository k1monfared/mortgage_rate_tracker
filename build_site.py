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
    <button class="btn" id="refreshBtn">Refresh Data</button>
    <span id="refreshStatus"></span>
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

  // ── Region / data-source config (substituted by build_site.py) ─────────
  var SLUG         = '__SLUG__';
  var REFRESH_API  = '__REFRESH_API__';   // 'boc' or 'fred'
  var SERIES_POLICY = '__SERIES_POLICY__';
  var SERIES_PRIME  = '__SERIES_PRIME__';
  var LABEL_POLICY  = '__LABEL_POLICY__';
  var LABEL_PRIME   = '__LABEL_PRIME__';
  var STATE_KEY    = 'tracker.state.' + SLUG;

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
      prime:    v('--accent-prime',   '#d2a8ff'),
    };
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
      series: [
        {
          lineStyle: { color: p.policy },
          itemStyle: { color: p.policy },
          markPoint: { label: { color: p.policy, backgroundColor: p.bg, borderColor: p.border } },
        },
        {
          lineStyle: { color: p.prime },
          itemStyle: { color: p.prime },
          markPoint: { label: { color: p.prime, backgroundColor: p.bg, borderColor: p.border } },
        },
      ],
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
    var lo = 0, hi = series.length - 1, result = null;
    while (lo <= hi) {
      var mid = (lo + hi) >> 1;
      var midTs = new Date(series[mid].date + 'T12:00:00Z').getTime();
      if (midTs <= ts) { result = series[mid].rate; lo = mid + 1; }
      else              { hi = mid - 1; }
    }
    return result;
  }

  function tooltipFormatter(params) {
    if (!params || !params.length) return '';
    var p = chartPalette();
    var ts   = params[0].axisValue;
    var date = new Date(ts).toISOString().slice(0, 10);
    var pRate = findStepRate(rates.policy, ts);
    var qRate = findStepRate(rates.prime,  ts);
    var html  = '<strong>' + date + '</strong><br>';
    if (pRate !== null)
      html += '<span style="color:' + p.policy + '">&#9679;</span> ' + LABEL_POLICY + ': <strong>'
            + pRate.toFixed(2) + '%</strong><br>';
    if (qRate !== null)
      html += '<span style="color:' + p.prime + '">&#9679;</span> ' + LABEL_PRIME + ': <strong>'
            + qRate.toFixed(2) + '%</strong><br>';
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
      series: [
        { data: rates.policy.map(function(d) { return [d.date, d.rate]; }) },
        { data: rates.prime.map(function(d)  { return [d.date, d.rate]; }) },
      ],
    });
  }

  // ── Fetch new records: BoC JSON or FRED CSV ─────────────────────────────
  function fetchBoCJson(series, startDate, endDate) {
    var url = 'https://www.bankofcanada.ca/valet/observations/'
              + series + '/json?start_date=' + startDate + '&end_date=' + endDate;
    return fetch(url)
      .then(function(r) {
        if (!r.ok) throw new Error('BoC API returned ' + r.status);
        return r.json();
      })
      .then(function(data) {
        var obs = data.observations || [];
        return obs
          .filter(function(o) { return o[series] && o[series].v != null; })
          .map(function(o) { return { date: o.d, rate: parseFloat(o[series].v) }; })
          .filter(function(d) { return !isNaN(d.rate); });
      });
  }

  function fetchFREDCsv(series, startDate, endDate) {
    var url = 'https://fred.stlouisfed.org/graph/fredgraph.csv?id=' + series
              + '&cosd=' + startDate + '&coed=' + endDate;
    return fetch(url)
      .then(function(r) {
        if (!r.ok) throw new Error('FRED returned ' + r.status);
        return r.text();
      })
      .then(function(text) {
        var lines = text.trim().split('\n');
        var out = [];
        for (var i = 1; i < lines.length; i++) {
          var parts = lines[i].split(',');
          if (parts.length < 2) continue;
          var v = parseFloat(parts[1]);
          if (isNaN(v)) continue;   // skips "." missing-value rows
          out.push({ date: parts[0], rate: v });
        }
        return out;
      });
  }

  function fetchSeriesRange(series, startDate, endDate) {
    return REFRESH_API === 'fred'
      ? fetchFREDCsv(series, startDate, endDate)
      : fetchBoCJson(series, startDate, endDate);
  }

  // ── Refresh button handler ─────────────────────────────────────────────
  function refreshData() {
    var btn    = document.getElementById('refreshBtn');
    var status = document.getElementById('refreshStatus');
    btn.disabled = true;
    btn.textContent = 'Refreshing…';
    status.textContent = '';

    var policyStart = nextDay(rates.policy[rates.policy.length - 1].date);
    var primeStart  = nextDay(rates.prime[rates.prime.length - 1].date);
    var end = today();

    Promise.all([
      fetchSeriesRange(SERIES_POLICY, policyStart, end),
      fetchSeriesRange(SERIES_PRIME,  primeStart,  end),
    ])
    .then(function(results) {
      var newPolicy = results[0];
      var newPrime  = results[1];
      var total = newPolicy.length + newPrime.length;

      if (total === 0) {
        status.textContent = 'Already up to date.';
      } else {
        rates.policy = rates.policy.concat(newPolicy);
        rates.prime  = rates.prime.concat(newPrime);

        if (newPolicy.length > 0) {
          var lp = newPolicy[newPolicy.length - 1];
          rates.meta.policy_current      = lp.rate;
          rates.meta.policy_current_date = lp.date;
        }
        if (newPrime.length > 0) {
          var lpr = newPrime[newPrime.length - 1];
          rates.meta.prime_current      = lpr.rate;
          rates.meta.prime_current_date = lpr.date;
        }

        updateStatBoxes();
        updateChartSeries();
        status.textContent = '✓ Added ' + total + ' new record' + (total > 1 ? 's' : '') + '.';
      }

      btn.textContent = 'Refresh Data';
      btn.disabled = false;
    })
    .catch(function(e) {
      console.error('Refresh failed:', e);
      status.textContent = 'Error fetching data — check console.';
      btn.textContent = 'Refresh Data';
      btn.disabled = false;
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

    var pal = chartPalette();
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
        data: [LABEL_POLICY, LABEL_PRIME],
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
      series: [
        {
          name: LABEL_POLICY,
          type: 'line', step: 'end',
          data: rates.policy.map(function(d) { return [d.date, d.rate]; }),
          lineStyle: { color: pal.policy, width: 2 },
          itemStyle: { color: pal.policy },
          symbol: 'none',
          markPoint: makeMarkPoint(pal.policy),
          markArea: emptyMarkArea,
        },
        {
          name: LABEL_PRIME,
          type: 'line', step: 'end',
          data: rates.prime.map(function(d) { return [d.date, d.rate]; }),
          lineStyle: { color: pal.prime, width: 2 },
          itemStyle: { color: pal.prime },
          symbol: 'none',
          markPoint: makeMarkPoint(pal.prime),
          markArea: emptyMarkArea,
        },
      ],
    });

    // Events toggle
    function setEventsVisible(v) {
      eventsVisible = v;
      chart.setOption({
        series: [
          { markArea: eventsVisible ? buildMarkArea() : emptyMarkArea },
          { markArea: emptyMarkArea },
        ],
      });
      var btn = document.getElementById('toggleEvents');
      btn.textContent = eventsVisible ? 'Hide Historical Events' : 'Show Historical Events';
      btn.classList.toggle('active', eventsVisible);
      saveState({ eventsVisible: eventsVisible });
    }
    document.getElementById('toggleEvents').addEventListener('click', function() {
      setEventsVisible(!eventsVisible);
    });

    // Refresh button
    document.getElementById('refreshBtn').addEventListener('click', refreshData);

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
        series: [
          { symbol: dotsVisible ? 'circle' : 'none', symbolSize: 5 },
          { symbol: dotsVisible ? 'circle' : 'none', symbolSize: 5 },
        ],
      });
    }
    function toMs(v, fallback) {
      if (v == null) return fallback;
      if (typeof v === 'number') return v;
      var t = new Date(v).getTime();
      return isNaN(t) ? fallback : t;
    }
    function updateDots() {
      var opt = chart.getOption();
      var dz  = opt.dataZoom && opt.dataZoom[0];
      if (!dz) return;
      var start = toMs(dz.startValue, 0);
      var end   = toMs(dz.endValue,   Date.now());
      applyDotsForRange(end - start);
      saveState({ zoomStartMs: start, zoomEndMs: end });
    }
    chart.on('datazoom', updateDots);

    // Initial render doesn't emit a datazoom event, so evaluate the default
    // window (defaultStart → today) directly to set dot visibility.
    applyDotsForRange(Date.now() - toMs(defaultStart, 0));

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
      chart.setOption(themedOption(p), false);
      chart.setOption({
        series: [
          { markPoint: makeMarkPoint(p.policy) },
          { markPoint: makeMarkPoint(p.prime) },
        ],
      }, false);
      if (eventsVisible) {
        chart.setOption({
          series: [ { markArea: buildMarkArea() }, { markArea: emptyMarkArea } ],
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
        "series":      {"policy": "V122530", "prime": "V80691311"},
        "refresh_api": "boc",
    },
    {
        "slug": "us",
        "title": "US Interest Rate Tracker",
        "fetcher": FREDRateFetcher(),
        "labels": {"policy": "Fed Funds Rate",
                   "prime":  "US Bank Prime Rate"},
        "source_name": "Federal Reserve Economic Data (FRED)",
        "source_url":  "https://fred.stlouisfed.org/",
        "events_file": Path("data/us_events.json"),
        "series":      {"policy": "DFF", "prime": "DPRIME"},
        "refresh_api": "fred",
    },
]


def write_rates_json(cfg, out_path, build_time):
    """Serialize a region's rates + metadata to JSON."""
    fetcher  = cfg["fetcher"]
    policy_df = fetcher.load_rate_data("policy")
    prime_df  = fetcher.load_rate_data("prime")
    if policy_df is None or prime_df is None or policy_df.empty or prime_df.empty:
        raise RuntimeError(f"Missing data for region {cfg['slug']}")

    latest_policy = policy_df.iloc[-1]
    latest_prime  = prime_df.iloc[-1]

    payload = {
        "policy": [
            {"date": row["date"].strftime("%Y-%m-%d"), "rate": float(row["rate"])}
            for _, row in policy_df.iterrows()
        ],
        "prime": [
            {"date": row["date"].strftime("%Y-%m-%d"), "rate": float(row["rate"])}
            for _, row in prime_df.iterrows()
        ],
        "meta": {
            "policy_current":      float(latest_policy["rate"]),
            "policy_current_date": latest_policy["date"].strftime("%Y-%m-%d"),
            "prime_current":       float(latest_prime["rate"]),
            "prime_current_date":  latest_prime["date"].strftime("%Y-%m-%d"),
            "built": build_time,
            "labels": cfg["labels"],
        },
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w") as f:
        json.dump(payload, f, separators=(",", ":"))
    print(f"Wrote {out_path} ({len(payload['policy'])} policy, {len(payload['prime'])} prime records)")


def render_region(cfg, build_time):
    slug = cfg["slug"]
    substitutions = {
        "__TITLE__":          cfg["title"],
        "__LABEL_POLICY__":   cfg["labels"]["policy"],
        "__LABEL_PRIME__":    cfg["labels"]["prime"],
        "__SOURCE_NAME__":    cfg["source_name"],
        "__SOURCE_URL__":     cfg["source_url"],
        "__SERIES_POLICY__":  cfg["series"]["policy"],
        "__SERIES_PRIME__":   cfg["series"]["prime"],
        "__SLUG__":           slug,
        "__REFRESH_API__":    cfg["refresh_api"],
        "__CA_ACTIVE__":      "active" if slug == "ca" else "",
        "__US_ACTIVE__":      "active" if slug == "us" else "",
        "__BUILD_TIME__":     build_time,
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
        fetcher.update_incremental("policy")
        fetcher.update_incremental("prime")

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


if __name__ == "__main__":
    build_site()
