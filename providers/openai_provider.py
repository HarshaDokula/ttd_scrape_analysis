import os
from openai import OpenAI
from openai import RateLimitError
from providers.provider_base import BaseProvider
from prompt_templates import ttd_prompt_tmpl2, ttd_info_extract_prompt_tmpl

class OpenAIProvider(BaseProvider):
    def __init__(self, api_key: str = None, model: str = None):
        self.client = OpenAI(api_key=api_key or os.getenv("OPENAI_API_KEY"))
        self.model = model or os.getenv("OPENAI_MODEL", "gpt-4o-mini")

    def classify_article(self, title: str, content: str) -> str:
        prompt = ttd_prompt_tmpl2.format(title=title, article_text=content[:800])
        try:
            resp = self.client.chat.completions.create(
                model=self.model,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.01,
                max_tokens=200
            )
            return resp.choices[0].message.content.strip().lower()
        except RateLimitError as e:
            raise e

    def extract_metrics(self, content: str) -> str:
        prompt = ttd_info_extract_prompt_tmpl.format(article_text=content)
        try:
            resp = self.client.chat.completions.create(
                model=self.model,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.01,
                max_tokens=600
            )
            return resp.choices[0].message.content.strip()
        except RateLimitError as e:
            raise e
