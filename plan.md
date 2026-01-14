# Bank of Canada Monitor Enhancement Plan

## Overview
Enhance the BoC Monitor with historical rate tracking, interactive plotting, intelligent document chunking, and bug fixes.

## User Requirements Summary
1. **Historical Rate Tracking**: Track both BoC Policy Rate and Commercial Prime Rate (100 years)
2. **Incremental Updates**: Fetch only missing data on subsequent runs
3. **Interactive Plotting**: Plotly-based visualization (date vs rate)
4. **Full Document Analysis**: Use intelligent chunking instead of 5K/15K char limits
5. **Bug Fixes**: Fix hardcoded `/home/claude/` paths

## Implementation Plan

### Phase 1: Configuration & Setup

**Create `config.py`** - Centralized configuration
```python
# Paths
PROJECT_ROOT = Path(__file__).parent
DATA_DIR = PROJECT_ROOT / "data"
OUTPUT_DIR = PROJECT_ROOT / "output"

# API Configuration
VALET_BASE_URL = "https://www.bankofcanada.ca/valet"
BOC_POLICY_RATE_SERIES = "V122530"
BOC_PRIME_RATE_SERIES = "V122495"  # To verify

# LLM Configuration
MAX_CHUNK_SIZE = 50000  # ~12.5K tokens
CHUNK_OVERLAP = 1000
```

**Update `requirements_boc.txt`**
Add:
- `pandas>=2.0.0`
- `plotly>=6.5.0`

**Create directories**: `data/`, `output/`

---

### Phase 2: Historical Rate Tracking

**Create `historical_rates.py`** (~300-400 lines)

Key class: `HistoricalRateFetcher`

**Core Methods:**
- `fetch_policy_rate(start_date, end_date)` - Fetch from Valet API V122530
- `fetch_prime_rate(start_date, end_date)` - Fetch from Valet API
- `initialize_historical_data()` - First run: get 100 years
- `update_incremental(rate_type)` - Fetch only missing data since last update
- `load_rate_data(rate_type)` - Load CSV as pandas DataFrame

**Data Sources:**
- Policy Rate: `https://www.bankofcanada.ca/valet/observations/V122530/csv`
- Prime Rate: Need to identify series (likely V122495 or from banking group)
- Parameters: `?start_date=YYYY-MM-DD&end_date=YYYY-MM-DD`

**Storage:**
- `data/boc_policy_rate.csv` - Columns: date, rate
- `data/commercial_prime_rate.csv` - Columns: date, rate
- `data/metadata.json` - Tracks last update timestamps

**Incremental Update Logic:**
1. Check if CSV exists
2. If not, fetch full 100-year history
3. If exists, read last date from CSV
4. Fetch from last_date + 1 day to today
5. Append to CSV (avoid duplicates using date index)
6. Update metadata.json timestamp

---

### Phase 3: Interactive Plotting

**Create `rate_plotter.py`** (~200-250 lines)

Key class: `RatePlotter`

**Core Methods:**
- `plot_single_rate(df, rate_name, title)` - Plot one rate series
- `plot_dual_rates(policy_df, prime_df)` - Both rates on same chart
- `plot_rate_changes(df, rate_column)` - Visualize rate deltas (increases/decreases)
- `plot_rolling_stats(df, rate_column, windows)` - Rolling averages
- `save_plot(fig, filename)` - Save as HTML
- `show_plot(fig)` - Display in browser

**Plotly Features:**
- Interactive zoom, pan, hover tooltips
- Professional styling with plotly_white template
- Dual-axis support if needed
- Color coding (red=increases, green=decreases)

**Usage:**
```python
plotter = RatePlotter()
policy_data = fetcher.load_rate_data('policy')
prime_data = fetcher.load_rate_data('prime')
fig = plotter.plot_dual_rates(policy_data, prime_data)
plotter.save_plot(fig, 'rates_comparison.html')
plotter.show_plot(fig)
```

---

### Phase 4: Intelligent Document Chunking

**Modify `boc_monitor.py`** (~200 lines added/modified)

**Add `DocumentChunker` class:**
- `chunk_text(text)` - Split into ~50K char chunks with 1K overlap
- Preserve paragraph boundaries when possible
- Return chunks with metadata: (text, index, total_chunks)

**Enhance LLM Analysis (Map-Reduce Pattern):**

1. **Split Phase**: Break document into chunks
2. **Map Phase**: Analyze each chunk independently
   - Modified prompt: "Analyzing PART X of Y"
   - Extract: key signals, inflation concerns, growth outlook from THIS section
   - Return JSON for each chunk

3. **Reduce Phase**: Synthesize all chunk analyses
   - New method: `_synthesize_chunk_analyses(chunk_analyses, source_type)`
   - Use Claude to combine chunk analyses into coherent final assessment
   - Pick top 5 most important signals (not all)
   - Determine overall stance across entire document

**Refactor `analyze_with_llm`:**
```python
def analyze_with_llm(content, source_type):
    if len(content) > 50000:
        return self.analyze_with_llm_chunked(content, source_type)
    else:
        return self._analyze_single_chunk(content, source_type)
```

**Remove character limits:**
- Line 40: Remove `[:5000]` - policy announcement
- Line 93: Remove `[:3000]` - MPR
- Line 250: Remove `[:15000]` - detailed content

---

### Phase 5: Bug Fixes

**Fix hardcoded paths (lines 404, 411):**
```python
from config import OUTPUT_DIR

# Replace:
filename = f'/home/claude/boc_analysis_{timestamp}.txt'
# With:
filename = OUTPUT_DIR / f'boc_analysis_{timestamp}.txt'
```

**Add API key validation in `__init__`:**
```python
if not anthropic_api_key or anthropic_api_key == 'your-api-key-here':
    raise ValueError("Valid ANTHROPIC_API_KEY required")
```

---

### Phase 6: CLI Integration

**Enhance `main()` with subcommands:**

```bash
# Original: Run policy analysis
python boc_monitor.py analyze

# NEW: Update historical data
python boc_monitor.py update-rates --full  # 100 years
python boc_monitor.py update-rates         # incremental

# NEW: Generate plots
python boc_monitor.py plot --type both --output rates.html
python boc_monitor.py plot --type policy
python boc_monitor.py plot --type prime
```

**Implementation using argparse:**
- Subparser for each command
- `analyze`: existing behavior
- `update-rates`: calls HistoricalRateFetcher
- `plot`: calls RatePlotter with specified options

---

## Critical Files to Modify/Create

### Files to Create (New):
1. **`config.py`** - Configuration management (create first)
2. **`historical_rates.py`** - Rate fetching and storage (~350 lines)
3. **`rate_plotter.py`** - Plotly visualizations (~225 lines)
4. **`data/README.md`** - Document data format

### Files to Modify (Existing):
5. **`boc_monitor.py`** - Add chunking, fix bugs (~200 lines modified)
   - Add DocumentChunker class
   - Add analyze_with_llm_chunked() method
   - Add _synthesize_chunk_analyses() method
   - Refactor analyze_with_llm to route based on length
   - Fix hardcoded paths (lines 404, 411)
   - Remove char limits (lines 40, 93, 250)
   - Add CLI subcommands in main()

6. **`requirements_boc.txt`** - Add pandas, plotly

### Files to Create (Optional):
7. **`tests/test_historical_rates.py`** - Unit tests
8. **`tests/test_chunking.py`** - Chunking tests
9. **`tests/test_plotter.py`** - Plotting tests

---

## Implementation Sequence

1. **Foundation** (10 min)
   - Create `config.py`
   - Update `requirements_boc.txt`
   - Create `data/` and `output/` directories

2. **Historical Data** (45 min)
   - Create `historical_rates.py`
   - Implement Valet API integration
   - Test data fetching

3. **Visualization** (30 min)
   - Create `rate_plotter.py`
   - Implement plotting functions
   - Test with sample data

4. **Chunking** (45 min)
   - Add DocumentChunker to `boc_monitor.py`
   - Implement chunked analysis methods
   - Test with long documents

5. **Bug Fixes** (15 min)
   - Fix hardcoded paths
   - Remove character limits
   - Add validation

6. **CLI Integration** (20 min)
   - Add argparse subcommands
   - Wire up new functionality

7. **Testing** (30 min)
   - Manual testing of each feature
   - Create basic unit tests

**Total Estimated Time: ~3 hours**

---

## Key Design Decisions

### 1. Data Source: Bank of Canada Valet API
- **Why**: Official, free, no registration, stable
- **API**: `https://www.bankofcanada.ca/valet/observations/{SERIES}/csv`
- **Series**: V122530 (policy rate), V122495 (prime rate - to verify)

### 2. Storage: CSV with pandas
- **Why**: Simple, human-readable, sufficient for ~36K rows (100 years daily)
- **Format**: date, rate columns
- **Trade-off**: Simpler than SQLite, adequate for this use case

### 3. Chunking: Map-Reduce Pattern
- **Why**: Preserves context, allows synthesis of insights
- **Chunk Size**: 50K chars (~12.5K tokens) - safe margin
- **Overlap**: 1K chars for boundary context
- **Trade-off**: Extra API call for synthesis, but much better results

### 4. Plotting: Plotly
- **Why**: User requirement, interactive features valuable for 100 years of data
- **Output**: HTML files (shareable, no dependencies)

---

## Verification Plan

### Manual Testing Checklist:

**Historical Data:**
- [ ] First run with `--full` fetches 100 years of both rates
- [ ] CSV files created in `data/` directory
- [ ] `metadata.json` created with timestamps
- [ ] Incremental update only fetches missing dates
- [ ] No duplicate dates in CSV
- [ ] Rate values are numeric and reasonable (0-100 range)

**Plotting:**
- [ ] Single rate plot displays correctly in browser
- [ ] Dual rate plot shows both series clearly
- [ ] Interactive features work (zoom, pan, hover tooltips)
- [ ] HTML files saved to `output/` directory
- [ ] Rate change plot highlights increases (red) vs decreases (green)

**Chunking:**
- [ ] Documents <50K chars analyzed without chunking
- [ ] Documents >50K chars split into multiple chunks
- [ ] Each chunk analyzed successfully (check API calls)
- [ ] Synthesis produces coherent final analysis
- [ ] No information loss compared to full-document analysis

**Bug Fixes:**
- [ ] Output files save to `output/` directory (not `/home/claude/`)
- [ ] No hardcoded paths in any output
- [ ] API key validation prevents execution with invalid key
- [ ] Full document text analyzed (no truncation)

**CLI:**
- [ ] `python boc_monitor.py analyze` - Original behavior works
- [ ] `python boc_monitor.py update-rates --full` - Fetches 100 years
- [ ] `python boc_monitor.py update-rates` - Incremental update
- [ ] `python boc_monitor.py plot --type both` - Creates dual plot
- [ ] `python boc_monitor.py plot --type policy` - Single policy plot
- [ ] `python boc_monitor.py plot --type prime` - Single prime plot

---

## Potential Issues & Mitigations

### Issue 1: Prime rate series code unknown
- **Mitigation**: Check Valet series list, use banking group endpoint, or fallback to Statistics Canada

### Issue 2: Very old data may be sparse or missing
- **Mitigation**: Forward-fill missing values, document data quality in metadata

### Issue 3: Chunking may split important context
- **Mitigation**: 1K char overlap + paragraph-boundary splitting + synthesis step

### Issue 4: Large documents may hit API rate limits
- **Mitigation**: Built-in `time.sleep(1)` between chunks, configurable rate limiting

---

## Documentation Updates

**Update `README_BOC_MONITOR.md`** - Add sections:
1. Historical Rate Tracking (new commands)
2. Visualization (plotting commands)
3. Advanced Analysis (chunking explanation)
4. Configuration (config.py settings)

**Create `data/README.md`** - Document CSV format and data sources

---

## Success Criteria

✅ Can fetch 100 years of both policy and prime rate data
✅ Incremental updates work without duplicates
✅ Interactive plots show historical trends clearly
✅ Long BoC documents (MPR, speeches) analyzed in full
✅ No hardcoded paths - all output to project directories
✅ Clean CLI interface for all features
✅ All verification tests pass
