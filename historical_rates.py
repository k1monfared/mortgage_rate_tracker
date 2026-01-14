"""
Historical Rate Fetcher for Bank of Canada Data
Fetches and stores historical policy and prime rate data from BoC Valet API
"""

import requests
import pandas as pd
from datetime import datetime, timedelta
from pathlib import Path
import json
from typing import Optional, Dict
import time

from config import (
    VALET_BASE_URL,
    BOC_POLICY_RATE_SERIES,
    BOC_PRIME_RATE_SERIES,
    POLICY_RATE_CSV,
    PRIME_RATE_CSV,
    METADATA_JSON,
    HISTORICAL_YEARS,
    DATA_DIR
)


class HistoricalRateFetcher:
    """
    Fetches and manages historical interest rate data from Bank of Canada
    """

    def __init__(self):
        """Initialize the fetcher"""
        self.base_url = VALET_BASE_URL
        self.policy_series = BOC_POLICY_RATE_SERIES
        self.prime_series = BOC_PRIME_RATE_SERIES

    def fetch_policy_rate(self, start_date: Optional[datetime] = None,
                         end_date: Optional[datetime] = None) -> Optional[pd.DataFrame]:
        """
        Fetch BoC policy rate from Valet API

        Args:
            start_date: Start date for data fetch (default: 100 years ago)
            end_date: End date for data fetch (default: today)

        Returns:
            DataFrame with columns: date, rate
        """
        if start_date is None:
            start_date = datetime.now() - timedelta(days=HISTORICAL_YEARS * 365)
        if end_date is None:
            end_date = datetime.now()

        return self._fetch_from_valet(
            self.policy_series,
            start_date,
            end_date,
            "Policy Rate"
        )

    def fetch_prime_rate(self, start_date: Optional[datetime] = None,
                        end_date: Optional[datetime] = None) -> Optional[pd.DataFrame]:
        """
        Fetch commercial prime rate from Valet API

        Args:
            start_date: Start date for data fetch (default: 100 years ago)
            end_date: End date for data fetch (default: today)

        Returns:
            DataFrame with columns: date, rate
        """
        if start_date is None:
            start_date = datetime.now() - timedelta(days=HISTORICAL_YEARS * 365)
        if end_date is None:
            end_date = datetime.now()

        # Try primary series first
        df = self._fetch_from_valet(
            self.prime_series,
            start_date,
            end_date,
            "Prime Rate"
        )

        # If primary series fails, try alternative approaches
        if df is None or len(df) == 0:
            print("   Primary prime rate series failed, trying banking group...")
            df = self._fetch_prime_from_banking_group(start_date, end_date)

        return df

    def _fetch_from_valet(self, series_code: str, start_date: datetime,
                         end_date: datetime, rate_name: str) -> Optional[pd.DataFrame]:
        """
        Fetch data from Valet API for a specific series

        Args:
            series_code: Series code (e.g., V122530)
            start_date: Start date
            end_date: End date
            rate_name: Name for logging

        Returns:
            DataFrame with date and rate columns
        """
        try:
            # Construct URL
            start_str = start_date.strftime('%Y-%m-%d')
            end_str = end_date.strftime('%Y-%m-%d')
            url = f"{self.base_url}/observations/{series_code}/csv"
            params = {
                'start_date': start_str,
                'end_date': end_str
            }

            print(f"   Fetching {rate_name} from Valet API...")
            print(f"   URL: {url}")
            print(f"   Date range: {start_str} to {end_str}")

            response = requests.get(url, params=params, timeout=30)
            response.raise_for_status()

            # Parse CSV - Valet API format has sections, find OBSERVATIONS section
            from io import StringIO
            lines = response.text.split('\n')

            # Find the OBSERVATIONS section
            obs_start = None
            for i, line in enumerate(lines):
                if 'OBSERVATIONS' in line:
                    obs_start = i + 1  # Data starts after this line
                    break

            if obs_start is None:
                print(f"   ⚠️  No data available for this date range")
                return None

            # Get data from observations section onwards
            csv_data = StringIO('\n'.join(lines[obs_start:]))
            df = pd.read_csv(csv_data)

            # Valet returns columns like: date, V122530
            # Rename the series column to 'rate'
            if 'date' not in df.columns:
                print(f"   ⚠️  Warning: No 'date' column in response")
                return None

            # Find the series column (should be the series code)
            rate_column = None
            for col in df.columns:
                if col != 'date':
                    rate_column = col
                    break

            if rate_column is None:
                print(f"   ⚠️  Warning: No rate column found in response")
                return None

            # Rename to standard format
            df = df.rename(columns={rate_column: 'rate'})
            df = df[['date', 'rate']]  # Keep only date and rate

            # Convert date to datetime
            df['date'] = pd.to_datetime(df['date'])

            # Convert rate to numeric (handle any non-numeric values)
            df['rate'] = pd.to_numeric(df['rate'], errors='coerce')

            # Drop rows with missing rates
            initial_len = len(df)
            df = df.dropna(subset=['rate'])
            if len(df) < initial_len:
                print(f"   ⚠️  Dropped {initial_len - len(df)} rows with missing rates")

            # Sort by date
            df = df.sort_values('date')

            print(f"   ✓ Fetched {len(df)} records for {rate_name}")
            print(f"   Date range: {df['date'].min()} to {df['date'].max()}")

            return df

        except requests.exceptions.RequestException as e:
            print(f"   ❌ Error fetching {rate_name}: {e}")
            return None
        except Exception as e:
            print(f"   ❌ Error parsing {rate_name} data: {e}")
            return None

    def _fetch_prime_from_banking_group(self, start_date: datetime,
                                       end_date: datetime) -> Optional[pd.DataFrame]:
        """
        Alternative method to fetch prime rate from banking group

        Args:
            start_date: Start date
            end_date: End date

        Returns:
            DataFrame with date and rate columns, or None if failed
        """
        try:
            start_str = start_date.strftime('%Y-%m-%d')
            end_str = end_date.strftime('%Y-%m-%d')
            url = f"{self.base_url}/observations/group/BANKING_AND_CREDIT/csv"
            params = {
                'start_date': start_str,
                'end_date': end_str
            }

            print(f"   Fetching from banking group...")
            response = requests.get(url, params=params, timeout=30)
            response.raise_for_status()

            from io import StringIO
            lines = response.text.split('\n')

            # Find the OBSERVATIONS section
            obs_start = None
            for i, line in enumerate(lines):
                if 'OBSERVATIONS' in line:
                    obs_start = i + 1
                    break

            if obs_start is None:
                print(f"   ⚠️  Could not find OBSERVATIONS section in banking group")
                return None

            csv_data = StringIO('\n'.join(lines[obs_start:]))
            df = pd.read_csv(csv_data)

            # Look for prime rate column
            # Common column names: might include "prime" in the name
            prime_col = None
            for col in df.columns:
                if 'prime' in col.lower() or col == self.prime_series:
                    prime_col = col
                    break

            if prime_col is None:
                print(f"   ⚠️  Could not find prime rate column in banking group")
                return None

            df = df.rename(columns={prime_col: 'rate'})
            df = df[['date', 'rate']]
            df['date'] = pd.to_datetime(df['date'])
            df['rate'] = pd.to_numeric(df['rate'], errors='coerce')
            df = df.dropna(subset=['rate'])
            df = df.sort_values('date')

            print(f"   ✓ Fetched {len(df)} records from banking group")
            return df

        except Exception as e:
            print(f"   ❌ Error fetching from banking group: {e}")
            return None

    def initialize_historical_data(self):
        """
        First-run initialization: fetch full historical data (100 years)
        """
        print("\n" + "="*70)
        print("INITIALIZING HISTORICAL DATA")
        print("="*70)
        print(f"Fetching {HISTORICAL_YEARS} years of historical rate data...\n")

        # Fetch policy rate
        print("1. Policy Rate")
        print("-" * 70)
        policy_df = self.fetch_policy_rate()
        if policy_df is not None and len(policy_df) > 0:
            self._save_rate_data(policy_df, POLICY_RATE_CSV, 'policy')
        else:
            print("   ⚠️  Failed to fetch policy rate data")

        time.sleep(1)  # Be nice to the API

        # Fetch prime rate
        print("\n2. Commercial Prime Rate")
        print("-" * 70)
        prime_df = self.fetch_prime_rate()
        if prime_df is not None and len(prime_df) > 0:
            self._save_rate_data(prime_df, PRIME_RATE_CSV, 'prime')
        else:
            print("   ⚠️  Failed to fetch prime rate data")

        print("\n" + "="*70)
        print("✓ Historical data initialization complete!")
        print("="*70 + "\n")

    def update_incremental(self, rate_type: str):
        """
        Fetch only missing data since last update

        Args:
            rate_type: Either 'policy' or 'prime'
        """
        print(f"\nUpdating {rate_type} rate data...")

        # Determine which CSV and fetch function to use
        if rate_type == 'policy':
            csv_path = POLICY_RATE_CSV
            fetch_func = self.fetch_policy_rate
        elif rate_type == 'prime':
            csv_path = PRIME_RATE_CSV
            fetch_func = self.fetch_prime_rate
        else:
            print(f"   ❌ Invalid rate type: {rate_type}")
            return

        # Check if CSV exists
        if not csv_path.exists():
            print(f"   No existing data found. Running full initialization...")
            if rate_type == 'policy':
                df = self.fetch_policy_rate()
                if df is not None:
                    self._save_rate_data(df, csv_path, rate_type)
            else:
                df = self.fetch_prime_rate()
                if df is not None:
                    self._save_rate_data(df, csv_path, rate_type)
            return

        # Load existing data
        try:
            existing_df = pd.read_csv(csv_path)
            existing_df['date'] = pd.to_datetime(existing_df['date'])
            last_date = existing_df['date'].max()

            print(f"   Last date in existing data: {last_date.strftime('%Y-%m-%d')}")

            # Check if we need to fetch new data
            today = datetime.now()
            if last_date.date() >= today.date():
                print(f"   ✓ Data is up to date (last date: {last_date.strftime('%Y-%m-%d')})")
                return

            # Fetch new data from day after last_date to today
            start_date = last_date + timedelta(days=1)
            print(f"   Fetching data from {start_date.strftime('%Y-%m-%d')} to today...")

            new_df = fetch_func(start_date=start_date, end_date=today)

            if new_df is None or len(new_df) == 0:
                print(f"   No new data available")
                return

            # Append new data
            combined_df = pd.concat([existing_df, new_df], ignore_index=True)

            # Remove duplicates (keep last occurrence)
            combined_df = combined_df.drop_duplicates(subset=['date'], keep='last')

            # Sort by date
            combined_df = combined_df.sort_values('date')

            print(f"   ✓ Added {len(new_df)} new records")

            # Save combined data
            self._save_rate_data(combined_df, csv_path, rate_type)

        except Exception as e:
            print(f"   ❌ Error during incremental update: {e}")

    def _save_rate_data(self, df: pd.DataFrame, csv_path: Path, rate_type: str):
        """
        Save rate data to CSV and update metadata

        Args:
            df: DataFrame with date and rate columns
            csv_path: Path to save CSV
            rate_type: 'policy' or 'prime'
        """
        try:
            # Save to CSV
            df.to_csv(csv_path, index=False)
            print(f"   💾 Saved to: {csv_path}")
            print(f"   Records: {len(df)}")
            print(f"   Date range: {df['date'].min()} to {df['date'].max()}")

            # Update metadata
            self._update_metadata(rate_type, df['date'].max())

        except Exception as e:
            print(f"   ❌ Error saving data: {e}")

    def _update_metadata(self, rate_type: str, last_date: datetime):
        """
        Update metadata JSON with last update timestamp

        Args:
            rate_type: 'policy' or 'prime'
            last_date: Last date in the dataset
        """
        try:
            # Load existing metadata or create new
            if METADATA_JSON.exists():
                with open(METADATA_JSON, 'r') as f:
                    metadata = json.load(f)
            else:
                metadata = {}

            # Update timestamp
            metadata[f'last_update_{rate_type}'] = datetime.now().isoformat()
            metadata[f'last_date_{rate_type}'] = last_date.isoformat()

            # Save metadata
            with open(METADATA_JSON, 'w') as f:
                json.dump(metadata, f, indent=2)

        except Exception as e:
            print(f"   ⚠️  Error updating metadata: {e}")

    def load_rate_data(self, rate_type: str) -> Optional[pd.DataFrame]:
        """
        Load rate data from CSV

        Args:
            rate_type: Either 'policy' or 'prime'

        Returns:
            DataFrame with date and rate columns, or None if file doesn't exist
        """
        if rate_type == 'policy':
            csv_path = POLICY_RATE_CSV
        elif rate_type == 'prime':
            csv_path = PRIME_RATE_CSV
        else:
            print(f"Invalid rate type: {rate_type}")
            return None

        if not csv_path.exists():
            print(f"No data file found at: {csv_path}")
            print("Run 'python boc_monitor.py update-rates --full' to initialize data")
            return None

        try:
            df = pd.read_csv(csv_path)
            df['date'] = pd.to_datetime(df['date'])
            return df
        except Exception as e:
            print(f"Error loading data: {e}")
            return None

    def get_metadata(self) -> Dict:
        """
        Get metadata about last updates

        Returns:
            Dictionary with metadata
        """
        if not METADATA_JSON.exists():
            return {}

        try:
            with open(METADATA_JSON, 'r') as f:
                return json.load(f)
        except Exception as e:
            print(f"Error loading metadata: {e}")
            return {}


def main():
    """Test the historical rate fetcher"""
    fetcher = HistoricalRateFetcher()

    print("Testing Historical Rate Fetcher")
    print("="*70)

    # Initialize data
    fetcher.initialize_historical_data()

    # Show metadata
    print("\nMetadata:")
    print("-"*70)
    metadata = fetcher.get_metadata()
    for key, value in metadata.items():
        print(f"  {key}: {value}")


if __name__ == "__main__":
    main()
