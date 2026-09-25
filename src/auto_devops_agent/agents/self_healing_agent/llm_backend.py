"""
llm_backend.py
--------------
Single LLM entry point for the Self-Healing agent (fixer, verifier, instructor).

Uses the provider/model the user picked at startup (set via set_provider()).
Falls back to the module's default Groq model only if nothing was picked.
Returns an object shaped like a Groq/OpenAI response so call sites stay unchanged:
    resp.choices[0].message.content / resp.choices[0].finish_reason
"""
import os
import re
from types import SimpleNamespace

_provider = None


def set_provider(provider) -> None:
    global _provider
    _provider = provider


def model_name(default: str) -> str:
    if _provider is not None:
        return f"{getattr(_provider, 'name', '?')} / {getattr(_provider, 'default_model', default)}"
    return default


def _wrap(text: str, finish_reason: str = "stop"):
    text = re.sub(r"<think>.*?</think>", "", text or "", flags=re.S).strip()
    return SimpleNamespace(choices=[SimpleNamespace(
        message=SimpleNamespace(content=text), finish_reason=finish_reason)])


def create(default_model: str, messages: list, max_tokens: int, **groq_kwargs):
    """chat completion with the chosen provider. groq_kwargs (reasoning_effort,
    temperature, ...) are only sent to Groq and dropped if the model rejects them."""
    p = _provider
    if p is not None and getattr(p, "name", "") != "groq":
        return _wrap(p.chat(messages, max_tokens=max_tokens).content)

    from groq import Groq, BadRequestError
    model = getattr(p, "default_model", None) or default_model
    key = getattr(p, "api_key", None) or os.environ.get("GROQ_API_KEY")
    client = Groq(api_key=key)
    try:
        r = client.chat.completions.create(model=model, messages=messages,
                                           max_tokens=max_tokens, **groq_kwargs)
    except BadRequestError:
        # model doesn't support e.g. reasoning_effort="none" / include_reasoning
        safe = {k: v for k, v in groq_kwargs.items() if k == "temperature"}
        r = client.chat.completions.create(model=model, messages=messages,
                                           max_tokens=max_tokens, **safe)
    return _wrap(r.choices[0].message.content, r.choices[0].finish_reason)
