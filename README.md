# TTD Darshan Data Analysis

TTD (Tirumala Tirupati Devasthanam) archival data analysis focusing on extracting daily visitor/pilgrim statistics from news articles for pattern analysis and forecasting.

## Overview

This project processes TTD news articles to:
1. Classify articles by topic (visitor statistics vs. other news)
2. Extract daily pilgrim counts and related metrics using OpenAI's GPT-4o-mini model
3. Aggregate data by date to build a clean time-series dataset
4. Handle failures and false positives intelligently

## Architecture

### Batch Processing Pipeline

The processor uses **OpenAI Batch API** for improved efficiency and cost savings:

```
CSV Files
  ↓
Load Articles (memory cache)
  ↓
Phase 1: Classification Batch
  ├─ Submit articles in batches (100 per batch)
  ├─ Poll until completion
  └─ Separate "visitor stats" articles from others
  ↓
Phase 2: Metric Extraction Batch
  ├─ Submit classified articles in batches
  ├─ Poll until completion
  └─ Parse JSON responses for pilgrim counts & metrics
  ↓
Phase 3: Aggregation
  ├─ Combine by date (YYYY-MM-DD)
  ├─ Keep highest count per date
  └─ Store article metadata for traceability
  ↓
Output: darshan_data.json + failed_records.csv
```

### Key Benefits

- **Cost Reduction**: ~50% cheaper than live API calls
- **Throughput**: Batch processing is much faster for large datasets
- **Reliability**: Built-in retry and error handling
- **Modularity**: Provider abstraction allows easy model/API swaps

## Requirements

- Python 3.11+
- OpenAI API key with Batch API access
- Dependencies: `python-dotenv`, `openai>=1.99.9`, `tqdm`

## Installation

1. **Create virtual environment:**
   ```bash
   python3.11 -m venv ttd_analysis_env
   source ttd_analysis_env/bin/activate
   ```

2. **Install dependencies:**
   ```bash
   pip install -r requirements.txt
   ```

3. **Set up environment:**
   ```bash
   cp .env.example .env  # or create .env manually
   export OPENAI_API_KEY="your-api-key"
   export OPENAI_MODEL="gpt-4o-mini"
   ```

## Usage

### Basic Processing

```bash
python process_articles.py <path_to_csv_directory>
```

**Example:**
```bash
python process_articles.py test_data/some_darshan/
```

### Input Format

CSV files in the directory should have columns:
- `article_id`: Unique identifier
- `title`: Article title
- `content`: Article full text
- `link`: Article URL
- `year`, `month`, `page`: Metadata

File naming convention: `articles_YYYY_MM.csv`

### Output Format

#### `darshan_data.json`
```json
{
  "2017-02-28": {
    "article_id": "post-41320",
    "title": "About 44,276 pilgrims had Srivari Dharshan from 3am to 6pm on Feb 28",
    "post": "https://news.tirumala.org/...",
    "data": {
      "pilgrim_count": 44276,
      "other_metrics": {
        "Sarva_Dharshan": "Free darshan",
        "Divya_Dharshan": "1 Compartment / 2 Hours"
      }
    }
  },
  "2017-02-27": { ... }
}
```

**Structure:**
- **Key**: ISO date (YYYY-MM-DD)
- **article_id**: Source article identifier
- **title**: Original article title
- **post**: URL to the article
- **data.pilgrim_count**: Extracted visitor count (integer)
- **data.other_metrics**: Additional metrics (e.g., darshan wait times, compartment counts)

#### `failed_records_YYYYMMDD_HHMMSS.csv`
Records that could not be processed (classification or extraction failed). Use these to:
- Review false positives
- Improve prompts
- Retry with updated logic

## Processing Details

### Classification

Articles are classified as **"true"** (visitor statistics) or **"false"** (other news).

**True examples:**
- "About 64,801 pilgrims had Srivari darshan..."
- "Total pilgrims – 52,643"

**False examples (filtered out):**
- "TTD ALLOTS SEVA TICKETS THROUGH ONLINE DIP"
- "SRI B.VENKATESWARA RAO SWORN IN AS TTD BOARD EX-OFFICIO"
- "Arrangements for summer rush announced"

### Metric Extraction

For classified articles, the system extracts:
- `day`: Day of the month (1-31) from the article text
- `pilgrim_count`: Number of pilgrims visiting on that day
- `other_metrics`: Any additional relevant data (waiting times, facility info, etc.)

### Date Aggregation

- **One record per unique date** in output
- **Highest count per date** is preserved
- If multiple articles report different counts for the same day, the highest is used (most likely accurate)
- **No date information**: Article is skipped (added to failed records)

## Logging

Logs are saved to `logs/log_YYYYMMDD_HHMMSS.log`. Includes:
- Phase progress and batch job IDs
- Success/failure counts
- Detailed errors for debugging

## Metrics & Success Rates

After processing, a summary is printed:

```
PROCESSING METRICS
============================================================
Total articles loaded:      5432
Classified as TRUE:         1205
Classified as FALSE:        4227
Successfully extracted:     1198
Extraction failed:          7
Invalid dates (no day):     4
Final unique dates:         897
Total failed records:       11
Overall success rate:       99.4%
```

## Configuration

### Environment Variables

- `OPENAI_API_KEY`: Required. Your OpenAI API key
- `OPENAI_MODEL`: Default: `gpt-4o-mini` (recommended for cost/performance)

### Batch Parameters (in `process_articles.py`)

- `MAX_BATCH_SIZE`: Articles per batch (default: 100, max: 10,000)
- `MAX_RETRIES`: Retry attempts per article (default: 3)

## Troubleshooting

### Common Issues

1. **"Missing OpenAI API key"**
   - Ensure `OPENAI_API_KEY` is set: `export OPENAI_API_KEY="sk-..."`

2. **High failure rate (>5%)**
   - Check classification prompts in `prompt_templates.py`
   - Review failed records CSV for patterns
   - Adjust thresholds if needed

3. **Batch stuck in processing**
   - Batches typically complete within minutes
   - Can take up to 24 hours (per OpenAI SLA)
   - Check batch status in OpenAI dashboard if needed

4. **Out of memory with large datasets**
   - Reduce `MAX_BATCH_SIZE` if processing 100k+ articles
   - Process data directory by directory

## Previous Run Reference

Existing full run data available in:
- `full_run_1/darshan_data_full_run.json` — 3,857 unique dates processed
- `failed_records_20250829_052106.csv` — 47 failed records from previous run

## Visualization

A separate visualization script is available to generate interactive graphs from processed `darshan_data.json`.

## Architecture Notes

- **Provider Pattern**: `providers/openai_provider.py` implements batch API methods
- **Factory Pattern**: `provider_factory.py` instantiates the provider
- **Modular Design**: Easy to add new providers or processing strategies
- **Fail-Safe**: Graceful handling of interrupts, rate limits, and API errors

## Future Enhancements

- Automatic prompt optimization based on failure analysis
- Support for other LLMs via same provider interface
- Incremental processing (resume from checkpoint)
- Date extraction from article metadata (when article text lacks date)
- Real-time dashboard for monitoring large batch jobs

## License

See LICENSE file.

