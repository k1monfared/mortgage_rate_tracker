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
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Canadian Interest Rate Tracker</title>
  <script src="https://cdn.jsdelivr.net/npm/echarts@5/dist/echarts.min.js"></script>
  <style>
    *, *::before, *::after { box-sizing: border-box; }
    body {
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      margin: 0; padding: 20px;
      background: #f5f6fa; color: #333;
    }
    h1 { text-align: center; color: #1a1a2e; margin: 0 0 20px; font-size: 1.6rem; }
    .stats {
      display: flex; gap: 14px; justify-content: center;
      margin-bottom: 16px; flex-wrap: wrap;
    }
    .stat-box {
      background: #fff; border: 1px solid #e0e0e0; border-radius: 10px;
      padding: 14px 28px; text-align: center; min-width: 160px; flex: 1 1 160px; max-width: 260px;
      box-shadow: 0 1px 4px rgba(0,0,0,.06);
    }
    .stat-box .label { font-size: 12px; color: #777; margin-bottom: 4px; text-transform: uppercase; letter-spacing: .5px; }
    .stat-box .value { font-size: 30px; font-weight: 700; color: #1a1a2e; }
    .stat-box .as-of { font-size: 11px; color: #aaa; margin-top: 3px; }
    .controls {
      display: flex; gap: 8px; justify-content: center; align-items: center;
      flex-wrap: wrap; margin-bottom: 12px;
    }
    .btn {
      padding: 9px 18px; border: 1px solid #ccc; border-radius: 6px;
      background: #fff; cursor: pointer; font-size: 14px; color: #444;
      min-height: 42px; touch-action: manipulation;
      transition: background .15s, border-color .15s;
    }
    .btn:hover { background: #f0f0f0; border-color: #aaa; }
    .btn.active { background: #1a1a2e; color: #fff; border-color: #1a1a2e; }
    .btn:disabled { opacity: .55; cursor: default; }
    #refreshStatus { font-size: 12px; color: #777; }
    .chart-wrap {
      background: #fff; border: 1px solid #e0e0e0; border-radius: 10px;
      padding: 8px; box-shadow: 0 1px 4px rgba(0,0,0,.06);
    }
    #chart { width: 100%; height: 560px; }
    .footer {
      text-align: center; font-size: 11px; color: #bbb; margin-top: 14px;
    }
    #error { color: #c00; text-align: center; padding: 20px; display: none; }

    @media (max-width: 640px) {
      body { padding: 12px 10px; }
      h1 { font-size: 1.25rem; margin-bottom: 14px; }
      .stats { gap: 10px; }
      .stat-box { padding: 10px 14px; min-width: 130px; }
      .stat-box .value { font-size: 24px; }
      .btn { font-size: 13px; padding: 9px 14px; }
      #chart { height: 360px; }
    }
  </style>
</head>
<body>
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
    <button class="btn" id="refreshBtn">Refresh Data</button>
    <span id="refreshStatus"></span>
  </div>

  <div class="chart-wrap">
    <div id="chart"></div>
  </div>

  <p id="error">Failed to load chart data. Please try refreshing.</p>

  <div class="footer">
    Data source: <a href="https://www.bankofcanada.ca/valet/docs" target="_blank">Bank of Canada Valet API</a>
    &mdash; Built __BUILD_TIME__
  </div>

  <script>
  // ── Module-level state ─────────────────────────────────────────────────
  var rates, events, chart, eventsVisible = false;

  // ── BoC series codes ───────────────────────────────────────────────────
  var BOC_POLICY = 'V122530';
  var BOC_PRIME  = 'V80691311';

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
    return {
      silent: true,
      label: {
        show: true, position: 'insideTop', distance: 6,
        fontSize: 10, color: '#555', rotate: 90, overflow: 'truncate',
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
    var ts   = params[0].axisValue;
    var date = new Date(ts).toISOString().slice(0, 10);
    var pRate = findStepRate(rates.policy, ts);
    var qRate = findStepRate(rates.prime,  ts);
    var html  = '<strong>' + date + '</strong><br>';
    if (pRate !== null)
      html += '<span style="color:#2E86AB">&#9679;</span> BoC Policy Rate: <strong>'
            + pRate.toFixed(2) + '%</strong><br>';
    if (qRate !== null)
      html += '<span style="color:#A23B72">&#9679;</span> Commercial Prime Rate: <strong>'
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

    // Default x-axis window: past 15 years
    var defaultStart = (function() {
      var d = new Date();
      d.setFullYear(d.getFullYear() - 15);
      return d.toISOString().slice(0, 10);
    })();

    // markPoint config: no symbol, text label above (max) / below (min), white bg to avoid overlap
    function makeMarkPoint(color) {
      var labelBase = {
        show: true,
        fontSize: 11,
        fontWeight: 'bold',
        color: color,
        formatter: mpFormatter,
        backgroundColor: 'rgba(255,255,255,0.82)',
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
      backgroundColor: '#fff',
      tooltip: {
        trigger: 'axis',
        axisPointer: { type: 'cross', crossStyle: { color: '#aaa' } },
        formatter: tooltipFormatter,
      },
      legend: {
        data: ['BoC Policy Rate', 'Commercial Prime Rate'],
        top: 8, itemGap: 24,
      },
      grid: { left: 8, right: 8, top: 48, bottom: 65, containLabel: true },
      xAxis: {
        type: 'time',
        boundaryGap: false,
        axisLine:  { lineStyle: { color: '#999' } },
        axisTick:  { lineStyle: { color: '#999' } },
        axisLabel: { fontSize: 13, color: '#333' },
        splitLine: { show: false },
      },
      yAxis: {
        type: 'value',
        min: 0,
        max: function(value) {
          return value.max > 0 ? Math.ceil(value.max * 1.12) : 25;
        },
        axisLabel: { formatter: '{value}%', fontSize: 13, color: '#333' },
        axisTick:  { lineStyle: { color: '#999' } },
        splitLine: { lineStyle: { color: '#e8e8e8' } },
        axisLine:  { show: true, lineStyle: { color: '#999' } },
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
        },
        {
          type: 'slider',
          xAxisIndex: [0],
          filterMode: 'filter',
          startValue: defaultStart,
          bottom: 8, height: 22,
          borderColor: '#ddd',
          fillerColor: 'rgba(26,26,46,0.08)',
          handleStyle: { color: '#1a1a2e' },
        },
      ],
      series: [
        {
          name: 'BoC Policy Rate',
          type: 'line', step: 'end',
          data: rates.policy.map(function(d) { return [d.date, d.rate]; }),
          lineStyle: { color: '#2E86AB', width: 2 },
          itemStyle: { color: '#2E86AB' },
          symbol: 'none',
          markPoint: makeMarkPoint('#2E86AB'),
          markArea: emptyMarkArea,
        },
        {
          name: 'Commercial Prime Rate',
          type: 'line', step: 'end',
          data: rates.prime.map(function(d) { return [d.date, d.rate]; }),
          lineStyle: { color: '#A23B72', width: 2 },
          itemStyle: { color: '#A23B72' },
          symbol: 'none',
          markPoint: makeMarkPoint('#A23B72'),
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
