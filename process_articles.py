import argparse
import csv
import json
from pathlib import Path
from datetime import datetime
import logging
import sys
import time
import pickle
from typing import Any, Dict, Callable
from openai import RateLimitError

from tqdm import tqdm
from dotenv import load_dotenv
load_dotenv()

# ---------------------------
# Logging Setup
# ---------------------------
def setup_logger() -> logging.Logger:
    logs_path = Path("logs")
    logs_path.mkdir(parents=True, exist_ok=True)
    log_filename = logs_path / f"log_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"

    logging.basicConfig(
        format="%(asctime)s [%(levelname)s] %(message)s",
        level=logging.INFO,
        handlers=[logging.FileHandler(log_filename), logging.StreamHandler(sys.stdout)]
    )
    return logging.getLogger(__name__)

logger = setup_logger()

# ---------------------------
# Provider Factory
# ---------------------------
from provider_factory import get_provider
provider = get_provider()

# ---------------------------
# Globals
# ---------------------------
article_ids = []
_DARSHAN_ROWS: Dict[str, Dict[str, Any]] = {}
failed_records = []   # store failed rows
max_attempts = 3

# ---------------------------
# Retry Decorator
# ---------------------------
def with_retry(max_attempts: int = 3):
    """
    Decorator to retry API calls with exponential backoff.
    - RateLimitError: sleep 1 hour.
    - Other exceptions: sleep 10s.
    - Ctrl+C: stop immediately and let main() save progress.
    """
    def decorator(func: Callable):
        def wrapper(*args, **kwargs):
            attempts = 0
            while attempts < max_attempts:
                try:
                    return func(*args, **kwargs)
                except RateLimitError:
                    attempts += 1
                    logger.warning(
                        f"Rate limit error in {func.__name__} attempt {attempts}. Sleeping 1 hour..."
                    )
                    try:
                        time.sleep(3600)
                    except KeyboardInterrupt:
                        logger.warning("Interrupted during 1-hour sleep. Saving progress...")
                        raise
                except KeyboardInterrupt:
                    logger.warning(f"Interrupted by user during {func.__name__}. Saving progress...")
                    raise
                except Exception as e:
                    attempts += 1
                    logger.warning(
                        f"Error in {func.__name__} attempt {attempts}: {e}. Sleeping 10s..."
                    )
                    try:
                        time.sleep(10)
                    except KeyboardInterrupt:
                        logger.warning("Interrupted during sleep. Saving progress...")
                        raise
            logger.error(f"Giving up on {func.__name__} after {max_attempts} attempts.")
            return None
        return wrapper
    return decorator

# ---------------------------
# Utility: Extract JSON safely
# ---------------------------
def extract_json(text: str) -> dict:
    start = text.find('{')
    end = text.rfind('}')
    if start == -1 or end == -1:
        return None

    json_str = text[start:end+1]
    try:
        data = json.loads(json_str)
    except json.JSONDecodeError as e:
        logger.error("JSON decode error: %s", e)
        logger.error("Raw metrics: %s", text)
        return None
    return data

# ---------------------------
# Wrapped Provider Calls
# ---------------------------
@with_retry(max_attempts)
def classify_article(title: str, content: str) -> str:
    return provider.classify_article(title, content)

@with_retry(max_attempts)
def extract_metrics(content: str) -> str:
    return provider.extract_metrics(content)

# ---------------------------
# Processing
# ---------------------------
def process_record(row: Dict[str, str], year: str, month: str) -> None:
    title = (row.get("title") or "").strip()
    content = (row.get("content") or "").strip()
    article_id = (row.get("article_id") or "").strip()

    # ---- Classification ----
    classification = classify_article(title, content)
    if not classification:
        failed_records.append(row)
        return
    logger.info(f"Classified article {article_id} as {classification}")

    if classification != "true":
        return
    article_ids.append(article_id)

    # ---- Metrics Extraction ----
    datastr = extract_metrics(content)
    if not datastr:
        failed_records.append(row)
        return

    data = extract_json(datastr)
    logger.info(f"Extracted data for article {article_id}: {data}")
    if not data:
        failed_records.append(row)
        return

    try:
        day = data['day']
        del data['day']  # remove day after use
    except Exception:
        logger.warning(f"Skipping record {article_id} due to missing 'day' in extracted data.")
        failed_records.append(row)
        return

    day = "00" if (day == 0 or day is None) else str(day).zfill(2)
    try:
        date_iso = f"{year}-{month.zfill(2)}-{day.zfill(2)}"
    except Exception:
        logger.warning(f"Skipping record with invalid date components: year={year}, month={month}, day={day}")
        failed_records.append(row)
        return

    if day == "00":
        logger.warning(f"Skipping record with invalid day in date: {date_iso}")
        failed_records.append(row)
        return

    payload = {
        "article_id": article_id,
        "title": title,
        "post": row.get("link"),
        "data": data,
    }

    # ---- Safe pilgrim_count handling ----
    for attempt in range(max_attempts):
        try:
            new_count = payload['data'].get('pilgrim_count')
            old_count = _DARSHAN_ROWS.get(date_iso, {}).get('data', {}).get('pilgrim_count')

            if isinstance(new_count, str) and new_count.isdigit():
                new_count = int(new_count)
                payload['data']['pilgrim_count'] = new_count
            if isinstance(old_count, str) and old_count.isdigit():
                old_count = int(old_count)
                _DARSHAN_ROWS[date_iso]['data']['pilgrim_count'] = old_count

            if new_count is None:
                raise ValueError("pilgrim_count is None in new payload")

            if date_iso in _DARSHAN_ROWS:
                if old_count is None or old_count < new_count:
                    _DARSHAN_ROWS[date_iso] = payload
            else:
                _DARSHAN_ROWS[date_iso] = payload

            break
        except KeyboardInterrupt:
            logger.warning("Interrupted during pilgrim_count handling. Saving progress...")
            raise
        except Exception as e:
            logger.warning(f"Error handling pilgrim_count for {article_id} attempt {attempt+1}: {e}")
            try:
                time.sleep(2)
            except KeyboardInterrupt:
                logger.warning("Interrupted during sleep. Saving progress...")
                raise
    else:
        logger.error(f"Skipping record {article_id} after repeated pilgrim_count errors.")
        failed_records.append(row)

# ---------------------------
# Output
# ---------------------------
def finalize_output(out_path: Path = Path("darshan_data.json")) -> None:
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(_DARSHAN_ROWS, f, ensure_ascii=False, indent=2)
    print(f"Saved {len(_DARSHAN_ROWS)} Darshan records to {out_path}")

def save_failed_records() -> None:
    if not failed_records:
        return
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_path = Path(f"failed_records_{timestamp}.csv")
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=failed_records[0].keys())
        writer.writeheader()
        writer.writerows(failed_records)
    logger.info(f"Saved {len(failed_records)} failed records to {out_path}")

# ---------------------------
# Main
# ---------------------------
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("data_dir", type=str)
    args = parser.parse_args()

    data_dir = Path(args.data_dir)
    csv_files = list(data_dir.glob("*.csv"))
    logger.info(f"Found {len(csv_files)} CSV files in {data_dir}")

    try:
        for csv_file in csv_files:
            logger.info(f"Processing file: {csv_file}")
            stem_parts = csv_file.stem.split("_")
            if len(stem_parts) >= 3:
                year, month = stem_parts[1], stem_parts[2]
            else:
                logger.error(f"Filename format unexpected: {csv_file.name}")
                continue

            with open(csv_file, encoding="utf-8") as f:
                reader = csv.DictReader(f)
                for row in tqdm(reader):
                    process_record(row, year, month)

    except KeyboardInterrupt:
        logger.warning("Interrupted by user. Saving progress before exit...")
    except Exception as e:
        logger.error(f"Fatal error occurred: {e}", exc_info=True)
    finally:
        finalize_output()
        with open("article_ids.pkl", "wb") as fp:
            pickle.dump(article_ids, fp)
        save_failed_records()
        logger.info("Progress saved. Exiting.")

if __name__ == "__main__":
    main()
