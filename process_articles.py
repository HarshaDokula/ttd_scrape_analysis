import argparse
import csv
import json
import logging
import sys
import time
import signal
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

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


# Rate-limit thresholds for OpenAI gpt-4o-mini.
# The RPD (requests-per-day) limit is 10,000. We pace proactively when
# approaching it to avoid long stretches of 429 retries.
RPD_LIMIT = 10_000
RPD_WARN_THRESHOLD = 0.85   # start logging warnings at 85%
RPD_PACE_THRESHOLD = 0.90   # start adding extra delay at 90%
RPD_HARD_STOP = 0.98        # stop and warn if we somehow exceed 98%

# TPM tracking defaults
DEFAULT_TPM_LIMIT = 2_000_000
DEFAULT_TPM_PACE_THRESHOLD = 0.75
DEFAULT_MAX_TOKENS_PER_REQUEST = 4096
DEFAULT_MAX_TOKENS_PER_BATCH = 1_500_000  # 25% under OpenAI's 2M per-batch limit
DEFAULT_PER_BATCH_TIMEOUT_SEC = 3600  # 60 minutes (was 15)


# ---------------------------
# TPM Sliding Window Tracker
# ---------------------------


class TPMSlidingWindow:
    """Track tokens-per-minute consumption with a sliding window.

    Records token consumption events and reports whether new submissions
    would exceed the configured TPM threshold. Use before each batch
    submission to pace against the organization-level TPM limit.
    """

    def __init__(
        self,
        tpm_limit: int = DEFAULT_TPM_LIMIT,
        pace_threshold: float = DEFAULT_TPM_PACE_THRESHOLD,
        window_sec: int = 60,
    ) -> None:
        if tpm_limit <= 0:
            raise ValueError("tpm_limit must be positive")
        if not 0 < pace_threshold <= 1:
            raise ValueError("pace_threshold must be in (0, 1]")

        self.tpm_limit = tpm_limit
        self.pace_threshold = pace_threshold
        self.window_sec = window_sec
        self._history: List[Tuple[float, int]] = []  # (timestamp, token_count)

    def current_tpm(self) -> int:
        """Return the total tokens consumed within the current sliding window."""
        cutoff = time.time() - self.window_sec
        self._history = [(t, c) for t, c in self._history if t > cutoff]
        return sum(c for _, c in self._history)

    def can_consume(self, tokens: int) -> bool:
        """Return True if consuming *tokens* stays within the pace threshold."""
        return (self.current_tpm() + tokens) <= (self.tpm_limit * self.pace_threshold)

    def consume(self, tokens: int) -> None:
        """Record *tokens* consumed at the current time."""
        self._history.append((time.time(), tokens))

    def wait_until_ready(self, tokens: int, max_wait_sec: float = 300.0) -> float:
        """Block until *tokens* can be consumed, then record them.

        Polls every 5 seconds. Returns the total seconds waited, or raises
        :class:`TimeoutError` if *max_wait_sec* is exceeded.
        """
        waited = 0.0
        while not self.can_consume(tokens):
            if waited >= max_wait_sec:
                raise TimeoutError(
                    f"TPM tracker waited {waited:.0f}s for {tokens} tokens but "
                    f"window is saturated ({self.current_tpm()}/{self.tpm_limit})"
                )
            time.sleep(5.0)
            waited += 5.0
        self.consume(tokens)
        return waited

    def log_snapshot(self, label: str = "") -> None:
        """Log current TPM usage for monitoring."""
        current = self.current_tpm()
        pct = current / self.tpm_limit * 100 if self.tpm_limit > 0 else 0.0
        logger.info(
            "TPM%s: %d / %d (%.1f%%)",
            f" [{label}]" if label else "",
            current,
            self.tpm_limit,
            pct,
        )


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
        max_inflight_batches: int = 10,
        state_path: Optional[Path] = None,
        # ── Token-aware parameters ─────────────────────────────────────────
        max_tokens_per_request: int = DEFAULT_MAX_TOKENS_PER_REQUEST,
        max_tokens_per_batch: int = DEFAULT_MAX_TOKENS_PER_BATCH,
        tpm_limit: int = DEFAULT_TPM_LIMIT,
        tpm_pace_threshold: float = DEFAULT_TPM_PACE_THRESHOLD,
        per_batch_timeout_sec: int = DEFAULT_PER_BATCH_TIMEOUT_SEC,
    ) -> None:
        if batch_size < 1 or batch_size > 10_000:
            raise ValueError("batch_size must be between 1 and 10_000")
        if max_inflight_batches < 1:
            raise ValueError("max_inflight_batches must be at least 1")
        if max_tokens_per_batch < 1:
            raise ValueError("max_tokens_per_batch must be at least 1")
        if max_tokens_per_request < 1:
            raise ValueError("max_tokens_per_request must be at least 1")

        self.provider = provider or get_provider()
        self.batch_size = batch_size
        self.max_retries = max_retries
        self.max_inflight_batches = max_inflight_batches

        # Token budget and TPM tracking
        self.max_tokens_per_request = max_tokens_per_request
        self.max_tokens_per_batch = max_tokens_per_batch
        self.tpm_tracker = TPMSlidingWindow(
            tpm_limit=tpm_limit,
            pace_threshold=tpm_pace_threshold,
        )
        self.per_batch_timeout_sec = per_batch_timeout_sec

        # Push token budget to provider (so it can truncate content accordingly)
        if hasattr(self.provider, "max_tokens_per_request"):
            self.provider.max_tokens_per_request = max_tokens_per_request

        # Create default output directory with timestamp
        self.output_dir = Path("output") / f"run_{_RUN_TIMESTAMP}"
        self.output_dir.mkdir(parents=True, exist_ok=True)

        # Allow caller to override where state is written; if provided,
        # treat the parent directory of the state file as the output dir
        # so subsequent outputs land alongside it.
        if state_path is not None:
            self.state_path = Path(state_path)
            self.output_dir = self.state_path.parent
        else:
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

        # Rate-limit tracking: total requests made in this session
        self._total_requests: int = 0
        self._last_rpd_warning_pct: float = 0.0

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
    # Token-aware Batching
    # -----------------------

    def _estimate_request_tokens(self, article: Dict[str, Any], kind: str) -> int:
        """Estimate the token count for a single article request.

        Uses the provider's token counter if available; otherwise falls back
        to a character-based estimate.
        """
        content = article.get("content", "")
        if hasattr(self.provider, "count_request_tokens"):
            return self.provider.count_request_tokens(content)

        # Fallback estimate: prompt overhead (~200 tokens) + content
        prompt_overhead = 200
        return prompt_overhead + len(content) // 4

    def _chunk_by_token_budget(
        self,
        articles: List[Dict[str, Any]],
        kind: str,
    ) -> List[List[Dict[str, Any]]]:
        """Split *articles* into batches that each fit within the token budget.

        Uses exact token counts via the provider (which uses tiktoken) to
        ensure no batch exceeds ``max_tokens_per_batch``. Replaces the old
        ``_chunk`` fixed-size batching.
        """

        batches: List[List[Dict[str, Any]]] = []
        current_batch: List[Dict[str, Any]] = []
        current_tokens = 0

        for article in articles:
            tokens = self._estimate_request_tokens(article, kind)

            # If a single article exceeds the per-batch budget, log a warning
            # and put it in its own batch anyway
            if tokens > self.max_tokens_per_batch:
                logger.warning(
                    "Article %s alone uses ~%d tokens, exceeding per-batch budget %d",
                    article.get("article_id", "?"),
                    tokens,
                    self.max_tokens_per_batch,
                )
                if current_batch:
                    batches.append(current_batch)
                    current_tokens = 0
                current_batch = [article]
                batches.append(current_batch)
                current_batch = []
                current_tokens = 0
                continue

            if current_tokens + tokens > self.max_tokens_per_batch:
                batches.append(current_batch)
                current_batch = [article]
                current_tokens = tokens
            else:
                current_batch.append(article)
                current_tokens += tokens

        if current_batch:
            batches.append(current_batch)

        logger.info(
            "Token-budget batching: %d articles split into %d batches "
            "(budget=%d tokens/batch)",
            len(articles),
            len(batches),
            self.max_tokens_per_batch,
        )
        return batches

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

    @staticmethod
    def _retry_delay_for_exception(
        exc: Exception,
        attempt: int,
        *,
        base_delay: float = 5.0,
        backoff_factor: float = 2.0,
        max_delay: float = 60.0,
    ) -> float:
        """Return a retry delay (seconds) for synchronous API errors.

        Respects ``Retry-After`` headers when present (e.g. for HTTP 429) and
        otherwise falls back to exponential backoff with jitter.

        The OpenAI SDK (httpx) raises ``APIError`` exceptions that carry a
        ``response`` attribute (an ``httpx.Response``) whose ``headers`` dict
        contains the ``Retry-After`` header sent by the server.  We check
        ``exc.response.headers`` first, then fall back to the legacy
        ``exc.headers`` path for other exception types.
        """

        retry_after: Optional[float] = None

        # Try exc.response.headers first (OpenAI SDK APIError pattern)
        response = getattr(exc, "response", None)
        if response is not None:
            resp_headers = getattr(response, "headers", None)
            if resp_headers is not None and hasattr(resp_headers, "get"):
                for key in ("Retry-After", "retry-after"):
                    value = resp_headers.get(key)
                    if value is None:
                        continue
                    try:
                        retry_after = float(value)
                        break
                    except (TypeError, ValueError):
                        retry_after = None

        # Fall back to exc.headers for other exception types
        if retry_after is None:
            headers = getattr(exc, "headers", None)
            if headers and hasattr(headers, "get"):
                for key in ("Retry-After", "retry-after"):
                    value = headers.get(key)
                    if value is None:
                        continue
                    try:
                        retry_after = float(value)
                        break
                    except (TypeError, ValueError):
                        retry_after = None

        if retry_after is not None:
            delay = retry_after
        else:
            delay = base_delay * (backoff_factor ** max(0, attempt - 1))

        delay = min(max_delay, delay)

        try:
            import random

            jitter = delay * 0.1
            if jitter > 0:
                delay += random.uniform(-jitter, jitter)
        except Exception:
            pass

        # Clamp again after jitter to guarantee max_delay is respected
        delay = min(max_delay, delay)

        if delay < 1:
            delay = 1

        return float(delay)

    def _check_rate_limit(self) -> float:
        """Check cumulative request count and return a pacing delay (seconds).

        OpenAI's gpt-4o-mini has a 10,000 RPD (requests-per-day) limit.
        Once we cross 85% usage, we log warnings.  At 90%+ we proactively
        insert extra delay proportional to how close we are to the cap,
        giving the daily bucket time to refill rather than hammering 429s.

        Returns:
            Seconds to sleep before the next request (0.0 if no pacing needed).
        """
        ratio = self._total_requests / RPD_LIMIT if RPD_LIMIT > 0 else 0.0

        if ratio >= RPD_HARD_STOP:
            logger.warning(
                "RPD usage at %.1f%% (%d/%d) — approaching hard cap, "
                "requests may fail until the daily window resets",
                ratio * 100,
                self._total_requests,
                RPD_LIMIT,
            )
            # Sleep for a significant time to let the bucket drain
            return 30.0

        if ratio >= RPD_PACE_THRESHOLD:
            # Exponential pacing: at 90% → 10s, 95% → 20s, 99% → 50s
            over = (ratio - RPD_PACE_THRESHOLD) / (1.0 - RPD_PACE_THRESHOLD)
            delay = 10.0 * (1.0 + over * 4.0)  # 10s at 90%, 50s at 100%

            # Log at most once per 5% band to avoid log spam
            band = round(ratio * 100 / 5) * 5
            if band > self._last_rpd_warning_pct:
                logger.warning(
                    "RPD usage at %.1f%% (%d/%d) — pacing requests with %.1fs delay",
                    ratio * 100,
                    self._total_requests,
                    RPD_LIMIT,
                    delay,
                )
                self._last_rpd_warning_pct = band

            return delay

        if ratio >= RPD_WARN_THRESHOLD:
            # Log warning once per 5% band
            band = round(ratio * 100 / 5) * 5
            if band > self._last_rpd_warning_pct:
                logger.warning(
                    "RPD usage at %.1f%% (%d/%d) — approaching rate limit",
                    ratio * 100,
                    self._total_requests,
                    RPD_LIMIT,
                )
                self._last_rpd_warning_pct = band

        return 0.0

    # -----------------------
    # TPM-guided Batch Submission
    # -----------------------

    def _estimate_batch_tokens(self, items: List[Dict[str, Any]], kind: str) -> int:
        """Estimate the total token count for a batch of articles."""
        return sum(
            self._estimate_request_tokens(item, kind) for item in items
        )

    def _throttle_inflight_and_tpm(
        self,
        active_batch_ids: List[str],
        pending_tokens: int = 0,
    ) -> None:
        """Block until both inflight batches and TPM allow a new submission.

        Combines the old inflight-batch throttle with the new TPM sliding
        window tracker. Waits until:
        1. ``len(active_batch_ids) < max_inflight_batches``
        2. TPM tracker has room for *pending_tokens*
        """

        if self.max_inflight_batches <= 0 and pending_tokens <= 0:
            return

        interval = getattr(self.provider, "poll_interval", 10)
        tpm_warning_logged = False

        while True:
            # --- Condition 1: not too many inflight batches ---
            if active_batch_ids:
                for batch_id in list(active_batch_ids):
                    try:
                        status = self.provider.get_batch_status(batch_id)
                    except Exception as exc:  # noqa: BLE001
                        logger.error(
                            "Error checking status for inflight batch %s: %s",
                            batch_id,
                            exc,
                        )
                        continue

                    state = status.get("status")
                    if state in {"completed", "failed", "expired"}:
                        active_batch_ids.remove(batch_id)

            # --- Condition 2: TPM budget available ---
            if pending_tokens > 0 and not self.tpm_tracker.can_consume(pending_tokens):
                if not tpm_warning_logged:
                    self.tpm_tracker.log_snapshot(label="waiting for TPM budget")
                    tpm_warning_logged = True
                time.sleep(interval)
                continue

            # --- Both conditions met? ---
            if (
                self.max_inflight_batches <= 0
                or len(active_batch_ids) < self.max_inflight_batches
            ):
                if pending_tokens > 0 and self.tpm_tracker.can_consume(pending_tokens):
                    self.tpm_tracker.consume(pending_tokens)
                    self.tpm_tracker.log_snapshot(label="after consume")
                    return

            time.sleep(interval)

    def _get_batch_article_ids(self, batch_id: str) -> List[str]:
        """Return the article_ids recorded for a given batch_id, if any."""

        for batch in self.pending_batches:
            if batch.get("batch_id") == batch_id:
                ids = batch.get("article_ids") or []
                # Ensure we always return a new list so callers can't mutate
                # the internal structure by accident.
                return list(ids)
        return []

    def _mark_batch_failed(self, batch_id: str, reason: str = "") -> None:
        """Mark all articles in the given batch as failed.

        This is used when a batch ends in a non-successful terminal state
        (failed/expired) or when we give up waiting for it due to timeout
        or polling errors.
        """

        article_ids = self._get_batch_article_ids(batch_id)
        if not article_ids:
            logger.warning(
                "No article_ids recorded for failed batch %s; nothing to mark", batch_id
            )
            return

        if reason:
            logger.error(
                "Marking %d articles from batch %s as failed (%s)",
                len(article_ids),
                batch_id,
                reason,
            )
        else:
            logger.error(
                "Marking %d articles from batch %s as failed", len(article_ids), batch_id
            )

        for article_id in article_ids:
            self._record_failure_by_article_id(article_id)

    def _poll_batches_round_robin(
        self,
        *,
        kind: str,
        batch_ids: List[str],
    ) -> Dict[str, Dict[str, Any]]:
        """Poll multiple batches in a round-robin fashion until they settle.

        This avoids getting stuck on a single long-running batch: we check the
        status of all outstanding batch IDs once per loop iteration, sleep for
        ``provider.poll_interval``, and repeat until each batch reaches a
        terminal state (``completed``, ``failed``, or ``expired``) or its
        per-batch timeout is exceeded.

        Returns a mapping of ``batch_id -> status_dict`` where ``status_dict``
        matches the shape returned by ``OpenAIProvider.get_batch_status``.
        """

        if not batch_ids:
            return {}

        outstanding = set(batch_ids)
        statuses: Dict[str, Dict[str, Any]] = {}
        start_times: Dict[str, float] = {bid: time.time() for bid in batch_ids}
        # Adaptive polling interval: start at provider.poll_interval (or 10s),
        # but enforce a minimum of 10s and exponential backoff up to 3600s (1h).
        min_interval = max(10, getattr(self.provider, "poll_interval", 10))
        max_interval = 3600
        backoff_factor = 2.0
        current_interval = float(min_interval)

        # Track last processed counts to detect progress and shrink backoff.
        last_processed: Dict[str, int] = {bid: -1 for bid in batch_ids}

        pretty_kind = kind.capitalize()

        while outstanding:
            made_progress = False

            for batch_id in list(outstanding):
                try:
                    status = self.provider.get_batch_status(batch_id)
                except Exception as exc:  # noqa: BLE001
                    logger.error(
                        "%s batch %s: polling error: %s", pretty_kind, batch_id, exc
                    )
                    self._mark_batch_failed(batch_id, reason="polling error")
                    outstanding.remove(batch_id)
                    made_progress = True
                    continue

                state = status.get("status")
                processed = int(status.get("processed") or 0)

                # Detect progress if processed count increased
                if processed != last_processed.get(batch_id, -1):
                    last_processed[batch_id] = processed
                    made_progress = True

                if state in {"completed", "failed", "expired"}:
                    statuses[batch_id] = status
                    outstanding.remove(batch_id)
                    made_progress = True

                    if state != "completed":
                        # Fetch and log batch-level errors for non-successful batches.
                        try:
                            errors = self.provider.get_batch_errors(batch_id)
                            for error in errors:
                                logger.error(
                                    "%s batch %s error: %s",
                                    pretty_kind,
                                    batch_id,
                                    error,
                                )
                        except Exception as exc:  # noqa: BLE001
                            logger.error(
                                "Failed to retrieve error file for %s batch %s: %s",
                                kind,
                                batch_id,
                                exc,
                            )

                        self._mark_batch_failed(
                            batch_id,
                            reason=f"terminal status {state}",
                        )

                    continue

                # Non-terminal state (e.g. validating/in_progress/finalizing)
                elapsed = time.time() - start_times[batch_id]
                if elapsed > self.per_batch_timeout_sec:
                    logger.error(
                        "%s batch %s did not reach terminal status within %d seconds; "
                        "treating as failed",
                        pretty_kind,
                        batch_id,
                        self.per_batch_timeout_sec,
                    )
                    status_with_timeout = dict(status)
                    status_with_timeout["status"] = "timeout"
                    statuses[batch_id] = status_with_timeout
                    outstanding.remove(batch_id)
                    try:
                        errors = self.provider.get_batch_errors(batch_id)
                        for error in errors:
                            logger.error(
                                "%s batch %s error (on timeout): %s",
                                pretty_kind,
                                batch_id,
                                error,
                            )
                    except Exception as exc:  # noqa: BLE001
                        logger.error(
                            "Failed to retrieve error file for %s batch %s after "
                            "timeout: %s",
                            kind,
                            batch_id,
                            exc,
                        )

                    self._mark_batch_failed(batch_id, reason="timeout")

            if outstanding:
                # Adjust polling interval: if we saw progress, reset to the
                # minimum; otherwise, back off exponentially up to
                # ``max_interval``. Add a small jitter so multiple workers
                # polling the same batches do not synchronize.
                if made_progress:
                    current_interval = float(min_interval)
                else:
                    current_interval = min(
                        max_interval,
                        current_interval * backoff_factor,
                    )

                try:
                    import random

                    jitter = current_interval * 0.1
                    sleep_time = current_interval + random.uniform(-jitter, jitter)
                except Exception:
                    sleep_time = current_interval

                if sleep_time < min_interval:
                    sleep_time = min_interval

                logger.debug(
                    "%s: %d outstanding, sleeping %.1fs (interval=%.1fs)",
                    pretty_kind,
                    len(outstanding),
                    sleep_time,
                    current_interval,
                )
                time.sleep(sleep_time)

        return statuses

    def submit_classification_batches(self) -> List[str]:
        """Submit all loaded articles for classification in batches.

        Uses token-budget-aware batching instead of fixed-size
        batches. Each batch is sized to stay within ``max_tokens_per_batch``.
        """

        articles_list = list(self.processed_articles.values())
        batch_ids: List[str] = []

        logger.info(
            "Phase 1: Submitting %d articles for classification "
            "(max_tokens_per_request=%d, max_tokens_per_batch=%d, "
            "max_inflight_batches=%d)",
            len(articles_list),
            self.max_tokens_per_request,
            self.max_tokens_per_batch,
            self.max_inflight_batches,
        )

        active_batches: List[str] = []

        # Token-budget-based batching
        for batch in self._chunk_by_token_budget(articles_list, "classification"):
            items = [
                {
                    "article_id": art["article_id"],
                    "title": art["title"],
                    "content": art["content"],
                }
                for art in batch
            ]

            # Estimate batch token count for TPM tracking
            batch_tokens = self._estimate_batch_tokens(batch, "classification")

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
                active_batches.append(batch_id)
                self.pending_batches.append(
                    {
                        "kind": "classification",
                        "batch_id": batch_id,
                        "article_ids": [a["article_id"] for a in batch],
                        "estimated_tokens": batch_tokens,
                    }
                )
                logger.info(
                    "Submitted classification batch %d: %s (%d articles, ~%d tokens)",
                    len(batch_ids),
                    batch_id,
                    len(batch),
                    batch_tokens,
                )

                # TPM + inflight-aware throttle before next classification submission
                self._throttle_inflight_and_tpm(active_batches, pending_tokens=batch_tokens)

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

        # Use round-robin polling so that a single slow or stuck batch cannot
        # block progress on all other batches.
        statuses = self._poll_batches_round_robin(
            kind="classification",
            batch_ids=batch_ids,
        )

        for batch_id, status in statuses.items():
            if status.get("status") != "completed":
                # Non-successful batches have already had their articles marked
                # as failed in _poll_batches_round_robin.
                logger.warning(
                    "Classification batch %s ended with status=%s; skipping results",
                    batch_id,
                    status.get("status"),
                )
                continue

            try:
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

                # Retrieve and log batch-level errors for successful batches as well
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
        """Submit classified-TRUE articles for metric extraction.

        Uses token-budget-aware batching.
        """

        logger.info(
            "Phase 2: Submitting %d articles for metric extraction", len(article_ids)
        )

        # Build the full article dicts for token-budget batching
        batch_articles_all: List[Dict[str, Any]] = []
        for article_id in article_ids:
            key = self.article_index.get(article_id)
            if not key:
                logger.warning(
                    "Article %s not found in cache when building extraction batch",
                    article_id,
                )
                continue
            article = self.processed_articles[key]
            batch_articles_all.append(article)

        if not batch_articles_all:
            logger.warning("No articles to submit for extraction")
            return []

        batch_ids: List[str] = []
        active_batches: List[str] = []

        for batch in self._chunk_by_token_budget(batch_articles_all, "extraction"):
            batch_items = [
                {
                    "article_id": art["article_id"],
                    "content": art["content"],
                }
                for art in batch
            ]

            # Estimate batch token count for TPM tracking
            batch_tokens = self._estimate_batch_tokens(batch, "extraction")

            try:
                requests_jsonl = self.provider.build_batch_requests(
                    "extraction", batch_items
                )
                batch_id = self.provider.submit_batch(
                    "extraction",
                    requests_jsonl,
                    max_retries=self.max_retries,
                )
                batch_ids.append(batch_id)
                active_batches.append(batch_id)
                self.pending_batches.append(
                    {
                        "kind": "extraction",
                        "batch_id": batch_id,
                        "article_ids": [a["article_id"] for a in batch],
                        "estimated_tokens": batch_tokens,
                    }
                )
                logger.info(
                    "Submitted extraction batch %d: %s (%d articles, ~%d tokens)",
                    len(batch_ids),
                    batch_id,
                    len(batch_items),
                    batch_tokens,
                )

                # TPM + inflight-aware throttle before next extraction submission
                self._throttle_inflight_and_tpm(active_batches, pending_tokens=batch_tokens)

            except Exception as exc:
                logger.error("Error submitting extraction batch: %s", exc)

        return batch_ids

    def process_extraction_results(self, batch_ids: List[str]) -> None:
        """Poll and process extraction batch results, updating darshan_rows."""

        logger.info(
            "Phase 2b: Polling %d extraction batches", len(batch_ids)
        )

        # Use the same round-robin polling strategy as classification to avoid
        # being blocked by a single slow or stuck extraction batch.
        statuses = self._poll_batches_round_robin(
            kind="extraction",
            batch_ids=batch_ids,
        )

        for batch_id, status in statuses.items():
            if status.get("status") != "completed":
                logger.warning(
                    "Extraction batch %s ended with status=%s; skipping results",
                    batch_id,
                    status.get("status"),
                )
                continue

            try:
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
    # Single-record Processing
    # -----------------------

    def process_articles_per_record(self) -> None:
        """Process loaded articles synchronously, one-by-one.

        This restores the legacy behaviour where each CSV row is classified
        and, if relevant, extracted via direct OpenAI completion calls instead
        of the batch API. The method updates :attr:`darshan_rows`,
        :attr:`failed_records`, and :attr:`metrics` in-place.

        Uses tiktoken for exact content truncation (via the provider)
        and the TPM sliding-window tracker to pace request throughput against
        the organization-level tokens-per-minute limit.
        """

        logger.info("Per-record mode enabled: processing articles one-by-one")

        for key in tqdm(
            list(self.processed_articles.keys()),
            desc="Processing",
            leave=False,
        ):
            article = self.processed_articles[key]
            article_id = article.get("article_id")
            title = article.get("title", "")
            content = article.get("content", "")

            if not article_id:
                continue

            # Estimate tokens for this request (for TPM pacing)
            est_tokens = self._estimate_request_tokens(article, "classification")

            classification: Optional[str] = None
            classification_failed = False

            # Proactive rate-limit pacing before each request
            pace_delay = self._check_rate_limit()
            if pace_delay > 0:
                time.sleep(pace_delay)

            # TPM-aware wait before the request
            self.tpm_tracker.wait_until_ready(est_tokens, max_wait_sec=300.0)

            for attempt in range(1, self.max_retries + 1):
                try:
                    classification = self.provider.classify_article(title, content)
                    self._total_requests += 1
                    break
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "Transient error classifying article %s (attempt %d/%d): %s",
                        article_id,
                        attempt,
                        self.max_retries,
                        exc,
                    )
                    if attempt == self.max_retries:
                        logger.error("Giving up classifying article %s", article_id)
                        self.failed_records.append(article["row"])
                        classification_failed = True
                    else:
                        delay = self._retry_delay_for_exception(exc, attempt)
                        status = getattr(exc, "http_status", None)
                        logger.info(
                            "Retrying classification for %s in %.1fs (status=%s)",
                            article_id,
                            delay,
                            status,
                        )
                        time.sleep(delay)

            if classification is None:
                if not classification_failed:
                    self.failed_records.append(article["row"])
                continue

            normalized = str(classification).strip().lower()
            if not normalized:
                self.failed_records.append(article["row"])
                continue

            logger.info("Classified article %s as %s", article_id, normalized)

            if normalized != "true":
                self.metrics["classified_false"] += 1
                continue

            self.metrics["classified_true"] += 1

            content = article.get("content", "")
            extracted_payload: Optional[Any] = None
            extraction_failed = False

            # Proactive rate-limit pacing before extraction request
            pace_delay = self._check_rate_limit()
            if pace_delay > 0:
                time.sleep(pace_delay)

            # TPM-aware wait before extraction request
            est_extract_tokens = self._estimate_request_tokens(article, "extraction")
            self.tpm_tracker.wait_until_ready(est_extract_tokens, max_wait_sec=300.0)

            for attempt in range(1, self.max_retries + 1):
                try:
                    raw_response = self.provider.extract_metrics(content)
                    self._total_requests += 1
                    try:
                        extracted_payload = json.loads(raw_response)
                    except Exception as exc:  # noqa: BLE001
                        logger.error(
                            "Failed to parse extraction JSON for %s: %s",
                            article_id,
                            exc,
                        )
                        extracted_payload = None
                    break
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "Transient error extracting metrics for %s (attempt %d/%d): %s",
                        article_id,
                        attempt,
                        self.max_retries,
                        exc,
                    )
                    if attempt == self.max_retries:
                        logger.error("Giving up extraction for %s", article_id)
                        extraction_failed = True
                        self.failed_records.append(article["row"])
                    else:
                        delay = self._retry_delay_for_exception(exc, attempt)
                        status = getattr(exc, "http_status", None)
                        logger.info(
                            "Retrying extraction for %s in %.1fs (status=%s)",
                            article_id,
                            delay,
                            status,
                        )
                        time.sleep(delay)

            if extracted_payload is None:
                if not extraction_failed:
                    self.failed_records.append(article["row"])
                self.metrics["extracted_failed"] += 1
                continue

            if not isinstance(extracted_payload, dict):
                logger.warning(
                    "Extraction for article %s returned non-dict payload: %r",
                    article_id,
                    extracted_payload,
                )
                self.failed_records.append(article["row"])
                self.metrics["extracted_failed"] += 1
                continue

            self._apply_extraction_result(article_id, extracted_payload)

        self.metrics["final_records"] = len(self.darshan_rows)

    # -----------------------
    # Persistence & Metrics
    # -----------------------

    def list_batches(self, kind: Optional[str] = None) -> List[Dict[str, Any]]:
        """Return metadata for batches submitted by this processor.

        Batches are recorded in :attr:`pending_batches` when they are submitted
        for classification or extraction and persisted as part of the
        :class:`ProcessorState` so they can be inspected later.

        Args:
            kind: Optional batch kind filter (e.g. "classification" or
                "extraction"). When provided, only batches with a matching
                ``kind`` field are returned.

        Returns:
            A new list with shallow copies of the recorded batch dictionaries.
        """

        if kind is None:
            return [dict(batch) for batch in self.pending_batches]

        return [
            dict(batch)
            for batch in self.pending_batches
            if batch.get("kind") == kind
        ]

    @staticmethod
    def list_batches_from_state(state_path: Path, kind: Optional[str] = None) -> List[Dict[str, Any]]:
        """Load a saved state file and return its recorded batches.

        This is a convenience for inspecting batches from a previous run
        without instantiating a full :class:`BatchProcessor`.

        Args:
            state_path: Path to a ``darshan_state.json`` file produced by a
                prior run.
            kind: Optional batch kind filter (e.g. "classification" or
                "extraction"). When provided, only batches with a matching
                ``kind`` field are returned.

        Returns:
            A list of batch dictionaries recorded under ``pending_batches`` in
            the state file. If the file does not exist or does not contain
            ``pending_batches``, an empty list is returned.
        """

        path = Path(state_path)
        if not path.exists():
            logger.error("State file not found when listing batches: %s", path)
            return []

        try:
            with path.open("r", encoding="utf-8") as f:
                raw = json.load(f)
        except Exception as exc:  # noqa: BLE001
            logger.error("Failed to read state file %s: %s", path, exc)
            return []

        pending = raw.get("pending_batches") or []
        if not isinstance(pending, list):
            logger.warning(
                "State file %s has non-list pending_batches field; ignoring",
                path,
            )
            return []

        if kind is None:
            return [dict(batch) for batch in pending if isinstance(batch, dict)]

        return [
            dict(batch)
            for batch in pending
            if isinstance(batch, dict) and batch.get("kind") == kind
        ]

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

    def load_state(self, state_path: Path) -> None:
        """Load a previously saved ProcessorState from disk.

        This is used to resume or extend an earlier run. Existing in-memory
        darshan_rows, failed_records, metrics, and pending_batches are
        overwritten with the loaded values.
        """

        if not state_path.exists():
            logger.error("State file not found: %s", state_path)
            return

        try:
            with state_path.open("r", encoding="utf-8") as f:
                data = json.load(f)
            state = ProcessorState(**data)
        except Exception as exc:  # noqa: BLE001
            logger.error("Failed to load state from %s: %s", state_path, exc)
            return

        self.darshan_rows = state.darshan_rows
        self.failed_records = state.failed_records
        self.metrics = state.metrics
        self.pending_batches = state.pending_batches

        # Make future saves/outputs land next to this state file.
        self.state_path = state_path
        self.output_dir = state_path.parent

        logger.info(
            "Loaded state from %s: %d records, %d failed, %d unique dates",
            state_path,
            len(self.darshan_rows),
            len(self.failed_records),
            self.metrics.get("final_records", len(self.darshan_rows)),
        )

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
        logger.info("Total API requests:         %d", self._total_requests)

        if self.metrics["total_loaded"] > 0:
            success_rate = (
                self.metrics["extracted_success"]
                / self.metrics["total_loaded"]
                * 100
            )
            logger.info("Overall success rate:       %.1f%%", success_rate)
        logger.info("=" * 60)
        logger.info("TPM tracking:")
        self.tpm_tracker.log_snapshot(label="final")
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
        "--max-inflight-batches",
        type=int,
        default=10,
        help=(
            "Maximum number of OpenAI batch jobs to keep in non-terminal "
            "states at once. Lower values reduce the risk of hitting the "
            "organization enqueued token limit."
        ),
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
        help=(
            "Optional path for saving incremental processing state "
            "(defaults to output/run_TIMESTAMP/darshan_state.json)"
        ),
    )
    parser.add_argument(
        "--resume-from-state",
        type=str,
        default=None,
        help=(
            "Path to an existing darshan_state.json file to resume/extend "
            "a previous run (darshan_rows, failed_records, and metrics "
            "will be loaded before processing new data)."
        ),
    )

    parser.add_argument(
        "--per-record",
        action="store_true",
        help="Process each article synchronously (one-by-one) instead of using batch API",
    )

    # ── Token-aware arguments ─────────────────────────────────────────────
    parser.add_argument(
        "--max-tokens-per-request",
        type=int,
        default=DEFAULT_MAX_TOKENS_PER_REQUEST,
        help=(
            f"Maximum tokens of article content per API request. "
            f"Content beyond this is truncated via tiktoken. (default: {DEFAULT_MAX_TOKENS_PER_REQUEST})"
        ),
    )
    parser.add_argument(
        "--max-tokens-per-batch",
        type=int,
        default=DEFAULT_MAX_TOKENS_PER_BATCH,
        help=(
            f"Maximum tokens per batch file (keep under OpenAI's 2M limit). "
            f"Batches are dynamically sized to not exceed this budget. "
            f"(default: {DEFAULT_MAX_TOKENS_PER_BATCH})"
        ),
    )
    parser.add_argument(
        "--tpm-limit",
        type=int,
        default=DEFAULT_TPM_LIMIT,
        help=(
            f"Organization TPM (tokens per minute) limit. Used by the TPM sliding "
            f"window tracker to pace batch submissions. (default: {DEFAULT_TPM_LIMIT})"
        ),
    )
    parser.add_argument(
        "--tpm-pace-threshold",
        type=float,
        default=DEFAULT_TPM_PACE_THRESHOLD,
        help=(
            f"Fraction of TPM limit at which to start pacing. "
            f"(default: {DEFAULT_TPM_PACE_THRESHOLD})"
        ),
    )
    parser.add_argument(
        "--per-batch-timeout",
        type=int,
        default=DEFAULT_PER_BATCH_TIMEOUT_SEC,
        help=(
            f"Seconds before giving up on a batch during polling. "
            f"Higher values help when batches are queued behind TPM limits. "
            f"(default: {DEFAULT_PER_BATCH_TIMEOUT_SEC})"
        ),
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
        max_inflight_batches=args.max_inflight_batches,
        state_path=Path(args.state_file) if args.state_file else None,
        # ── Token-aware parameters ────────────────────────────────────────
        max_tokens_per_request=args.max_tokens_per_request,
        max_tokens_per_batch=args.max_tokens_per_batch,
        tpm_limit=args.tpm_limit,
        tpm_pace_threshold=args.tpm_pace_threshold,
        per_batch_timeout_sec=args.per_batch_timeout,
    )

    # Register signal handlers so the process can be cleanly interrupted
    # when run under nohup or as a background job. SIGINT/SIGTERM will
    # save state/output and exit; SIGUSR1 will trigger a save without
    # exiting so you can snapshot progress on demand.
    def _graceful_exit(signum, frame):
        logger.warning("Received signal %s. Saving progress and exiting...", signum)
        try:
            processor.save_outputs()
            processor.save_state()
            processor.print_metrics()
        except Exception as exc:
            logger.error("Error during graceful shutdown: %s", exc)
        sys.exit(1)

    def _save_state_only(signum, frame):
        logger.info("Received signal %s. Saving progress (no exit).", signum)
        try:
            processor.save_outputs()
            processor.save_state()
        except Exception as exc:
            logger.error("Error saving state on signal %s: %s", signum, exc)

    signal.signal(signal.SIGINT, _graceful_exit)
    signal.signal(signal.SIGTERM, _graceful_exit)
    # SIGUSR1 is commonly used to request saving state without terminating
    # the process. Use `kill -USR1 <pid>` to trigger.
    try:
        signal.signal(signal.SIGUSR1, _save_state_only)
    except AttributeError:
        # Windows/Python may not have SIGUSR1; ignore if unavailable.
        pass

    # Optional: load retry-failed CSV first
    if args.retry_failed:
        retry_path = Path(args.retry_failed)
        if retry_path.exists():
            processor.load_retry_failed(retry_path)
        else:
            logger.error("Retry-failed CSV not found: %s", retry_path)

    # Optional: resume from an existing state file (e.g. from a previous run)
    if args.resume_from_state:
        resume_path = Path(args.resume_from_state)
        if resume_path.exists():
            processor.load_state(resume_path)
        else:
            logger.error("State file to resume from not found: %s", resume_path)

    try:
        # Phase 1: Load all articles
        logger.info("=" * 60)
        logger.info("PHASE 1: LOADING ARTICLES")
        logger.info("=" * 60)
        processor.load_articles_from_csv(data_dir)

        if args.per_record:
            processor.process_articles_per_record()
            processor.save_outputs()
            processor.save_state()
            processor.print_metrics()

        else:
            # Phase 2: Submit and process classification (batch)
            logger.info("=" * 60)
            logger.info("PHASE 2: CLASSIFICATION")
            logger.info("=" * 60)
            classify_batch_ids = processor.submit_classification_batches()
            classify_article_ids = processor.process_classification_results(
                classify_batch_ids
            )

            # Phase 3: Submit and process extraction (batch)
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
