# -*- coding: utf-8 -*-
"""Роутер: единственное место, где решается судьба входящего сообщения.

Порядок слоёв здесь не произволен, и каждый стоит там, где стоит, по причине.

**Нейронка ведёт.** Она понимает падежи, опечатки, сленг и эмоцию несопоставимо
лучше правил — отладка морфологии в `gate/intent.py` это доказала трижды.
Поэтому основной путь всегда идёт через модель.

**Правила страхуют, но не классифицируют.** У `gate/intent.py` две роли:
деградированный режим, когда все ключи в кулдауне и модели нет вообще, и
ПОТОЛОК маршрута. Потолок — самое важное и самое неочевидное:

    прохождение вето НЕ повышает маршрут с DRAFT до AUTO.

Если `intent` сказал, что тема — цена, симптомы или запись, ответ уйдёт
администратору даже когда модель написала идеальный текст и вето не возражает.
Это гибридный режим, выбранный владельцем: автоматически уходят только
безобидные факты. Обратное направление разрешено — вето роняет AUTO в DRAFT,
но никогда не поднимает.

**Вето — последним.** Оно смотрит на уже сгенерированный текст, потому что
единственный изъян модели, который не лечится промптом, — способность назвать
цифру, которой ей не давали.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Literal

_BRAIN = Path(__file__).resolve().parent
sys.path.insert(0, str(_BRAIN))
sys.path.insert(0, str(_BRAIN / "gate"))

import delay as delay_mod  # noqa: E402
import facts  # noqa: E402
import guard  # noqa: E402
import hours  # noqa: E402
import intent as intent_mod  # noqa: E402
import pii  # noqa: E402
from intent import Kind, Route  # noqa: E402
from llm import client as llm  # noqa: E402
from prompt import builder as prompt_builder  # noqa: E402
from prompt.builder import Turn  # noqa: E402

# Насколько «горячий» текст мы разрешаем администратору-модели. Прейскурант
# полон диапазонов, и творческая температура здесь работает против нас.
TEMPERATURE = 0.35
MAX_TOKENS = 400


@dataclass(frozen=True)
class Incoming:
    chat_id: str
    external_id: str
    text: str
    at: datetime
    history: tuple[Turn, ...] = ()

    @property
    def is_first_reply(self) -> bool:
        return not any(t.role == "clinic" for t in self.history)


@dataclass(frozen=True)
class Outcome:
    route: Literal["auto", "draft", "ignore", "skip"]
    text: str | None
    send_at: datetime | None
    kind: str
    reason: str
    topic: str | None = None
    llm_failure: str | None = None
    veto: tuple[str, ...] = field(default_factory=tuple)
    degraded: bool = False

    @property
    def will_reach_patient_without_human(self) -> bool:
        return self.route == "auto"


def _ceiling(model_route: Route, intent_route: Route) -> Route:
    """Маршрут не может быть свободнее того, что разрешил intent.

    Порядок строгости: IGNORE > DRAFT > AUTO. Берём более строгий из двух.
    """
    order = {Route.AUTO: 0, Route.DRAFT: 1, Route.IGNORE: 2}
    return model_route if order[model_route] >= order[intent_route] else intent_route


def _canned_safe_answer(decision: intent_mod.Decision) -> str | None:
    """Ответ без модели. Только факты, только из data/, только белый список.

    Работает, когда LLM недоступна целиком: клиника не должна оставаться
    молчащей из-за того, что у провайдера кончилась квота.
    """
    if decision.kind is not Kind.SAFE_FACT:
        return None

    if decision.topic == "schedule":
        return f"{hours.describe_now()} {hours.describe_schedule()}"

    contact = facts.clinic_contact_facts()
    if decision.topic == "address":
        return (f"Мы на {contact['address']}, {contact['district']}. "
                f"Ближайшее метро — {contact['metro']}. Телефон {contact['phone']}.")
    if decision.topic == "parking":
        return (f"Парковка есть рядом с входом. Адрес — {contact['address']}. "
                f"Если удобнее, наберите {contact['phone']}.")
    if decision.topic == "greeting":
        return ("Здравствуйте. Подскажите, что беспокоит — врач посмотрит на бесплатной "
                f"консультации. Или наберите {contact['phone']}.")
    return None


async def handle(incoming: Incoming, store) -> Outcome:
    """Обработать одно входящее сообщение. Ничего не отправляет — только решает."""
    # 1. Пациент дописывает мысль — дожидаемся. Человек не отвечает построчно.
    #
    #    Стоит ПЕРВЫМ, до дедупа, и это не косметика. `mark_seen` необратим: он
    #    регистрирует сообщение навсегда. Если отложить дебаунсом уже
    #    зарегистрированное сообщение, при следующем заходе оно вернётся как
    #    дубликат — то есть обращение пациента потеряется молча, без единой
    #    ошибки в логе. Дебаунс означает «ещё не сейчас», а не «не нужно», и
    #    единственное место для него — до всего, что нельзя отменить.
    since = (hours.now() - incoming.at).total_seconds()
    if delay_mod.should_wait_for_more(since):
        return Outcome("skip", None, None, "debounce",
                       f"пациент писал {since:.0f} с назад, ждём продолжения")

    # 2. Дедуп на уровне схемы. Поллер перезапускается, id Авито не уникальны
    #    ничем на нашей стороне, и повторная отправка пациенту того же текста —
    #    худшее, что может сделать этот бот.
    if not store.mark_seen(incoming.external_id, incoming.chat_id, incoming.at):
        return Outcome("skip", None, None, "duplicate",
                       f"сообщение {incoming.external_id} уже обработано")

    store.touch_dialog(incoming.chat_id, patient_message_at=incoming.at)

    # 3. Человек за рулём — бот молчит. Проверяется до обращения к модели, чтобы
    #    не тратить вызов на диалог, который уже ведёт администратор.
    if not store.is_ai_active(incoming.chat_id, incoming.at):
        store.audit("ai_silent", chat_id=incoming.chat_id,
                    payload={"reason": "перехват или пауза"})
        return Outcome("skip", None, None, "ai_paused",
                       "диалог у администратора или ИИ на паузе")

    decision = intent_mod.classify(incoming.text)
    if decision.route is Route.IGNORE:
        store.audit("ignored", chat_id=incoming.chat_id,
                    payload={"kind": decision.kind.value})
        return Outcome("ignore", None, None, decision.kind.value, decision.reason,
                       topic=decision.topic)

    # 4. Основной путь — модель.
    topics = [decision.topic] if decision.topic else []
    system = prompt_builder.build_system_prompt(topics=topics, moment=incoming.at)
    user = prompt_builder.build_user_prompt(incoming.history, incoming.text)

    result = await llm.complete(system, user, temperature=TEMPERATURE,
                               max_tokens=MAX_TOKENS, purpose="reply")

    if result.text is None:
        # 5. Деградированный режим. Белый список отвечает сам, остальное —
        #    человеку с оригинальным текстом пациента.
        canned = _canned_safe_answer(decision)
        if canned is not None:
            plan = delay_mod.plan_reply(canned, is_first_reply=incoming.is_first_reply,
                                        received_at=incoming.at)
            store.audit("degraded_auto", chat_id=incoming.chat_id,
                        payload={"failure": result.failure})
            return Outcome("auto", canned, plan.send_at, decision.kind.value,
                           f"LLM недоступна ({result.failure}), ответ из белого списка",
                           topic=decision.topic, llm_failure=result.failure, degraded=True)

        store.audit("degraded_draft", chat_id=incoming.chat_id,
                    payload={"failure": result.failure})
        return Outcome("draft", None, None, decision.kind.value,
                       f"LLM недоступна ({result.failure}) — нужен администратор",
                       topic=decision.topic, llm_failure=result.failure, degraded=True)

    reply = result.text.strip()

    # 6. Вето поверх текста модели. Может только ужесточить маршрут.
    verdict = guard.check(reply, topic=decision.topic)
    model_route = Route.AUTO if verdict.ok else Route.DRAFT
    route = _ceiling(model_route, decision.route)

    if not verdict.ok:
        store.audit("veto", chat_id=incoming.chat_id,
                    payload={"violations": list(verdict.violations),
                             "model": result.model,
                             "reply": pii.scrub(reply)})

    plan = delay_mod.plan_reply(reply, is_first_reply=incoming.is_first_reply,
                               received_at=incoming.at)

    if route is Route.AUTO:
        reason = f"белый список ({decision.topic}), вето пройдено, {result.model}"
    elif verdict.ok:
        reason = f"{decision.reason} — решает администратор"
    else:
        reason = f"вето: {verdict.reason}"

    return Outcome("auto" if route is Route.AUTO else "draft",
                   reply, plan.send_at, decision.kind.value, reason,
                   topic=decision.topic, veto=verdict.violations)
