"""
Build script for GitHub Pages site.
Fetches latest rate data from BoC API and generates an interactive HTML page.
"""

import os
from datetime import datetime, timezone
from pathlib import Path

import plotly.io as pio

from historical_rates import HistoricalRateFetcher
from rate_plotter import RatePlotter


def build_site():
    site_dir = Path("site")
    site_dir.mkdir(exist_ok=True)

    fetcher = HistoricalRateFetcher()
    plotter = RatePlotter()

    print("Fetching BoC Policy Rate...")
    policy_df = fetcher.fetch_policy_rate()
    print("Fetching Commercial Prime Rate...")
    prime_df = fetcher.fetch_prime_rate()

    if policy_df is None or prime_df is None:
        raise RuntimeError("Failed to fetch rate data from BoC API")

    fig = plotter.plot_dual_rates(policy_df, prime_df)

    chart_html = pio.to_html(fig, include_plotlyjs="cdn", full_html=False)

    latest_policy = policy_df.sort_values("date").iloc[-1]
    latest_prime = prime_df.sort_values("date").iloc[-1]

    policy_rate = f"{latest_policy['rate']:.2f}%"
    policy_date = latest_policy["date"].strftime("%Y-%m-%d")
    prime_rate = f"{latest_prime['rate']:.2f}%"
    prime_date = latest_prime["date"].strftime("%Y-%m-%d")

    build_time = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    page = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Canadian Interest Rate Tracker</title>
  <style>
    body {{
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      margin: 0;
      padding: 20px;
      background: #f8f9fa;
      color: #333;
    }}
    h1 {{
      text-align: center;
      color: #222;
      margin-bottom: 24px;
    }}
    .stats {{
      display: flex;
      gap: 16px;
      justify-content: center;
      margin-bottom: 24px;
      flex-wrap: wrap;
    }}
    .stat-box {{
      background: white;
      border: 1px solid #ddd;
      border-radius: 8px;
      padding: 16px 28px;
      text-align: center;
      min-width: 180px;
    }}
    .stat-box .label {{
      font-size: 13px;
      color: #666;
      margin-bottom: 6px;
    }}
    .stat-box .value {{
      font-size: 28px;
      font-weight: 700;
      color: #222;
    }}
    .stat-box .as-of {{
      font-size: 12px;
      color: #999;
      margin-top: 4px;
    }}
    .chart-container {{
      background: white;
      border: 1px solid #ddd;
      border-radius: 8px;
      padding: 8px;
    }}
    .footer {{
      text-align: center;
      font-size: 12px;
      color: #aaa;
      margin-top: 16px;
    }}
  </style>
</head>
<body>
  <h1>Canadian Interest Rate Tracker</h1>
  <div class="stats">
    <div class="stat-box">
      <div class="label">BoC Policy Rate</div>
      <div class="value">{policy_rate}</div>
      <div class="as-of">as of {policy_date}</div>
    </div>
    <div class="stat-box">
      <div class="label">Commercial Prime Rate</div>
      <div class="value">{prime_rate}</div>
      <div class="as-of">as of {prime_date}</div>
    </div>
  </div>
  <div class="chart-container">
    {chart_html}
  </div>
  <div class="footer">
    Data source: Bank of Canada Valet API &mdash; Built {build_time}
  </div>
</body>
</html>
"""

    output_path = site_dir / "index.html"
    output_path.write_text(page, encoding="utf-8")
    print(f"Site built successfully: {output_path}")


if __name__ == "__main__":
    build_site()
