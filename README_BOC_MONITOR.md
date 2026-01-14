# Bank of Canada Monetary Policy Monitor

Automatically monitors Bank of Canada communications and uses Claude AI to analyze monetary policy stance and predict rate changes.

## Features

- ✅ Fetches latest BoC policy announcements
- ✅ Analyzes press releases and speeches
- ✅ Reviews Monetary Policy Reports
- ✅ Uses Claude AI to interpret hawkish/dovish signals
- ✅ Generates comprehensive analysis reports
- ✅ Provides actionable insights for mortgage decisions

## Installation

```bash
pip install -r requirements_boc.txt
```

## Setup

1. Get an Anthropic API key from https://console.anthropic.com/

2. Set your API key as an environment variable:
```bash
export ANTHROPIC_API_KEY='your-api-key-here'
```

Or on Windows:
```cmd
set ANTHROPIC_API_KEY=your-api-key-here
```

## Usage

### Basic Usage

```bash
python boc_monitor.py
```

This will:
1. Fetch the latest BoC communications
2. Analyze them using Claude AI
3. Generate a comprehensive report
4. Save results to timestamped files

### Output Files

The script generates two files:
- `boc_analysis_YYYYMMDD_HHMMSS.txt` - Human-readable report
- `boc_analysis_YYYYMMDD_HHMMSS.json` - Raw analysis data

### Example Report

```
╔═══════════════════════════════════════════════════════════════════╗
║       BANK OF CANADA MONETARY POLICY MONITORING REPORT          ║
║                Generated: 2026-01-12 14:30:00                    ║
╚═══════════════════════════════════════════════════════════════════╝

📊 AGGREGATE ANALYSIS (Based on 3 sources)
======================================================================
Average Confidence Level: 78.3%
Average Rate Change Probability (3 months): 25.0%

Stance Distribution:
  🔴 HAWKISH (Rate increases likely):  0 source(s)
  🟢 DOVISH (Rate cuts likely):        0 source(s)
  🟡 NEUTRAL (No clear direction):     1 source(s)
  🔵 HOLD (Maintaining current rate):  2 source(s)

Overall Assessment: ⏸️  HOLD STANCE - Rates likely to remain stable
```

## Programmatic Usage

```python
from boc_monitor import BoCMonitor

# Initialize
monitor = BoCMonitor(anthropic_api_key='your-key')

# Run full analysis
report = monitor.run_full_analysis()
print(report)

# Or fetch specific components
policy = monitor.fetch_latest_policy_announcement()
releases = monitor.fetch_press_releases()

# Analyze custom text
analysis = monitor.analyze_with_llm(
    content="Your text here",
    source_type="Custom"
)
```

## Scheduling Automated Monitoring

### Using cron (Linux/Mac)

Add to your crontab to run daily at 9 AM:
```bash
0 9 * * * cd /path/to/script && /usr/bin/python3 boc_monitor.py >> boc_monitor.log 2>&1
```

### Using Windows Task Scheduler

1. Open Task Scheduler
2. Create Basic Task
3. Set trigger (e.g., daily at 9 AM)
4. Action: Start a program
5. Program: `python`
6. Arguments: `C:\path\to\boc_monitor.py`

## Understanding the Analysis

### Policy Stances

- **HAWKISH** 🔴: Signals rate increases likely (combat inflation)
- **DOVISH** 🟢: Signals rate cuts likely (stimulate economy)
- **NEUTRAL** 🟡: Balanced, no clear direction
- **HOLD** 🔵: Maintaining current rates

### Key Metrics

- **Confidence Level**: How confident the AI is in its assessment (0-100%)
- **Rate Change Probability**: Likelihood of rate change in next 3 months (0-100%)
- **Inflation Concern**: HIGH/MEDIUM/LOW
- **Growth Outlook**: STRONG/MODERATE/WEAK

### Key Signals to Watch For

**Hawkish (Rate Increase) Indicators:**
- "Inflation remains elevated"
- "Economy operating above capacity"
- "Labour market is tight"
- "Wage pressures persist"
- "Upside risks to inflation"

**Dovish (Rate Cut) Indicators:**
- "Inflation has cooled"
- "Economic growth slowing"
- "Unemployment rising"
- "Downside risks to growth"
- "Achieving price stability"

**Hold Indicators:**
- "Appropriate level of restriction"
- "Monitor economic developments"
- "Balanced risks"
- "Data-dependent approach"

## Advanced Configuration

### Custom Analysis Parameters

You can modify the LLM analysis by editing the `analyze_with_llm` method:

```python
# Adjust temperature for more/less conservative analysis
message = self.client.messages.create(
    model="claude-sonnet-4-20250514",
    temperature=0.3,  # Lower = more consistent, Higher = more creative
    ...
)
```

### Adding Custom Data Sources

Add your own data sources by extending the monitor:

```python
def fetch_custom_source(self):
    # Your custom fetching logic
    return {'url': '...', 'text': '...', 'type': 'Custom'}
```

## Troubleshooting

### "ANTHROPIC_API_KEY not set"
- Make sure you've exported the environment variable
- Or pass it directly: `BoCMonitor(anthropic_api_key='your-key')`

### "Error fetching..."
- Check your internet connection
- BoC website may be temporarily down
- Some pages may have changed structure

### "Error parsing LLM response"
- The script will show the raw response for debugging
- May need to adjust prompt if BoC communication format changes

## Limitations

- Web scraping may break if BoC changes website structure
- LLM analysis is interpretive, not financial advice
- Rate limiting on API calls (includes delays)
- Historical data not automatically analyzed

## Disclaimer

This tool is for informational purposes only. It does NOT constitute financial advice. 
Always consult with qualified financial advisors before making mortgage or investment decisions.

The analysis is based on AI interpretation of publicly available Bank of Canada communications 
and may not capture all nuances of monetary policy.

## License

MIT License - Feel free to modify and use as needed

## Contributing

Suggestions for improvement:
- Add email notifications
- Create dashboard visualization
- Add historical tracking database
- Integrate with economic data APIs (Statistics Canada, etc.)
- Add sentiment analysis trending over time
