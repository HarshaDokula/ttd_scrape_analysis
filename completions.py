class OpenAICompletionClient:
    def __init__(self, api_key, model="gpt-3.5-turbo"):
        from openai import OpenAI
        self.api_key = api_key
        self.model = model
        self.client = OpenAI(api_key=api_key)

    def chat_completion(self, prompt, max_tokens=150, temperature=None, stop=None):
        """
        Calls the OpenAI chat completion API with a preset system message.
        """
        messages = [
            {"role": "system", "content": "You classify TTD news articles."},
            {"role": "user", "content": prompt}
        ]
        params = {
            "model": self.model,
            "messages": messages,
            "max_tokens": max_tokens,
            "stream": False,
            "stop": stop
        }

        if temperature is not None:
            params["temperature"] = temperature

        response = self.client.chat.completions.create(**params)
        return response


class PerplexityCompletionClient:
    def __init__(self, api_key, model="perplexity-ai-model"):
        from openai import OpenAI
        self.api_key = api_key
        self.model = model
        # Set the base URL to Perplexity's endpoint
        self.client = OpenAI(api_key=api_key, base_url="https://api.perplexity.ai")

    def chat_completion(self, prompt, max_tokens=150, temperature=0.2, extra_body=None):
        """
        Calls the Perplexity AI chat completion API with a preset system message and default extra body.
        """
        messages = [
            {"role": "system", "content": "You classify TTD news articles."},
            {"role": "user", "content": prompt}
        ]
        # Default extra_body for perplexity to disable search if not provided
        if extra_body is None:
            extra_body = {"disable_search": True}

        response = self.client.chat.completions.create(
            model=self.model,
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
            stream=False,
            extra_body=extra_body
        )
        return response

