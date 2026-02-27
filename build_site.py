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
      padding: 14px 28px; text-align: center; min-width: 180px;
      box-shadow: 0 1px 4px rgba(0,0,0,.06);
    }
    .stat-box .label { font-size: 12px; color: #777; margin-bottom: 4px; text-transform: uppercase; letter-spacing: .5px; }
    .stat-box .value { font-size: 30px; font-weight: 700; color: #1a1a2e; }
    .stat-box .as-of { font-size: 11px; color: #aaa; margin-top: 3px; }
    .controls { text-align: center; margin-bottom: 12px; }
    .btn {
      padding: 7px 18px; border: 1px solid #ccc; border-radius: 6px;
      background: #fff; cursor: pointer; font-size: 13px; color: #444;
      transition: background .15s, border-color .15s;
    }
    .btn:hover { background: #f0f0f0; border-color: #aaa; }
    .btn.active { background: #1a1a2e; color: #fff; border-color: #1a1a2e; }
    .chart-wrap {
      background: #fff; border: 1px solid #e0e0e0; border-radius: 10px;
      padding: 8px; box-shadow: 0 1px 4px rgba(0,0,0,.06);
    }
    #chart { width: 100%; height: 580px; }
    .footer {
      text-align: center; font-size: 11px; color: #bbb; margin-top: 14px;
    }
    #error { color: #c00; text-align: center; padding: 20px; display: none; }
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
  (async () => {
    // ── Load data ──────────────────────────────────────────────────────────
    let rates, events;
    try {
      [rates, events] = await Promise.all([
        fetch('data/rates.json').then(r => { if (!r.ok) throw new Error(r.status); return r.json(); }),
        fetch('data/events.json').then(r => { if (!r.ok) throw new Error(r.status); return r.json(); }),
      ]);
    } catch (e) {
      document.getElementById('error').style.display = 'block';
      console.error('Data load failed:', e);
      return;
    }

    // ── Stat boxes ─────────────────────────────────────────────────────────
    const meta = rates.meta;
    document.getElementById('policyRate').textContent = meta.policy_current.toFixed(2) + '%';
    document.getElementById('policyDate').textContent = 'as of ' + meta.policy_current_date;
    document.getElementById('primeRate').textContent  = meta.prime_current.toFixed(2) + '%';
    document.getElementById('primeDate').textContent  = 'as of ' + meta.prime_current_date;

    // ── Prepare series data ────────────────────────────────────────────────
    const policyData = rates.policy.map(d => [d.date, d.rate]);
    const primeData  = rates.prime.map(d => [d.date, d.rate]);

    // ── markPoint label formatter ──────────────────────────────────────────
    function mpFormatter(p) {
      var val  = Array.isArray(p.value) ? p.value[1] : p.value;
      var date = '';
      if (p.data && p.data.coord && p.data.coord[0]) {
        date = '\n' + new Date(p.data.coord[0]).toISOString().slice(0, 10);
      }
      return p.name + ': ' + Number(val).toFixed(2) + '%' + date;
    }

    // ── markArea builder from events ───────────────────────────────────────
    function buildMarkArea() {
      return {
        silent: true,
        label: {
          show: true,
          position: 'insideTop',
          distance: 6,
          fontSize: 10,
          color: '#555',
          rotate: 90,
          overflow: 'truncate',
        },
        data: events.regions.map(function(r) {
          return [
            { name: r.label, xAxis: r.start, itemStyle: { color: r.color } },
            { xAxis: r.end },
          ];
        }),
      };
    }

    const emptyMarkArea = { data: [] };

    // ── Tooltip formatter ──────────────────────────────────────────────────
    function tooltipFormatter(params) {
      if (!params || !params.length) return '';
      var date = new Date(params[0].axisValue).toISOString().slice(0, 10);
      var html = '<strong>' + date + '</strong><br>';
      params.forEach(function(p) {
        if (p.value && p.value[1] != null) {
          html += '<span style="color:' + p.color + '">&#9679;</span> '
                + p.seriesName + ': <strong>'
                + Number(p.value[1]).toFixed(2) + '%</strong><br>';
        }
      });
      return html;
    }

    // ── ECharts init ───────────────────────────────────────────────────────
    const chart = echarts.init(document.getElementById('chart'));

    const markPointStyle = {
      symbolSize: 44,
      label: { show: true, fontSize: 10, formatter: mpFormatter },
    };

    const option = {
      backgroundColor: '#fff',
      tooltip: {
        trigger: 'axis',
        axisPointer: { type: 'cross', crossStyle: { color: '#aaa' } },
        formatter: tooltipFormatter,
      },
      legend: {
        data: ['BoC Policy Rate', 'Commercial Prime Rate'],
        top: 8,
        itemGap: 24,
      },
      grid: { left: 54, right: 24, top: 48, bottom: 70, containLabel: false },
      xAxis: {
        type: 'time',
        boundaryGap: false,
        axisLine: { lineStyle: { color: '#ccc' } },
        splitLine: { show: false },
      },
      yAxis: {
        type: 'value',
        min: 0,
        axisLabel: { formatter: '{value}%' },
        splitLine: { lineStyle: { color: '#f0f0f0' } },
        axisLine: { show: true, lineStyle: { color: '#ccc' } },
      },
      dataZoom: [
        {
          type: 'inside',
          xAxisIndex: [0],
          yAxisIndex: [0],
          zoomOnMouseWheel: true,
          moveOnMouseWheel: false,
          moveOnMouseMove: true,
        },
        {
          type: 'slider',
          xAxisIndex: [0],
          bottom: 8,
          height: 22,
          borderColor: '#ddd',
          fillerColor: 'rgba(26,26,46,0.08)',
          handleStyle: { color: '#1a1a2e' },
        },
      ],
      series: [
        {
          name: 'BoC Policy Rate',
          type: 'line',
          step: 'end',
          data: policyData,
          lineStyle: { color: '#2E86AB', width: 2 },
          itemStyle: { color: '#2E86AB' },
          symbol: 'none',
          markPoint: Object.assign({
            data: [
              { type: 'max', name: 'Max', itemStyle: { color: '#2E86AB' } },
              { type: 'min', name: 'Min', itemStyle: { color: '#2E86AB' } },
            ],
          }, markPointStyle),
          markArea: emptyMarkArea,
        },
        {
          name: 'Commercial Prime Rate',
          type: 'line',
          step: 'end',
          data: primeData,
          lineStyle: { color: '#A23B72', width: 2 },
          itemStyle: { color: '#A23B72' },
          symbol: 'none',
          markPoint: Object.assign({
            data: [
              { type: 'max', name: 'Max', itemStyle: { color: '#A23B72' } },
              { type: 'min', name: 'Min', itemStyle: { color: '#A23B72' } },
            ],
          }, markPointStyle),
          markArea: emptyMarkArea,
        },
      ],
    };

    chart.setOption(option);

    // ── Events toggle ──────────────────────────────────────────────────────
    var eventsVisible = false;
    document.getElementById('toggleEvents').addEventListener('click', function() {
      eventsVisible = !eventsVisible;
      var ma = eventsVisible ? buildMarkArea() : emptyMarkArea;
      chart.setOption({ series: [{ markArea: ma }, { markArea: emptyMarkArea }] });
      this.textContent = eventsVisible ? 'Hide Historical Events' : 'Show Historical Events';
      this.classList.toggle('active', eventsVisible);
    });

    // ── Responsive ────────────────────────────────────────────────────────
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
