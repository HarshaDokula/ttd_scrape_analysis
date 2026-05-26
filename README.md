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
- Run the **unified batch pipeline**: classify articles AND extract metrics in a single event loop, with overlapping phases
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
| `--max-tokens-per-batch` | 500,000 | Max tokens per batch file. Smaller batches allow 2-3 to run concurrently within the enqueued token budget, preventing a single stuck batch from blocking the pipeline. |
| `--tpm-limit` | 2,000,000 | Organization TPM (tokens per minute) limit. Used to pace batch submissions. |
| `--tpm-pace-threshold` | 0.75 | Fraction of TPM at which to start pacing submissions. |
| `--per-batch-timeout` | 86400 | Absolute timeout — seconds before giving up on a batch entirely (default 24h, matches OpenAI's completion window). Batches at `processed=0` are usually just queued — increase this rather than cancelling. |
| `--batch-stall-timeout` | 3900 | **Stall detection** — seconds without progress after which a batch is cancelled via the API and retried as new batches (default 1h 5m). Prevents a single stuck batch from blocking the pipeline. |
| `--max-enqueued-tokens` | 2,000,000 | Org-level enqueued token limit (from batch error messages). Total tokens across all non-terminal batches must stay under this. |
| `--enqueued-pace-threshold` | 0.90 | Fraction of enqueued limit at which to pause submissions. Increased from 0.75 so more batches can coexist in-flight. |

Example with custom settings:

```bash
python process_articles.py ../ttd_scrape/scraped_data \
  --max-tokens-per-request 4096 \
  --max-tokens-per-batch 500000 \
  --max-inflight-batches 5 \
  --batch-stall-timeout 3900
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

## How the pipeline works

The pipeline uses a **unified event loop** that handles both classification and extraction in parallel, rather than running them in two sequential phases. Here's the full flow:

1. **Load articles** — reads all CSV files from the data directory.

2. **Split into token-budgeted batches** — articles are grouped into classification batches sized by token count (not article count) using tiktoken.

3. **Submit batches** — classification batches are submitted to the OpenAI Batch API, up to `max_inflight_batches` at a time.

4. **Poll in round-robin** — the event loop polls ALL active batches every ~10 seconds. Both classification and extraction batches share the same inflight slot pool.

5. **Classification completes → feeds extraction** — when a classification batch finishes, its TRUE articles are immediately added to an extraction queue. Extraction batches are submitted from this queue as inflight slots become available.

6. **Stall detection** — if a batch's `processed` count hasn't advanced for `batch_stall_timeout` (default 1h 5m), it is **cancelled via the OpenAI API** and its articles are redistributed into new batches and re-submitted. This frees the inflight slot.

7. **Absolute timeout** — if a batch still hasn't completed after `per_batch_timeout` (default 24h), it's marked as failed and its articles are recorded in `failed_records_*.csv`.

8. **Done** — the loop exits when both classification and extraction queues are empty and no active batches remain.

### Why overlapping phases matter

Old behavior (two-phase):
```
[classify batch 1] [classify batch 2] [classify batch 3]  ← all must finish
                                                              ↓
                                   [extract on all TRUE articles]  ← blocked until classification is DONE
```

New behavior (unified):
```
[classify batch 1] [classify batch 2] [classify batch 3]
        ↓                ↓
[extract batch 1a]  [extract batch 1b]  [classify batch 3...]
        ↓                                   ↓
[extract batch 2a]                  [extract batch 3a]
```

Classification results feed extraction immediately — if batch 3 gets stuck, extraction on batches 1 and 2 is already running.

### How stall detection avoids the 24-hour deadlock

Without stall detection, a batch stuck at `processed=1323` for 16+ minutes would block the pipeline for the full `per_batch_timeout` (24h). With stall detection:

- After **5 minutes** (configurable via `--batch-stall-timeout`) of no progress, the batch is cancelled.
- Its articles are split into fresh batches and re-submitted.
- The inflight slot is freed so other work can proceed.
- If the new batches also stall, they'll be retried again — the process doesn't deadlock.

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

### Rate limit layers

1. **Per-batch token limit** (2M input tokens): If a batch exceeds this, it fails with `token_limit_exceeded`. Mitigated by token-budget batching (`--max-tokens-per-batch`).
2. **Enqueued token limit** (2M total): Total tokens across ALL non-terminal batches. If exceeded, new batches are rejected. The processor now uses **optimistic submission** — it submits batches without proactively blocking on the enqueued budget. If OpenAI rejects a batch with `token_limit_exceeded`, the error handler parses the actual limit and backs off automatically. This prevents a single stuck batch from deadlocking the entire pipeline.
3. **TPM (Tokens Per Minute)**: Organization-level throughput cap. When exceeded, batches queue at `processed=0`. Mitigated by the TPM sliding-window tracker.
4. **Enqueued batch count**: Max number of concurrent batch jobs. Mitigated by `--max-inflight-batches`.

### TPM sliding window tracker

The processor maintains a 60-second sliding window of token consumption. Before each batch submission (or each request in per-record mode), it checks whether consuming the estimated tokens would exceed `TPM_LIMIT * TPM_PACE_THRESHOLD`. If so, it waits for the window to drain before proceeding.

TPM snapshots are logged after each batch submission and at the end of the run.

### Token-budget batching (replaces fixed-size batching)

Instead of grouping articles by a fixed count (`--batch-size 75`), the processor now groups articles by **token budget** (`--max-tokens-per-batch 500000`). It uses tiktoken to count exact tokens per article and dynamically sizes batches. Keeping batches small (500K tokens) allows multiple batches to run concurrently within the org-level enqueued token limit.

### Content truncation (replaces `[:800]`)

The old hard 800-character truncation has been replaced by **tiktoken-based token budget truncation**. Article content is truncated to `--max-tokens-per-request` tokens (default 4096, ~16K chars), giving the model ~10× more context while keeping costs predictable.

### Unified pipeline (overlapping classification + extraction)

Instead of running classification and extraction as two sequential phases, the processor now uses a **single event loop** that manages both. Classification batches and extraction batches share the same `max_inflight_batches` pool, polled together in round-robin. As soon as a classification batch completes, its TRUE articles are fed into the extraction queue and submitted as inflight slots free up.

This means:
- Extraction starts **immediately** on completed classification results — no need to wait for ALL batches.
- If one classification batch gets stuck, extraction on earlier batches is already running.
- A stuck batch is detected via `--batch-stall-timeout` (no progress for N seconds), cancelled, and retried as new batches.

### How to tune for your environment

| Symptom | Tuning |
|---|---|
| Batches stuck at `processed=0` for long periods | Increase `--per-batch-timeout` (default 86400s / 24h). Lower `--tpm-pace-threshold` (try 0.5). Check `--tpm-limit` matches your tier. |
| `token_limit_exceeded` errors | **Proactive enqueued gating** now waits before submitting if the projected enqueued tokens would exceed `max_enqueued_tokens * enqueued_pace_threshold`. If you still see these errors, decrease `--max-inflight-batches` (try 2–3) or decrease `--max-tokens-per-batch` (try 250000). |
| Too many small batches | Increase `--max-tokens-per-batch` (try 1000000). |
| Pipeline blocked on one stuck batch | **Stall detection** cancels the batch after `--batch-stall-timeout` (default 1h 5m) without progress and retries its articles in new batches. Overlapping phases also mean extraction on completed batches is already running. |
| Submission failures lose articles | **Re-queue on failure** — if a batch submission fails (e.g. due to rate limits), its articles are re-queued for retry rather than being permanently marked as failed. |
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
