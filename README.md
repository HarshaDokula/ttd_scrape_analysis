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

## Logs

Logs are saved in the `logs/` directory with timestamped filenames.

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

## Batch size rationale and throttling

Default batch size is 75. You may wonder why we keep that default even if some batches failed with a `token_limit_exceeded` error. Important clarifications:

- `token_limit_exceeded` is an organization-level *enqueued tokens* limit (e.g. "2,000,000 enqueued tokens"). It is triggered by too many tokens queued across all active batches — not necessarily by any single batch's size.

- Batch size controls how many articles are grouped into a single batch request. Larger batch sizes reduce the total number of batches (fewer batch submissions to manage) but increase the token footprint per batch; smaller batch sizes reduce per-batch tokens but increase the number of concurrent batches when you submit many at once.

Why 75 is a good default:

- It balances throughput and manageability for this dataset and prompt sizes (we truncate article text to ~800 chars in requests to reduce token usage).
- It avoids extremely large per-batch token counts, keeping individual batch requests reasonable while allowing decent parallelism.
- Empirically it produced good throughput for previous runs while keeping per-batch parsing/simple retry logic straightforward.

Why you still saw `token_limit_exceeded`:

- The failures you observed were caused by submitting too many batches at once (many in-flight batches), which together exceeded the organization's enqueued token quota. The fix is to control concurrency, not necessarily to reduce the batch size.

What we changed to address this:

- `--max-inflight-batches` (default 10) — limits how many OpenAI batch jobs we keep active at once. The processor will pause submissions and poll existing batches until capacity frees up.
- Round-robin polling — we no longer block permanently on a single stuck batch (this prevented progress earlier).
- Improved error logging — inline batch errors (like `token_limit_exceeded`) are now surfaced immediately and saved to logs.

How to tune for your environment:

- If you see token_limit_exceeded errors, decrease `--max-inflight-batches` (try 1–5) when restarting.
- If you have a larger token quota and want higher throughput, increase `--max-inflight-batches` and/or batch size (but monitor enqueued tokens).
- If your articles are longer, keep batch size smaller to cap tokens per batch; if your articles are short, you can increase batch size.

Example restart with safer defaults:

```bash
python process_articles.py ../ttd_scrape/scraped_data --batch-size 75 --max-inflight-batches 5
```

We intentionally keep a conservative default (75) because it works well in practice with the provided prompt truncation and because the concurrency control (`--max-inflight-batches`) is the primary knob to avoid organization-wide token limits.

## Project Structure

- `process_articles.py`: Main batch processing engine
- `providers/openai_provider.py`: OpenAI batch API provider
- `prompt_templates.py`: Prompts for classification and extraction
- `README.md`: This file

## License

Licensed under the LICENSE file provided.
