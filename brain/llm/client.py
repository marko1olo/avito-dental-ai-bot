# -*- coding: utf-8 -*-
"""Каскад попыток: единственная функция, которую вызывает роутер.

Модуль ничего не знает ни о значениях ключей (их держит `keys`), ни о сети (её
держит `transport`), ни о том, какие модели существуют (это данные `cascade`),
ни о том, где хранится здоровье пула (`state`). Здесь живёт только порядок
попыток — и весь этот порядок выстрадан разбором stomchat.

Четыре решения, каждое из которых там стоило работающих запросов.

**429 разбирается РАНЬШЕ 5xx.** Тело отказа по квоте содержит
`quota_limit_value: 500 per day`, а проверка перегрузки была подстрочным
поиском — подстрока «500» из описания квоты совпадала, и здоровая модель
уезжала в бан на двадцать минут при живом втором ключе. Поэтому здесь 5xx
опознаётся статусом, а не текстом, и очередь проверок начинается с 429.

**429 — свойство ключа, 5xx — свойство модели.** Квота выбита у конкретного
ключа: ключ остывает (`state.KEY_COOLDOWN_SECONDS`), модель не наказывается и
пробуется тем же вызовом с другого ключа. Перегрузка же — свойство модели у
провайдера, и следующий ключ ничего не изменит: модель банится
(`state.MODEL_BAN_SECONDS`), попытка переходит к следующей строке каскада.

**Остывающие ключи отсеиваются до цикла**, в `keys.available()`. Внутри цикла
`continue` по кулдауну съедал попытку, не отправив запроса, и при бюджете в три
попытки трёх подряд остывших хватало, чтобы модель была пропущена при семи
здоровых ключах.

**Провал — это ответ, а не исключение.** `complete()` не поднимает ничего:
роутер по `text is None` уходит в деградированный режим и отдаёт диалог
администратору. Исключение здесь означало бы трассу с телом ответа провайдера
в журнале, то есть ровно ту утечку ключа, от которой защищается весь пакет.

Секреты. Наружу из этого модуля уходят: код провала из закрытого списка
`state.FAILURE_CODES`, идентификатор модели, отпечаток ключа и статус HTTP.
Тело ответа приходит уже вымаранным (`transport.HttpReply` вымарывает его в
конструкторе), журнал — тоже с вымарыванием (`keys.logger`), и оба рубежа
независимы: даже если завтра в этот файл впишут `log.warning("%s", body)`,
ключа в записи не окажется.
"""
from __future__ import annotations

import json
import random
import time
from dataclasses import dataclass

from . import cascade, keys, state, transport

log = keys.logger(__name__)

# Сколько ключей пробовать на одну модель, прежде чем перейти к следующей.
# Не «побольше на всякий случай»: пациент на Авито ждёт ответа, и десять
# последовательных 429 по одной модели — это десять сетевых задержек подряд,
# после которых всё равно нужна следующая модель. Три — компромисс: локальная
# невезуха с ключом лечится, выбитая дневная квота провайдера не растягивает
# ожидание.
KEYS_PER_MODEL = 3

# Потолок на весь вызов, поперёк каскада. Ограничивает худший случай по
# времени: без него четыре модели по три ключа дают двенадцать таймаутов по
# 25 с каждый, и роутер отвечал бы через пять минут вместо честного провала.
MAX_ATTEMPTS = 6

# Слова, по которым провайдер сам говорит «перегружен/не успел», когда статус
# этого не сказал. Проверяются ТОЛЬКО после того, как отвергнуты 429 и 401/403,
# и только этими словами: подстрочный поиск числа «500» — тот самый дефект,
# из-за которого квота читалась как перегрузка.
_OVERLOAD_WORDS = ("deadline", "unavailable", "overloaded", "try again later")

# Слова квоты. Нужны потому, что оба провайдера умеют отдавать исчерпание
# квоты не только статусом 429.
_RATE_WORDS = ("rate limit", "rate_limit", "quota", "resource_exhausted",
               "too many requests")

# Внутренние исходы попытки. Совпадают с кодами провала контракта там, где
# исход провальный, плюс "ok" и "wrong_token_field" — последний не провал, а
# указание повторить запрос в другой форме.
_OK = "ok"
_TOKEN_FIELD = "wrong_token_field"


@dataclass(frozen=True)
class LlmResult:
    """Итог вызова модели. `text is None` — единственный признак провала.

    `model`/`provider` при провале описывают ПОСЛЕДНЮЮ пробованную модель, а не
    ту, что ответила: ответившей нет. Пустые строки означают, что до сетевой
    попытки дело не дошло вовсе — нет ключей, все остывают или весь каскад
    забанен.
    """

    text: str | None
    model: str
    provider: str
    attempts: int
    latency_ms: int
    failure: str | None


def _payload(model: cascade.Model, system: str, user: str, *,
             temperature: float, max_tokens: int, completion_field: bool) -> dict:
    """Запрос в OpenAI-совместимой форме — она общая для Gemini и Groq.

    `completion_field` переключает `max_tokens` на `max_completion_tokens`:
    часть моделей Groq первое поле уже отвергает, и отличить это можно только
    по ответу 400. Без повтора во второй форме живая модель выглядела бы
    сломанной — та же развилка есть в `cascade.probe`.
    """
    field = "max_completion_tokens" if completion_field else "max_tokens"
    return {
        "model": model.id,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": temperature,
        field: max_tokens,
    }


def _classify(reply: transport.HttpReply) -> str:
    """Исход одной попытки. Порядок проверок в этой функции — суть модуля.

    1. Таймаут и обрыв: до статуса дело не дошло, тело осмысленного не несёт.
    2. Успех.
    3. **Квота (429) — раньше перегрузки (5xx).** Иначе `quota_limit_value:
       500 per day` в теле отказа по квоте банит здоровую модель.
    4. Ключ не принят (401/403) — раньше перегрузки по той же причине: тело
       вида `401 invalid api key ...` не должно попасть под общий разбор.
    5. Перегрузка — статусом, и только потом словами провайдера.

    Пункт 3 намеренно не смотрит слова квоты в теле ответа 5xx: в теле
    перегрузки может оказаться цитата лимита («…quota resets…»), и признать её
    квотой значило бы остудить исправный ключ вместо бана перегруженной модели.
    Статус 5xx — это утверждение провайдера о себе, оно точнее любых слов.
    """
    if reply.status == transport.STATUS_TIMEOUT:
        return "timeout"
    if reply.status == transport.STATUS_NO_CONNECTION:
        return "request_failed"
    if reply.ok:
        return _OK

    low = reply.body.lower()
    server_side = reply.status >= 500

    if reply.status == 429 or (not server_side and any(w in low for w in _RATE_WORDS)):
        return "rate_limited"
    if reply.status in (401, 403):
        return "key_denied"
    if server_side or any(w in low for w in _OVERLOAD_WORDS):
        return "model_overloaded"
    if reply.status == 400 and "max_tokens" in low:
        return _TOKEN_FIELD
    return "request_failed"


def _extract(reply: transport.HttpReply) -> tuple[str | None, str | None]:
    """Текст ответа модели или код провала. Тело уже вымарано транспортом.

    Пустой `content` при статусе 200 — не успех: модель, потратившая весь
    бюджет токенов на служебные рассуждения, возвращает именно это, и отдать
    роутеру пустую строку значило бы отправить пациенту пустое сообщение.
    """
    try:
        data = json.loads(reply.body)
    except ValueError:
        # 200 с телом, которое не JSON, — это не «модель промолчала», а
        # сломанный ответ: прокси, страница ошибки, обрезанный поток.
        return None, "request_failed"
    choices = data.get("choices") if isinstance(data, dict) else None
    if not isinstance(choices, list) or not choices:
        return None, "empty_response"
    message = choices[0].get("message") if isinstance(choices[0], dict) else None
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, str) or not content.strip():
        return None, "empty_response"
    return content, None


def _result(*, text: str | None, model: str, provider: str, attempts: int,
            started: float, failure: str | None) -> LlmResult:
    """Сборка итога. Провал попутно запоминается на диске для `health()`."""
    latency_ms = int((time.monotonic() - started) * 1000)
    if failure is not None:
        # note_failure принимает только коды из закрытого списка и падает на
        # чём угодно другом — это и есть гарантия, что в файл состояния не
        # уедет текст провайдера.
        state.note_failure(failure)
    return LlmResult(text, model, provider, attempts, latency_ms, failure)


def _no_key_failure(providers: set[str]) -> tuple[str, int]:
    """Почему нет ни одного живого ключа: не настроены или все остывают.

    Разные коды — разные действия оператора: `no_keys` означает пустую
    переменную окружения и лечится руками, `all_keys_on_cooldown` пройдёт само
    и означает, что пул мал для текущего потока сообщений.
    """
    configured = 0
    wait = 0
    for provider in sorted(providers):
        state_of_pool = keys.available(provider)
        configured += state_of_pool.total
        if state_of_pool.wait_seconds:
            wait = min(wait, state_of_pool.wait_seconds) if wait else state_of_pool.wait_seconds
    return ("all_keys_on_cooldown" if configured else "no_keys"), wait


async def complete(system: str, user: str, *, temperature: float = 0.35,
                   max_tokens: int = 400, purpose: str = "reply",
                   timeout_s: float = 25.0) -> LlmResult:
    """Пройти каскад моделей и вернуть текст ответа либо код провала.

    `purpose` не влияет на выбор модели и существует для журнала: каскад — это
    данные в `cascade.py`, и ветвление по виду работы внутри функции попыток —
    ровно то, из-за чего в stomchat таблица маршрутизации разъехалась с
    действительностью. Когда для дожима или сводки понадобится другой порядок
    моделей, он появится вторым кортежем рядом с `cascade.CHAT`, а не `if` здесь.

    Исключений не поднимает: роутеру нужен ответ, а не трасса.
    """
    started = time.monotonic()

    models = cascade.active()
    if not models:
        log.warning("llm(%s): весь каскад забанен, попыток не будет", purpose)
        return _result(text=None, model="", provider="", attempts=0,
                       started=started, failure="all_models_banned")

    providers = {m.provider for m in models}

    # Ключи берутся ОДИН раз на вызов и перемешанными: `keys.available()` уже
    # отсеяла остывающие. Свой пул на провайдера, свой курсор — так каждая
    # следующая попытка гарантированно берёт другой ключ, а не тот же самый.
    fresh: dict[str, list[str]] = {p: list(keys.available(p).fresh) for p in providers}
    cursor: dict[str, int] = {p: 0 for p in providers}

    if not any(fresh.values()):
        failure, wait = _no_key_failure(providers)
        log.warning("llm(%s): живых ключей нет (%s), ближайший освободится через %s с",
                    purpose, failure, wait)
        return _result(text=None, model="", provider="", attempts=0,
                       started=started, failure=failure)

    attempts = 0
    last_model = ""
    last_provider = ""
    last_failure = "request_failed"

    for model in models:
        pool = fresh.get(model.provider, [])
        if cursor[model.provider] >= len(pool):
            # Ключи этого провайдера кончились: либо их не было, либо все уже
            # ушли в кулдаун или оказались отвергнуты. Следующая модель может
            # принадлежать другому провайдеру — каскад на этом не заканчивается.
            log.info("llm(%s): %s пропущена, у провайдера не осталось ключей",
                     purpose, model.key)
            continue

        last_model, last_provider = model.id, model.provider
        completion_field = False
        tried_on_model = 0

        while tried_on_model < KEYS_PER_MODEL and attempts < MAX_ATTEMPTS:
            index = cursor[model.provider]
            if index >= len(pool):
                break
            api_key = pool[index]
            cursor[model.provider] = index + 1
            tried_on_model += 1
            attempts += 1
            mark = keys.fingerprint(model.provider, api_key)

            payload = _payload(model, system, user, temperature=temperature,
                               max_tokens=max_tokens, completion_field=completion_field)
            reply = await transport.post_chat(model.provider, api_key, payload, timeout_s)
            outcome = _classify(reply)

            if outcome is _TOKEN_FIELD and not completion_field:
                # Не провал, а другая форма поля бюджета токенов. Повтор идёт
                # тем же ключом: ключ тут ни при чём, поэтому попытку возвращаем
                # в бюджет и курсор откатываем.
                completion_field = True
                cursor[model.provider] = index
                tried_on_model -= 1
                attempts -= 1
                log.info("llm(%s): %s требует max_completion_tokens, повтор",
                         purpose, model.key)
                continue

            if outcome is _OK:
                text, why = _extract(reply)
                if text is not None:
                    # Удачный ответ — прямое доказательство здоровья ключа, а
                    # кулдаун лишь предположение о болезни. Снимаем: соседний
                    # процесс мог остудить этот ключ, пока запрос летел.
                    state.clear_key_cooldown(mark)
                    log.info("llm(%s): ответила %s, ключ %s, попытка %s, %s мс",
                             purpose, model.key, mark, attempts, reply.elapsed_ms)
                    return _result(text=text, model=model.id, provider=model.provider,
                                   attempts=attempts, started=started, failure=None)
                last_failure = why or "empty_response"
                log.warning("llm(%s): %s ответила 200, но текста нет (%s), ключ %s",
                            purpose, model.key, last_failure, mark)
                break  # молчание модели ключом не лечится — следующая модель

            last_failure = outcome

            if outcome == "rate_limited":
                # Квота — у ключа. Модель не наказываем: она только что была
                # доступна, просто с этого ключа спрашивать больше нечего.
                state.set_key_cooldown(mark)
                log.warning("llm(%s): 429 на %s, ключ %s остывает %s с, модель не банится",
                            purpose, model.key, mark, state.KEY_COOLDOWN_SECONDS)
                continue

            if outcome == "key_denied":
                # Ключ отозван или не имеет доступа к модели. Кулдаун тут
                # бессмысленен: через 300 с он не оживёт. Просто следующий ключ.
                log.warning("llm(%s): %s отказал ключу %s (статус %s), беру следующий",
                            purpose, model.key, mark, reply.status)
                continue

            if outcome in ("model_overloaded", "timeout"):
                # Свойство модели, а не ключа: следующий ключ получит то же
                # самое. Бан и переход к следующей строке каскада.
                state.ban_model(model.key)
                log.warning("llm(%s): %s недоступна (%s, статус %s), бан %s с",
                            purpose, model.key, outcome, reply.status,
                            state.MODEL_BAN_SECONDS)
                break

            # request_failed: сеть, прокси или отказ, который мы не опознали.
            # Может быть и разовым, и общим, поэтому пробуем следующий ключ, но
            # модель не баним — оснований нет.
            log.warning("llm(%s): %s не ответила осмысленно (статус %s), ключ %s",
                        purpose, model.key, reply.status, mark)

        if attempts >= MAX_ATTEMPTS:
            log.warning("llm(%s): исчерпан бюджет попыток (%s)", purpose, MAX_ATTEMPTS)
            break

    log.warning("llm(%s): все попытки провалились (%s), последняя модель %s:%s",
                purpose, last_failure, last_provider, last_model)
    return _result(text=None, model=last_model, provider=last_provider,
                   attempts=attempts, started=started, failure=last_failure)


def health() -> dict:
    """Сводка для ops. Ни ключа, ни его хвоста в ней нет.

    Считается по всем настроенным провайдерам, а не по одному каскаду: вопрос
    «сколько ключей осталось» задают до того, как что-то сломалось.

    `models_banned` — список пометок вида `provider:model`. Это имя модели, не
    секрет; знать, что забанен именно `groq:llama-3.1-8b-instant`, — весь смысл
    вызова. `last_failure` — код из закрытого списка плюс момент по
    Europe/Moscow строкой ISO: словарь уходит в JSON телеграм-отчёта, а
    `datetime` в `json.dumps` не сериализуется.
    """
    cooldowns = state.key_cooldowns()
    now = time.time()
    total = 0
    on_cooldown = 0
    for provider in sorted(keys.POOL_ENV):
        for api_key in keys.pool(provider):
            total += 1
            if cooldowns.get(keys.fingerprint(provider, api_key), 0.0) > now:
                on_cooldown += 1

    failure = state.last_failure()
    return {
        "keys_total": total,
        "keys_on_cooldown": on_cooldown,
        "models_banned": sorted(state.model_bans()),
        "last_failure": None if failure is None else {
            "code": failure.code,
            "at": failure.at.isoformat(timespec="seconds"),
        },
    }
