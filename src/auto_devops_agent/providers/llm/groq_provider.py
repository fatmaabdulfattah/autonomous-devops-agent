"""
providers/llm/groq_provider.py
--------------------------------
Groq cloud LLM provider — requires GROQ_API_KEY.
"""

from providers.llm.base_llm_provider import BaseLLMProvider, LLMResponse


class GroqProvider(BaseLLMProvider):

    name = "groq"

    # Used only if the live list can't be fetched (Sep 2026).
    DEFAULT_MODELS = [
        "openai/gpt-oss-120b",
        "qwen/qwen3.8-27b",
        "openai/gpt-oss-20b",
    ]
    # speech / TTS / safety classifiers — not usable for chat, hidden from the picker
    _NON_CHAT = ("whisper", "orpheus", "prompt-guard", "safeguard", "tts", "playai", "distil")

    def __init__(self, api_key: str, model: str = "llama-3.3-70b-versatile"):
        self.api_key       = api_key
        self.default_model = model

    def chat(self, messages: list[dict], model: str = None, **kwargs) -> LLMResponse:
        from groq import Groq
        model  = model or self.default_model
        client = Groq(api_key=self.api_key)
        resp   = client.chat.completions.create(
            model      = model,
            messages   = messages,
            max_tokens = kwargs.get("max_tokens", 4096),
        )
        return LLMResponse(
            content  = resp.choices[0].message.content,
            model    = model,
            provider = self.name,
        )

    def is_available(self) -> bool:
        try:
            from groq import Groq
            Groq(api_key=self.api_key).models.list()
            return True
        except Exception:
            return False

    def list_models(self) -> list[str]:
        try:
            from groq import Groq
            models = Groq(api_key=self.api_key).models.list()
            ids = [m.id for m in models.data
                   if not any(t in m.id.lower() for t in self._NON_CHAT)]
            return sorted(ids) or self.DEFAULT_MODELS
        except Exception:
            return self.DEFAULT_MODELS