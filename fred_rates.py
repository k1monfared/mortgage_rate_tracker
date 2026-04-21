"""
Historical rate fetcher for US Federal Reserve data.

Pulls Fed Funds (DFF) and US Bank Prime Loan Rate (DPRIME) from the FRED
fredgraph CSV endpoint, which is public and requires no API key.

Public API mirrors HistoricalRateFetcher so build_site.py can treat both
fetchers identically. rate_type tokens:

    "policy" -> DFF    (Federal Funds Effective Rate, daily)
    "prime"  -> DPRIME (Bank Prime Loan Rate, daily)

CSV schema on disk is the same as the BoC side: columns date,rate.
"""

import json
import time
from datetime import datetime, timedelta
from io import StringIO
from pathlib import Path
from typing import Dict, Optional

import pandas as pd
import requests

from config import DATA_DIR, HISTORICAL_YEARS

FRED_BASE_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv"

FED_FUNDS_CSV = DATA_DIR / "us_fed_funds_rate.csv"
US_PRIME_CSV = DATA_DIR / "us_prime_rate.csv"
US_METADATA_JSON = DATA_DIR / "us_metadata.json"


class FREDRateFetcher:
    """Fetches and manages US interest rate data from FRED (St. Louis Fed)."""

    SERIES = {
        "policy": ("DFF",    FED_FUNDS_CSV, "Fed Funds Rate"),
        "prime":  ("DPRIME", US_PRIME_CSV,  "US Bank Prime Rate"),
    }

    def __init__(self):
        self.base_url = FRED_BASE_URL

    def _fetch(self, series_code: str, start_date: datetime,
               end_date: datetime, rate_name: str) -> Optional[pd.DataFrame]:
        try:
            params = {
                "id": series_code,
                "cosd": start_date.strftime("%Y-%m-%d"),
                "coed": end_date.strftime("%Y-%m-%d"),
            }
            url = self.base_url
            print(f"   Fetching {rate_name} from FRED...")
            print(f"   URL: {url}?id={series_code}")
            print(f"   Date range: {params['cosd']} to {params['coed']}")

            resp = requests.get(url, params=params, timeout=30)
            resp.raise_for_status()

            df = pd.read_csv(StringIO(resp.text))
            if df.empty:
                print("   ⚠️  Empty response from FRED")
                return None

            date_col = df.columns[0]
            value_col = series_code if series_code in df.columns else df.columns[1]

            df = df.rename(columns={date_col: "date", value_col: "rate"})
            df = df[["date", "rate"]]
            df["date"] = pd.to_datetime(df["date"])
            df["rate"] = pd.to_numeric(df["rate"], errors="coerce")

            initial_len = len(df)
            df = df.dropna(subset=["rate"])
            if len(df) < initial_len:
                print(f"   ⚠️  Dropped {initial_len - len(df)} rows with missing rates")

            df = df.sort_values("date")
            print(f"   ✓ Fetched {len(df)} records for {rate_name}")
            if len(df):
                print(f"   Date range: {df['date'].min()} to {df['date'].max()}")
            return df

        except requests.exceptions.RequestException as e:
            print(f"   ❌ Error fetching {rate_name}: {e}")
            return None
        except Exception as e:
            print(f"   ❌ Error parsing {rate_name} data: {e}")
            return None

    def initialize_historical_data(self):
        """First-run: fetch full history for both series."""
        print("\n" + "=" * 70)
        print("INITIALIZING US HISTORICAL DATA")
        print("=" * 70)
        start = datetime.now() - timedelta(days=HISTORICAL_YEARS * 365)
        end = datetime.now()

        for rate_type, (code, path, name) in self.SERIES.items():
            print(f"\n{name}")
            print("-" * 70)
            df = self._fetch(code, start, end, name)
            if df is not None and len(df):
                self._save_rate_data(df, path, rate_type)
            else:
                print(f"   ⚠️  Failed to fetch {name}")
            time.sleep(1)

        print("\n" + "=" * 70)
        print("✓ US historical data initialization complete!")
        print("=" * 70 + "\n")

    def update_incremental(self, rate_type: str):
        """Fetch only missing data since last update."""
        if rate_type not in self.SERIES:
            print(f"   ❌ Invalid rate type: {rate_type}")
            return

        code, csv_path, name = self.SERIES[rate_type]
        print(f"\nUpdating US {rate_type} rate data...")

        if not csv_path.exists():
            print("   No existing data found. Running full initialization for this series...")
            start = datetime.now() - timedelta(days=HISTORICAL_YEARS * 365)
            df = self._fetch(code, start, datetime.now(), name)
            if df is not None and len(df):
                self._save_rate_data(df, csv_path, rate_type)
            return

        try:
            existing_df = pd.read_csv(csv_path)
            existing_df["date"] = pd.to_datetime(existing_df["date"])
            last_date = existing_df["date"].max()
            print(f"   Last date in existing data: {last_date.strftime('%Y-%m-%d')}")

            today = datetime.now()
            if last_date.date() >= today.date():
                print(f"   ✓ Data is up to date (last date: {last_date.strftime('%Y-%m-%d')})")
                return

            start_date = last_date + timedelta(days=1)
            print(f"   Fetching data from {start_date.strftime('%Y-%m-%d')} to today...")
            new_df = self._fetch(code, start_date, today, name)
            if new_df is None or len(new_df) == 0:
                print("   No new data available")
                return

            combined = pd.concat([existing_df, new_df], ignore_index=True)
            combined = combined.drop_duplicates(subset=["date"], keep="last")
            combined = combined.sort_values("date")
            print(f"   ✓ Added {len(new_df)} new records")
            self._save_rate_data(combined, csv_path, rate_type)

        except Exception as e:
            print(f"   ❌ Error during incremental update: {e}")

    def _save_rate_data(self, df: pd.DataFrame, csv_path: Path, rate_type: str):
        try:
            df.to_csv(csv_path, index=False)
            print(f"   💾 Saved to: {csv_path}")
            print(f"   Records: {len(df)}")
            print(f"   Date range: {df['date'].min()} to {df['date'].max()}")
            self._update_metadata(rate_type, df["date"].max())
        except Exception as e:
            print(f"   ❌ Error saving data: {e}")

    def _update_metadata(self, rate_type: str, last_date: datetime):
        try:
            if US_METADATA_JSON.exists():
                with open(US_METADATA_JSON, "r") as f:
                    metadata = json.load(f)
            else:
                metadata = {}

            metadata[f"last_update_{rate_type}"] = datetime.now().isoformat()
            metadata[f"last_date_{rate_type}"] = last_date.isoformat()

            with open(US_METADATA_JSON, "w") as f:
                json.dump(metadata, f, indent=2)
        except Exception as e:
            print(f"   ⚠️  Error updating metadata: {e}")

    def load_rate_data(self, rate_type: str) -> Optional[pd.DataFrame]:
        if rate_type not in self.SERIES:
            print(f"Invalid rate type: {rate_type}")
            return None
        _, csv_path, _ = self.SERIES[rate_type]
        if not csv_path.exists():
            print(f"No data file found at: {csv_path}")
            return None
        try:
            df = pd.read_csv(csv_path)
            df["date"] = pd.to_datetime(df["date"])
            return df
        except Exception as e:
            print(f"Error loading data: {e}")
            return None

    def get_metadata(self) -> Dict:
        if not US_METADATA_JSON.exists():
            return {}
        try:
            with open(US_METADATA_JSON, "r") as f:
                return json.load(f)
        except Exception as e:
            print(f"Error loading metadata: {e}")
            return {}


if __name__ == "__main__":
    fetcher = FREDRateFetcher()
    fetcher.initialize_historical_data()
    print("\nMetadata:")
    for k, v in fetcher.get_metadata().items():
        print(f"  {k}: {v}")
