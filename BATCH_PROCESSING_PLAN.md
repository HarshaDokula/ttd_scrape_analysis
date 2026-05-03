# TTD Batch Processing Implementation Plan

## Overview
Redesign the article processing pipeline to use OpenAI's Batch Processing API instead of individual synchronous calls. This will reduce API costs, improve throughput, and maintain modularity while removing Perplexity provider support.

---

## Requirements

### Functional Requirements
1. **Batch Classification**: Classify multiple articles in a single batch request (true/false for visitor metrics articles)
2. **Batch Extraction**: Extract pilgrim counts and metrics from classified articles in batch mode
3. **Improved Classification**: Reduce false positives from ticket/arrangement articles (e.g., "SEVA TICKETS", "ONLINE DIP")
4. **Failed Record Reprocessing**: Support retrying previously failed records to reduce error percentage
5. **Modular Provider Interface**: Keep provider abstraction but support only OpenAI
6. **Complete Data Traceability**: Preserve article metadata (article_id, title, post URL) in output

### Non-Functional Requirements
1. Remove all Perplexity provider code and dependencies
2. Clean up repository structure
3. Maintain backward compatibility with existing `darshan_data.json` format
4. Support graceful interruption and progress saving

---

## Architecture Changes

### Current Flow (Single Article Processing)
```
CSV → Read Row → Classify (OpenAI sync) → Extract (OpenAI sync) → Store/Fail
```

### New Flow (Batch Processing)
```
CSV → Collect Articles (batch size N) → Format Batch Input → 
  Submit Batch (classify) → Poll for completion → 
  Process Results → Submit Batch (extract) → Poll for completion →
  Extract JSON from responses → Store/Fail → Retry failures
```

---

## File Structure and Changes

### 1. `process_articles.py` (Main Processing Engine)
**Changes:**
- Remove individual `process_record()` function
- Add `BatchProcessor` class with:
  - `collect_articles_batch()`: Group CSV rows into batches
  - `submit_classification_batch()`: Submit articles for classification
  - `submit_extraction_batch()`: Submit classified articles for metric extraction
  - `poll_batch_status()`: Monitor batch job completion
  - `process_batch_results()`: Parse batch responses and store results
  - `retry_failed_records()`: Reprocess failures from previous runs
- Implement retry logic with exponential backoff
- Add metrics tracking (total processed, success rate, failure rate)
- Output single `darshan_data.json` with article metadata

**Key Parameters:**
- Batch size: 10,000 requests per batch (OpenAI limit)
- Classification batch window: 50-100 articles per batch call
- Extraction batch window: 50-100 articles per batch call
- Max retries: 3 attempts per failed record

---

### 2. `providers/openai_provider.py` (Refactored)
**Changes:**
- Remove direct HTTP session management
- Implement OpenAI client-based batch submission
- Add methods:
  - `submit_classify_batch(articles: List[Dict])`: Submit batch for classification
  - `submit_extract_batch(articles: List[Dict])`: Submit batch for extraction
  - `poll_batch_status(batch_id: str)`: Check batch job status
  - `get_batch_results(batch_id: str)`: Retrieve completed batch results
- Keep single-call methods for backward compatibility if needed
- Error handling for rate limits and API failures

**Batch Request Format (Classification):**
```json
[
  {
    "custom_id": "req-1-classify",
    "method": "POST",
    "url": "/v1/chat/completions",
    "body": {
      "model": "gpt-4o-mini",
      "messages": [{"role": "user", "content": "..."}],
      "temperature": 0.01,
      "max_tokens": 2
    }
  },
  ...
]
```

**Batch Request Format (Extraction):**
```json
[
  {
    "custom_id": "req-1-extract",
    "method": "POST",
    "url": "/v1/chat/completions",
    "body": {
      "model": "gpt-4o-mini",
      "messages": [{"role": "user", "content": "..."}],
      "temperature": 0.01,
      "max_tokens": 200
    }
  },
  ...
]
```

---

### 3. `prompt_templates.py` (Improved Prompts)
**Current Classification Prompt Issues:**
- Too broad; catches ticket/arrangement articles as false positives

**New Classification Prompt (ttd_prompt_tmpl3):**
```
You are a strict classifier for TTD news articles.

TASK: Return ONLY "true" or "false"

RULES:
- "true": Daily/periodic VISITOR COUNT statistics with actual numbers (e.g., "About 64,801 pilgrims...")
- "false": Everything else INCLUDING:
  - Ticket releases, booking announcements, online DIP
  - Festival/event descriptions, maintenance notices
  - Administrative announcements, dignitaries visits
  - General news about arrangements or facilities

CRITICAL: Look for actual pilgrim count numbers. Arrangement/logistics articles should be "false" even if they mention pilgrims or tickets.

Title: {title}
Article text (first 800 chars): {article_text}

Answer (only "true" or "false"):
```

**New Extraction Prompt (ttd_info_extract_prompt_tmpl_v2):**
```
You are a strict data extractor for TTD articles ABOUT DAILY VISITOR COUNTS.

TASK: Extract only the following from the article:
- day: Integer day of month (1-31), or null if not mentioned
- pilgrim_count: Integer number of pilgrims, or null if not found
- other_metrics: Object with any additional metrics (waiting times, compartments, etc.)

RULES:
- Return VALID JSON ONLY. No extra text.
- If day or pilgrim_count cannot be extracted, use null
- Do NOT invent numbers
- Do NOT extract predictions or future numbers
- Extract ONLY actual historical/current counts

Output Format (and ONLY this, no other text):
```json
{
  "day": <integer or null>,
  "pilgrim_count": <integer or null>,
  "other_metrics": {<key: value pairs>}
}
```

Article text:
{article_text}

Response:
```

---

### 4. `provider_factory.py` (Simplified)
**Changes:**
- Remove Perplexity case
- Return only OpenAI provider
- Remove conditional logic for multiple providers

---

### 5. `providers/perplexity_provider.py` (DELETE)
- Entire file will be removed
- No Perplexity support going forward

---

### 6. `README.md` (Updated)
**Changes:**
- Remove Perplexity references
- Document batch processing flow and performance benefits
- Update usage instructions
- Add section on retry logic and failure handling

---

## Processing Flow Details

### Phase 1: Batch Classification
1. Read CSV files line-by-line into memory
2. Group articles into batches of N (50-100)
3. For each batch:
   - Format batch request (classification prompts)
   - Submit to OpenAI Batch API
   - Log batch_id and submission timestamp
   - Poll status every 10-30 seconds
   - On completion, parse results
   - Separate into "true" (visitors) and "false" (other)
4. Store "true" articles for Phase 2

### Phase 2: Batch Extraction
1. Group classified articles into batches of N (50-100)
2. For each batch:
   - Format batch request (extraction prompts)
   - Submit to OpenAI Batch API
   - Poll status
   - On completion, parse JSON responses
3. For each extracted record:
   - Extract day, pilgrim_count, other_metrics
   - Combine with article metadata
   - Store in `_DARSHAN_ROWS[date_iso]`
4. Handle conflicts: Keep highest pilgrim_count per date

### Phase 3: Failure Handling
1. Collect all records that failed extraction or JSON parsing
2. Optional: Retry failed batch with reduced batch size or single-call API
3. Save unresolved failures to `failed_records_YYYYMMDD_HHMMSS.csv`

### Phase 4: Retry (Optional)
- Accept `--failed-records` CLI argument pointing to previous failed CSV
- Reprocess failed records through Phases 1-3

---

## Output Format

### `darshan_data.json`
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
        "Divya_Dharshan": "1 Compartment / 2 Hours",
        "Special_Entry_Darshan": "closed"
      }
    }
  },
  "2017-02-27": {
    ...
  }
}
```

**Structure:**
- Top-level key: ISO date (YYYY-MM-DD)
- `article_id`, `title`, `post`: Source article metadata
- `data.pilgrim_count`: Extracted visitor count (integer)
- `data.other_metrics`: Additional extracted fields (object, can be empty)

---

## Error Handling & Resilience

### Batch API Errors
- 429 (Rate Limit): Exponential backoff + retry
- 500 (Server Error): Retry up to 3 times
- Timeout: Retry with reduced batch size

### JSON Parse Errors
- Invalid JSON in response: Log error, add to failed records
- Missing pilgrim_count: Use null, don't skip record
- Missing day: Set to null (will be skipped later in pilgrim_count aggregation)

### Date Construction Errors
- If day is null: Skip record (no date to aggregate to)
- If day is 0 or invalid: Skip record
- Invalid month/year: Skip record

### Interrupt Safety
- On KeyboardInterrupt: Save current state, write partial output, append to failed records
- Support resumption from previous failed records

---

## CLI Interface

### Primary Usage
```bash
python process_articles.py <csv_data_dir>
```
- Processes all CSV files in the directory
- Outputs `darshan_data.json` and `failed_records_YYYYMMDD_HHMMSS.csv`

### Retry Failed Records
```bash
python process_articles.py <csv_data_dir> --retry-failed <path_to_failed_csv>
```
- Reprocesses records from a previous failed CSV
- Merges results with new output

---

## Dependencies

**Current:**
- openai==1.99.9 ✓ (already has batch API support)
- python-dotenv
- tqdm
- requests (can be removed after Perplexity removal)

**To Remove:**
- None required; openai client already supports batch API

---

## Testing & Validation

### Sanity Checks
1. Classification accuracy: Sample 20 articles, verify true/false classifications
2. Extraction accuracy: Verify pilgrim_count matches article text
3. Date aggregation: Ensure only one article per date (highest count wins)
4. Failed records: Confirm retries improve success rate

### Edge Cases
- Empty CSV files
- Articles with no pilgrim count
- Duplicate article IDs across dates
- Malformed JSON in batch responses
- Very large article content (truncate to 8000 chars for prompt)

---

## Cleanup Tasks

1. **Remove Perplexity Support:**
   - Delete `providers/perplexity_provider.py`
   - Remove perplexity imports from `provider_factory.py`
   - Remove PERPLEXITY_* env vars from docs

2. **Remove Unused Imports:**
   - Remove `requests` if no longer used
   - Clean up provider imports in `process_articles.py`

3. **Update .gitignore:**
   - Ensure `*.log` is ignored (logs/ dir)
   - Ensure `darshan_*.json` outputs are ignored
   - Ensure `failed_records_*.csv` outputs are ignored

4. **Remove Old Scripts:**
   - Check for and remove any old single-call processing scripts
   - Keep only `process_articles.py`, `visualize.py` (if it exists and is still needed)

---

## Metrics & Logging

### Key Metrics to Track
- Total articles processed
- Classification success rate
- Extraction success rate
- Average time per batch
- Final pilgrim records count
- Failed records count and % of total

### Log Output
```
[INFO] Processing 5 CSV files from data_dir
[INFO] Phase 1: Batch Classification
[INFO] Batch 1: 50 articles → submitted (batch_id: batch_xxx)
[INFO] Batch 1: Polling status... [Pending]
[INFO] Batch 1: Complete ✓ (48 true, 2 false)
...
[INFO] Phase 2: Batch Extraction
[INFO] Batch 1: 48 articles → submitted (batch_id: batch_yyy)
...
[INFO] Phase 3: Failure Handling
[INFO] Found 3 failed records, retrying...
[INFO] Saved 3857 darshan records to darshan_data.json
[INFO] Saved 2 failed records to failed_records_20260502_123456.csv
```

---

## Success Criteria

✓ All Perplexity code removed
✓ Batch OpenAI API used for classification and extraction
✓ False positives reduced (ticket/arrangement articles filtered)
✓ Failed records can be retried
✓ Output is `darshan_data.json` with full metadata
✓ Repository cleaned of unused files
✓ Error percentage reduced to < 5% after retries
✓ Full test run completes successfully

---

## Timeline & Phases

**Phase A: Refactoring & Setup**
- Remove Perplexity provider
- Update provider_factory.py
- Update prompt templates

**Phase B: Batch Processing Core**
- Implement OpenAIProvider batch methods
- Implement BatchProcessor in process_articles.py
- Add batch submission and polling logic

**Phase C: Failure Handling & Retries**
- Implement failure detection and logging
- Add retry logic
- Test with failed records from full_run_1

**Phase D: Testing & Cleanup**
- Integration test with small dataset
- Validate output format
- Remove unused code and imports
- Update README

---

## Questions for Clarification (if any)

1. Should batch size be configurable via CLI or env var? (Currently fixed at 50-100)
2. Should we preserve batch job IDs for auditing? (Currently log them)
3. Should failed records auto-retry or require explicit --retry flag? (Currently explicit)
4. How many days back should we support for archival data? (Currently all dates)

---

## Notes

- OpenAI Batch API has a 24-hour processing SLA (usually much faster)
- Batch requests cost ~50% less than equivalent live API calls
- Each batch can contain up to 10,000,000 tokens of requests
- Responses are available for 29 days after completion
