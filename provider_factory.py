import os
from providers.openai_provider import OpenAIProvider
from providers.perplexity_provider import PerplexityProvider

def get_provider():
    provider_name = os.getenv("PROVIDER", "openai").lower()
    match provider_name:
        case "openai":
            return OpenAIProvider()
        case "perplexity":
            return PerplexityProvider()
        case _:
            raise ValueError(f"Unknown provider: {provider_name}")
