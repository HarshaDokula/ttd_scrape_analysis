import os
from providers.openai_provider import OpenAIProvider

def get_provider():
    """Return OpenAI provider for TTD article processing."""
    return OpenAIProvider()
