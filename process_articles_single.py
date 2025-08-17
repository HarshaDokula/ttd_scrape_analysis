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
from openai import RateLimitError
import logging
import sys
from prompt_templates import ttd_prompt_tmpl, ttd_prompt_tmpl2, ttd_info_extract_prompt_tmpl
import time
from tqdm import tqdm
import pickle
from completions import OpenAICompletionClient, PerplexityCompletionClient

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
max_attempts = 3
# Load env vars from your config .env file
load_dotenv()
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
PERPLEXITY_MODEL = os.getenv("PERPLEXITYAI_MODEL", "sonar")
PERPLEXITY_API_KEY = os.getenv("PERPLEXITYAI_API_KEY")

client = PerplexityCompletionClient(
    api_key=PERPLEXITY_API_KEY,
    model=PERPLEXITY_MODEL
)

client_openai = OpenAICompletionClient(
    api_key=OPENAI_API_KEY,
    model=OPENAI_MODEL
)
# ---------------------------
# LLM-based article classifier
# ---------------------------

def classify_article_with_gpt(title: str, content: str) -> str:
    """
    Calls GPT to classify an article into 'darshan' or 'other'.
    Returns the string 'darshan' or 'other'.

    Args:
        title (str): The article title.
        content (str): The article content.
    Returns:
        str: Classification label, either 'true' or 'false'.
    """
    # Limit content length to keep costs low
    # TODO:we could do nlp preprocessing here like removing stopwords,lemmatization, etc. if we want to reduce tokens, better than truncating randomly
    snippet = (content or "")[:800]

    
    prompt = ttd_prompt_tmpl2.format(title=title.strip(), article_text=snippet.strip())
    # logger.info(f"prompt: {prompt}")

    # Call OpenAI API
    # Use a very low max_tokens to ensure we only get the label
    attempts=0
    while attempts  < max_attempts:
        try:
            resp = client_openai.chat_completion(
            prompt=prompt,
            max_tokens=2,
            temperature=0.01,  # Low temperature for deterministic output
            extra_body={"disable_search": True}  # Disable search for Perplexity
            )
            break
        except RateLimitError as rate:
            attempts +=1
            logger.warning(f"RateLimitError encounteredon attempt {attempts}. Sleeping for 10 seconds before retrying...")
            time.sleep(10)
    else:
        raise RateLimitError
    
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


def _extract_metrics(content: str) -> Dict[str, Optional[Any]]:
    """
    Extracts date, pilgrim count, and other metrics from the content.
    Returns a dictionary with the extracted information.
    """
    
    extractor_prompt = ttd_info_extract_prompt_tmpl.format(article_text=content.strip())
    logger.info(f"extractor prompt: {extractor_prompt}")

    attempts=0
    while attempts  < max_attempts:
        try:
            info = client_openai.chat_completion(
            prompt=extractor_prompt,max_tokens=200,
            temperature=0.01,  # Low temperature for deterministic output
            # stop="```"  # Stop sequence to end the response
        )
            break
        except RateLimitError as rate:
            attempts +=1
            logger.warning(f"RateLimitError encounteredon attempt {attempts}. Sleeping for 10 seconds before retrying...")
            time.sleep(10)
    else:
        raise RateLimitError
    
    logger.info(info)
    metrics = info.choices[0].message.content.strip().lower()
    logger.info(f"extracted metrics: {metrics}")
    
    return metrics

#this function needed if we use perplexity client beacause perplexity doesn't have a stop word param so we get json prefixed with with ```json
# and suffixed with ```
def extract_json(text: str) -> dict:
    """
    Extracts a JSON object from the provided text.
    
    The function looks for the first occurrence of '{'
    and the last occurrence of '}' in the string,
    extracts that substring and tries to parse it as JSON.
    
    Returns the JSON object (as a dict) if successful, otherwise None.
    """
    import json

    # Find the boundaries of the JSON object
    start = text.find('{')
    end = text.rfind('}')
    if start == -1 or end == -1:
        return None

    json_str = text[start:end+1]
    
    try:
        data = json.loads(json_str)
    except json.JSONDecodeError as e:
        logger.error("JSON decode error:", e)
        logger.error("Raw metrics:", text)
        return None

    return data  

def process_record(row: Dict[str, str]) -> None:
    title = (row.get("title") or "").strip()
    content = (row.get("content") or "").strip()
    article_id = (row.get("article_id") or "").strip()

    try:
        classification = classify_article_with_gpt(title, content)
    except RateLimitError as rate:
        raise RateLimitError
    if classification != "true":
        return
    article_ids.append(article_id)

    # data = extract_json(_extract_metrics(content))
    
    try:
        datastr = _extract_metrics(content)
        data = json.loads(datastr)
    except json.JSONDecodeError as e:
        logger.error("JSON decode error:", e)
        logger.error("Raw metrics:", datastr)
    payload = {
        "article_id": row.get("article_id"),
        "title": title,
        "post": row.get("link"),
        "data": data
    }
    # year,month,day = data['date']['year'], data['date']['month'], data['date']['day']
    day = data['date']['day']
    date_iso = ""

    # year = "0000" if year == 0 else str(year)
    # month = "00" if month == 0 else str(month)
    day = "00" if day == 0 else str(day)
    
    #TODO:
    # get month and year from the file and place here
    # date_iso = f"{year}-{month.zfill(2)}-{day.zfill(2)}" # 0000-00-00


    if date_iso == "00":
        logger.warning(f"Skipping record with invalid date: {date_iso}")
        return
    
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
                try:
                    process_record(row)
                except RateLimitError:
                    logger.warning(f"Couldn't get a successful completions even after retrying, ratelimitted, continuing to the next record.")
                    continue

    finalize_output()

if __name__ == "__main__":
    main()
    with open('article_ids3.pkl','wb') as fp:
        pickle.dump(article_ids,fp)
