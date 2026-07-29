# -*- coding: utf-8 -*-
"""Пул ключей, их отпечатки и вымарывание секретов из любого текста.

Один модуль на две вещи намеренно: **всё, что знает значения ключей, живёт
здесь**. Остальные модули пакета получают ключ, отдают его в заголовок запроса
и больше нигде не держат; в журнал и в файлы уходит только отпечаток.

Про вымарывание. В stomchat скраббер вычищал из текста ТОЛЬКО те ключи, что
перечислены в конфиге. Это защита по белому списку, и она даёт ровно ту дыру,
которую даёт любой белый список: ключ, которого в конфиге нет — отозванный,
чужой, из заголовка соседнего сервиса, — проходил в журнал целиком. Здесь
наоборот: маскируется всё, что ПОХОЖЕ на секрет, независимо от того, знаем мы
его или нет. Ложное срабатывание на длинной строке в журнале стоит одной
непрочитанной подробности; пропущенный ключ стоит доступа к платному API.

Русский текст под маску не попадает: шаблоны требуют подряд идущих ASCII-букв,
цифр, `-` и `_`, а слова «стоматология» и «здравствуйте» — не ASCII.
"""
from __future__ import annotations

import hashlib
import logging
import os
import random
import re
import time
from dataclasses import dataclass

from . import state

# Одна переменная окружения на провайдера, ключи через запятую. На ноутбуке
# лежат 10 гугловых и 7 groq — общий пул на 17 попыток.
POOL_ENV: dict[str, str] = {
    "gemini": "GOOGLE_API_KEYS",
    "groq": "GROQ_API_KEYS",
}

MASK = "***"

# Известные приставки ключей: они опознаются даже в коротком виде, потому что
# сама приставка уже говорит, что дальше секрет.
_PREFIXED = re.compile(
    r"(?:AIza|gsk_|sk-|sk_live_|xoxb-|ya29\.|ghp_|glpat-)[A-Za-z0-9_\-]{6,}"
)

# Общий случай — то, что похоже на секрет, но приставки не имеет. Два шаблона
# вместо одного, и оба ограничения оплачены ложными срабатываниями.
#
# Первый: длинная строка, где есть И строчные, И ЗАГЛАВНЫЕ, И цифры. Так
# выглядит base62/base64, то есть практически любой ключ и любой JWT. Требование
# смешанного регистра — не украшение: без него под маску уходили
# идентификаторы моделей (`5-flash-lite-preview-09-2025` — это 28 подряд
# допустимых символов), и отчёт разведки превращался в частокол звёздочек.
#
# Второй: слитный ASCII-блок от 32 символов без дефисов и подчёркиваний — так
# выглядит hex-подпись или строчный base32-токен, у которых заглавных нет.
# Идентификаторы моделей под него не попадают: они всегда разделены дефисами.
_LONG_MIXED = re.compile(
    r"(?<![A-Za-z0-9_\-])"
    r"(?=[A-Za-z0-9_\-]*[a-z])(?=[A-Za-z0-9_\-]*[A-Z])(?=[A-Za-z0-9_\-]*\d)"
    r"[A-Za-z0-9_\-]{20,}"
    r"(?![A-Za-z0-9_\-])"
)
_LONG_FLAT = re.compile(
    r"(?<![A-Za-z0-9])(?=[A-Za-z0-9]*\d)[A-Za-z0-9]{32,}(?![A-Za-z0-9])"
)


def _configured() -> list[str]:
    """Все настроенные ключи, длинные первыми.

    Порядок важен: короткий ключ может оказаться началом длинного, и замена
    короткого первой оставила бы хвост длинного в тексте.
    """
    found: list[str] = []
    for env_name in POOL_ENV.values():
        for raw in os.environ.get(env_name, "").split(","):
            token = raw.strip()
            if len(token) >= 8:
                found.append(token)
    return sorted(set(found), key=len, reverse=True)


def redact(text: str) -> str:
    """Вымарывает из текста всё, что похоже на секрет. Идемпотентна.

    Порядок: сначала точные совпадения с настроенными ключами (их мы знаем
    наверняка), потом шаблоны. Второй проход нужен и после первого: тело вида
    `401 API key not valid: AIzaSyD...` может содержать ключ, которого у нас
    нет вовсе.
    """
    if not text:
        return ""
    clean = text
    for secret in _configured():
        if secret in clean:
            clean = clean.replace(secret, MASK)
    clean = _PREFIXED.sub(MASK, clean)
    clean = _LONG_MIXED.sub(MASK, clean)
    return _LONG_FLAT.sub(MASK, clean)


class _RedactingFilter(logging.Filter):
    """Второй рубеж: вымарывает секреты из уже собранной строки журнала.

    Первый рубеж — то, что сырое тело ответа провайдера не покидает
    `transport.post_chat` (см. `transport.HttpReply`). Фильтр стоит потому, что
    первый рубеж держится на дисциплине автора, а этот — нет: даже если завтра
    кто-то напишет `logger.warning("... %s", exc)` с сырым исключением, ключ из
    записи уйдёт замаскированным.

    Именно тут исправлен унаследованный дефект. В stomchat на двух местах
    (`logger.warning(f"... {exc}")` и `_write_generation_status(..., error=str(exc)[:500])`)
    текст исключения провайдера шёл в журнал и в файл статуса, минуя скраббер,
    который был написан рядом и вызывался только из `_record_failure`.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001 — сломанный %-формат не должен ронять журнал
            record.args = ()
            return True
        safe = redact(message)
        if safe != message:
            record.msg = safe
            record.args = ()
        return True


def logger(name: str) -> logging.Logger:
    """Журнал с вымарыванием. Другого способа получить журнал в пакете нет.

    Фильтр вешается на сам логгер, а не на обработчик: обработчики ставит
    приложение, и полагаться на то, что оно поставит фильтр, нельзя. Фильтр
    логгера применяется к записям, созданным через этот логгер, — то есть ко
    всему, что пишет пакет.
    """
    log = logging.getLogger(name)
    if not any(isinstance(f, _RedactingFilter) for f in log.filters):
        log.addFilter(_RedactingFilter())
    return log


# --- отпечатки и пул --------------------------------------------------------

def fingerprint(provider: str, api_key: str) -> str:
    """Устойчивый отпечаток ключа: `sha256(provider:key)[:16]`.

    Ровно это и только это уходит в файлы состояния и в журнал. Хвост ключа
    (в stomchat в журнал шло `provider...{api_key[-5:]}`) — это часть секрета,
    отданная в обмен на удобство чтения журнала; отпечаток даёт то же удобство
    и ничего не отдаёт.
    """
    return hashlib.sha256(f"{provider}:{api_key}".encode()).hexdigest()[:16]


def pool(provider: str) -> tuple[str, ...]:
    """Ключи провайдера из окружения. Пустой кортеж, если не настроено.

    Дубликаты убираются: один и тот же ключ, вписанный дважды, съедал бы две
    попытки из бюджета, имея одну квоту.
    """
    env_name = POOL_ENV.get(provider)
    if not env_name:
        raise ValueError(f"неизвестный провайдер: {provider!r}")
    seen: list[str] = []
    for raw in os.environ.get(env_name, "").split(","):
        token = raw.strip()
        if token and token not in seen:
            seen.append(token)
    return tuple(seen)


@dataclass(frozen=True)
class Availability:
    """Пул, разделённый на живые и остывающие ключи."""

    fresh: tuple[str, ...]      # уже перемешаны
    cooling: tuple[str, ...]
    wait_seconds: int           # до освобождения ближайшего, если живых нет

    @property
    def total(self) -> int:
        return len(self.fresh) + len(self.cooling)


def available(provider: str, *, rng: random.Random | None = None) -> Availability:
    """Живые ключи провайдера, перемешанные, кулдаунные — отдельно.

    Перемешивание размазывает квоту: при постоянном порядке первый ключ
    выбивался бы в 429 каждый час, а десятый не использовался бы никогда.

    Кулдаунные отсеиваются ЗДЕСЬ, до цикла попыток. В stomchat проверка стояла
    внутри цикла и делала `continue`: остывающий ключ съедал попытку целиком, не
    отправив запроса. При 10 настроенных ключах и бюджете в 3 попытки трёх
    подряд попавшихся остывающих хватало, чтобы модель была пропущена при семи
    полностью здоровых.
    """
    keys = list(pool(provider))
    (rng or random).shuffle(keys)

    cooldowns = state.key_cooldowns()
    now = time.time()
    fresh: list[str] = []
    cooling: list[str] = []
    for key in keys:
        if cooldowns.get(fingerprint(provider, key), 0.0) > now:
            cooling.append(key)
        else:
            fresh.append(key)

    wait = 0
    if cooling and not fresh:
        wait = state.seconds_until(
            cooldowns, tuple(fingerprint(provider, k) for k in cooling)
        )
    return Availability(tuple(fresh), tuple(cooling), wait)
