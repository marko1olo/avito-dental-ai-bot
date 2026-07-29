# -*- coding: utf-8 -*-
"""Здоровье ключей и моделей на диске. Файл, а не переменная в памяти.

Почему файл. Кулдаун ключа и бан модели — это знание вида «сюда стучать
бесполезно ещё четыре минуты». В stomchat оно жило словарём в памяти модуля,
а каждый вызов уходил в свежий подпроцесс, который импортировал модуль заново.
Словарь всегда был пуст, и пятиминутный кулдаун после 429 не действовал ни
одного запроса — следующее же сообщение било в тот же выдохшийся ключ. Здесь
процессы тоже разные: опрос Авито в node, ответ в python, ops-проверка
`health()` вообще из третьего процесса. Знание обязано переживать процесс.

Почему на диске нет ни ключей, ни текста провайдера. Ключ идентифицируется
отпечатком `sha256(provider:key)[:16]` — этого хватает, чтобы узнать «тот же
ключ», и не хватает, чтобы им воспользоваться. Причина последнего провала
пишется **кодом из закрытого списка** (`FAILURE_CODES`), и запись чего-либо
другого — исключение, а не предупреждение. Это тот же запрет, что в
`transport.py`, только с другой стороны: там сырое тело ответа не выходит за
пределы одной функции, здесь оно физически не может попасть в файл, даже если
кто-то попытается передать его как «детали».

Время внутри файлов — unix-секунды (`time.time()`). Это сознательное
отступление от общего правила «только tz-aware datetime»: файл переживает
перезагрузку и читается разными процессами, а срок «истекает в 14:12:03+04:00»
после смены зоны или перевода часов означал бы другой момент, чем при записи.
Наружу, в `health()`, тот же момент отдаётся уже как tz-aware datetime по
Europe/Moscow — через `hours.tz()`, как требует контракт.
"""
from __future__ import annotations

import json
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "gate"))
import hours  # noqa: E402

# Кулдаун ключа после 429 и бан модели после 5xx. Значения из разбора stomchat:
# 300 с — характерное окно минутной квоты Gemini/Groq, 1200 с — время, за
# которое перегруженная модель обычно приходит в себя.
KEY_COOLDOWN_SECONDS = 300
MODEL_BAN_SECONDS = 1200

# Полный список кодов провала из docs/CONTRACTS.md. Закрытый: `note_failure`
# отвергает всё, чего здесь нет, и именно поэтому в файл состояния не может
# попасть текст провайдера — а провайдеры возвращают тела вида
# «401 invalid api key <ключ>».
FAILURE_CODES = frozenset({
    "no_keys",
    "all_keys_on_cooldown",
    "all_models_banned",
    "rate_limited",
    "model_overloaded",
    "key_denied",
    "timeout",
    "empty_response",
    "request_failed",
})

KEY_COOLDOWN_FILE = "key_cooldowns.json"
MODEL_BAN_FILE = "banned_models.json"
LAST_FAILURE_FILE = "llm_last_failure.json"

_DEFAULT_DIR = Path(__file__).resolve().parents[2]  # рядом с data/
_DIR_ENV = "AVITO_LLM_STATE_DIR"


def state_dir() -> Path:
    """Каталог файлов состояния. По умолчанию — корень проекта, рядом с data/.

    Переменная окружения нужна не «для гибкости», а чтобы тест не трогал
    боевые кулдауны: тест, который сбивает реальные баны моделей, ломает
    работающего бота сильнее, чем ловит регрессий.
    """
    override = os.environ.get(_DIR_ENV, "").strip()
    target = Path(override) if override else _DEFAULT_DIR
    target.mkdir(parents=True, exist_ok=True)
    return target


# --- карты «пометка -> когда истекает» --------------------------------------

def _load(name: str) -> dict[str, float]:
    """Читает {пометка: unix_истечения}, молча отбрасывая протухшее и мусор.

    Битый или недописанный файл трактуется как пустой: сорванная запись не
    должна выключать бота, а пустая карта означает «все ключи считаются
    живыми» — это худший случай в один лишний запрос, а не в отказ.
    """
    path = state_dir() / name
    try:
        with path.open(encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    now = time.time()
    return {
        str(k): float(v)
        for k, v in data.items()
        if isinstance(v, (int, float)) and not isinstance(v, bool) and float(v) > now
    }


def _save(name: str, data: dict[str, float]) -> None:
    """Атомарная запись через временный файл и `os.replace`.

    Запись поверх напрямую при обрыве оставляла обрезанный JSON, и весь список
    банов молча превращался в пустой — все перегруженные модели снова считались
    рабочими. Файл общий для параллельных процессов, поэтому редкая потеря
    одной пометки при одновременной записи возможна и допустима: цена — один
    лишний запрос, а не порча файла.
    """
    path = state_dir() / name
    tmp = path.with_suffix(path.suffix + f".{os.getpid()}.tmp")
    try:
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(data, fh)
        os.replace(tmp, path)
    except OSError:
        # Диск не пишется — это не повод не ответить пациенту. Пометка будет
        # потеряна, поведение деградирует до «состояние не помним».
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def _mark(name: str, entry: str, seconds: int) -> None:
    data = _load(name)
    data[entry] = time.time() + seconds
    _save(name, data)


def _unmark(name: str, entry: str) -> bool:
    data = _load(name)
    if entry not in data:
        return False
    data.pop(entry)
    _save(name, data)
    return True


# --- ключи ------------------------------------------------------------------

def key_cooldowns() -> dict[str, float]:
    """Живые кулдауны: {отпечаток ключа: unix_истечения}."""
    return _load(KEY_COOLDOWN_FILE)


def set_key_cooldown(fingerprint: str, seconds: int = KEY_COOLDOWN_SECONDS) -> None:
    _mark(KEY_COOLDOWN_FILE, fingerprint, seconds)


def clear_key_cooldown(fingerprint: str) -> bool:
    """Снимает кулдаун. Возвращает True, если он там был.

    Нужно потому, что удачный ответ — прямое доказательство здоровья ключа, а
    кулдаун — всего лишь предположение о болезни. Доказательство сильнее: без
    этого ключ, ответивший на второй попытке, ещё пять минут обходился бы
    стороной.
    """
    return _unmark(KEY_COOLDOWN_FILE, fingerprint)


# --- модели -----------------------------------------------------------------

def model_bans() -> dict[str, float]:
    """Живые баны: {«provider:model»: unix_истечения}."""
    return _load(MODEL_BAN_FILE)


def ban_model(model_key: str, seconds: int = MODEL_BAN_SECONDS) -> None:
    _mark(MODEL_BAN_FILE, model_key, seconds)


def clear_model_ban(model_key: str) -> bool:
    return _unmark(MODEL_BAN_FILE, model_key)


# --- причина последнего провала ---------------------------------------------

@dataclass(frozen=True)
class LastFailure:
    code: str
    at: datetime  # tz-aware, Europe/Moscow


def note_failure(code: str) -> None:
    """Запоминает КОД провала. Ничего, кроме кода, записать нельзя.

    Раньше (stomchat) рядом с кодом лежало поле `detail` с текстом исключения
    провайдера, и оно было единственной причиной, по которой файл состояния
    вообще требовал скраббинга ключей. Поля здесь просто нет: диагностику даёт
    журнал, а файл состояния хранит только то, по чему принимаются решения.
    """
    if code not in FAILURE_CODES:
        raise ValueError(f"неизвестный код провала: {code!r}")
    _save(LAST_FAILURE_FILE, {code: time.time()})


def last_failure() -> LastFailure | None:
    """Последний провал или None. Момент — tz-aware, как требует контракт."""
    path = state_dir() / LAST_FAILURE_FILE
    try:
        with path.open(encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    for code, ts in data.items():
        if code in FAILURE_CODES and isinstance(ts, (int, float)):
            return LastFailure(code, datetime.fromtimestamp(float(ts), hours.tz()))
    return None


def clear_last_failure() -> None:
    _save(LAST_FAILURE_FILE, {})


# --- утилита для отчётов ----------------------------------------------------

def seconds_until(expiries: dict[str, float], entries: tuple[str, ...]) -> int:
    """Через сколько секунд освободится ближайшая из перечисленных пометок."""
    live = [expiries[e] for e in entries if e in expiries]
    if not live:
        return 0
    return max(0, int(min(live) - time.time()))
