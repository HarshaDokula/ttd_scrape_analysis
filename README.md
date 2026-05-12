# TTD Darshan Data Analysis

This project processes archival TTD news articles to extract daily visitor/pilgrim statistics for time-series analysis and forecasting.

## Installation

1. Ensure Python 3.10+ is installed.
2. Create and activate a virtual environment:
   ```bash
   python3 -m venv venv
   source venv/bin/activate  # On Windows use `venv\Scripts\activate`
   ```
3. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```
4. Set up your `.env` file with your OpenAI API key:
   ```bash
   echo "OPENAI_API_KEY=your_api_key_here" > .env
   ```

## Usage

Run the article processing pipeline on a directory containing CSV files:

```bash
python process_articles.py <path_to_csv_directory>
```

Example:
```bash
python process_articles.py test_data/some_darshan
```

This will:
- Load all CSV files in the directory
- Classify articles in batches using OpenAI Batch API
- Extract pilgrim counts and metrics in batches
- Aggregate data by date
- Save a consolidated `darshan_data.json` file with metadata
- Save `failed_records_*.csv` for any problematic articles

### Retry Failed Records

To retry processing previously failed records:

```bash
python process_articles.py <path_to_csv_directory> --retry-failed <failed_records.csv>
```

### Token-Aware Rate Limit Management

The pipeline uses **tiktoken** for exact token counting and intelligent rate-limit management. Article content is truncated to a **token budget** (default 4096 tokens/request, ~16K chars) instead of a hard character limit.

Key CLI arguments for tuning:

| Argument | Default | Description |
|---|---|---|
| `--max-tokens-per-request` | 4096 | Max tokens of article content per API request. Content beyond this is truncated via tiktoken. |
| `--max-tokens-per-batch` | 1,500,000 | Max tokens per batch file (25% headroom under OpenAI's 2M per-batch limit). Batches are dynamically sized to fit this budget. |
| `--tpm-limit` | 2,000,000 | Organization TPM (tokens per minute) limit. Used to pace batch submissions. |
| `--tpm-pace-threshold` | 0.75 | Fraction of TPM at which to start pacing submissions (default: 75%). |
| `--per-batch-timeout` | 86400 | Seconds before giving up on a batch (default 24h, matches OpenAI's completion window). Batches at `processed=0` are usually just queued — increase this rather than cancelling. |

Example with custom settings:

```bash
python process_articles.py ../ttd_scrape/scraped_data \
  --max-tokens-per-request 4096 \
  --max-tokens-per-batch 1500000 \
  --tpm-limit 2000000 \
  --per-batch-timeout 3600
```

### Per-Record Mode

Process articles one-by-one (slower, no batch API):

```bash
python process_articles.py <data_dir> --per-record
```

Per-record mode also uses tiktoken for content truncation and TPM-aware pacing.

## Logs

Logs are saved in the `logs/` directory with timestamped filenames. Each batch submission now logs its estimated token count for TPM tracking.

## Testing and Validation

- Test with smaller CSV datasets to verify classification and extraction accuracy.
- Check `darshan_data.json` for output correctness.
- Monitor `failed_records_*.csv` for any errors and retry if needed.

## Notes

- Batch API reduces cost and latency compared to individual calls.
- Processing large datasets may take time due to polling batch completion.
- The project currently supports only the OpenAI provider.
- Prompts are tuned for strict classification and extraction of visitor counts.

## Graceful stopping and snapshotting

The processor now supports graceful shutdown and on-demand state snapshots so you can stop a background (nohup) run without losing progress.

Signals supported (Linux / Unix):

- SIGUSR1: request a snapshot (save outputs + darshan_state.json) without exiting. Example:

  ```bash
  # find the running pid
  pgrep -fa 'process_articles.py'
  kill -USR1 <pid>
  # The process will write darshan_state.json and failed_records_*.csv to the output directory.
  ```

- SIGINT (Ctrl+C) or SIGTERM: gracefully save outputs, state, and metrics, then exit. Example:

  ```bash
  kill -INT <pid>   # or: kill <pid>
  ```

Notes:
- If the process was started before updating the code, SIGUSR1 may not be handled. KeyboardInterrupt (SIGINT) was handled previously and will save state.
- If the process is unresponsive to signals (e.g. blocked in a C-level system call), you may need to `kill -9 <pid>` (force-kill). Force-kill will not allow the program to save state.
- The repo includes two helper scripts:
  - `list_batches.py` — list OpenAI batches for your account
  - `cancel_all_batches.py` — dry-run or cancel non-terminal batches (pass `--yes` to actually cancel)

Example safe workflow to stop a run:

1. Request a snapshot (non-blocking):
   ```bash
   kill -USR1 <pid>
   ls output/run_*/darshan_state.json
   ```
2. Then gracefully stop:
   ```bash
   kill -INT <pid>
   ```
3. If needed, inspect and cancel in-progress batches in OpenAI with `cancel_all_batches.py`.

## Rate limit management

### Three rate limit layers

1. **Per-batch token limit** (2M input tokens): If a batch exceeds this, it stays `in_progress` with `processed=0` forever. Mitigated by `--max-tokens-per-batch` (default 1.5M).
2. **TPM (Tokens Per Minute)**: Organization-level throughput cap. When exceeded, new batches queue at `processed=0`. Mitigated by the TPM sliding-window tracker that paces submissions to stay under `TPM_LIMIT * pace_threshold`.
3. **Enqueued token limit**: Total tokens across all in-flight batches. Mitigated by `--max-inflight-batches`.

### TPM sliding window tracker

The processor maintains a 60-second sliding window of token consumption. Before each batch submission (or each request in per-record mode), it checks whether consuming the estimated tokens would exceed `TPM_LIMIT * TPM_PACE_THRESHOLD`. If so, it waits for the window to drain before proceeding.

TPM snapshots are logged after each batch submission and at the end of the run.

### Token-budget batching (replaces fixed-size batching)

Instead of grouping articles by a fixed count (`--batch-size 75`), the processor now groups articles by **token budget** (`--max-tokens-per-batch 1500000`). It uses tiktoken to count exact tokens per article and dynamically sizes batches. This ensures no batch exceeds OpenAI's 2M input token limit.

### Content truncation (replaces `[:800]`)

The old hard 800-character truncation has been replaced by **tiktoken-based token budget truncation**. Article content is truncated to `--max-tokens-per-request` tokens (default 4096, ~16K chars), giving the model ~10× more context while keeping costs predictable.

### How to tune for your environment

| Symptom | Tuning |
|---|---|
| Batches stuck at `processed=0` for long periods | Increase `--per-batch-timeout` (try 7200). Lower `--tpm-pace-threshold` (try 0.5). Check `--tpm-limit` matches your tier. |
| `token_limit_exceeded` errors | Decrease `--max-inflight-batches` (try 1–5). Decrease `--max-tokens-per-batch` (try 500000). |
| Too many small batches | Increase `--max-tokens-per-batch` (up to 2000000). |
| Per-record mode hitting rate limits | The TPM tracker also paces per-record requests. Adjust `--tpm-limit` and `--tpm-pace-threshold`. |

Example restart with conservative throttling:

```bash
python process_articles.py ../ttd_scrape/scraped_data \
  --max-tokens-per-batch 500000 \
  --max-inflight-batches 3 \
  --tpm-pace-threshold 0.5 \
  --per-batch-timeout 7200
```

## Project Structure

- `process_articles.py`: Main batch processing engine
- `providers/openai_provider.py`: OpenAI batch API provider
- `prompt_templates.py`: Prompts for classification and extraction
- `README.md`: This file

## License

Licensed under the LICENSE file provided.
