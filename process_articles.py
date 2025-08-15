import argparse
import csv
import json
import os
import re
import logging
from pathlib import Path
from datetime import datetime
from typing import Any, Dict, Optional, List
from dotenv import load_dotenv
from openai import OpenAI

# ---------------------------
# Setup logging
# ---------------------------
logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    level=logging.INFO
)

# Load env vars from your config .env file
load_dotenv()
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")

client = OpenAI(api_key=OPENAI_API_KEY)

# ---------------------------
# GPT-based article classifier (batched)
# ---------------------------

def classify_articles_with_gpt(batch: List[Dict[str, str]]) -> List[str]:
    """
    Calls GPT to classify multiple articles into 'darshan' or 'other'.
    Returns a list of labels in the same order as the batch.
    """
    # Build prompt for all items in batch
    prompt_lines = [
        "You are a classifier for news articles from the Tirumala Tirupati Devasthanams (TTD).",
        "Given each article title and snippet, classify strictly as one of:",
        "- darshan: Daily/periodic pilgrim statistics at Tirumala, e.g., 'About 64,801 pilgrims had Srivari darshan...'",
        "- other: All other news (festivals, dignitaries visits, events, admin notices, etc.)",
        "Respond with one label per line in the same order, ONLY 'darshan' or 'other'.",
        ""
    ]

    for i, item in enumerate(batch, start=1):
        snippet = (item["content"] or "")[:800]
        prompt_lines.append(f"Article {i}:\nTitle: {item['title'].strip()}\nSnippet: {snippet.strip()}")
        prompt_lines.append("")

    prompt = "\n".join(prompt_lines)

    resp = client.chat.completions.create(
        model=OPENAI_MODEL,
        messages=[
            {"role": "system", "content": "You classify TTD news articles."},
            {"role": "user", "content": prompt}
        ],
        max_tokens=len(batch) * 5,
        temperature=0
    )

    lines = [line.strip().lower() for line in resp.choices[0].message.content.strip().splitlines()]
    # Ensure we always return the right length
    labels = []
    for label in lines:
        if label not in ("darshan", "other"):
            labels.append("other")
        else:
            labels.append(label)

    # If API returned fewer lines than batch size
    while len(labels) < len(batch):
        labels.append("other")

    return labels

# ---------------------------
# Parsing logic (unchanged)
# ---------------------------

_DARSHAN_ROWS: Dict[str, Dict[str, Any]] = {}

def _normalize_year(y: int) -> int:
    return 2000 + y if 0 <= y < 100 else y

def _best_effort_iso_date(row: Dict[str, str], title: str, content: str) -> Optional[str]:
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

def process_records(rows: List[Dict[str, str]]) -> None:
    batch_labels = classify_articles_with_gpt(rows)
    for row, classification in zip(rows, batch_labels):
        if classification != "darshan":
            continue
        title = (row.get("title") or "").strip()
        content = (row.get("content") or "").strip()
        date_iso = _best_effort_iso_date(row, title, content)
        if not date_iso:
            continue
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
    logging.info(f"Saved {len(_DARSHAN_ROWS)} Darshan records to {out_path}")

# ---------------------------
# Main entry
# ---------------------------

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("data_dir", type=str)
    parser.add_argument("--batch-size", type=int, default=10, help="Number of rows per GPT request")
    args = parser.parse_args()

    data_dir = Path(args.data_dir)
    logging.info(f"Processing directory: {data_dir}")

    for csv_file in data_dir.glob("*.csv"):
        logging.info(f"Reading file: {csv_file}")
        with open(csv_file, encoding="utf-8") as f:
            reader = csv.DictReader(f)
            batch = []
            for row in reader:
                batch.append({
                    "title": row.get("title", ""),
                    "content": row.get("content", ""),
                    "article_id": row.get("article_id", ""),
                    "link": row.get("link", ""),
                    "year": row.get("year", ""),
                    "month": row.get("month", "")
                })
                if len(batch) >= args.batch_size:
                    logging.info(f"Classifying batch of {len(batch)} rows from {csv_file.name}")
                    process_records(batch)
                    batch = []
            # Process remaining rows
            if batch:
                logging.info(f"Classifying final batch of {len(batch)} rows from {csv_file.name}")
                process_records(batch)

    finalize_output()

if __name__ == "__main__":
    main()
