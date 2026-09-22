# collateral-service/provider.py
import httpx, os, json

from engines.token_usage import add_usage, usage_from


class Provider:
    def __init__(self):
        self.base = os.environ["LLM_BASE_URL"]      # e.g. https://openrouter.ai/api/v1
        self.key  = os.environ["LLM_API_KEY"]

    def call(self, model, messages, temperature=0.2, max_tokens=None, reasoning_effort=None):
        content, _ = self.call_usage(model, messages, temperature)
        return content

    def call_usage(self, model, messages, temperature=0.2):
        """``call`` plus the token counts the response already carries.

        Separate from ``call`` only so the five engines sharing this module keep
        their single-value return.
        """
        r = httpx.post(
            f"{self.base}/chat/completions",
            headers={"Authorization": f"Bearer {self.key}"},
            json={"model": model, "messages": messages, "temperature": temperature},
            timeout=300,
        )
        r.raise_for_status()
        body = r.json()
        return body["choices"][0]["message"]["content"], usage_from(body)

    def stream(self, model, messages, temperature=0.2, usage_sink=None):
        """Yield content deltas as they arrive (OpenAI/OpenRouter SSE format).

        Used for the human-readable observation step so the frontend can show
        tokens live instead of waiting for the whole call. Same request as
        ``call`` but with ``stream: True``; non-content lines (``: comment``
        keep-alives, ``[DONE]``, malformed chunks) are skipped defensively.

        A streamed response reports its token counts only if asked, and only in
        a final chunk that carries no choices — so ``stream_options`` is set and
        that chunk is copied into ``usage_sink`` (a dict) when one is given.
        Callers that pass nothing are unaffected.
        """
        with httpx.stream(
            "POST",
            f"{self.base}/chat/completions",
            headers={"Authorization": f"Bearer {self.key}"},
            json={"model": model, "messages": messages, "temperature": temperature,
                  "stream": True, "stream_options": {"include_usage": True}},
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
                    chunk = json.loads(data)
                except json.JSONDecodeError:
                    continue
                if usage_sink is not None and chunk.get("usage"):
                    add_usage(usage_sink, usage_from(chunk))
                try:
                    delta = chunk["choices"][0]["delta"].get("content")
                except (KeyError, IndexError):
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
