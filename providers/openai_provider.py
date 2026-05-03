import os
import time
import logging
import json
from typing import List, Dict, Optional, Any
from openai import OpenAI
from prompt_templates import ttd_prompt_tmpl2, ttd_info_extract_prompt_tmpl

logger = logging.getLogger(__name__)


class OpenAIProvider:
    """OpenAI provider with batch processing support for TTD article analysis."""

    def __init__(self, api_key: str = None, model: str = None):
        self.api_key = api_key or os.getenv("OPENAI_API_KEY")
        if not self.api_key:
            raise ValueError("Missing OpenAI API key")
        self.model = model or os.getenv("OPENAI_MODEL", "gpt-4o-mini")
        self.client = OpenAI(api_key=self.api_key)
        self.poll_interval = 10  # seconds between batch status checks
        self.max_poll_attempts = 1440  # ~24 hours at 10s intervals

    # ---- Single Call Methods (backward compatibility) ----

    def classify_article(self, title: str, content: str) -> str:
        """Classify a single article (synchronous)."""
        prompt = ttd_prompt_tmpl2.format(title=title, article_text=content[:800])
        try:
            response = self.client.chat.completions.create(
                model=self.model,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.01,
                max_tokens=2,
            )
            return response.choices[0].message.content.strip().lower()
        except Exception as e:
            logger.error(f"Error classifying article: {e}")
            return None

    def extract_metrics(self, content: str) -> str:
        """Extract metrics from a single article (synchronous)."""
        prompt = ttd_info_extract_prompt_tmpl.format(article_text=content)
        try:
            response = self.client.chat.completions.create(
                model=self.model,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.01,
                max_tokens=200,
                stop="```",
            )
            return response.choices[0].message.content.strip()
        except Exception as e:
            logger.error(f"Error extracting metrics: {e}")
            return None

    # ---- Batch Processing Methods ----

    def submit_classify_batch(self, articles: List[Dict[str, str]]) -> str:
        """
        Submit a batch of articles for classification.
        
        Args:
            articles: List of dicts with 'id', 'title', 'content' keys
            
        Returns:
            batch_id: The batch job ID for polling
        """
        batch_requests = []
        for i, article in enumerate(articles):
            prompt = ttd_prompt_tmpl2.format(
                title=article.get("title", ""),
                article_text=article.get("content", "")[:800]
            )
            batch_requests.append({
                "custom_id": f"{article['id']}-classify",
                "method": "POST",
                "url": "/v1/chat/completions",
                "body": {
                    "model": self.model,
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": 0.01,
                    "max_tokens": 2,
                }
            })

        # Upload batch file and submit
        try:
            batch_file = self.client.files.create(
                file=(
                    "batch_classify.jsonl",
                    self._format_jsonl(batch_requests),
                    "application/json"
                ),
                purpose="batch",
            )
            
            batch = self.client.batches.create(
                input_file_id=batch_file.id,
                endpoint="/v1/chat/completions",
                completion_window="24h",
            )
            
            logger.info(f"Submitted classification batch: {batch.id}")
            return batch.id
        except Exception as e:
            logger.error(f"Error submitting classification batch: {e}")
            raise

    def submit_extract_batch(self, articles: List[Dict[str, str]]) -> str:
        """
        Submit a batch of articles for metric extraction.
        
        Args:
            articles: List of dicts with 'id', 'content' keys
            
        Returns:
            batch_id: The batch job ID for polling
        """
        batch_requests = []
        for article in articles:
            prompt = ttd_info_extract_prompt_tmpl.format(
                article_text=article.get("content", "")
            )
            batch_requests.append({
                "custom_id": f"{article['id']}-extract",
                "method": "POST",
                "url": "/v1/chat/completions",
                "body": {
                    "model": self.model,
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": 0.01,
                    "max_tokens": 200,
                    "stop": "```"
                }
            })

        # Upload batch file and submit
        try:
            batch_file = self.client.files.create(
                file=(
                    "batch_extract.jsonl",
                    self._format_jsonl(batch_requests),
                    "application/json"
                ),
                purpose="batch",
            )
            
            batch = self.client.batches.create(
                input_file_id=batch_file.id,
                endpoint="/v1/chat/completions",
                completion_window="24h",
            )
            
            logger.info(f"Submitted extraction batch: {batch.id}")
            return batch.id
        except Exception as e:
            logger.error(f"Error submitting extraction batch: {e}")
            raise

    def poll_batch_status(self, batch_id: str) -> Dict[str, Any]:
        """
        Poll batch status until completion.
        
        Args:
            batch_id: The batch job ID
            
        Returns:
            status_dict with keys: status, processed, failed
        """
        attempt = 0
        while attempt < self.max_poll_attempts:
            try:
                batch = self.client.batches.retrieve(batch_id)
                
                logger.info(
                    f"Batch {batch_id}: status={batch.status} "
                    f"(processed={batch.request_counts.completed}, "
                    f"failed={batch.request_counts.failed})"
                )
                
                if batch.status in ["completed", "failed", "expired"]:
                    return {
                        "status": batch.status,
                        "processed": batch.request_counts.completed,
                        "failed": batch.request_counts.failed,
                        "output_file_id": batch.output_file_id,
                        "error_file_id": batch.error_file_id,
                    }
                
                time.sleep(self.poll_interval)
                attempt += 1
                
            except Exception as e:
                logger.error(f"Error polling batch status: {e}")
                raise

        raise TimeoutError(f"Batch {batch_id} did not complete within timeout")

    def get_batch_results(self, batch_id: str) -> List[Dict[str, Any]]:
        """
        Retrieve results from a completed batch.
        
        Args:
            batch_id: The batch job ID
            
        Returns:
            List of result dicts with 'custom_id' and 'response' keys
        """
        try:
            batch = self.client.batches.retrieve(batch_id)
            
            if not batch.output_file_id:
                logger.warning(f"No output file for batch {batch_id}")
                return []
            
            results = []
            output_file = self.client.files.content(batch.output_file_id)
            
            for line in output_file.text.strip().split('\n'):
                if line:
                    result = json.loads(line)
                    results.append(result)
            
            logger.info(f"Retrieved {len(results)} results from batch {batch_id}")
            return results
            
        except Exception as e:
            logger.error(f"Error retrieving batch results: {e}")
            raise

    def get_batch_errors(self, batch_id: str) -> List[Dict[str, Any]]:
        """
        Retrieve error details from a failed batch.
        
        Args:
            batch_id: The batch job ID
            
        Returns:
            List of error dicts
        """
        try:
            batch = self.client.batches.retrieve(batch_id)
            
            if not batch.error_file_id:
                logger.info(f"No errors for batch {batch_id}")
                return []
            
            errors = []
            error_file = self.client.files.content(batch.error_file_id)
            
            for line in error_file.text.strip().split('\n'):
                if line:
                    error = json.loads(line)
                    errors.append(error)
            
            logger.warning(f"Retrieved {len(errors)} errors from batch {batch_id}")
            return errors
            
        except Exception as e:
            logger.error(f"Error retrieving batch errors: {e}")
            return []

    # ---- Utility Methods ----

    @staticmethod
    def _format_jsonl(data: List[Dict[str, Any]]) -> str:
        """Format a list of dicts as JSONL (one JSON object per line)."""
        return "\n".join(json.dumps(item, ensure_ascii=False) for item in data)

    def parse_response(self, result: Dict[str, Any]) -> Optional[str]:
        """
        Parse a batch result to extract the text response.
        
        Args:
            result: A single result dict from batch output
            
        Returns:
            The text content of the response, or None if error
        """
        try:
            if "error" in result:
                logger.error(f"Request error: {result['error']}")
                return None
            
            response = result.get("response", {})
            body = response.get("body", {})
            choices = body.get("choices", [])
            
            if choices:
                return choices[0].get("message", {}).get("content", "").strip()
            
            return None
        except Exception as e:
            logger.error(f"Error parsing response: {e}")
            return None
