import argparse
import csv
import json
import logging
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from dotenv import load_dotenv
from tqdm import tqdm

from provider_factory import get_provider

load_dotenv()


# ---------------------------
# Logging Setup
# ---------------------------


_RUN_TIMESTAMP: str = ""


def setup_logger() -> logging.Logger:
    global _RUN_TIMESTAMP
    logs_path = Path("logs")
    logs_path.mkdir(parents=True, exist_ok=True)
    _RUN_TIMESTAMP = datetime.now().strftime('%Y%m%d_%H%M%S')
    log_filename = logs_path / f"log_{_RUN_TIMESTAMP}.log"

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
# State Dataclass
# ---------------------------


@dataclass
class ProcessorState:
    """Serializable snapshot of processor state for persistence.

    This is intentionally JSON-serializable only: dicts, lists, ints, strs.
    """

    darshan_rows: Dict[str, Dict[str, Any]]
    failed_records: List[Dict[str, Any]]
    metrics: Dict[str, int]
    pending_batches: List[Dict[str, Any]]


# ---------------------------
# Batch Processor
# ---------------------------


class BatchProcessor:
    """Process TTD articles using the OpenAI Batch API.

    This class encapsulates all mutable state required for a run so we avoid
    module-level globals and can persist progress across batches.
    """

    def __init__(
        self,
        *,
        provider: Optional[Any] = None,
        batch_size: int = 75,
        max_retries: int = 3,
    ) -> None:
        if batch_size < 1 or batch_size > 10_000:
            raise ValueError("batch_size must be between 1 and 10_000")

        self.provider = provider or get_provider()
        self.batch_size = batch_size
        self.max_retries = max_retries
        
        # Create output directory with timestamp
        self.output_dir = Path("output") / f"run_{_RUN_TIMESTAMP}"
        self.output_dir.mkdir(parents=True, exist_ok=True)
        
        # Set state_path to use new output directory
        self.state_path = self.output_dir / "darshan_state.json"

        # Article storage and indexing
        self.processed_articles: Dict[str, Dict[str, Any]] = {}
        self.article_index: Dict[str, str] = {}  # article_id -> composite key

        # Outputs
        self.darshan_rows: Dict[str, Dict[str, Any]] = {}
        self.failed_records: List[Dict[str, Any]] = []

        # Batch bookkeeping (for logging/persistence)
        self.pending_batches: List[Dict[str, Any]] = []

        # Metrics
        self.metrics: Dict[str, int] = {
            "total_loaded": 0,
            "classified_true": 0,
            "classified_false": 0,
            "extracted_success": 0,
            "extracted_failed": 0,
            "invalid_date": 0,
            "final_records": 0,
        }

    # -----------------------
    # Data Loading & Caching
    # -----------------------

    def _add_article(
        self,
        *,
        year: str,
        month: str,
        row: Dict[str, Any],
        retry: bool = False,
    ) -> None:
        """Add a single article row into the in-memory index.

        Deduplicates by article_id: first-seen article wins. For retry rows we
        still respect this rule but tag the stored article as a retry source.
        """

        article_id = (row.get("article_id") or "").strip()
        title = (row.get("title") or "").strip()
        content = (row.get("content") or "").strip()
        link = (row.get("link") or "").strip()

        if not article_id or not content:
            return

        if article_id in self.article_index:
            # Deduplicate by article_id as per spec
            logger.debug("Skipping duplicate article_id %s", article_id)
            return

        key = f"{year}_{month}_{article_id}"
        self.article_index[article_id] = key
        self.processed_articles[key] = {
            "year": year,
            "month": month,
            "article_id": article_id,
            "title": title,
            "content": content,
            "link": link,
            "row": row,
            "retry": retry,
        }
        self.metrics["total_loaded"] += 1

    def load_retry_failed(self, csv_path: Path) -> None:
        """Load rows from a previous failed_records_*.csv for retry.

        The failed CSV retains original headers, including year/month, which
        we use instead of deriving from the filename.
        """

        if not csv_path.exists():
            logger.error("Retry-failed CSV not found: %s", csv_path)
            return

        logger.info("Loading retry-failed CSV: %s", csv_path)
        with csv_path.open(encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                year = str(row.get("year") or "").strip()
                month = str(row.get("month") or "").strip()
                if not year or not month:
                    logger.warning("Retry row missing year/month: %s", row)
                    continue
                self._add_article(year=year, month=month, row=row, retry=True)

    def load_articles_from_csv(self, data_dir: Path) -> None:
        """Load all articles from CSV files and store by date/article_id."""

        csv_files = sorted(data_dir.glob("*.csv"))
        logger.info("Found %d CSV files in %s", len(csv_files), data_dir)

        for csv_file in csv_files:
            logger.info("Loading file: %s", csv_file)
            stem_parts = csv_file.stem.split("_")
            if len(stem_parts) < 3:
                logger.error("Filename format unexpected: %s", csv_file.name)
                continue

            year, month = stem_parts[1], stem_parts[2]

            with csv_file.open(encoding="utf-8") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    self._add_article(year=year, month=month, row=row, retry=False)

        logger.info("Loaded %d articles for processing", self.metrics["total_loaded"])

    # -----------------------
    # Batch Helpers
    # -----------------------

    @staticmethod
    def _chunk(iterable: Iterable[Any], size: int) -> Iterable[List[Any]]:
        """Yield consecutive chunks of at most `size` items from iterable."""

        batch: List[Any] = []
        for item in iterable:
            batch.append(item)
            if len(batch) >= size:
                yield batch
                batch = []
        if batch:
            yield batch

    # -----------------------
    # Classification Workflow
    # -----------------------

    def submit_classification_batches(self) -> List[str]:
        """Submit all loaded articles for classification in batches."""

        articles_list = list(self.processed_articles.values())
        batch_ids: List[str] = []

        logger.info(
            "Phase 1: Submitting %d articles for classification (batch_size=%d)",
            len(articles_list),
            self.batch_size,
        )

        for batch in self._chunk(articles_list, self.batch_size):
            items = [
                {
                    "article_id": art["article_id"],
                    "title": art["title"],
                    "content": art["content"],
                }
                for art in batch
            ]

            try:
                requests_jsonl = self.provider.build_batch_requests(
                    "classification", items
                )
                batch_id = self.provider.submit_batch(
                    "classification",
                    requests_jsonl,
                    max_retries=self.max_retries,
                )
                batch_ids.append(batch_id)
                self.pending_batches.append(
                    {
                        "kind": "classification",
                        "batch_id": batch_id,
                        "article_ids": [a["article_id"] for a in batch],
                    }
                )
                logger.info(
                    "Submitted classification batch %d: %s (%d articles)",
                    len(batch_ids),
                    batch_id,
                    len(batch),
                )
            except Exception as exc:  # network/API errors; logged + mark failed
                logger.error("Error submitting classification batch: %s", exc)
                for art in batch:
                    self.failed_records.append(art["row"])

        return batch_ids

    def process_classification_results(self, batch_ids: List[str]) -> List[str]:
        """Poll and process classification batch results.

        Returns a list of article_ids that were classified as TRUE and should
        be forwarded to extraction.
        """

        logger.info(
            "Phase 1b: Polling %d classification batches", len(batch_ids)
        )

        extract_article_ids: List[str] = []

        for batch_id in batch_ids:
            try:
                status = self.provider.poll_batch_status(
                    batch_id, max_retries=self.max_retries
                )
                logger.info(
                    "Classification batch %s: status=%s (processed=%s, failed=%s)",
                    batch_id,
                    status["status"],
                    status["processed"],
                    status["failed"],
                )

                if status["status"] != "completed":
                    logger.warning(
                        "Classification batch %s did not complete successfully", batch_id
                    )
                    continue

                results = self.provider.get_batch_results(batch_id)

                for result in results:
                    custom_id = result.get("custom_id", "")
                    if not custom_id.endswith("-classification") and not custom_id.endswith(
                        "-classify"
                    ):
                        continue

                    # Support both {id}-classify and {id}-classification custom IDs.
                    if custom_id.endswith("-classification"):
                        article_id = custom_id[: -len("-classification")]
                    else:
                        article_id = custom_id[: -len("-classify")]

                    label = self.provider.parse_response(
                        result, kind="classification"
                    )

                    if label is None:
                        logger.warning(
                            "No classification response for article %s", article_id
                        )
                        self._record_failure_by_article_id(article_id)
                        continue

                    if str(label).strip().lower() == "true":
                        self.metrics["classified_true"] += 1
                        extract_article_ids.append(article_id)
                        logger.debug("Article %s: classified as TRUE", article_id)
                    else:
                        self.metrics["classified_false"] += 1
                        logger.debug("Article %s: classified as FALSE", article_id)

                # Retrieve and log batch-level errors, if any
                errors = self.provider.get_batch_errors(batch_id)
                for error in errors:
                    logger.error("Classification batch %s error: %s", batch_id, error)

            except Exception as exc:
                logger.error(
                    "Error processing classification batch %s: %s", batch_id, exc
                )

        logger.info(
            "Classification complete: %d true, %d false",
            self.metrics["classified_true"],
            self.metrics["classified_false"],
        )
        return extract_article_ids

    # -----------------------
    # Extraction Workflow
    # -----------------------

    def submit_extraction_batches(self, article_ids: List[str]) -> List[str]:
        """Submit classified-TRUE articles for metric extraction."""

        logger.info(
            "Phase 2: Submitting %d articles for metric extraction", len(article_ids)
        )

        batch_ids: List[str] = []

        for id_batch in self._chunk(article_ids, self.batch_size):
            batch_articles: List[Dict[str, Any]] = []
            for article_id in id_batch:
                key = self.article_index.get(article_id)
                if not key:
                    logger.warning(
                        "Article %s not found in cache when building extraction batch",
                        article_id,
                    )
                    continue
                article = self.processed_articles[key]
                batch_articles.append(
                    {
                        "article_id": article_id,
                        "content": article["content"],
                    }
                )

            if not batch_articles:
                continue

            try:
                requests_jsonl = self.provider.build_batch_requests(
                    "extraction", batch_articles
                )
                batch_id = self.provider.submit_batch(
                    "extraction",
                    requests_jsonl,
                    max_retries=self.max_retries,
                )
                batch_ids.append(batch_id)
                self.pending_batches.append(
                    {
                        "kind": "extraction",
                        "batch_id": batch_id,
                        "article_ids": id_batch,
                    }
                )
                logger.info(
                    "Submitted extraction batch %d: %s (%d articles)",
                    len(batch_ids),
                    batch_id,
                    len(batch_articles),
                )
            except Exception as exc:
                logger.error("Error submitting extraction batch: %s", exc)

        return batch_ids

    def process_extraction_results(self, batch_ids: List[str]) -> None:
        """Poll and process extraction batch results, updating darshan_rows."""

        logger.info(
            "Phase 2b: Polling %d extraction batches", len(batch_ids)
        )

        for batch_id in batch_ids:
            try:
                status = self.provider.poll_batch_status(
                    batch_id, max_retries=self.max_retries
                )
                logger.info(
                    "Extraction batch %s: status=%s (processed=%s, failed=%s)",
                    batch_id,
                    status["status"],
                    status["processed"],
                    status["failed"],
                )

                if status["status"] != "completed":
                    logger.warning(
                        "Extraction batch %s did not complete successfully", batch_id
                    )
                    continue

                results = self.provider.get_batch_results(batch_id)

                for result in results:
                    custom_id = result.get("custom_id", "")
                    if not custom_id.endswith("-extraction") and not custom_id.endswith(
                        "-extract"
                    ):
                        continue

                    if custom_id.endswith("-extraction"):
                        article_id = custom_id[: -len("-extraction")]
                    else:
                        article_id = custom_id[: -len("-extract")]

                    data = self.provider.parse_response(result, kind="extraction")
                    if data is None:
                        logger.warning(
                            "No extraction JSON for article %s", article_id
                        )
                        self._record_failure_by_article_id(article_id)
                        self.metrics["extracted_failed"] += 1
                        continue

                    # Validate schema
                    if not isinstance(data, dict):
                        logger.warning(
                            "Extraction for article %s returned non-dict payload: %r",
                            article_id,
                            data,
                        )
                        self._record_failure_by_article_id(article_id)
                        self.metrics["extracted_failed"] += 1
                        continue

                    self._apply_extraction_result(article_id, data)

                # After each completed extraction batch, persist state
                self.save_state()

            except Exception as exc:
                logger.error(
                    "Error processing extraction batch %s: %s", batch_id, exc
                )

        self.metrics["final_records"] = len(self.darshan_rows)
        logger.info(
            "Extraction complete: %d success, %d failed, %d unique dates",
            self.metrics["extracted_success"],
            self.metrics["extracted_failed"],
            self.metrics["final_records"],
        )

    # -----------------------
    # Extraction Helpers
    # -----------------------

    def _record_failure_by_article_id(self, article_id: str) -> None:
        key = self.article_index.get(article_id)
        if not key:
            return
        article = self.processed_articles.get(key)
        if not article:
            return
        self.failed_records.append(article["row"])

    def _apply_extraction_result(self, article_id: str, data: Dict[str, Any]) -> None:
        """Validate and apply a single extraction JSON payload."""

        key = self.article_index.get(article_id)
        if not key:
            logger.warning("Article %s not found in cache", article_id)
            return

        article = self.processed_articles[key]
        year = str(article["year"])
        month = str(article["month"]).zfill(2)

        try:
            day = data.get("day")
            if not isinstance(day, int) or not (1 <= day <= 31):
                logger.warning(
                    "Article %s: invalid day %r, treating as failure", article_id, day
                )
                self.failed_records.append(article["row"])
                self.metrics["invalid_date"] += 1
                return

            date_iso = f"{year}-{month}-{str(day).zfill(2)}"

            pilgrim_count = data.get("pilgrim_count")
            if pilgrim_count is None:
                logger.warning(
                    "Article %s: missing pilgrim_count, treating as failure",
                    article_id,
                )
                self.failed_records.append(article["row"])
                self.metrics["extracted_failed"] += 1
                return

            if isinstance(pilgrim_count, str):
                try:
                    pilgrim_count = int(pilgrim_count.replace(",", ""))
                except ValueError:
                    logger.warning(
                        "Article %s: non-numeric pilgrim_count %r", article_id, pilgrim_count
                    )
                    self.failed_records.append(article["row"])
                    self.metrics["extracted_failed"] += 1
                    return

            if not isinstance(pilgrim_count, int) or pilgrim_count < 0:
                logger.warning(
                    "Article %s: invalid pilgrim_count %r", article_id, pilgrim_count
                )
                self.failed_records.append(article["row"])
                self.metrics["extracted_failed"] += 1
                return

            other_metrics = data.get("other_metrics") or {}
            if not isinstance(other_metrics, dict):
                other_metrics = {}

            payload = {
                "article_id": article_id,
                "title": article["title"],
                "post": article["link"],
                "data": {
                    "pilgrim_count": pilgrim_count,
                    "other_metrics": other_metrics,
                },
            }

            # Keep highest pilgrim_count per date
            if date_iso in self.darshan_rows:
                existing_count = self.darshan_rows[date_iso]["data"].get(
                    "pilgrim_count", 0
                )
                if pilgrim_count > existing_count:
                    self.darshan_rows[date_iso] = payload
            else:
                self.darshan_rows[date_iso] = payload

            self.metrics["extracted_success"] += 1
            logger.debug(
                "Article %s: extracted count=%d for %s",
                article_id,
                pilgrim_count,
                date_iso,
            )
        except Exception as exc:
            logger.error("Error applying extraction for %s: %s", article_id, exc)
            self.failed_records.append(article["row"])
            self.metrics["extracted_failed"] += 1

    # -----------------------
    # Persistence & Metrics
    # -----------------------

    def to_state(self) -> ProcessorState:
        return ProcessorState(
            darshan_rows=self.darshan_rows,
            failed_records=self.failed_records,
            metrics=self.metrics,
            pending_batches=self.pending_batches,
        )

    def save_state(self) -> None:
        """Persist current state to JSON for manual resumption/inspection."""

        try:
            state = self.to_state()
            data = asdict(state)
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            with self.state_path.open("w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            logger.info("Saved processor state to %s", self.state_path)
        except Exception as exc:
            logger.error("Failed to save state to %s: %s", self.state_path, exc)

    def save_outputs(self) -> None:
        """Save darshan_data.json and failed_records_*.csv."""

        # Save darshan_data.json
        output_path = self.output_dir / "darshan_data.json"
        with output_path.open("w", encoding="utf-8") as f:
            json.dump(self.darshan_rows, f, ensure_ascii=False, indent=2)
        logger.info(
            "Saved %d Darshan records to %s",
            len(self.darshan_rows),
            output_path,
        )

        # Save failed records
        if self.failed_records:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            failed_path = self.output_dir / f"failed_records_{timestamp}.csv"
            with failed_path.open("w", newline="", encoding="utf-8") as f:
                fieldnames = list(self.failed_records[0].keys())
                writer = csv.DictWriter(f, fieldnames=fieldnames)
                writer.writeheader()
                writer.writerows(self.failed_records)
            logger.info(
                "Saved %d failed records to %s", len(self.failed_records), failed_path
            )

    def print_metrics(self) -> None:
        """Print processing metrics."""

        logger.info("=" * 60)
        logger.info("PROCESSING METRICS")
        logger.info("=" * 60)
        logger.info("Total articles loaded:      %d", self.metrics["total_loaded"])
        logger.info("Classified as TRUE:         %d", self.metrics["classified_true"])
        logger.info("Classified as FALSE:        %d", self.metrics["classified_false"])
        logger.info(
            "Successfully extracted:     %d", self.metrics["extracted_success"]
        )
        logger.info(
            "Extraction failed:          %d", self.metrics["extracted_failed"]
        )
        logger.info("Invalid dates (no day):     %d", self.metrics["invalid_date"])
        logger.info("Final unique dates:         %d", self.metrics["final_records"])
        logger.info("Total failed records:       %d", len(self.failed_records))

        if self.metrics["total_loaded"] > 0:
            success_rate = (
                self.metrics["extracted_success"]
                / self.metrics["total_loaded"]
                * 100
            )
            logger.info("Overall success rate:       %.1f%%", success_rate)
        logger.info("=" * 60)


# ---------------------------
# Main
# ---------------------------


def main() -> None:
    """Main entry point."""

    parser = argparse.ArgumentParser(
        description="Process TTD articles using OpenAI Batch API",
    )
    parser.add_argument(
        "data_dir", type=str, help="Directory containing CSV files",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=75,
        help="Batch size for classification/extraction (1-10000)",
    )
    parser.add_argument(
        "--max-retries",
        type=int,
        default=3,
        help="Max retries for batch submission/polling",
    )
    parser.add_argument(
        "--retry-failed",
        type=str,
        default=None,
        help="Optional failed_records_*.csv to retry before normal CSV ingest",
    )
    parser.add_argument(
        "--state-file",
        type=str,
        default=None,
        help="Optional path for saving incremental processing state (defaults to output/run_TIMESTAMP/darshan_state.json)",
    )

    args = parser.parse_args()

    data_dir = Path(args.data_dir)
    if not data_dir.exists():
        logger.error("Data directory not found: %s", data_dir)
        sys.exit(1)

    if args.batch_size < 1 or args.batch_size > 10_000:
        logger.error("--batch-size must be between 1 and 10_000")
        sys.exit(1)
    if args.batch_size > 1000:
        logger.warning(
            "Batch size %d is large; OpenAI limit is 10_000 but consider smaller batches",
            args.batch_size,
        )

    processor = BatchProcessor(
        batch_size=args.batch_size,
        max_retries=args.max_retries,
    )

    # Optional: load retry-failed CSV first
    if args.retry_failed:
        retry_path = Path(args.retry_failed)
        if retry_path.exists():
            processor.load_retry_failed(retry_path)
        else:
            logger.error("Retry-failed CSV not found: %s", retry_path)

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
        classify_article_ids = processor.process_classification_results(
            classify_batch_ids
        )

        # Phase 3: Submit and process extraction
        logger.info("=" * 60)
        logger.info("PHASE 3: EXTRACTION")
        logger.info("=" * 60)
        extract_batch_ids = processor.submit_extraction_batches(classify_article_ids)
        processor.process_extraction_results(extract_batch_ids)

        # Phase 4: Save outputs
        logger.info("=" * 60)
        logger.info("PHASE 4: SAVING OUTPUTS")
        logger.info("=" * 60)
        processor.save_outputs()
        processor.save_state()
        processor.print_metrics()

    except KeyboardInterrupt:
        logger.warning("Interrupted by user. Saving progress and exiting...")
        processor.save_outputs()
        processor.save_state()
        processor.print_metrics()
        sys.exit(1)
    except Exception as exc:  # noqa: BLE001
        logger.error("Fatal error: %s", exc, exc_info=True)
        processor.save_outputs()
        processor.save_state()
        processor.print_metrics()
        sys.exit(1)


if __name__ == "__main__":  # pragma: no cover
    main()
