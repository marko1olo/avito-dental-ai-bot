# -*- coding: utf-8 -*-
"""Три метода Bot API поверх httpx. Больше клинике не нужно.

Почему здесь нет aiogram или python-telegram-bot. Панель администратора
использует ровно `sendMessage`, `answerCallbackQuery` и `getUpdates`. Фреймворк
бота приносит роутеры, FSM, middlewares и свой event loop — и вместе с ними
своё представление о том, как хранить состояние диалога. Состояние у нас уже
живёт в SQLite (`brain/store/db.py`), и второй источник правды о том, «ждём ли
мы правку от администратора», — это готовый рассинхрон. Три HTTP-вызова дешевле.

Главная опасность этого файла — **токен лежит в URL**: `/bot<токен>/sendMessage`.
Любое исключение httpx печатает URL целиком, и токен уезжает в лог, в трейсбек,
в отчёт об ошибке. Поэтому наружу не выходит ни один сырой объект httpx: каждая
ошибка пересобирается в `TelegramApiError` с текстом, прогнанным через
`redact()`. По той же причине `Config.__repr__` переопределён — иначе
`print(config)` в отладке выдаёт токен, и это происходит именно в тот момент,
когда что-то не работает и отладку включают.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import httpx

log = logging.getLogger("tg.api")

API_ROOT = "https://api.telegram.org"

TOKEN_ENV = "TELEGRAM_BOT_TOKEN"
CHAT_ENV = "TELEGRAM_CHAT_ID"

#: Лимит Telegram на текст одного сообщения. Превышение — HTTP 400, а не
#: обрезка на стороне сервера: сообщение не доходит вообще.
TEXT_LIMIT = 4096
#: Лимит на `callback_data` кнопки — 64 БАЙТА, не символа. Русский текст в
#: callback_data съел бы его вдвое быстрее, поэтому в `panel.py` там латиница.
CALLBACK_DATA_LIMIT = 64

MAX_ATTEMPTS = 3
RETRY_AFTER_CAP = 30.0
#: Долгий read-таймаут обязателен для long polling: `getUpdates` с
#: `timeout=25` держит соединение открытым 25 с, и httpx по умолчанию (5 с)
#: рвёт его раньше, чем Telegram успевает ответить.
CONNECT_TIMEOUT = 10.0

_TOKEN_IN_URL = re.compile(r"/bot[0-9A-Za-z:_\-]+")
_TOKEN_SHAPE = re.compile(r"\b\d{6,12}:[A-Za-z0-9_\-]{30,}\b")
_HIDDEN = "<токен>"


class TelegramConfigError(RuntimeError):
    """Конфигурация Telegram неполна. Проверяется при старте супервизора."""


class TelegramApiError(RuntimeError):
    """Ответ Bot API с `ok: false` или транспортная ошибка после всех попыток.

    Текст уже прогнан через `redact()`: исключение можно логировать целиком.
    """

    def __init__(self, method: str, description: str, *,
                 error_code: int | None = None) -> None:
        super().__init__(f"{method}: {description}")
        self.method = method
        self.description = description
        self.error_code = error_code


def redact(text: str) -> str:
    """Вырезает токен из произвольного текста.

    Работает без знания самого токена — по форме (`123456789:AA...`) и по месту
    в URL. Так функция годится и для текста, пришедшего от Telegram: провайдеры
    любят возвращать запрос обратно в теле ошибки.
    """
    cleaned = _TOKEN_IN_URL.sub("/bot" + _HIDDEN, text)
    return _TOKEN_SHAPE.sub(_HIDDEN, cleaned)


@dataclass(frozen=True)
class Config:
    """Токен и чат. Единственное место, где токен существует в процессе."""

    token: str
    chat_id: str

    def __repr__(self) -> str:  # noqa: D105 - см. модульный docstring
        return f"Config(chat_id={self.chat_id!r}, token={_HIDDEN})"

    __str__ = __repr__

    def url(self, method: str) -> str:
        return f"{API_ROOT}/bot{self.token}/{method}"


def load_config(env: Mapping[str, str] | None = None) -> Config:
    """Читает конфигурацию из окружения.

    `env` параметром — чтобы тест мог проверить поведение без правки
    `os.environ` процесса. В продакшене вызывается без аргументов.
    """
    source = os.environ if env is None else env
    missing = [name for name in (TOKEN_ENV, CHAT_ENV) if not (source.get(name) or "").strip()]
    if missing:
        raise TelegramConfigError(
            "не заданы переменные окружения: " + ", ".join(missing)
            + ". Панель администратора без них не работает: черновики некуда отправлять."
        )

    token = source[TOKEN_ENV].strip()
    chat_id = source[CHAT_ENV].strip()

    # Форму токена проверяем, но в сообщение об ошибке не кладём ни его, ни
    # длину, ни первые символы: по префиксу восстанавливается id бота.
    if ":" not in token or not token.split(":", 1)[0].isdigit():
        raise TelegramConfigError(
            f"{TOKEN_ENV} не похож на токен Bot API (ожидается «<id бота>:<секрет>»)")
    if not re.fullmatch(r"-?\d+|@[A-Za-z][A-Za-z0-9_]{4,}", chat_id):
        raise TelegramConfigError(
            f"{CHAT_ENV}={chat_id!r} не похож на id чата: нужен числовой id "
            "(у групп он отрицательный) или @username канала")
    return Config(token=token, chat_id=chat_id)


def check_config(env: Mapping[str, str] | None = None) -> str:
    """Проверка при старте. Возвращает строку для лога БЕЗ токена.

    Существует отдельно от `load_config`, чтобы супервизор мог упасть на
    старте, а не в момент первого лида: без Telegram бот бесполезен целиком —
    все цены, симптомы и записи уходят администратору, и терять их молча
    хуже, чем не запуститься.
    """
    config = load_config(env)
    return f"Telegram: чат {config.chat_id}, токен получен из {TOKEN_ENV}"


def clamp(text: str, limit: int = TEXT_LIMIT) -> str:
    """Обрезает текст до лимита Telegram, отмечая обрезку.

    Обрезка по последнему переводу строки, если он близко к концу: рвать
    сообщение посреди суммы или телефона — прямой путь к тому, что
    администратор прочитает «удаление 1» вместо «1500».
    """
    if len(text) <= limit:
        return text
    mark = "\n…обрезано, целиком — в Авито"
    room = limit - len(mark)
    head = text[:room]
    cut = head.rfind("\n")
    if cut >= room - 220:
        head = head[:cut]
    return head.rstrip() + mark


async def _request(config: Config, method: str, payload: dict[str, Any], *,
                   client: httpx.AsyncClient | None = None,
                   read_timeout: float = 20.0) -> Any:
    """Один вызов Bot API с повторами. Наружу — только очищенные ошибки."""
    timeout = httpx.Timeout(read_timeout, connect=CONNECT_TIMEOUT)
    own_client = client is None
    http = client or httpx.AsyncClient(timeout=timeout)
    last: str = "попыток не было"
    try:
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                response = await http.post(config.url(method), json=payload, timeout=timeout)
            except httpx.HTTPError as exc:
                # Сюда попадает и таймаут, и обрыв DNS. У httpx в тексте
                # исключения лежит URL — то есть токен. Наружу его нельзя.
                last = f"транспорт: {type(exc).__name__}"
                log.warning("%s попытка %d/%d: %s", method, attempt, MAX_ATTEMPTS, last)
                await _backoff(attempt)
                continue

            try:
                body = response.json()
            except ValueError:
                last = f"HTTP {response.status_code}, ответ не JSON"
                log.warning("%s попытка %d/%d: %s", method, attempt, MAX_ATTEMPTS, last)
                await _backoff(attempt)
                continue

            if body.get("ok"):
                return body.get("result")

            code = body.get("error_code")
            last = redact(str(body.get("description") or f"HTTP {response.status_code}"))

            if code == 429:
                pause = min(float(body.get("parameters", {}).get("retry_after", 1)),
                            RETRY_AFTER_CAP)
                log.warning("%s: Telegram просит подождать %.0f с", method, pause)
                await asyncio.sleep(pause)
                continue
            if code is not None and 500 <= int(code) < 600:
                log.warning("%s попытка %d/%d: %s", method, attempt, MAX_ATTEMPTS, last)
                await _backoff(attempt)
                continue
            # 400/401/403 — повторять бессмысленно: это мы, а не сеть.
            raise TelegramApiError(method, last, error_code=code)

        raise TelegramApiError(method, f"не удалось за {MAX_ATTEMPTS} попыток: {last}")
    finally:
        if own_client:
            await http.aclose()


async def _backoff(attempt: int) -> None:
    await asyncio.sleep(min(2.0 * attempt, 8.0))


async def send_message(text: str, *, reply_markup: dict | None = None,
                       reply_to_message_id: int | None = None,
                       silent: bool = False,
                       chat_id: str | None = None,
                       config: Config | None = None,
                       client: httpx.AsyncClient | None = None) -> int:
    """Отправляет сообщение в чат администраторов. Возвращает message_id.

    Без `parse_mode` намеренно. В черновик идёт оригинальный текст пациента, а
    он содержит что угодно: «<3», «стоимость < 5000», амперсанды, звёздочки.
    С `parse_mode=HTML` такое сообщение Telegram отклоняет целиком (400
    can't parse entities) — то есть лид теряется из-за символа в чужом тексте.
    Экранировать можно, но тогда обрезка по лимиту способна разрубить
    `&amp;` посередине и вернуть ту же ошибку. Простой текст не ломается.
    """
    cfg = config or load_config()
    payload: dict[str, Any] = {
        "chat_id": chat_id or cfg.chat_id,
        "text": clamp(text),
        # Пациенты присылают ссылки на объявления; превью раздувает сообщение
        # и прячет кнопки под картинкой.
        "link_preview_options": {"is_disabled": True},
        "disable_notification": silent,
    }
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup
    if reply_to_message_id is not None:
        payload["reply_parameters"] = {"message_id": reply_to_message_id,
                                       "allow_sending_without_reply": True}
    result = await _request(cfg, "sendMessage", payload, client=client)
    return int(result["message_id"])


async def answer_callback_query(callback_query_id: str, *, text: str | None = None,
                                show_alert: bool = False,
                                config: Config | None = None,
                                client: httpx.AsyncClient | None = None) -> bool:
    """Гасит «часики» на кнопке.

    Telegram держит кнопку в состоянии загрузки, пока не придёт этот вызов, и
    примерно через 10 с показывает администратору ошибку — даже если действие
    выполнено. Поэтому вызывается сразу при разборе апдейта, до записи в БД.
    """
    cfg = config or load_config()
    payload: dict[str, Any] = {"callback_query_id": callback_query_id,
                               "show_alert": show_alert}
    if text:
        payload["text"] = text[:200]
    return bool(await _request(cfg, "answerCallbackQuery", payload, client=client))


async def get_updates(offset: int | None = None, *, timeout_s: float = 25.0,
                      allowed_updates: Sequence[str] = ("message", "callback_query"),
                      config: Config | None = None,
                      client: httpx.AsyncClient | None = None) -> list[dict]:
    """Long polling. Возвращает список апдейтов.

    `allowed_updates` сужен до двух типов сознательно, вместе с включённой
    приватностью бота в группе: администраторы обсуждают в этом чате пациентов,
    и боту незачем видеть переписку целиком. С приватностью до бота доходят
    только команды и **ответы на его собственные сообщения** — на этом и
    построена кнопка «Правка».

    Вебхука нет намеренно: у ноутбука клиники нет публичного адреса и
    сертификата, а туннель — ещё один процесс, который умирает молча.
    """
    cfg = config or load_config()
    payload: dict[str, Any] = {"timeout": int(timeout_s),
                               "allowed_updates": list(allowed_updates)}
    if offset is not None:
        payload["offset"] = offset
    result = await _request(cfg, "getUpdates", payload, client=client,
                            read_timeout=timeout_s + 15.0)
    return list(result or [])


def dumps(value: Any) -> str:
    """JSON без экранирования кириллицы — для логов и отладки клавиатур."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True)
