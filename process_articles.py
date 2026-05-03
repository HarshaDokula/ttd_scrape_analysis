import argparse
import csv
import json
from pathlib import Path
from datetime import datetime
import logging
import sys
import time
from typing import Any, Dict, List, Optional

from tqdm import tqdm
from dotenv import load_dotenv

load_dotenv()

from provider_factory import get_provider

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
        handlers=[
            logging.FileHandler(log_filename),
            logging.StreamHandler(sys.stdout),
        ],
    )
    return logging.getLogger(__name__)


logger = setup_logger()

# ---------------------------
# Global State
# ---------------------------

provider = get_provider()
_DARSHAN_ROWS: Dict[str, Dict[str, Any]] = {}
failed_records: List[Dict[str, str]] = []
processed_articles: Dict[str, Dict[str, Any]] = {}  # Track all loaded articles
MAX_BATCH_SIZE = 100
MAX_RETRIES = 3

# ---------------------------
# Batch Processor
# ---------------------------


class BatchProcessor:
    """Process TTD articles using OpenAI Batch API."""

    def __init__(self):
        self.provider = provider
        self.classify_batches = []
        self.extract_batches = []
        self.metrics = {
            "total_loaded": 0,
            "classified_true": 0,
            "classified_false": 0,
            "extracted_success": 0,
            "extracted_failed": 0,
            "invalid_date": 0,
            "final_records": 0,
        }

    def load_articles_from_csv(self, data_dir: Path) -> None:
        """Load all articles from CSV files and store by date/article_id."""
        global processed_articles
        csv_files = list(data_dir.glob("*.csv"))
        logger.info(f"Found {len(csv_files)} CSV files in {data_dir}")

        for csv_file in csv_files:
            logger.info(f"Loading file: {csv_file}")
            stem_parts = csv_file.stem.split("_")
            if len(stem_parts) < 3:
                logger.error(f"Filename format unexpected: {csv_file.name}")
                continue

            year, month = stem_parts[1], stem_parts[2]

            with open(csv_file, encoding="utf-8") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    article_id = (row.get("article_id") or "").strip()
                    title = (row.get("title") or "").strip()
                    content = (row.get("content") or "").strip()
                    link = row.get("link", "").strip()

                    if not article_id or not content:
                        continue

                    key = f"{year}_{month}_{article_id}"
                    processed_articles[key] = {
                        "year": year,
                        "month": month,
                        "article_id": article_id,
                        "title": title,
                        "content": content,
                        "link": link,
                        "row": row,  # Keep original row for failed records
                    }
                    self.metrics["total_loaded"] += 1

        logger.info(
            f"Loaded {self.metrics['total_loaded']} articles for processing"
        )

    def submit_classification_batches(self) -> List[str]:
        """Submit articles for classification in batches."""
        articles_list = list(processed_articles.values())
        batch_ids = []

        logger.info(f"Phase 1: Submitting {len(articles_list)} articles for classification")

        for i in range(0, len(articles_list), MAX_BATCH_SIZE):
            batch = articles_list[i : i + MAX_BATCH_SIZE]
            batch_articles = [
                {
                    "id": art["article_id"],
                    "title": art["title"],
                    "content": art["content"],
                }
                for art in batch
            ]

            try:
                batch_id = self.provider.submit_classify_batch(batch_articles)
                batch_ids.append(batch_id)
                logger.info(
                    f"Submitted classification batch {len(batch_ids)}: {batch_id} "
                    f"({len(batch)} articles)"
                )
            except Exception as e:
                logger.error(f"Error submitting classification batch: {e}")
                # Add all articles in this batch to failed records
                for art in batch:
                    failed_records.append(art["row"])

        return batch_ids

    def process_classification_results(self, batch_ids: List[str]) -> List[str]:
        """Poll and process classification batch results."""
        logger.info(f"Phase 1b: Polling {len(batch_ids)} classification batches")

        extract_article_ids = []

        for batch_id in batch_ids:
            try:
                status = self.provider.poll_batch_status(batch_id)
                logger.info(
                    f"Classification batch {batch_id}: status={status['status']} "
                    f"(processed={status['processed']}, failed={status['failed']})"
                )

                if status["status"] != "completed":
                    logger.warning(f"Batch {batch_id} did not complete successfully")
                    continue

                results = self.provider.get_batch_results(batch_id)

                for result in results:
                    custom_id = result.get("custom_id", "")
                    if not custom_id.endswith("-classify"):
                        continue

                    article_id = custom_id.replace("-classify", "")
                    response_text = self.provider.parse_response(result)

                    if response_text and response_text.lower() == "true":
                        self.metrics["classified_true"] += 1
                        extract_article_ids.append(article_id)
                        logger.debug(f"Article {article_id}: classified as TRUE")
                    else:
                        self.metrics["classified_false"] += 1
                        logger.debug(f"Article {article_id}: classified as FALSE")

            except Exception as e:
                logger.error(f"Error processing classification batch {batch_id}: {e}")

        logger.info(
            f"Classification complete: {self.metrics['classified_true']} true, "
            f"{self.metrics['classified_false']} false"
        )
        return extract_article_ids

    def submit_extraction_batches(
        self, article_ids: List[str]
    ) -> List[tuple[str, str]]:
        """Submit classified articles for metric extraction."""
        logger.info(
            f"Phase 2: Submitting {len(article_ids)} articles for metric extraction"
        )

        batch_ids = []

        for i in range(0, len(article_ids), MAX_BATCH_SIZE):
            batch_ids_chunk = article_ids[i : i + MAX_BATCH_SIZE]
            batch_articles = [
                {
                    "id": art_id,
                    "content": processed_articles[
                        next(
                            k
                            for k in processed_articles.keys()
                            if k.endswith(f"_{art_id}")
                        )
                    ]["content"],
                }
                for art_id in batch_ids_chunk
            ]

            try:
                batch_id = self.provider.submit_extract_batch(batch_articles)
                batch_ids.append((batch_id, ",".join(batch_ids_chunk)))
                logger.info(
                    f"Submitted extraction batch {len(batch_ids)}: {batch_id} "
                    f"({len(batch_articles)} articles)"
                )
            except Exception as e:
                logger.error(f"Error submitting extraction batch: {e}")

        return batch_ids

    def process_extraction_results(
        self, batch_id_pairs: List[tuple[str, str]]
    ) -> None:
        """Poll and process extraction batch results."""
        logger.info(f"Phase 2b: Polling {len(batch_id_pairs)} extraction batches")

        for batch_id, article_ids_str in batch_id_pairs:
            try:
                status = self.provider.poll_batch_status(batch_id)
                logger.info(
                    f"Extraction batch {batch_id}: status={status['status']} "
                    f"(processed={status['processed']}, failed={status['failed']})"
                )

                if status["status"] != "completed":
                    logger.warning(f"Batch {batch_id} did not complete successfully")
                    continue

                results = self.provider.get_batch_results(batch_id)

                for result in results:
                    custom_id = result.get("custom_id", "")
                    if not custom_id.endswith("-extract"):
                        continue

                    article_id = custom_id.replace("-extract", "")
                    response_text = self.provider.parse_response(result)

                    # Find the article in processed_articles
                    article_key = next(
                        (k for k in processed_articles.keys() if k.endswith(f"_{article_id}")),
                        None,
                    )
                    if not article_key:
                        logger.warning(f"Article {article_id} not found in cache")
                        continue

                    article = processed_articles[article_key]
                    year = article["year"]
                    month = article["month"]

                    # Parse JSON response
                    if not response_text:
                        logger.warning(f"No response for article {article_id}")
                        failed_records.append(article["row"])
                        self.metrics["extracted_failed"] += 1
                        continue

                    data = self._extract_json(response_text)
                    if not data:
                        logger.warning(f"Failed to parse JSON for article {article_id}")
                        failed_records.append(article["row"])
                        self.metrics["extracted_failed"] += 1
                        continue

                    # Extract day and pilgrim_count
                    try:
                        day = data.get("day")
                        if day is None or day == 0:
                            logger.warning(
                                f"Article {article_id}: invalid day={day}, skipping"
                            )
                            failed_records.append(article["row"])
                            self.metrics["invalid_date"] += 1
                            continue

                        date_iso = f"{year}-{month.zfill(2)}-{str(day).zfill(2)}"

                        pilgrim_count = data.get("pilgrim_count")
                        if pilgrim_count is None:
                            logger.warning(
                                f"Article {article_id}: no pilgrim_count, skipping"
                            )
                            failed_records.append(article["row"])
                            self.metrics["extracted_failed"] += 1
                            continue

                        # Convert string counts to int
                        if isinstance(pilgrim_count, str):
                            pilgrim_count = int(pilgrim_count)

                        payload = {
                            "article_id": article_id,
                            "title": article["title"],
                            "post": article["link"],
                            "data": {
                                "pilgrim_count": pilgrim_count,
                                "other_metrics": data.get("other_metrics", {}),
                            },
                        }

                        # Store or update (keep highest count per date)
                        if date_iso in _DARSHAN_ROWS:
                            existing_count = _DARSHAN_ROWS[date_iso]["data"].get(
                                "pilgrim_count", 0
                            )
                            if pilgrim_count > existing_count:
                                _DARSHAN_ROWS[date_iso] = payload
                        else:
                            _DARSHAN_ROWS[date_iso] = payload

                        self.metrics["extracted_success"] += 1
                        logger.debug(
                            f"Article {article_id}: extracted count={pilgrim_count} for {date_iso}"
                        )

                    except Exception as e:
                        logger.error(
                            f"Error processing extraction for {article_id}: {e}"
                        )
                        failed_records.append(article["row"])
                        self.metrics["extracted_failed"] += 1

            except Exception as e:
                logger.error(f"Error processing extraction batch {batch_id}: {e}")

        self.metrics["final_records"] = len(_DARSHAN_ROWS)
        logger.info(
            f"Extraction complete: {self.metrics['extracted_success']} success, "
            f"{self.metrics['extracted_failed']} failed, "
            f"{self.metrics['final_records']} unique dates"
        )

    @staticmethod
    def _extract_json(text: str) -> Optional[Dict[str, Any]]:
        """Extract and parse JSON from text."""
        try:
            start = text.find("{")
            end = text.rfind("}") + 1
            if start == -1 or end == 0:
                return None

            json_str = text[start:end]
            return json.loads(json_str)
        except json.JSONDecodeError as e:
            logger.error(f"JSON parse error: {e}")
            return None

    def save_outputs(self) -> None:
        """Save processed data and failed records."""
        # Save darshan_data.json
        output_path = Path("darshan_data.json")
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(_DARSHAN_ROWS, f, ensure_ascii=False, indent=2)
        logger.info(f"Saved {len(_DARSHAN_ROWS)} Darshan records to {output_path}")

        # Save failed records
        if failed_records:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            failed_path = Path(f"failed_records_{timestamp}.csv")
            with open(failed_path, "w", newline="", encoding="utf-8") as f:
                if failed_records:
                    fieldnames = failed_records[0].keys()
                    writer = csv.DictWriter(f, fieldnames=fieldnames)
                    writer.writeheader()
                    writer.writerows(failed_records)
            logger.info(f"Saved {len(failed_records)} failed records to {failed_path}")

    def print_metrics(self) -> None:
        """Print processing metrics."""
        logger.info("=" * 60)
        logger.info("PROCESSING METRICS")
        logger.info("=" * 60)
        logger.info(f"Total articles loaded:      {self.metrics['total_loaded']}")
        logger.info(f"Classified as TRUE:         {self.metrics['classified_true']}")
        logger.info(f"Classified as FALSE:        {self.metrics['classified_false']}")
        logger.info(f"Successfully extracted:     {self.metrics['extracted_success']}")
        logger.info(f"Extraction failed:          {self.metrics['extracted_failed']}")
        logger.info(f"Invalid dates (no day):     {self.metrics['invalid_date']}")
        logger.info(f"Final unique dates:         {self.metrics['final_records']}")
        logger.info(f"Total failed records:       {len(failed_records)}")

        if self.metrics["total_loaded"] > 0:
            success_rate = (
                self.metrics["extracted_success"] / self.metrics["total_loaded"] * 100
            )
            logger.info(f"Overall success rate:       {success_rate:.1f}%")
        logger.info("=" * 60)


# ---------------------------
# Output Functions
# ---------------------------


def finalize_output(out_path: Path = Path("darshan_data.json")) -> None:
    """Save darshan_data.json."""
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(_DARSHAN_ROWS, f, ensure_ascii=False, indent=2)
    logger.info(f"Saved {len(_DARSHAN_ROWS)} Darshan records to {out_path}")


def save_failed_records() -> None:
    """Save failed records to CSV."""
    if not failed_records:
        return
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_path = Path(f"failed_records_{timestamp}.csv")
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        if failed_records:
            writer = csv.DictWriter(f, fieldnames=failed_records[0].keys())
            writer.writeheader()
            writer.writerows(failed_records)
    logger.info(f"Saved {len(failed_records)} failed records to {out_path}")


# ---------------------------
# Main
# ---------------------------


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Process TTD articles using OpenAI Batch API"
    )
    parser.add_argument("data_dir", type=str, help="Directory containing CSV files")
    args = parser.parse_args()

    data_dir = Path(args.data_dir)
    if not data_dir.exists():
        logger.error(f"Data directory not found: {data_dir}")
        sys.exit(1)

    processor = BatchProcessor()

    try:
        # Phase 1: Load all articles
        logger.info("=" * 60)
        logger.info("PHASE 1: LOADING ARTICLES")
        logger.info("=" * 60)
        processor.load_articles_from_csv(data_dir)

        # Phase 2: Submit and process classification
        logger.info("=" * 60)
        logger.info("PHASE 2: CLASSIFICATION")
        logger.info("=" * 60)
        classify_batch_ids = processor.submit_classification_batches()
        classify_article_ids = processor.process_classification_results(classify_batch_ids)

        # Phase 3: Submit and process extraction
        logger.info("=" * 60)
        logger.info("PHASE 3: EXTRACTION")
        logger.info("=" * 60)
        extract_batch_pairs = processor.submit_extraction_batches(classify_article_ids)
        processor.process_extraction_results(extract_batch_pairs)

        # Phase 4: Save outputs
        logger.info("=" * 60)
        logger.info("PHASE 4: SAVING OUTPUTS")
        logger.info("=" * 60)
        processor.save_outputs()
        processor.print_metrics()

    except KeyboardInterrupt:
        logger.warning("Interrupted by user. Saving progress...")
        processor.save_outputs()
        processor.print_metrics()
        sys.exit(1)
    except Exception as e:
        logger.error(f"Fatal error: {e}", exc_info=True)
        processor.save_outputs()
        processor.print_metrics()
        sys.exit(1)


if __name__ == "__main__":
    main()
