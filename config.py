"""
Configuration for the Bank of Canada Monitor (boc_monitor.py, rate_plotter.py).
Rate series, sources and data paths live in rates.py.
"""

from pathlib import Path

# Project paths
PROJECT_ROOT = Path(__file__).parent
OUTPUT_DIR = PROJECT_ROOT / "output"
OUTPUT_DIR.mkdir(exist_ok=True)

# LLM Configuration
MAX_CHUNK_SIZE = 50000  # Characters per chunk (~12.5K tokens)
CHUNK_OVERLAP = 1000     # Overlap between chunks for context preservation
ANTHROPIC_MODEL = "claude-sonnet-4-20250514"
ANTHROPIC_TEMPERATURE = 0.3  # Lower for more consistent analysis

# Rate limiting
API_RATE_LIMIT_DELAY = 1  # Seconds between API calls
