from abc import ABC, abstractmethod
from typing import Any, Dict

class BaseProvider(ABC):
    @abstractmethod
    def classify_article(self, title: str, content: str) -> str:
        """Return 'true' or 'false' for the article."""
        pass

    @abstractmethod
    def extract_metrics(self, content: str) -> str:
        """Return raw JSON string of extracted metrics."""
        pass
