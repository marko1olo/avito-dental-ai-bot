# -*- coding: utf-8 -*-
"""Дожим молчащего диалога. Самый большой неиспользованный резерв конверсии.

Основная масса диалогов на Авито умирает после первого обмена: человек написал
в пять клиник, собрал ответы и ушёл думать. Одно вовремя отправленное сообщение
возвращает заметную долю. Два — предел: дальше это рассылка, её видит и пациент,
и Авито.

Политика и формулировки — docs/dialogue-playbook.md. Здесь только решение
«отправлять ли сейчас», и оно намеренно скупое: у модуля больше причин
промолчать, чем причин написать. Ошибка в сторону молчания стоит одного лида,
ошибка в сторону спама стоит аккаунта Авито.

По умолчанию оба дожима — ЧЕРНОВИК администратору, а не автоотправка: ответ на
вопрос и инициативное сообщение молчащему человеку — качественно разные вещи.
"""
from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "gate"))
import hours  # noqa: E402

FIRST_AFTER = timedelta(hours=3)
FIRST_DEADLINE = timedelta(hours=5)
SECOND_AFTER = timedelta(hours=24)
MAX_FOLLOWUPS = 2

REFUSAL = re.compile(
    r"\bне надо\b|\bне нужно\b|\bспасибо,? нет\b|\bуже (?:вылечил|записал|была|был)\b"
    r"|\bнашел друг|\bнашла друг|\bпередумал|\bне интересует\b|\bне актуальн"
    r"|\bв другую\b|\bотказыва",
    re.IGNORECASE)

PHONE_GIVEN = re.compile(r"\+?[78][\s\-()]*\d{3}[\s\-()]*\d{3}[\s\-]*\d{2}[\s\-]*\d{2}")
WILL_CALL = re.compile(r"\bсам позвон|\bперезвоню\b|\bнаберу\b|\bпозвоню\b", re.IGNORECASE)


class Route(str, Enum):
    DRAFT = "draft"
    AUTO = "auto"
    NONE = "none"


@dataclass(frozen=True)
class DialogState:
    """Всё, что нужно знать о диалоге, чтобы решить про дожим."""

    our_last_message_at: datetime
    patient_last_message_at: datetime | None
    followups_sent: int = 0
    last_followup_at: datetime | None = None
    phone_captured: bool = False
    human_took_over: bool = False
    is_spam: bool = False
    patient_texts: tuple[str, ...] = ()

    @property
    def patient_went_silent(self) -> bool:
        """Мы написали последними и ответа не было."""
        if self.patient_last_message_at is None:
            return True
        return self.patient_last_message_at < self.our_last_message_at


@dataclass(frozen=True)
class Followup:
    number: int
    route: Route
    send_at: datetime
    text: str
    reason: str


FIRST_TEXT = ("Если удобнее по телефону — наберите +7 800 555-35-35, администратор подскажет "
              "по времени. Или напишите номер, наберём сами.")

SECOND_TEXT = ("На всякий случай: осмотр бесплатный и ни к чему не обязывает, можно просто узнать, "
               "что с зубом. Если сейчас неактуально — ничего страшного.")


def _refused(state: DialogState) -> bool:
    return any(REFUSAL.search(t) for t in state.patient_texts)


def _phone_or_promise(state: DialogState) -> bool:
    if state.phone_captured:
        return True
    return any(PHONE_GIVEN.search(t) or WILL_CALL.search(t) for t in state.patient_texts)


def plan(state: DialogState, now: datetime | None = None, *,
         first_route: Route = Route.DRAFT) -> Followup | None:
    """Что отправить сейчас, или None, если писать не нужно.

    `first_route` вынесен параметром, чтобы первый дожим можно было перевести
    в автоотправку, когда статистика покажет, что администратор отправляет его
    без правок. Второй дожим автоматическим не становится никогда.
    """
    now = now or hours.now()

    if state.is_spam:
        return None
    if state.human_took_over:
        return None
    if not state.patient_went_silent:
        return None
    if _phone_or_promise(state):
        return None
    if _refused(state):
        return None
    if state.followups_sent >= MAX_FOLLOWUPS:
        return None

    # Писать, когда позвонить нельзя, — сжигать сообщение впустую.
    if not hours.is_booking_open(now):
        return None

    silence = now - state.our_last_message_at

    if state.followups_sent == 0:
        if silence < FIRST_AFTER:
            return None
        if silence > FIRST_DEADLINE * 4:
            # Диалог остыл настолько, что дожим выглядит как рассылка из ниоткуда.
            return None
        return Followup(1, first_route, now, FIRST_TEXT,
                        f"молчание {silence.total_seconds() / 3600:.1f} ч после нашего сообщения")

    if state.last_followup_at is None:
        return None
    since_followup = now - state.last_followup_at
    if since_followup < SECOND_AFTER:
        return None
    return Followup(2, Route.DRAFT, now, SECOND_TEXT,
                    f"первый дожим без ответа {since_followup.total_seconds() / 3600:.0f} ч назад")
