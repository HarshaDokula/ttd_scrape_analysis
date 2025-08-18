# providers/perplexity_provider.py
import os
from providers.provider_base import BaseProvider
from prompt_templates import ttd_prompt_tmpl2, ttd_info_extract_prompt_tmpl
from openai import OpenAI
from openai.error import RateLimitError

class PerplexityProvider(BaseProvider):
    def __init__(self, api_key: str = None, model: str = None):
        self.api_key = api_key or os.getenv("OPENAI_API_KEY")
        self.model = model or os.getenv("PERPLEXITY_MODEL", "perplexity-ai-model")
        # Use OpenAI client but with Perplexity base URL
        self.client = OpenAI(api_key=self.api_key, base_url="https://api.perplexity.ai")

    def classify_article(self, title: str, content: str) -> str:
        prompt = ttd_prompt_tmpl2.format(title=title, article_text=content[:800])
        messages = [
            {"role": "system", "content": "You classify TTD news articles."},
            {"role": "user", "content": prompt}
        ]
        try:
            resp = self.client.chat.completions.create(
                model=self.model,
                messages=messages,
                temperature=0.01,
                max_tokens=200,
                stream=False,
                extra_body={"disable_search": True}  # Perplexity-specific
            )
            return resp.choices[0].message.content.strip().lower()
        except RateLimitError as e:
            raise e

    def extract_metrics(self, content: str) -> str:
        prompt = ttd_info_extract_prompt_tmpl.format(article_text=content)
        messages = [
            {"role": "system", "content": "You extract TTD news metrics."},
            {"role": "user", "content": prompt}
        ]
        try:
            resp = self.client.chat.completions.create(
                model=self.model,
                messages=messages,
                temperature=0.01,
                max_tokens=600,
                stream=False,
                extra_body={"disable_search": True}  # Perplexity-specific
            )
            return resp.choices[0].message.content.strip()
        except RateLimitError as e:
            raise e
