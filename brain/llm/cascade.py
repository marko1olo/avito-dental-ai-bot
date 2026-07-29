# -*- coding: utf-8 -*-
"""Каскад моделей — данными, а не ветвлениями внутри функции.

В stomchat каскад был тремя списками литералов внутри `generate_text`, и из-за
этого таблица маршрутизации разъехалась с действительностью: десять реально
передаваемых видов работы в неё не попадали и сваливались в тяжёлую ветку.
Здесь каскад — модуль данных: его видно целиком, его проверяет
`verify_models()`, и он меняется без правки логики попыток.

Порядок в каскаде — это порядок «сначала дешёвое и быстрое, потом чужое».
Пациент на Авито ждёт ответа секунды, а не качества уровня консилиума: реплика
короткая, тон задан промптом, и lite-модель справляется. Groq стоит в конце
как второй провайдер: у него другая инфраструктура и другая квота, поэтому он
переживает падение Google, а не падает вместе с ним.

Идентификаторы моделей подтверждены обращением к живым API 2026-07-29 —
`python brain/llm/cascade.py` повторяет проверку. Это не формальность: в
stomchat зашиты `gemini-3.5-flash-lite`, `gemini-3.1-flash-lite` и
`qwen/qwen3.6-27b`, которых у провайдеров нет вовсе, и каскад из четырёх
моделей фактически состоял из одной живой.
"""
from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass

from . import keys, state, transport

log = keys.logger(__name__)


@dataclass(frozen=True)
class Model:
    id: str
    provider: str
    why: str

    @property
    def key(self) -> str:
        """Как модель помечается в файле банов: провайдер отдельно от id.

        Один и тот же id у двух провайдеров — обычное дело (`llama-3.3-70b`
        есть и в Groq, и в других), а перегружен бывает конкретный провайдер.
        """
        return f"{self.provider}:{self.id}"


# Боевой каскад для ответа пациенту. Проверен verify_models() 2026-07-29.
CHAT: tuple[Model, ...] = (
    Model("gemini-2.5-flash-lite", "gemini",
          "самая дешёвая живая gemini, отвечает за ~1 с — основной рабочий вариант"),
    Model("gemini-2.5-flash", "gemini",
          "тот же провайдер, но полноразмерная: страхует падение lite-варианта"),
    Model("llama-3.3-70b-versatile", "groq",
          "второй провайдер и другая квота — переживает отказ Google целиком"),
    Model("llama-3.1-8b-instant", "groq",
          "последний шанс: самая быстрая и дешёвая на Groq"),
)

# Кандидаты для разведки: боевой каскад плюс всё, что было зашито в stomchat, —
# чтобы проверка отвечала не только «работает ли наше», но и «существует ли то,
# что мы не взяли».
CANDIDATES: tuple[Model, ...] = CHAT + (
    Model("gemini-3-flash-preview", "gemini", "stomchat: config.GEMINI_MODEL"),
    Model("gemini-3.5-flash-lite", "gemini", "stomchat: первая модель диалогового каскада"),
    Model("gemini-3.1-flash-lite", "gemini", "stomchat: вторая модель диалогового каскада"),
    Model("gemini-3.5-flash", "gemini", "stomchat: сводочный каскад"),
    Model("gemini-2.0-flash", "gemini", "прошлое поколение, ещё может быть живо"),
    Model("qwen/qwen3.6-27b", "groq", "stomchat: третья модель диалогового каскада"),
    Model("openai/gpt-oss-120b", "groq", "stomchat: резерв при thinking_level=HIGH"),
)


def active(cascade: tuple[Model, ...] = CHAT) -> tuple[Model, ...]:
    """Каскад без забаненных за 5xx моделей.

    Если забанены все — возвращается пустой кортеж, и вызывающий получает
    `failure="all_models_banned"`. Здесь сознательное расхождение с stomchat:
    там при полном бане принудительно пробовалась последняя модель, потому что
    альтернативой была тишина в ответ врачу. У нас альтернатива другая —
    честный провал уходит черновиком администратору в Telegram, и он ответит
    руками. Обречённый запрос к заведомо перегруженной модели в этой схеме
    только добавляет секунд ожидания пациенту.
    """
    banned = state.model_bans()
    now = time.time()
    alive = tuple(m for m in cascade if banned.get(m.key, 0.0) <= now)
    if len(alive) < len(cascade):
        log.info("каскад: %s из %s моделей доступно, остальные забанены",
                 len(alive), len(cascade))
    return alive


# --- разведка: какие идентификаторы вообще существуют -----------------------

@dataclass(frozen=True)
class Probe:
    model: str
    provider: str
    alive: bool
    status: int
    verdict: str
    latency_ms: int
    detail: str  # выдержка из ответа, уже вымаранная transport.HttpReply


def _verdict(reply: transport.HttpReply) -> tuple[bool, str]:
    """Короткий вывод по ответу. Порядок проверок тот же, что в client.py."""
    if reply.status == transport.STATUS_TIMEOUT:
        return False, "таймаут"
    if reply.status == transport.STATUS_NO_CONNECTION:
        return False, "нет сети"
    if reply.ok:
        return True, "отвечает"
    low = reply.body.lower()
    if reply.status == 429 or "rate limit" in low or "quota" in low:
        # Квота — свойство ключа, а не доказательство отсутствия модели.
        return False, "квота ключа исчерпана — про модель ничего не известно"
    if reply.status in (401, 403):
        return False, "ключ не принят или нет доступа к модели"
    if reply.status == 404 or "not found" in low or "does not exist" in low:
        return False, "нет такой модели"
    if reply.status >= 500:
        return False, "провайдер перегружен"
    return False, f"отказ {reply.status}"


async def probe(model: Model, api_key: str, timeout_s: float = 25.0) -> Probe:
    """Один самый дешёвый запрос: один токен на выходе, температура 0.

    `max_tokens` часть моделей Groq уже не принимает и требует
    `max_completion_tokens`. Разница видна только по ответу, поэтому при отказе
    ровно по этому поводу запрос повторяется во второй форме — иначе живая
    модель выглядела бы мёртвой.
    """
    payload = {
        "model": model.id,
        "messages": [{"role": "user", "content": "ping"}],
        "temperature": 0,
        "max_tokens": 1,
    }
    reply = await transport.post_chat(model.provider, api_key, payload, timeout_s)
    if reply.status == 400 and "max_tokens" in reply.body.lower():
        payload.pop("max_tokens")
        payload["max_completion_tokens"] = 1
        reply = await transport.post_chat(model.provider, api_key, payload, timeout_s)

    alive, verdict = _verdict(reply)
    return Probe(model.id, model.provider, alive, reply.status, verdict,
                 reply.elapsed_ms, reply.excerpt)


async def verify_models(candidates: tuple[Model, ...] = CANDIDATES,
                        timeout_s: float = 25.0,
                        keys_per_model: int = 5) -> tuple[Probe, ...]:
    """Проверяет каждый идентификатор живым запросом. Разведка, не тест.

    Ключи перебираются, пока ответ 429. Первый прогон этой функции 2026-07-29
    именно на этом и споткнулся: первый же гугловый ключ был выбран по дневной
    квоте, все восемь кандидатов Gemini получили 429 и отчёт сказал «про модели
    ничего не известно» — то есть не сказал ничего. 429 — это свойство ключа, и
    единственный способ отделить «квота» от «нет такой модели» — спросить с
    другого ключа.
    """
    pools = {p: keys.available(p).fresh for p in {m.provider for m in candidates}}

    results: list[Probe] = []
    for model in candidates:
        api_keys = pools.get(model.provider, ())[:keys_per_model]
        if not api_keys:
            results.append(Probe(model.id, model.provider, False, transport.STATUS_NO_CONNECTION,
                                 f"нет живых ключей ({keys.POOL_ENV[model.provider]})", 0, ""))
            continue
        for number, api_key in enumerate(api_keys, start=1):
            result = await probe(model, api_key, timeout_s)
            if result.status != 429:
                break
        if result.status == 429:
            result = Probe(result.model, result.provider, False, 429,
                           f"квота исчерпана на всех {number} пробованных ключах — "
                           f"про саму модель ничего не известно",
                           result.latency_ms, result.detail)
        results.append(result)
    return tuple(results)


async def list_models(provider: str, timeout_s: float = 20.0) -> tuple[str, ...]:
    """Все идентификаторы, которые провайдер признаёт своими.

    Отвечает на вопрос, на который `verify_models` ответить не может: чем
    заменить умерший идентификатор. Без этого списка замена подбирается
    угадыванием.
    """
    fresh = keys.available(provider).fresh
    if not fresh:
        return ()
    reply = await transport.get_models(provider, fresh[0], timeout_s)
    if not reply.ok:
        log.warning("%s: список моделей не получен, статус %s: %s",
                    provider, reply.status, reply.excerpt)
        return ()
    try:
        data = json.loads(reply.body)
    except ValueError:
        return ()
    entries = data.get("data") if isinstance(data, dict) else None
    if not isinstance(entries, list):
        return ()
    found = [str(e.get("id", "")) for e in entries if isinstance(e, dict)]
    return tuple(sorted(i for i in found if i))


def _report(probes: tuple[Probe, ...], catalogues: dict[str, tuple[str, ...]]) -> str:
    """Печатный отчёт разведки. Проверяет себя на утечку секретов перед выдачей."""
    lines = ["РАЗВЕДКА МОДЕЛЕЙ", ""]
    for p in probes:
        mark = "ЖИВА " if p.alive else "МЁРТВА"
        lines.append(f"{mark} {p.provider:7} {p.model:45} {p.status:>4} "
                     f"{p.latency_ms:>6} мс  {p.verdict}")
        if not p.alive and p.detail:
            lines.append(f"       {p.detail[:200]}")
    for provider, ids in sorted(catalogues.items()):
        lines += ["", f"каталог {provider} ({len(ids)}):"]
        lines += [f"  {i}" for i in ids]

    text = "\n".join(lines)
    # Отчёт печатается человеку и попадёт в переписку — проверяем, что
    # вымарывание сработало, а не верим в это.
    if keys.redact(text) != text:
        raise RuntimeError("в отчёте разведки осталось похожее на секрет — не печатаю")
    return text


async def _main() -> None:
    probes = await verify_models()
    catalogues = {p: await list_models(p) for p in sorted(keys.POOL_ENV)}
    print(_report(probes, catalogues))


if __name__ == "__main__":
    asyncio.run(_main())
