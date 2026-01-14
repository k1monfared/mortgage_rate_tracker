"""
Configuration file for Bank of Canada Monitor
Centralizes paths, API endpoints, and constants
"""

from pathlib import Path

# Project paths
PROJECT_ROOT = Path(__file__).parent
DATA_DIR = PROJECT_ROOT / "data"
OUTPUT_DIR = PROJECT_ROOT / "output"

# Create directories if they don't exist
DATA_DIR.mkdir(exist_ok=True)
OUTPUT_DIR.mkdir(exist_ok=True)

# Bank of Canada Valet API Configuration
VALET_BASE_URL = "https://www.bankofcanada.ca/valet"
BOC_POLICY_RATE_SERIES = "V122530"  # Policy Interest Rate (overnight rate)
BOC_PRIME_RATE_SERIES = "V80691311"  # Commercial Prime Rate (verified)

# File paths for data storage
POLICY_RATE_CSV = DATA_DIR / "boc_policy_rate.csv"
PRIME_RATE_CSV = DATA_DIR / "commercial_prime_rate.csv"
METADATA_JSON = DATA_DIR / "metadata.json"

# LLM Configuration
MAX_CHUNK_SIZE = 50000  # Characters per chunk (~12.5K tokens)
CHUNK_OVERLAP = 1000     # Overlap between chunks for context preservation
ANTHROPIC_MODEL = "claude-sonnet-4-20250514"
ANTHROPIC_TEMPERATURE = 0.3  # Lower for more consistent analysis

# Rate limiting
API_RATE_LIMIT_DELAY = 1  # Seconds between API calls

# Historical data configuration
HISTORICAL_YEARS = 100  # Number of years to fetch on first run
