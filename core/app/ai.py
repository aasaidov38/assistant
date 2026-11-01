"""Работа с ИИ: языковая модель (текст и фото) и распознавание речи.

Провайдер меняется в .env без правки кода:
  LLM_PROVIDER=anthropic  — Claude напрямую (api.anthropic.com)
  LLM_PROVIDER=openai     — любой OpenAI-совместимый API: OpenRouter, Google Gemini,
                            OpenAI, локальная Ollama. Адрес задаётся в LLM_BASE_URL.
"""
import asyncio
import json
import logging
import re

import httpx

from .config import get_settings

log = logging.getLogger("ai")
_client = httpx.AsyncClient(timeout=httpx.Timeout(90.0, connect=15.0))


class AIError(Exception):
    pass


async def _post(url: str, **kwargs) -> httpx.Response:
    """POST с повтором при перегрузке или сбое сервиса (до 3 попыток)."""
    last: Exception | None = None
    for attempt in range(3):
        try:
            r = await _client.post(url, **kwargs)
            if r.status_code in (429, 500, 502, 503, 504, 529):
                last = AIError(f"{r.status_code}: {r.text[:300]}")
            else:
                return r
        except httpx.HTTPError as e:
            last = e
        if attempt < 2:
            await asyncio.sleep(2 * (attempt + 1))
    detail = str(last) if isinstance(last, AIError) else f"{type(last).__name__}: {last}"
    raise AIError(f"Сервис ИИ недоступен ({detail})"[:500])


async def complete(system: str, text: str, image_b64: str | None = None,
                   mime: str = "image/jpeg", max_tokens: int = 1500) -> str:
    """Спрашивает основную модель; если она недоступна, пробует запасную (LLM_FALLBACK_MODEL)."""
    s = get_settings()
    key = s.llm_api_key.strip()
    if not key:
        raise AIError("Не задан LLM_API_KEY в .env")
    if not key.isascii():
        raise AIError("LLM_API_KEY содержит русские буквы или лишние символы: вставьте настоящий ключ в .env")
    primary = s.vision_model if image_b64 else s.llm_model
    models = [primary]
    if s.llm_fallback_model and s.llm_fallback_model != primary:
        models.append(s.llm_fallback_model)
    err: AIError | None = None
    for model in models:
        try:
            return await _complete_with(model, key, system, text, image_b64, mime, max_tokens)
        except AIError as e:
            err = e
            if model != models[-1]:
                log.warning("Модель %s не ответила (%s). Пробую запасную.", model, str(e)[:150])
    raise err


async def _complete_with(model: str, key: str, system: str, text: str, image_b64: str | None,
                         mime: str, max_tokens: int) -> str:
    s = get_settings()

    if s.llm_provider == "anthropic":
        content: list[dict] = []
        if image_b64:
            content.append({"type": "image",
                            "source": {"type": "base64", "media_type": mime, "data": image_b64}})
        content.append({"type": "text", "text": text})
        base = s.llm_base_url.rstrip("/") if s.llm_base_url else "https://api.anthropic.com"
        r = await _post(
            f"{base}/v1/messages",
            headers={"x-api-key": key, "anthropic-version": "2023-06-01"},
            json={"model": model, "max_tokens": max_tokens, "system": system,
                  "messages": [{"role": "user", "content": content}]},
        )
        if r.status_code != 200:
            raise AIError(f"Anthropic {r.status_code}: {r.text[:300]}")
        data = r.json()
        return "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")

    # OpenAI-совместимый
    if not s.llm_base_url:
        raise AIError("Для LLM_PROVIDER=openai задайте LLM_BASE_URL")
    content = [{"type": "text", "text": text}]
    if image_b64:
        content.append({"type": "image_url", "image_url": {"url": f"data:{mime};base64,{image_b64}"}})
    r = await _post(
        f"{s.llm_base_url.rstrip('/')}/chat/completions",
        headers={"Authorization": f"Bearer {key}"},
        json={"model": model, "max_tokens": max_tokens,
              "messages": [{"role": "system", "content": system},
                           {"role": "user", "content": content}]},
    )
    if r.status_code != 200:
        raise AIError(f"LLM {r.status_code}: {r.text[:300]}")
    return r.json()["choices"][0]["message"]["content"] or ""


async def complete_document(system: str, text: str, doc_b64: str, mime: str = "application/pdf",
                            max_tokens: int = 8000) -> dict:
    """PDF целиком в модель (например, банковская выписка). Работает только с Claude (LLM_PROVIDER=anthropic)."""
    s = get_settings()
    if s.llm_provider != "anthropic":
        raise AIError("PDF-файлы читает только Claude (LLM_PROVIDER=anthropic). Пришлите выписку в CSV.")
    key = s.llm_api_key.strip()
    if not key or not key.isascii():
        raise AIError("Не задан или неверен LLM_API_KEY в .env")
    base = s.llm_base_url.rstrip("/") if s.llm_base_url else "https://api.anthropic.com"
    content = [{"type": "document", "source": {"type": "base64", "media_type": mime, "data": doc_b64}},
               {"type": "text", "text": text}]
    models = [s.vision_model] + ([s.llm_fallback_model] if s.llm_fallback_model and s.llm_fallback_model != s.vision_model else [])
    err: AIError | None = None
    for model in models:
        r = await _post(f"{base}/v1/messages", headers={"x-api-key": key, "anthropic-version": "2023-06-01"},
                        json={"model": model, "max_tokens": max_tokens, "system": system,
                              "messages": [{"role": "user", "content": content}]})
        if r.status_code == 200:
            raw = "".join(b.get("text", "") for b in r.json().get("content", []) if b.get("type") == "text")
            return parse_json(raw)
        err = AIError(f"Anthropic {r.status_code}: {r.text[:300]}")
    raise err


def parse_json(raw: str) -> dict:
    """Достаёт JSON из ответа модели, даже если он обёрнут в ```json ... ```."""
    m = re.search(r"\{.*\}", raw, re.S)
    if not m:
        raise AIError(f"Модель вернула не JSON: {raw[:200]}")
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError as e:
        raise AIError(f"Не удалось разобрать JSON: {e}") from e


async def complete_json(system: str, text: str, image_b64: str | None = None,
                        mime: str = "image/jpeg", max_tokens: int = 2000) -> dict:
    raw = await complete(system, text, image_b64, mime, max_tokens)
    try:
        return parse_json(raw)
    except AIError:
        # Одна повторная попытка с напоминанием про формат
        raw = await complete(system + "\nОтвечай ТОЛЬКО JSON-объектом, без текста вокруг.",
                             text, image_b64, mime, max_tokens)
        return parse_json(raw)


async def transcribe(audio: bytes, filename: str = "voice.ogg", mime: str = "audio/ogg") -> str:
    """Голос в текст через OpenAI-совместимый API (Groq, OpenAI и др.)."""
    s = get_settings()
    if not s.stt_api_key:
        raise AIError("Не задан STT_API_KEY в .env: голосовые сообщения пока недоступны")
    r = await _post(
        f"{s.stt_base_url.rstrip('/')}/audio/transcriptions",
        headers={"Authorization": f"Bearer {s.stt_api_key}"},
        files={"file": (filename, audio, mime)},
        data={"model": s.stt_model, "language": "ru", "response_format": "json"},
    )
    if r.status_code != 200:
        raise AIError(f"Распознавание речи {r.status_code}: {r.text[:300]}")
    return (r.json().get("text") or "").strip()
