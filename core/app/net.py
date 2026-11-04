"""Запросы к внешним бесплатным сервисам (версия 1.10): погода, курсы, индексы, ставки, бури, пыльца.

Одна точка входа get() — её подменяют тесты, а все модули получают одинаковые таймауты и заголовки.
"""
import httpx

_client = httpx.AsyncClient(timeout=15, follow_redirects=True,
                            headers={"User-Agent": "Mozilla/5.0 (compatible; FamilyAssistant/1.10.3)"})


async def get(url: str, params: dict | None = None, as_: str = "json", headers: dict | None = None):
    """as_: json — разобранный JSON, text — строка, bytes — сырые байты. Ошибки сети и HTTP — исключения."""
    r = await _client.get(url, params=params, headers=headers)
    r.raise_for_status()
    if as_ == "bytes":
        return r.content
    if as_ == "text":
        return r.text
    return r.json()
