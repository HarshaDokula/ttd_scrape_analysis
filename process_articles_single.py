import argparse
import csv
import json
import os
import re
from pathlib import Path
from datetime import datetime
from typing import Any, Dict, Optional
from dotenv import load_dotenv
from openai import OpenAI
import logging
import sys
from prompt_templates import ttd_prompt_tmpl
import time
from tqdm import tqdm
import pickle

logs_path = Path("logs")
logs_path.mkdir(parents=True, exist_ok=True)

# Create a new log file name with timestamp
log_filename = logs_path / f"log_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    level=logging.INFO,
    handlers=[logging.FileHandler(Path(log_filename))]
)

logger = logging.getLogger(__name__)

article_ids = []
# Load env vars from your config .env file
load_dotenv()
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")

client = OpenAI(api_key=OPENAI_API_KEY)

# ---------------------------
# GPT-based article classifier
# ---------------------------

def classify_article_with_gpt(title: str, content: str) -> str:
    """
    Calls GPT to classify an article into 'darshan' or 'other'.
    Returns the string 'darshan' or 'other'.

    Args:
        title (str): The article title.
        content (str): The article content.
    Returns:
        str: Classification label, either 'darshan' or 'other'.
    """
    # Limit content length to keep costs low
    # TODO:we could do nlp preprocessing here like removing stopwords,lemmatization, etc. if we want to reduce tokens, better than truncating randomly
    snippet = (content or "")[:800]

    
    prompt = ttd_prompt_tmpl.format(title=title.strip(), article_text=snippet.strip())
    # logger.info(f"prompt: {prompt}")

    # Call OpenAI API
    # Use a very low max_tokens to ensure we only get the label
    resp = client.chat.completions.create(
        model=OPENAI_MODEL,
        messages=[
            {"role": "system", "content": "You classify TTD news articles."},
            {"role": "user", "content": prompt}
        ],
        max_tokens=2,
        temperature=0.01
    )
    logger.info(resp)
    label = resp.choices[0].message.content.strip().lower()
    logger.info(f"predicted label: {label}")
    if label not in ("true", "false"):
        logger.info(f"Unexpected label: {label}, defaulting to 'false'")
        label = "false"
    return label

# ---------------------------
# Your existing parsing logic
# (only shortened here for clarity)
# ---------------------------

_DARSHAN_ROWS: Dict[str, Dict[str, Any]] = {}

def _normalize_year(y: int) -> int:
    return 2000 + y if 0 <= y < 100 else y

def _best_effort_iso_date(row: Dict[str, str], title: str, content: str) -> Optional[str]:
    # Simple: if row['year'], row['month'], try to guess day from title
    y_csv = row.get("year")
    m_csv = row.get("month")

    m_day = re.search(r"\b([A-Z][a-z]+)\s+(\d{1,2})\b", title)
    if m_day and y_csv:
        month_name, day = m_day.group(1), int(m_day.group(2))
        month_map = {m.lower(): i for i, m in enumerate(
            ["January","February","March","April","May","June",
             "July","August","September","October","November","December"], 1)}
        if month_name.lower() in month_map:
            try:
                return datetime(int(y_csv), month_map[month_name.lower()], day).date().isoformat()
            except ValueError:
                pass

    if y_csv and m_csv:
        try:
            return datetime(int(y_csv), int(m_csv), 1).date().isoformat()
        except ValueError:
            return None
    return None

def _extract_metrics(title: str, content: str) -> Dict[str, Any]:
    metrics = {}
    m = re.search(r"(\d{1,3}(?:,\d{3})+|\d+)\s+pilgrims?\s+had", title, re.I)
    if m:
        metrics["total_pilgrims"] = int(m.group(1).replace(",", ""))
    return metrics

def process_record(row: Dict[str, str]) -> None:
    title = (row.get("title") or "").strip()
    content = (row.get("content") or "").strip()
    article_id = (row.get("article_id") or "").strip()

    classification = classify_article_with_gpt(title, content)
    if classification != "true":
        return
    article_ids.append(article_id)
    date_iso = _best_effort_iso_date(row, title, content)
    if not date_iso:
        logger.info(f"date_iso not found")
        return

    data = _extract_metrics(title, content)
    payload = {
        "article_id": row.get("article_id"),
        "title": title,
        "post": row.get("link"),
        "data": data
    }
    _DARSHAN_ROWS[date_iso] = payload

def finalize_output(out_path: Path = Path("darshan_data.json")) -> None:
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(_DARSHAN_ROWS, f, ensure_ascii=False, indent=2)
    print(f"Saved {len(_DARSHAN_ROWS)} Darshan records to {out_path}")

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("data_dir", type=str)
    args = parser.parse_args()

    data_dir = Path(args.data_dir)
    csv_files = list(data_dir.glob("*.csv"))
    logger.info(f"Found {len(csv_files)} CSV files in {data_dir}")
    logger.info(f"Processing files {csv_files}")
    for csv_file in csv_files:
        logger.info(f"Processing file: {csv_file}")
        with open(csv_file, encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in tqdm(reader):
                process_record(row)

    finalize_output()

if __name__ == "__main__":
    main()
    with open('article_ids.pkl','wb') as fp:
        pickle.dump(article_ids,fp)
