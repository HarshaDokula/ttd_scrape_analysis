import argparse
import csv
import json
from pathlib import Path
from datetime import datetime
import logging
import sys
import time
import pickle
from typing import Any, Dict

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

article_ids = []
_DARSHAN_ROWS: Dict[str, Dict[str, Any]] = {}
max_attempts = 3

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
# Processing
# ---------------------------
def process_record(row: Dict[str, str], year: str, month: str) -> None:
    title = (row.get("title") or "").strip()
    content = (row.get("content") or "").strip()
    article_id = (row.get("article_id") or "").strip()

    # Retry wrapper
    attempts = 0
    while attempts < max_attempts:
        try:
            classification = provider.classify_article(title, content)
            break
        except Exception:
            attempts += 1
            logger.warning(f"Rate limit or error in classification attempt {attempts}. Sleeping 10s...")
            time.sleep(10)
    else:
        logger.warning(f"Skipping record {article_id} due to repeated classification errors.")
        return

    if classification != "true":
        return
    article_ids.append(article_id)

    # Extract metrics
    attempts = 0
    while attempts < max_attempts:
        try:
            datastr = provider.extract_metrics(content)
            break
        except Exception:
            attempts += 1
            logger.warning(f"Rate limit or error in extraction attempt {attempts}. Sleeping 10s...")
            time.sleep(10)
    else:
        logger.warning(f"Skipping record {article_id} due to repeated extraction errors.")
        return

    data = extract_json(datastr)
    if not data:
        return

    payload = {
        "article_id": article_id,
        "title": title,
        "post": row.get("link"),
        "data": data,
    }

    day = data.get("date", {}).get("day", "00")
    day = "00" if day == 0 else str(day).zfill(2)

    try:
        date_iso = f"{year}-{month.zfill(2)}-{day.zfill(2)}"
    except Exception:
        logger.warning(f"Skipping record with invalid date components: year={year}, month={month}, day={day}")
        return

    if day == "00":
        logger.warning(f"Skipping record with invalid day in date: {date_iso}")
        return

    _DARSHAN_ROWS[date_iso] = payload

# ---------------------------
# Output
# ---------------------------
def finalize_output(out_path: Path = Path("darshan_data.json")) -> None:
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(_DARSHAN_ROWS, f, ensure_ascii=False, indent=2)
    print(f"Saved {len(_DARSHAN_ROWS)} Darshan records to {out_path}")

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

    finalize_output()

    with open("article_ids.pkl", "wb") as fp:
        pickle.dump(article_ids, fp)

if __name__ == "__main__":
    main()
