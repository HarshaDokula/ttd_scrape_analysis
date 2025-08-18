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
# Import Client from factory
# ---------------------------
from client_factory import get_client
client, client_type, RateLimitError = get_client()

# ---------------------------
# Prompt Templates
# ---------------------------
from prompt_templates import (
    ttd_prompt_tmpl2,
    ttd_info_extract_prompt_tmpl,
)

article_ids = []
max_attempts = 3
_DARSHAN_ROWS: Dict[str, Dict[str, Any]] = {}

# ---------------------------
# Classification Function
# ---------------------------
def classify_article(title: str, content: str) -> str:
    snippet = (content or "")[:800]
    prompt = ttd_prompt_tmpl2.format(title=title.strip(), article_text=snippet.strip())

    attempts = 0
    while attempts < max_attempts:
        try:
            extra_kwargs = {"extra_body": {"disable_search": True}} if client_type == "perplexity" else {}
            resp = client.chat_completion(
                prompt=prompt,
                max_tokens=2,
                temperature=0.01,
                **extra_kwargs
            )
            break
        except RateLimitError:
            attempts += 1
            logger.warning(f"RateLimitError attempt {attempts}. Sleeping before retry...")
            time.sleep(10)
    else:
        raise RateLimitError

    label = resp.choices[0].message.content.strip().lower()
    return label if label in ("true", "false") else "false"

# ---------------------------
# Metric Extraction
# ---------------------------
def _extract_metrics(content: str) -> str:
    extractor_prompt = ttd_info_extract_prompt_tmpl.format(article_text=content.strip())
    attempts = 0

    while attempts < max_attempts:
        try:
            info = client.chat_completion(
                prompt=extractor_prompt,
                max_tokens=200,
                temperature=0.01,
            )
            break
        except RateLimitError:
            attempts += 1
            logger.warning(f"RateLimitError attempt {attempts}. Sleeping before retry...")
            time.sleep(10)
    else:
        raise RateLimitError

    return info.choices[0].message.content.strip()

# ---------------------------
# Processing
# ---------------------------
def process_record(row: Dict[str, str]) -> None:
    title = (row.get("title") or "").strip()
    content = (row.get("content") or "").strip()
    article_id = (row.get("article_id") or "").strip()

    classification = classify_article(title, content)
    if classification != "true":
        return
    article_ids.append(article_id)

    datastr = _extract_metrics(content)
    try:
        data = json.loads(datastr)
    except json.JSONDecodeError:
        logger.error(f"JSON decode error. Raw: {datastr}")
        return

    payload = {
        "article_id": article_id,
        "title": title,
        "post": row.get("link"),
        "data": data,
    }

    day = data.get("date", {}).get("day", "00")
    day = "00" if day == 0 else str(day)

    date_iso = ""  # TODO: add proper year/month parsing
    if date_iso == "00":
        logger.warning(f"Skipping record with invalid date: {date_iso}")
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
        with open(csv_file, encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in tqdm(reader):
                try:
                    process_record(row)
                except RateLimitError:
                    logger.warning("Rate limit hit, skipping record.")
                    continue

    finalize_output()

    with open("article_ids3.pkl", "wb") as fp:
        pickle.dump(article_ids, fp)


if __name__ == "__main__":
    main()
