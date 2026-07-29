# -*- coding: utf-8 -*-
"""Единственное место в пакете, где происходит сетевой вызов.

Два следствия из того, что оно единственное.

Первое — **сырое тело ответа провайдера не покидает эту функцию**. Оно
превращается в `HttpReply`, а `HttpReply` вымарывает секреты в конструкторе.
Выше по стеку раздобыть невымаранный текст неоткуда: другого пути к сокету
нет. Именно так исправлен дефект stomchat, где скраббер существовал, но
вызывался только из `_record_failure`, а `logger.warning(f"... {exc}")` и
`_write_generation_status(..., error=str(exc)[:500])` шли мимо него — при теле
вида «401 invalid api key <ключ>» ключ уходил и в журнал, и в файл статуса.
Здесь мимо не пройти: вымарывание стоит не на пути к журналу, а на границе
объекта.

Второе — тест подменяет `post_chat` целиком и работает без сети и без ключей.
Ни один мок не нужен в продуктовом коде: подменяется одна функция, у которой
ровно одна ответственность.

Текст исключений транспорта не сохраняется вообще, даже вымаранным: от
`httpx.ConnectError` полезен только класс ошибки, а сообщение способно
содержать и URL, и заголовки. Класс — не секрет никогда.
"""
from __future__ import annotations

import time
from dataclasses import dataclass

import httpx

from . import keys

log = keys.logger(__name__)

# Оба провайдера отвечают по OpenAI-совместимому протоколу, поэтому запрос один
# и тот же и различие сводится к базовому адресу.
BASE_URLS: dict[str, str] = {
    "gemini": "https://generativelanguage.googleapis.com/v1beta/openai",
    "groq": "https://api.groq.com/openai/v1",
}

# Свои коды на месте HTTP-статуса: до статуса дело не дошло.
STATUS_TIMEOUT = -1
STATUS_NO_CONNECTION = 0

EXCERPT_CHARS = 300


@dataclass(frozen=True)
class HttpReply:
    """Ответ провайдера, из которого секреты уже вымараны.

    `body` намеренно не обрезается: по нему разбирается успешный ответ модели.
    Для журнала есть `excerpt` — короткий и тоже вымаранный.
    """

    status: int
    body: str
    elapsed_ms: int

    def __post_init__(self) -> None:
        # frozen-датакласс правится через object.__setattr__ — единственный
        # способ гарантировать, что невымаранного `body` не существует ни на
        # одну строчку кода, а не «до первого логирования».
        object.__setattr__(self, "body", keys.redact(self.body))

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300

    @property
    def excerpt(self) -> str:
        return self.body[:EXCERPT_CHARS]


def _url(provider: str, path: str) -> str:
    base = BASE_URLS.get(provider)
    if not base:
        raise ValueError(f"неизвестный провайдер: {provider!r}")
    return f"{base}/{path.lstrip('/')}"


async def _send(method: str, provider: str, path: str, api_key: str,
                payload: dict | None, timeout_s: float) -> HttpReply:
    """Один запрос. Соединение не переиспользуется — и это осознанно.

    Общий `AsyncClient` экономил бы установку TCP, но привязывал бы пакет к
    конкретному живому event loop: в тестах и в ops-скриптах loop свой, и
    закэшированный клиент от чужого loop падает на первом же запросе. Поток
    сообщений Авито — десятки в сутки, экономия здесь не стоит этой связки.
    """
    started = time.monotonic()
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    try:
        async with httpx.AsyncClient(timeout=timeout_s) as client:
            response = await client.request(
                method, _url(provider, path), headers=headers, json=payload
            )
        elapsed = int((time.monotonic() - started) * 1000)
        return HttpReply(response.status_code, response.text, elapsed)
    except httpx.TimeoutException:
        return HttpReply(STATUS_TIMEOUT, "timeout",
                         int((time.monotonic() - started) * 1000))
    except (httpx.HTTPError, OSError) as exc:
        # Только класс ошибки. Сообщение транспорта не сохраняем принципиально.
        return HttpReply(STATUS_NO_CONNECTION, exc.__class__.__name__,
                         int((time.monotonic() - started) * 1000))


async def post_chat(provider: str, api_key: str, payload: dict,
                    timeout_s: float) -> HttpReply:
    """Запрос генерации. Именно эту функцию подменяет тест."""
    return await _send("POST", provider, "chat/completions", api_key, payload, timeout_s)


async def get_models(provider: str, api_key: str, timeout_s: float = 20.0) -> HttpReply:
    """Список моделей провайдера. Нужен разведке в `cascade.py`."""
    return await _send("GET", provider, "models", api_key, None, timeout_s)
