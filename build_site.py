"""
Build script for GitHub Pages site.

Data strategy:
  - data/boc_policy_rate.csv and data/commercial_prime_rate.csv are committed to the repo
    with full historical data.  On each build, only new records since the last stored date
    are fetched from the BoC API (incremental update).
  - data/events.json is manually maintained and committed.

Outputs:
  - site/data/rates.json   — policy + prime arrays, current-value meta
  - site/data/events.json  — copy of data/events.json
  - site/index.html        — ECharts-based interactive page
"""

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

from historical_rates import HistoricalRateFetcher


# ---------------------------------------------------------------------------
# HTML template — uses __BUILD_TIME__ as the only Python-side substitution.
# All other dynamic content (rates, events) is loaded at runtime via fetch().
# ---------------------------------------------------------------------------
HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en" data-theme="dark">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Canadian Interest Rate Tracker</title>
  <link rel="stylesheet" href="https://k1monfared.github.io/site_kit/css/base.css">
  <script src="https://k1monfared.github.io/site_kit/js/theme.js"></script>
  <script src="https://cdn.jsdelivr.net/npm/echarts@5/dist/echarts.min.js"></script>
  <style>
    /* Page-specific palette, layered on top of site_kit's base palette.
       Dark defaults live on :root; light overrides live under [data-theme="light"]. */
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

    @media (max-width: 640px) {
      body { padding: 12px 10px; }
      h1 { font-size: 1.25rem; margin-bottom: 14px; padding-right: 44px; }
      .stats { gap: 10px; }
      .stat-box { padding: 10px 14px; min-width: 130px; }
      .stat-box .value { font-size: 24px; }
      .btn { font-size: 13px; padding: 9px 14px; }
      #chart { height: 360px; }
      #theme-toggle { top: 8px; right: 8px; width: 32px; height: 32px; font-size: 14px; }
    }
  </style>
</head>
<body>
  <button id="theme-toggle" aria-label="Toggle theme"><span class="theme-icon"></span></button>

  <h1>Canadian Interest Rate Tracker</h1>

  <div class="stats">
    <div class="stat-box">
      <div class="label">BoC Policy Rate</div>
      <div class="value" id="policyRate">–</div>
      <div class="as-of" id="policyDate"></div>
    </div>
    <div class="stat-box">
      <div class="label">Commercial Prime Rate</div>
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
    <p>The <strong>BoC Policy Rate</strong> is the overnight interest rate target set by the
    Bank of Canada at up to eight scheduled announcements per year; it directly steers
    borrowing costs across the economy. The <strong>Commercial Prime Rate</strong> is the
    benchmark lending rate used by major Canadian banks, historically running about
    2–2.5 percentage points above the Policy Rate. Hover over the chart to read exact
    values for any date. Zoom in to under three years to see individual announcement
    dots on the line.</p>
  </div>

  <div class="footer">
    Data source: <a href="https://www.bankofcanada.ca/valet/docs" target="_blank" rel="external noopener">Bank of Canada Valet API</a>
    &mdash; Built __BUILD_TIME__
    &mdash; <a href="https://k1monfared.github.io/sponsor.html" rel="external noopener">Sponsor</a>
  </div>

  <script>
  // ── Module-level state ─────────────────────────────────────────────────
  var rates, events, chart, eventsVisible = false, dotsVisible = false;
  var THREE_YEARS_MS = 3 * 365.25 * 24 * 3600 * 1000;

  // ── BoC series codes ───────────────────────────────────────────────────
  var BOC_POLICY = 'V122530';
  var BOC_PRIME  = 'V80691311';

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

  // Binary search: last entry in sorted series whose date <= ts (ms).
  // Returns the rate value, or null if ts is before all data.
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
      html += '<span style="color:' + p.policy + '">&#9679;</span> BoC Policy Rate: <strong>'
            + pRate.toFixed(2) + '%</strong><br>';
    if (qRate !== null)
      html += '<span style="color:' + p.prime + '">&#9679;</span> Commercial Prime Rate: <strong>'
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

  // ── Fetch new records from BoC JSON API ────────────────────────────────
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
      fetchBoCJson(BOC_POLICY, policyStart, end),
      fetchBoCJson(BOC_PRIME,  primeStart,  end),
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

    // markPoint config: no symbol, text label above (max) / below (min), card bg to avoid overlap
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
        data: ['BoC Policy Rate', 'Commercial Prime Rate'],
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
          name: 'BoC Policy Rate',
          type: 'line', step: 'end',
          data: rates.policy.map(function(d) { return [d.date, d.rate]; }),
          lineStyle: { color: pal.policy, width: 2 },
          itemStyle: { color: pal.policy },
          symbol: 'none',
          markPoint: makeMarkPoint(pal.policy),
          markArea: emptyMarkArea,
        },
        {
          name: 'Commercial Prime Rate',
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
    document.getElementById('toggleEvents').addEventListener('click', function() {
      eventsVisible = !eventsVisible;
      chart.setOption({
        series: [
          { markArea: eventsVisible ? buildMarkArea() : emptyMarkArea },
          { markArea: emptyMarkArea },
        ],
      });
      this.textContent = eventsVisible ? 'Hide Historical Events' : 'Show Historical Events';
      this.classList.toggle('active', eventsVisible);
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
    function updateDots() {
      var opt = chart.getOption();
      var dz  = opt.dataZoom && opt.dataZoom[0];
      if (!dz) return;
      var rangeMs   = (dz.endValue || 0) - (dz.startValue || 0);
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
    chart.on('datazoom', updateDots);

    // Theme recolor: merge-apply a palette-derived option when data-theme flips.
    // setOption(..., false) merges rather than replaces, so zoom state is preserved.
    function applyThemeToChart() {
      if (!chart) return;
      var p = chartPalette();
      chart.setOption(themedOption(p), false);
      // markPoint labels capture colors at construction time, so rebuild them.
      chart.setOption({
        series: [
          { markPoint: makeMarkPoint(p.policy) },
          { markPoint: makeMarkPoint(p.prime) },
        ],
      }, false);
      // If historical events are currently shown, rebuild their label color too.
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

    // Responsive
    window.addEventListener('resize', function() { chart.resize(); });
  })();
  </script>
</body>
</html>
"""


def build_site():
    site_dir = Path("site")
    data_out = site_dir / "data"
    site_dir.mkdir(exist_ok=True)
    data_out.mkdir(exist_ok=True)

    fetcher = HistoricalRateFetcher()

    # Fetch only new records since last stored date (or full history on first run)
    print("Updating policy rate data...")
    fetcher.update_incremental("policy")
    print("Updating prime rate data...")
    fetcher.update_incremental("prime")

    # Load full data from local CSVs
    policy_df = fetcher.load_rate_data("policy")
    prime_df = fetcher.load_rate_data("prime")

    if policy_df is None or prime_df is None:
        raise RuntimeError("Failed to load rate data from local CSV files")

    policy_df = policy_df.sort_values("date")
    prime_df = prime_df.sort_values("date")

    latest_policy = policy_df.iloc[-1]
    latest_prime = prime_df.iloc[-1]
    build_time = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    # Write site/data/rates.json
    rates_data = {
        "policy": [
            {"date": row["date"].strftime("%Y-%m-%d"), "rate": float(row["rate"])}
            for _, row in policy_df.iterrows()
        ],
        "prime": [
            {"date": row["date"].strftime("%Y-%m-%d"), "rate": float(row["rate"])}
            for _, row in prime_df.iterrows()
        ],
        "meta": {
            "policy_current": float(latest_policy["rate"]),
            "policy_current_date": latest_policy["date"].strftime("%Y-%m-%d"),
            "prime_current": float(latest_prime["rate"]),
            "prime_current_date": latest_prime["date"].strftime("%Y-%m-%d"),
            "built": build_time,
        },
    }
    with open(data_out / "rates.json", "w") as f:
        json.dump(rates_data, f, separators=(",", ":"))
    print(f"Wrote site/data/rates.json ({len(rates_data['policy'])} policy, {len(rates_data['prime'])} prime records)")

    # Copy events.json
    events_src = Path("data") / "events.json"
    if events_src.exists():
        shutil.copy(events_src, data_out / "events.json")
        print("Copied data/events.json → site/data/events.json")
    else:
        print("WARNING: data/events.json not found — writing empty events file")
        with open(data_out / "events.json", "w") as f:
            json.dump({"regions": []}, f)

    # Write site/index.html
    html = HTML_TEMPLATE.replace("__BUILD_TIME__", build_time)
    (site_dir / "index.html").write_text(html, encoding="utf-8")
    print("Site built successfully: site/index.html")


if __name__ == "__main__":
    build_site()
