import os
import sys
import logging
import time
from dotenv import load_dotenv

load_dotenv()

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_MODEL = os.getenv("OPENAI_MODEL")
PERPLEXITY_API_KEY = os.getenv("PERPLEXITYAI_API_KEY")
PERPLEXITY_MODEL = os.getenv("PERPLEXITYAI_MODEL")

logger = logging.getLogger(__name__)

def get_client():
    """Factory function that returns the appropriate LLM client and its type."""
    try:
        if OPENAI_MODEL and OPENAI_API_KEY:
            from openai import RateLimitError
            from completions import OpenAICompletionClient
            client = OpenAICompletionClient(api_key=OPENAI_API_KEY, model=OPENAI_MODEL)
            logger.info(f"Using OpenAI client with model: {OPENAI_MODEL}")
            return client, "openai", RateLimitError

        elif PERPLEXITY_MODEL and PERPLEXITY_API_KEY:
            from openai import RateLimitError  # assuming reuse
            from completions import PerplexityCompletionClient
            client = PerplexityCompletionClient(api_key=PERPLEXITY_API_KEY, model=PERPLEXITY_MODEL)
            logger.info(f"Using Perplexity client with model: {PERPLEXITY_MODEL}")
            return client, "perplexity", RateLimitError

        else:
            logger.error("No valid model configured in .env file.")
            sys.exit(1)

    except ImportError as e:
        logger.error(f"Client import error: {e}")
        sys.exit(1)
