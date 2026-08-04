# collateral-service/provider.py
import httpx, os, json

class Provider:
    def __init__(self):
        self.base = os.environ["LLM_BASE_URL"]      # e.g. https://openrouter.ai/api/v1
        self.key  = os.environ["LLM_API_KEY"]

    def call(self, model, messages, temperature=0.2, max_tokens=None, reasoning_effort=None):
        r = httpx.post(
            f"{self.base}/chat/completions",
            headers={"Authorization": f"Bearer {self.key}"},
            json={"model": model, "messages": messages, "temperature": temperature},
            timeout=300,
        )
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"]

    def stream(self, model, messages, temperature=0.2):
        """Yield content deltas as they arrive (OpenAI/OpenRouter SSE format).

        Used for the human-readable observation step so the frontend can show
        tokens live instead of waiting for the whole call. Same request as
        ``call`` but with ``stream: True``; non-content lines (``: comment``
        keep-alives, ``[DONE]``, malformed chunks) are skipped defensively.
        """
        with httpx.stream(
            "POST",
            f"{self.base}/chat/completions",
            headers={"Authorization": f"Bearer {self.key}"},
            json={"model": model, "messages": messages, "temperature": temperature, "stream": True},
            timeout=300,
        ) as r:
            r.raise_for_status()
            for line in r.iter_lines():
                if not line or not line.startswith("data:"):
                    continue
                data = line[len("data:"):].strip()
                if data == "[DONE]":
                    break
                try:
                    delta = json.loads(data)["choices"][0]["delta"].get("content")
                except (json.JSONDecodeError, KeyError, IndexError):
                    continue
                if delta:
                    yield delta

    def embed(self, model, texts):
        r = httpx.post(
            f"{self.base}/embeddings",
            headers={"Authorization": f"Bearer {self.key}"},
            json={"model": model, "input": list(texts)},
            timeout=300,
        )
        r.raise_for_status()
        data = r.json()["data"]
        return [d["embedding"] for d in sorted(data, key=lambda x: x["index"])]
