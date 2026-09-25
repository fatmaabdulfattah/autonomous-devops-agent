"""
providers/llm/gemini_provider.py
----------------------------------
Google Gemini cloud LLM provider — requires GEMINI_API_KEY.

Uses the current Google Gen AI SDK (`google-genai`); the old
`google-generativeai` package is discontinued.
"""

from providers.llm.base_llm_provider import BaseLLMProvider, LLMResponse


class GeminiProvider(BaseLLMProvider):

    name = "gemini"

    # Current (Sep 2026). Used only if the live model list can't be fetched.
    DEFAULT_MODELS = [
        "gemini-3.8-flash",
        "gemini-3.7-flash",
        "gemini-3.5-flash-lite",
        "gemini-3.1-flash-lite",
    ]
    # Tried automatically when the chosen model stays overloaded (503).
    FALLBACK_MODELS = ["gemini-3.5-flash-lite", "gemini-3.1-flash-lite"]
    RETRY_WAITS = (3, 8, 20)          # seconds between retries on 503 / 429 / 500

    # not chat/text models — hidden from the picker
    _NON_TEXT = ("embedding", "tts", "image", "live", "audio", "robotics", "omni",
                 "transcribe", "veo", "lyria", "imagen", "aqa", "native", "computer-use")

    def __init__(self, api_key: str, model: str = "gemini-3.8-flash"):
        self.api_key       = api_key
        self.default_model = model
        self._client_obj   = None
        self._check_package()

    @staticmethod
    def _check_package():
        try:
            from google import genai  # noqa: F401
        except ImportError:
            raise ImportError("google-genai is not installed.\nRun: pip install google-genai")

    def _client(self):
        # Keep ONE client alive for the provider's lifetime. A throwaway
        # genai.Client() gets garbage-collected mid-call, which closes its HTTP
        # connection -> "Cannot send a request, as the client has been closed."
        if self._client_obj is None:
            from google import genai
            self._client_obj = genai.Client(api_key=self.api_key)
        return self._client_obj

    def chat(self, messages: list[dict], model: str = None, **kwargs) -> LLMResponse:
        from google.genai import types

        model = model or self.default_model
        system_parts = [m["content"] for m in messages if m["role"] == "system"]
        contents = [
            types.Content(
                role="model" if m["role"] == "assistant" else "user",
                parts=[types.Part(text=m["content"])],
            )
            for m in messages if m["role"] != "system"
        ]
        config = types.GenerateContentConfig(
            system_instruction="\n".join(system_parts) if system_parts else None,
            max_output_tokens=kwargs.get("max_tokens", 4096),
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        )
        tried = []
        for m in [model] + [f for f in self.FALLBACK_MODELS if f != model]:
            try:
                resp = self._generate_with_retry(m, contents, config)
                if m != model:
                    print(f"[Gemini] Used fallback model {m} ({model} was overloaded)")
                return LLMResponse(content=resp.text or "", model=m, provider=self.name)
            except Exception as exc:
                if self._status(exc) not in (503, 500, 429):
                    raise
                tried.append(f"{m}: {self._status(exc)}")
        raise RuntimeError("Gemini is overloaded right now (tried " + ", ".join(tried) +
                           "). Wait a few minutes, or pick another provider.")

    @staticmethod
    def _status(exc) -> int:
        return int(getattr(exc, "code", 0) or getattr(exc, "status_code", 0) or 0)

    def _generate_with_retry(self, model, contents, config):
        import time
        for attempt, wait in enumerate(list(self.RETRY_WAITS) + [None]):
            try:
                return self._client().models.generate_content(
                    model=model, contents=contents, config=config)
            except Exception as exc:
                if self._status(exc) not in (503, 500, 429) or wait is None:
                    raise
                print(f"[Gemini] {model} busy ({self._status(exc)}) — retry "
                      f"{attempt + 1}/{len(self.RETRY_WAITS)} in {wait}s...")
                time.sleep(wait)

    def is_available(self) -> bool:
        try:
            next(iter(self._client().models.list()))
            return True
        except Exception:
            return False

    def list_models(self) -> list[str]:
        try:
            out = []
            for m in self._client().models.list():
                actions = getattr(m, "supported_actions", None) or []
                name = m.name.replace("models/", "")
                if actions and "generateContent" not in actions:
                    continue
                if not name.startswith("gemini") or any(t in name for t in self._NON_TEXT):
                    continue
                out.append(name)
            # newest first: gemini-3.8-flash before gemini-3.5-flash before gemini-2.5-...
            import re
            def key(n):
                v = re.search(r"gemini-(\d+(?:\.\d+)?)", n)
                return (-(float(v.group(1)) if v else 0), "preview" in n, n)
            return sorted(set(out), key=key)[:12] or self.DEFAULT_MODELS
        except Exception:
            return self.DEFAULT_MODELS
